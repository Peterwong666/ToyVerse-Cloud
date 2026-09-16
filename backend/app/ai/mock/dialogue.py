"""规则对话引擎（离线）。

它在整条链路里的位置
--------------------

    用户文本 ──► detect_intent ──► 素材选择 ──► 角色预设改写 ──► 分块流式输出
                     │                │
                     │                └─ 知识库关键词检索（命中即引用）
                     └─ 内容安全关键词拦截（命中写 safety_flag）

三条刻意为之的约束
------------------

1. **纯函数 + 显式随机源**：所有回复都由 ``random.Random(seed)`` 决定，
   同样的种子必然得到同样的结果。这不是「为了测试而测试」——
   可复现是离线引擎能作为**基准（baseline）**去对比真实厂商延迟的前提
   （见 docs/10 的基准数据表）。
2. **流式是真的分块**：:func:`iter_chunks` 按标点与固定长度切分，
   一段正常回复必然产出多块。若一次性吐出整段，前端逐字渲染与首字延迟
   指标就都验不了。
3. **可解释**：:class:`MockReply` 会带回命中的意图、关键词、素材标题与
   ``safety_flag``，调试台据此说明「为什么这样回答」。
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass

from app.ai.mock.scenarios import (
    BUILTIN_ROLE_PRESETS,
    DEFAULT_ROLE_PRESET,
    FALLBACK_REPLIES,
    GREETING_REPLIES,
    INTENT_FALLBACK,
    INTENT_GREETING,
    INTENT_QUESTION,
    INTENT_SONG,
    INTENT_STORY,
    INTENT_WEATHER,
    SONGS,
    STORIES,
    WEATHER_REPLIES,
    ContentSnippet,
    detect_intent,
    detect_safety,
    match_snippet,
    pick,
    safety_reply,
    search_knowledge,
)

#: 流式分块的期望长度（字符）。选 8 是因为中文短句通常 6~10 字，
#: 这样每块接近「一口气说完的语义单元」，前端逐字渲染的节奏自然。
DEFAULT_CHUNK_SIZE = 8


@dataclass(slots=True, frozen=True)
class MockReply:
    """一次规则回复的完整结果（供 API 落库与调试台展示）。"""

    text: str
    intent: str
    matched_keyword: str | None = None
    snippet_title: str | None = None
    role_preset: str | None = None
    safety_flag: str | None = None


def resolve_role_preset(code: str | None) -> tuple[str, dict[str, object]]:
    """解析角色预设编码。

    未指定或不存在时回退到默认角色——**不报错**。理由是玩具场景里
    「角色没配好」不应该让对话直接失败，回退比中断对用户体验更友好。
    """
    chosen = code if code in BUILTIN_ROLE_PRESETS else DEFAULT_ROLE_PRESET
    return chosen, BUILTIN_ROLE_PRESETS[chosen]


def compose(
    text: str,
    *,
    seed: int,
    role_preset: str | None = None,
    knowledge: tuple[ContentSnippet, ...] = (),
) -> MockReply:
    """把用户输入合成为一次规则回复。

    Args:
        text: 用户输入文本。
        seed: 随机种子（``MOCK_RANDOM_SEED``），决定素材挑选结果。
        role_preset: 角色预设编码。
        knowledge: 知识库素材（优先于内置素材参与匹配）。

    Returns:
        :class:`MockReply`。
    """
    rng = random.Random(seed)
    preset_code, preset = resolve_role_preset(role_preset)
    cleaned = text.strip()

    # 1) 安全拦截优先于一切意图——命中就直接给统一话术，不进入素材选择
    hit = detect_safety(cleaned)
    if hit is not None:
        return MockReply(
            text=safety_reply(hit),
            intent=INTENT_FALLBACK,
            role_preset=preset_code,
            safety_flag=hit,
        )

    match = detect_intent(cleaned)
    snippet_title: str | None = None

    if match.intent == INTENT_STORY:
        snippet = match_snippet(cleaned, knowledge) or match_snippet(cleaned, STORIES)
        if snippet is None:
            snippet = pick(STORIES, rng)
        snippet_title = snippet.title
        body = f"好呀，我给你讲《{snippet.title}》。{snippet.body}"

    elif match.intent == INTENT_SONG:
        snippet = match_snippet(cleaned, knowledge) or match_snippet(cleaned, SONGS)
        if snippet is None:
            snippet = pick(SONGS, rng)
        snippet_title = snippet.title
        body = f"那我们唱《{snippet.title}》吧——{snippet.body} 啦啦啦，一起唱！"

    elif match.intent == INTENT_WEATHER:
        body = pick(WEATHER_REPLIES, rng)

    elif match.intent == INTENT_GREETING:
        body = str(preset["greeting"]) + " " + pick(GREETING_REPLIES, rng)

    elif match.intent == INTENT_QUESTION:
        # 知识库命中就是在这一步「影响回复」：命中时引用素材标题与正文
        snippet = search_knowledge(cleaned, knowledge)
        if snippet is not None and snippet.kind == "story":
            snippet_title = snippet.title
            body = f"我从《{snippet.title}》里找到了答案：{snippet.body}"
        elif snippet is not None:
            snippet_title = snippet.title
            body = f"我记得《{snippet.title}》里说过：{snippet.body}"
        else:
            body = pick(FALLBACK_REPLIES, rng)

    else:
        # 兜底也尝试一次知识库检索：能命中就引用，命中不了才用兜底话术
        snippet = search_knowledge(cleaned, knowledge)
        if snippet is not None:
            snippet_title = snippet.title
            body = f"关于这个，我从《{snippet.title}》里想到：{snippet.body}"
        else:
            body = pick(FALLBACK_REPLIES, rng)

    # 2) 角色预设改写：加上语气前缀与收尾，让不同角色真的听起来不一样
    rewritten = f"{preset['prefix']}{body}{preset['suffix']}"
    return MockReply(
        text=rewritten,
        intent=match.intent,
        matched_keyword=match.matched_keyword,
        snippet_title=snippet_title,
        role_preset=preset_code,
    )


def iter_chunks(text: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[str]:
    """把整段文本切成多个流式分块。

    切分策略：先在标点后断开成「语义片段」，再按 ``chunk_size`` 合并，
    避免把「小狐狸」切成「小狐」+「狸」。最终保证：

    * 文本长于 ``chunk_size`` 时**至少两块**（前端才能验证流式渲染）
    * 同样的输入必然产出同样的切分（纯函数）
    """
    if not text:
        return

    boundaries = "。！？；…，、,.!?;\n"
    segments: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in boundaries:
            segments.append(buffer)
            buffer = ""
    if buffer:
        segments.append(buffer)

    merged: list[str] = []
    current = ""
    for segment in segments:
        if current and len(current) + len(segment) > chunk_size:
            merged.append(current)
            current = segment
        else:
            current += segment
    if current:
        merged.append(current)

    # 兜底：整段过长且无标点（如纯字母字符串）时，等分切块
    if len(merged) == 1 and len(merged[0]) > chunk_size:
        whole = merged[0]
        merged = [whole[i : i + chunk_size] for i in range(0, len(whole), chunk_size)]

    yield from merged


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "MockReply",
    "compose",
    "iter_chunks",
    "resolve_role_preset",
]
