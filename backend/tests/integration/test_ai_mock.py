"""集成测试：``AI_DEFAULT_PROVIDER=mock`` 下的 AI 全链路（P7 验收）。

覆盖本阶段验收标准
------------------
* ``/ai/chat`` ``/ai/tts`` ``/ai/asr`` 在**零密钥**下全部可用
* ★ 流式回复**真的分批**（``chunkCount > 1``）——否则前端逐字渲染与首字延迟无法验证
* ★ 会话与消息**落库**：``dialogue_sessions.message_count``、
  ``dialogue_messages.chunkCount`` / ``latencyMs`` 都要有值（P8/P9 的数据来源）
* 多轮续聊复用同一会话，序号连续
* 内容安全命中写 ``safetyFlag``
* 角色预设真的改变回复
* ``/ai/providers`` 能区分「已注册」与「已配置」
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import registry
from app.ai.mock.engine import MockProvider
from app.core.ids import new_id
from app.models.ai import DialogueMessage, DialogueSession
from app.models.catalog import ClientProduct, ProductTemplate
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

AI = f"{API_PREFIX}/ai"


@pytest.fixture(autouse=True)
def _restore_registry() -> Iterator[None]:
    """用例结束后重建默认注册表。

    为什么要它：个别用例会注册「替换版 mock」（如 ``wav`` 模式的 TTS），
    若不还原，会影响同进程内后续用例（注册表是模块级全局状态）。
    """
    yield
    registry.build_default_registry(force=True)


@pytest.fixture
async def p7_product(db: AsyncSession, make_tenant: Any) -> ClientProduct:
    """准备一个最小可用的客户产品。

    对话接口需要「归属租户」（``dialogue_sessions.tenant_id`` 为 NOT NULL），
    平台账号调用时由 ``clientProductId`` 反查租户，因此这里必须先有产品。
    模板刻意**不绑定云服务商**：这样供应商解析会落到平台默认（mock）。
    """
    tenant = await make_tenant(code="P7-TENANT", name="P7 测试租户")
    template = ProductTemplate(
        id=new_id("product_template"), code="P7-TPL", name="P7 模板", network_type="WIFI"
    )
    db.add(template)
    await db.flush()

    product = ClientProduct(
        id=new_id("client_product"),
        tenant_id=tenant.id,
        template_id=template.id,
        code="P7-PROD",
        name="P7 客户产品",
        network_type="WIFI",
    )
    db.add(product)
    await db.commit()
    return product


async def _stream_events(
    client: AsyncClient, headers: dict[str, str], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """发起流式对话并解析 NDJSON 事件列表。"""
    response = await client.post(f"{AI}/chat", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    rows = [row for row in response.text.splitlines() if row.strip()]
    return [json.loads(row) for row in rows]


def _reply_text(events: list[dict[str, Any]]) -> str:
    """把 delta 事件拼回整段回复。"""
    return "".join(event.get("delta", "") for event in events if event["type"] == "delta")


# ---------------------------------------------------------------------------
# 对话：流式
# ---------------------------------------------------------------------------


async def test_chat_stream_yields_multiple_chunks_and_persists(
    client: AsyncClient, auth: Any, db: AsyncSession, p7_product: ClientProduct
) -> None:
    """★ 核心验收：流式真的分多块，且会话与消息都落库。"""
    headers = await auth.platform_headers()
    events = await _stream_events(
        client, headers, {"message": "给我讲个故事吧", "clientProductId": p7_product.id}
    )

    assert events[0]["type"] == "session"
    deltas = [event for event in events if event["type"] == "delta"]
    assert len(deltas) > 1, "流式必须真的分多块产出，否则前端逐字渲染无法验证"
    assert deltas[-1]["isFinal"] is True
    assert _reply_text(events)

    done = events[-1]
    assert done["type"] == "done"
    assert done["chunkCount"] == len(deltas)
    assert done["totalLatencyMs"] is not None
    assert done["provider"] == "mock"
    assert done["providerLayer"] == "platform_default"

    # ---- 落库校验 ----
    session_id = done["sessionId"]
    dialogue = (
        await db.execute(select(DialogueSession).where(DialogueSession.id == session_id))
    ).scalar_one()
    assert dialogue.message_count == 2
    assert dialogue.total_latency_ms >= 0
    assert dialogue.started_at is not None

    messages = list(
        (
            await db.execute(
                select(DialogueMessage)
                .where(DialogueMessage.session_id == session_id)
                .order_by(DialogueMessage.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [message.role for message in messages] == ["USER", "ASSISTANT"]
    assert messages[0].content == "给我讲个故事吧"
    assert messages[1].chunk_count == len(deltas)
    assert messages[1].latency_ms is not None
    assert messages[1].provider_code == "mock"


async def test_chat_non_stream_returns_whole_reply(
    client: AsyncClient, auth: Any, p7_product: ClientProduct
) -> None:
    """``stream=false`` 时返回整段响应，但仍要如实报告分块数。"""
    response = await client.post(
        f"{AI}/chat",
        json={"message": "唱首歌吧", "clientProductId": p7_product.id, "stream": False},
        headers=await auth.platform_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chunks"] > 1
    assert body["provider"] == "mock"
    assert body["simulated"] is True
    assert body["reply"]
    assert body["latencyMs"] >= 0


async def test_chat_continues_existing_session(
    client: AsyncClient, auth: Any, db: AsyncSession, p7_product: ClientProduct
) -> None:
    """续聊复用同一会话，消息序号连续（1..4）。"""
    headers = await auth.platform_headers()
    first = await _stream_events(
        client, headers, {"message": "你好", "clientProductId": p7_product.id}
    )
    session_id = first[-1]["sessionId"]

    second = await _stream_events(
        client,
        headers,
        {"message": "唱首歌吧", "sessionId": session_id, "clientProductId": p7_product.id},
    )
    assert second[-1]["sessionId"] == session_id

    messages = list(
        (
            await db.execute(
                select(DialogueMessage)
                .where(DialogueMessage.session_id == session_id)
                .order_by(DialogueMessage.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [message.seq for message in messages] == [1, 2, 3, 4]

    dialogue = (
        await db.execute(select(DialogueSession).where(DialogueSession.id == session_id))
    ).scalar_one()
    assert dialogue.message_count == 4


async def test_chat_safety_hit_is_recorded(
    client: AsyncClient, auth: Any, db: AsyncSession, p7_product: ClientProduct
) -> None:
    """内容安全命中要在事件与落库消息上都留下标记（为 P8 三开关预留）。"""
    events = await _stream_events(
        client,
        await auth.platform_headers(),
        {"message": "我们聊点暴力的话题吧", "clientProductId": p7_product.id},
    )
    done = events[-1]

    assert done["safetyFlag"] == "KEYWORD"

    assistant = (
        await db.execute(
            select(DialogueMessage).where(DialogueMessage.id == done["messageId"])
        )
    ).scalar_one()
    assert assistant.safety_flag == "KEYWORD"


async def test_chat_role_preset_changes_reply(
    client: AsyncClient, auth: Any, p7_product: ClientProduct
) -> None:
    """角色预设要真的改变回复语气（否则「支持角色预设」只是空话）。"""
    events = await _stream_events(
        client,
        await auth.platform_headers(),
        {"message": "你好呀", "clientProductId": p7_product.id, "rolePreset": "little_dinosaur"},
    )

    assert "小恐龙" in _reply_text(events)


# ---------------------------------------------------------------------------
# 语音：ASR / TTS
# ---------------------------------------------------------------------------


async def test_asr_echo_mode_echoes_hint_text(
    client: AsyncClient, auth: Any, p7_product: ClientProduct
) -> None:
    """``echo`` 模式复读 ``hintText``，让语音链路在无音频素材时也能联调。"""
    response = await client.post(
        f"{AI}/asr",
        json={"hintText": "今天天气怎么样", "clientProductId": p7_product.id},
        headers=await auth.platform_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "今天天气怎么样"
    assert body["provider"] == "mock"
    assert body["isFinal"] is True
    assert body["simulated"] is True


async def test_tts_text_mode_is_honest_about_missing_audio(
    client: AsyncClient, auth: Any
) -> None:
    """``text`` 模式必须如实返回「无音频」，调用方据此回退文本播报。"""
    response = await client.post(
        f"{AI}/tts", json={"text": "你好呀小玩具"}, headers=await auth.platform_headers()
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contentType"].startswith("text/plain")
    assert body["audioBase64"] is None
    assert body["durationMs"] is not None
    assert body["simulated"] is True


async def test_tts_wav_mode_returns_base64_audio(client: AsyncClient, auth: Any) -> None:
    """``wav`` 模式返回结构合法的静音 WAV（base64），用于验证音频播放链路。"""
    registry.register(MockProvider(tts_mode="wav"))

    response = await client.post(
        f"{AI}/tts", json={"text": "你好呀小玩具"}, headers=await auth.platform_headers()
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contentType"] == "audio/wav"
    assert body["audioBase64"] is not None
    # 解码后应能认出 RIFF 头（说明不是随便凑的字符串）
    assert base64.b64decode(body["audioBase64"])[:4] == b"RIFF"


# ---------------------------------------------------------------------------
# 供应商视图与离线素材
# ---------------------------------------------------------------------------


async def test_providers_view_separates_registered_from_configured(
    client: AsyncClient, auth: Any
) -> None:
    """``registered`` 与 ``configured`` 必须分开：区别「没实现」与「没配置」。"""
    headers = await auth.platform_headers()
    response = await client.get(f"{AI}/providers", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    by_code = {record["code"]: record for record in body["records"]}

    assert body["defaultCode"] == "mock"
    assert by_code["mock"]["registered"] is True
    assert by_code["mock"]["configured"] is True
    assert by_code["mock"]["requiresCredentials"] is False
    assert by_code["volcano"]["configured"] is False
    # 响应里不允许出现任何密钥字段
    assert "apiKey" not in json.dumps(body)
    assert "secretKey" not in json.dumps(body)


async def test_mock_health_is_up(client: AsyncClient, auth: Any) -> None:
    """mock 恒可用——这正是它能作为「离线基线」的原因。"""
    response = await client.get(
        f"{AI}/providers/mock/health", headers=await auth.platform_headers()
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "UP"
    assert body["code"] == "mock"


async def test_mock_scenarios_expose_engine_boundary(
    client: AsyncClient, auth: Any
) -> None:
    """调试台要能看到离线引擎的素材与规则（回答「为什么这样回答」）。"""
    response = await client.get(
        f"{AI}/mock/scenarios", headers=await auth.platform_headers()
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["stories"]) >= 3
    assert len(body["songs"]) >= 3
    assert "story" in body["intentKeywords"]
    assert "天气" in body["intentKeywords"]["weather"]
    assert len(body["rolePresets"]) >= 4
    assert "暴力" in body["safetyKeywords"]
