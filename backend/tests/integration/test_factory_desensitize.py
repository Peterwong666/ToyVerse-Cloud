"""集成测试：P6 工厂端**脱敏红线**（本阶段最高优先级的安全验收）。

被验证的契约
------------
工厂是跨租户角色：一张工单背后是某个品牌客户的订单。工厂需要知道
「做什么、做多少、烧哪个固件」，但**不需要也不应该**知道
客户是谁、单价多少、联系人电话与邮箱。

因此本文件用**唯一哨兵串**把敏感数据「标记」出来，再递归扫描工厂端
**每一个**响应体，断言哨兵出现 0 次：

* 客户侧哨兵：租户名称 / 联系人 / 电话 / 邮箱 / 客户产品名；
* 订单侧哨兵：申请人姓名 / 申请人电话 / 单价 / 总额。

为什么必须同时写「平台端对照」
------------------------------
只有「工厂端搜不到哨兵」这一半，是一种**可以自欺的断言**：
如果造数时数据根本没落库（字段名写错、事务没提交、查询条件不匹配），
工厂端当然也搜不到哨兵——测试全绿，但什么也没证明。
因此每个用例都在同一份数据上用平台端接口**反证哨兵确实存在于数据库**：
平台端拿得到，工厂端拿不到，才说明「是脱敏在起作用」。

本文件还覆盖一个**已知的可接受面**：派单时平台运营自己填的
``productionNote`` 会原样下发给工厂（见
``TestProductionNoteAdversarial``）——运营在备注里手写客户名属于
「自己把信息带过去」，不是系统脱敏的漏洞，但它必须被**记录在案**。

测试写法对齐 ``tests/integration/test_catalog.py``：模块级 helper、
camelCase 断言、中文注释说明「为什么这样断言」、递归扫描工具。
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.ids import new_id
from app.models.enums import EnableStatus, OrderStatus, RoleType
from app.models.identity import UserAccount
from app.models.order import Order
from app.models.org import Factory
from app.services import catalog_service
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

PLATFORM = f"{API_PREFIX}/platform"
MERCHANT = f"{API_PREFIX}/merchant"
FACTORY = f"{API_PREFIX}/factory"

#: 外部会话口令（仅存在于测试进程）
OUTSIDER_PASSWORD = "0utsider-Pass#2026"


# ---------------------------------------------------------------------------
# 哨兵值：一旦出现在工厂端响应里，就说明脱敏被绕过
# ---------------------------------------------------------------------------

#: 客户（租户）与订单侧的敏感值，全部用**唯一哨兵串**。
#:
#: 用哨兵而不是「真实的电话号码」：哨兵在整段响应文本里出现即代表泄漏，
#: 不存在「这个数字恰好是别处的业务值」这种误判。
SENTINELS: dict[str, str] = {
    "租户名称": "SENTINEL-CUSTOMER-8f3a2b",
    "租户联系人": "SENTINEL-CONTACT-4d7e1c",
    "租户联系电话": "SENTINEL-PHONE-7c9a11",
    "租户邮箱": "SENTINEL-EMAIL-2f8b3d@example.invalid",
    "申请人姓名": "SENTINEL-APPLICANT-6b2e5d",
    "申请人电话": "SENTINEL-APPPHONE-3a9f40",
    "客户产品名": "SENTINEL-PRODUCT-1e7c88",
}

#: 金额哨兵：用高辨识度的两位小数（``13333.33`` × 3 = ``39999.99``）。
#: 金额在 JSON 里是数字，因此按**字符串形式**搜索响应文本——
#: 用一个「不像任何业务统计值」的数值可以避免与工期 / 数量等整数互相误伤。
UNIT_PRICE_SENTINEL = "13333.33"
TOTAL_AMOUNT_SENTINEL = "39999.99"
ORDER_QUANTITY = 3

#: 工厂端响应里**不允许存在**的键名（递归查 key，不只是顶层）。
#:
#: 口径按对外 JSON 字段名（camelCase）给出，与
#: ``FactoryOrderSerializer.DENIED_FIELDS`` 对齐并补上租户侧的邮箱。
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "unitPrice",
        "totalAmount",
        "applicantName",
        "applicantPhone",
        "contactName",
        "contactPhone",
        "email",
        "tenantName",
        "clientProductName",
    }
)

#: 客户名脱敏后的形状：首字符 + 若干星号 + 尾字符。
TENANT_NAME_MASKED_PATTERN = re.compile(r"^S\*{10,}b$")


# ---------------------------------------------------------------------------
# 递归扫描工具
# ---------------------------------------------------------------------------


def _iter_keys(node: Any) -> list[str]:
    """递归取出 JSON 结构里的全部**键名**（任意深度）。

    只查顶层不够：工单详情内嵌 ``burnReports`` / ``inspections`` 数组与
    ``inspectionSummary`` 对象，泄漏点可能藏在这些嵌套结构里。
    """
    keys: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(key)
            keys.extend(_iter_keys(value))
    elif isinstance(node, list):
        for item in node:
            keys.extend(_iter_keys(item))
    return keys


def _json_text(body: Any) -> str:
    """把响应体序列化为整段文本（金额等数字按字符串形式出现）。"""
    return json.dumps(body, ensure_ascii=False, default=str)


def _assert_no_client_data(response: Any, *, where: str, expect_status: int = 200) -> None:
    """★ 断言工厂端响应里既没有敏感值，也没有敏感字段名。

    三类检查缺一不可：
    1. **值**——哨兵串（客户名 / 联系方式 / 申请人）出现 0 次；
    2. **金额**——按字符串形式搜索两个高辨识度数值；
    3. **字段名**——``unitPrice`` 这类键名不得出现（哪怕值为 null：
       字段名本身就会告诉工厂「这里有一笔金额，只是没给你看」）。
    """
    assert response.status_code == expect_status, (
        f"{where}：[{response.status_code}] {response.text}"
    )
    body = response.json()
    text = _json_text(body)

    for label, sentinel in SENTINELS.items():
        assert sentinel not in text, f"{where}：响应泄漏了{label}「{sentinel}」"
        # 兜底再扫一遍**原始响应文本**（防止有未被 JSON 解析的部分）
        assert sentinel not in response.text, f"{where}：原始响应文本泄漏了{label}"

    for label, amount in (
        ("单价", UNIT_PRICE_SENTINEL),
        ("总额", TOTAL_AMOUNT_SENTINEL),
    ):
        assert amount not in text, f"{where}：响应泄漏了{label} {amount}"

    keys = set(_iter_keys(body))
    leaked_keys = keys & FORBIDDEN_KEYS
    assert leaked_keys == set(), f"{where}：响应出现禁发字段名 {sorted(leaked_keys)}"


def _assert_platform_sees_client_data(
    response: Any, *, where: str, labels: tuple[str, ...]
) -> None:
    """反证：同一份数据在**平台端**必须能看到（证明哨兵真的落库了）。

    没有这一步，「工厂端搜不到哨兵」可能只是因为造数失败——
    那是最典型的假通过。

    ``labels`` 只列出**该端点应当能拿到**的哨兵：订单接口不含租户联系人 /
    邮箱（那是租户资料，走租户接口），逐端点挑选才能既证明落库、
    又不把「本该在另一个接口」当成缺陷。
    """
    assert response.status_code == 200, f"{where}：[{response.status_code}] {response.text}"
    body = response.json()
    text = _json_text(body)
    for label in labels:
        assert SENTINELS[label] in text, (
            f"{where}：平台端竟然拿不到{label}（哨兵未落库，工厂端断言失去意义）"
        )
    if "申请人姓名" in labels:
        # 金额只在订单接口上出现
        assert UNIT_PRICE_SENTINEL in text, f"{where}：平台端拿不到单价，脱敏断言失去意义"
        assert TOTAL_AMOUNT_SENTINEL in text, f"{where}：平台端拿不到总额，脱敏断言失去意义"


# ---------------------------------------------------------------------------
# 模块级辅助（不跨模块私有导入，避免耦合既有测试文件）
# ---------------------------------------------------------------------------


def _platform_ctx() -> AuthContext:
    """平台超管上下文（通配权限），供服务层建链使用。"""
    return AuthContext(
        user_id="u-factory-desens-it",
        account="factory-desens-it",
        role_code="PLATFORM_ADMIN",
        role_type=RoleType.PLATFORM,
        tenant_id=None,
        permissions=["*"],
    )


async def _seed_catalog(
    db: AsyncSession, tenant_id: str, *, suffix: str, product_name: str
) -> dict[str, str]:
    """建「云服务商 → 模板 → 授权 → 客户产品」最小链条（走真实服务层）。

    ``network_type=WIFI``（京东方案）：设备 SN 由平台本地生成，
    不依赖厂商接口，也不受厂商密钥配置影响。
    """
    platform = _platform_ctx()
    cloud = await catalog_service.create_cloud(
        db,
        payload={
            "code": f"CLOUD-{suffix}",
            "name": f"{suffix} 测试云",
            "vendor": "JOYINSIDE",
            "network_type": "WIFI",
        },
        actor=platform,
    )
    template = await catalog_service.create_template(
        db,
        payload={
            "code": f"TPL-{suffix}",
            "name": f"{suffix} 测试模板",
            "network_type": "WIFI",
            "cloud_provider_id": cloud.id,
            "firmware_version": "2.0.0",
            # 型号会出现在工单的 productModel 上（工厂**需要**知道，属允许下发）
            "model": "ESP32-S3",
            "status": "ENABLED",
        },
        actor=platform,
    )
    await catalog_service.authorize_template(
        db, template_id=template.id, tenant_ids=[tenant_id], actor=platform
    )
    product = await catalog_service.create_client_product(
        db,
        payload={
            "tenant_id": tenant_id,
            "template_id": template.id,
            "code": f"PROD-{suffix}",
            # 客户产品的名字属于**禁发**字段（clientProductName）
            "name": product_name,
        },
        actor=platform,
    )
    return {"cloud_id": cloud.id, "template_id": template.id, "product_id": product.id}


async def _make_factory_account(
    db: AsyncSession, *, account: str, factory_id: str | None, role_code: str = "FACTORY_ADMIN"
) -> UserAccount:
    """直接落一个工厂账号（``user_accounts.factory_id`` 是 P6 新增的归属来源）。

    刻意不经 HTTP 造数：本文件验证的是**响应脱敏**，
    造数过程本身不应成为变量（与 ``test_binding.py`` 的库级造数同理）。
    """
    from app.core.security import hash_password

    user = UserAccount(
        id=new_id("user"),
        account=account,
        password_hash=hash_password(OUTSIDER_PASSWORD),
        nickname=account,
        role_code=role_code,
        tenant_id=None,  # 工厂跨租户
        factory_id=factory_id,
        status="ACTIVE",
    )
    db.add(user)
    await db.flush()
    return user


async def _make_factory(db: AsyncSession, *, code: str, name: str) -> Factory:
    """直接落一家工厂。"""
    factory = Factory(
        id=new_id("factory"),
        code=code,
        name=name,
        contact_name="赵厂长",
        contact_phone="13600000000",
        address="测试工业园 1 栋",
        status=str(EnableStatus.ENABLED),
        daily_capacity=1000,
        is_verified=True,
    )
    db.add(factory)
    await db.flush()
    return factory


async def _build_stocked_order(
    client: AsyncClient,
    auth: Any,
    db: AsyncSession,
    make_tenant: Any,
    make_user: Any,
    *,
    suffix: str,
    merchant_account: str,
) -> dict[str, Any]:
    """造一条「已入库（``IN_STOCK``）且带设备」的订单，并写入全部敏感哨兵。

    步骤对应真实业务链：建租户（写联系人与邮箱哨兵）→ 建目录链 →
    商户下单（写申请人哨兵）→ 库级补金额哨兵 → 平台审核 → 生成设备入库。

    Returns:
        含 ``tenant`` / ``catalog`` / ``order``（HTTP 响应体）/ ``order_row`` 的字典。
    """
    tenant = await make_tenant(code=f"DSEN-{suffix}", name=SENTINELS["租户名称"])
    # 租户的联系人 / 电话 / 邮箱 / 行业：同样是「工厂不该看到」的客户信息
    tenant.contact_name = SENTINELS["租户联系人"]
    tenant.contact_phone = SENTINELS["租户联系电话"]
    tenant.email = SENTINELS["租户邮箱"]
    tenant.industry = "SENTINEL-INDUSTRY-5b1a9c"
    await db.flush()

    catalog = await _seed_catalog(
        db, tenant.id, suffix=suffix, product_name=SENTINELS["客户产品名"]
    )
    merchant = await _merchant_headers(auth, make_user, tenant.id, merchant_account)

    created = await client.post(
        f"{MERCHANT}/orders",
        headers=merchant,
        json={
            "clientProductId": catalog["product_id"],
            "quantity": ORDER_QUANTITY,
            "applicantName": SENTINELS["申请人姓名"],
            "applicantPhone": SENTINELS["申请人电话"],
        },
    )
    assert created.status_code == 201, created.text
    order = created.json()

    # 金额只能由平台侧维护，商户下单接口不接收金额字段——这里库级补上，
    # 目的是让「金额已存在于数据库」成为事实，从而让后续的「0 次出现」有判别力。
    order_row = (await db.execute(select(Order).where(Order.id == order["id"]))).scalar_one()
    order_row.unit_price = 13333.33
    order_row.total_amount = 39999.99
    await db.flush()

    platform = await auth.platform_headers()
    audited = await client.post(
        f"{PLATFORM}/orders/{order['id']}/audit",
        headers=platform,
        json={"decision": "APPROVED", "remark": "脱敏测试"},
    )
    assert audited.status_code == 200, audited.text
    generated = await client.post(f"{PLATFORM}/orders/{order['id']}/generate", headers=platform)
    assert generated.status_code == 200, generated.text

    detail = await client.get(f"{PLATFORM}/orders/{order['id']}", headers=platform)
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == str(OrderStatus.IN_STOCK), (
        "派单的前置条件是订单 IN_STOCK，这里先确认造数真的走到了该状态"
    )

    return {
        "tenant": tenant,
        "catalog": catalog,
        "order": order,
        "order_row": order_row,
        "merchant": merchant,
        "platform": platform,
    }


async def _merchant_headers(
    auth: Any, make_user: Any, tenant_id: str, account: str
) -> dict[str, str]:
    """建一个商户管理员并登录，返回鉴权头。"""
    password = "Merch4nt-Dsen#2026"
    await make_user(
        account=account, password=password, role_code="MERCHANT_ADMIN", tenant_id=tenant_id
    )
    return auth.headers(await auth.token(account, password))


async def _factories_headers(
    auth: Any, db: AsyncSession, *, factory_code: str, account: str
) -> dict[str, Any]:
    """建一家工厂 + 一个绑定该厂的工厂账号，返回上下文。"""
    factory = await _make_factory(db, code=factory_code, name=f"{factory_code} 号工厂")
    await _make_factory_account(db, account=account, factory_id=factory.id)
    headers = auth.headers(await auth.token(account, OUTSIDER_PASSWORD))
    return {"factory": factory, "account": account, "headers": headers}


# ===========================================================================
# 一、★ 红线：工厂端任何响应都不得出现客户名 / 金额 / 联系方式
# ===========================================================================


class TestFactoryResponseDesensitization:
    """递归扫描工厂端**全部**响应体，逐个断言哨兵 0 次出现。"""

    async def test_no_client_data_in_any_factory_response(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 本阶段的核心红线：把工单走完整条链路，扫描每一步的响应。

        扫描范围刻意覆盖「列表 / 详情 / 烧录后 / 抽检后 / 出货后 /
        二维码清单 / 抽检列表 / 固件列表 / 统计」——脱敏最容易漏的地方
        不是详情，而是**后加的那些端点**（固件聚合、统计卡片、
        二维码导出）：它们各自拼装字段，最容易被写成「顺手带上客户名」。
        """
        ctx = await _build_stocked_order(
            client, auth, db, make_tenant, make_user, suffix="A001", merchant_account="13200001001"
        )
        order_id = ctx["order"]["id"]
        platform = ctx["platform"]
        factory = await _factories_headers(
            auth, db, factory_code="FAC-A001", account="13600001001"
        )
        fh = factory["headers"]

        # ---- ⓪ 先反证哨兵确实落库（否则后面的「0 次出现」是假通过）----
        _assert_platform_sees_client_data(
            await client.get(f"{PLATFORM}/orders/{order_id}", headers=platform),
            where="平台端订单详情（本用例的落库自检）",
            labels=("租户名称", "申请人姓名", "申请人电话", "客户产品名"),
        )

        # ---- ① 派单（平台端，工单进入 PENDING）----
        dispatched = await client.post(
            f"{PLATFORM}/orders/{order_id}/dispatch",
            headers=platform,
            json={"factoryId": factory["factory"].id},
        )
        assert dispatched.status_code == 201, dispatched.text
        factory_order_id = dispatched.json()["id"]

        # ---- ② 分批烧录（先报 1 台 → 部分；再报 2 台 → 烧满）----
        partial = await client.post(
            f"{FACTORY}/orders/{factory_order_id}/burn", headers=fh, json={"burnedCount": 1}
        )
        assert partial.status_code == 200, partial.text
        assert partial.json()["burnedCount"] == 1
        done = await client.post(
            f"{FACTORY}/orders/{factory_order_id}/burn", headers=fh, json={"burnedCount": 2}
        )
        assert done.status_code == 200, done.text

        # ---- ③ 抽检（合格 / 不合格各一条，抽检改状态不改工单）----
        devices = (
            await client.get(f"{PLATFORM}/devices", headers=platform, params={"orderId": order_id})
        ).json()["records"]
        assert len(devices) == ORDER_QUANTITY
        pass_resp = await client.post(
            f"{FACTORY}/orders/{factory_order_id}/inspect",
            headers=fh,
            json={"sn": devices[0]["sn"], "result": "PASS"},
        )
        assert pass_resp.status_code == 201, pass_resp.text
        fail_resp = await client.post(
            f"{FACTORY}/orders/{factory_order_id}/inspect",
            headers=fh,
            json={"sn": devices[1]["sn"], "result": "FAIL", "defectCode": "E01"},
        )
        assert fail_resp.status_code == 201, fail_resp.text

        # ---- ④ 出货登记 ----
        shipped = await client.post(f"{FACTORY}/orders/{factory_order_id}/ship", headers=fh)
        assert shipped.status_code == 200, shipped.text
        assert shipped.json()["status"] == "SHIPPED"

        # ---- ⑤ 逐端点扫描 ----
        responses: dict[str, Any] = {
            "派单响应(平台)": dispatched,
            "烧录上报(部分)": partial,
            "烧录上报(烧满)": done,
            "抽检上报(合格)": pass_resp,
            "抽检上报(不合格)": fail_resp,
            "出货登记": shipped,
            "工单列表": await client.get(f"{FACTORY}/orders", headers=fh),
            "工单列表(关键字命中订单号)": await client.get(
                f"{FACTORY}/orders", headers=fh, params={"keyword": ctx["order"]["orderNo"]}
            ),
            "工单详情": await client.get(f"{FACTORY}/orders/{factory_order_id}", headers=fh),
            "二维码清单": await client.get(
                f"{FACTORY}/orders/{factory_order_id}/qrcodes", headers=fh
            ),
            "抽检记录列表": await client.get(f"{FACTORY}/inspections", headers=fh),
            "固件版本列表": await client.get(f"{FACTORY}/firmwares", headers=fh),
            "工作台统计": await client.get(f"{FACTORY}/stats", headers=fh),
            # 平台端的工单端点与工厂端**共用同一响应模型**，一并扫：
            # 若有人把平台端改回真名，这条会立刻失败。
            "平台工单列表": await client.get(f"{PLATFORM}/factory-orders", headers=platform),
            "平台工单详情": await client.get(
                f"{PLATFORM}/factory-orders/{factory_order_id}", headers=platform
            ),
        }
        # 派单与抽检上报是「创建」语义，状态码 201；其余为 200。
        created_responses = {"派单响应(平台)", "抽检上报(合格)", "抽检上报(不合格)"}
        for where, response in responses.items():
            _assert_no_client_data(
                response,
                where=where,
                expect_status=201 if where in created_responses else 200,
            )

        # ---- ⑥ 客户名只有脱敏表达，且形状正确 ----
        listed = responses["工单列表"].json()
        assert listed["total"] == 1, "该工厂只有一张工单"
        item = listed["records"][0]
        assert "customerNameMasked" in item
        assert item["customerNameMasked"] != SENTINELS["租户名称"]
        assert TENANT_NAME_MASKED_PATTERN.match(item["customerNameMasked"]), (
            f"脱敏名形状不符：{item['customerNameMasked']!r}"
        )
        # 详情与列表的脱敏名一致（同一客户在不同端点不能被显示成两个值）
        assert responses["工单详情"].json()["customerNameMasked"] == item["customerNameMasked"]

        # 工厂**应该**看得到的东西仍在：型号、固件版本、数量、脱敏客户名
        detail = responses["工单详情"].json()
        assert detail["productModel"] == "ESP32-S3"
        assert detail["firmwareVersion"] == "2.0.0"
        assert detail["quantity"] == ORDER_QUANTITY
        assert detail["orderNo"] == ctx["order"]["orderNo"], "订单号用于对账，必须可见"

    async def test_platform_endpoints_prove_sentinels_exist_in_database(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """★ 反证：同一份数据在平台端**必须**能看到金额与申请人。

        没有这条，前面所有「0 次出现」都可能是假通过（数据本来就没落库）。
        这条同时证明：哨兵确实存在于数据库与平台端响应里，
        工厂端的「搜不到」只能是脱敏的功劳。
        """
        ctx = await _build_stocked_order(
            client, auth, db, make_tenant, make_user, suffix="A002", merchant_account="13200001002"
        )
        platform = ctx["platform"]

        order_detail = await client.get(
            f"{PLATFORM}/orders/{ctx['order']['id']}", headers=platform
        )
        _assert_platform_sees_client_data(
            order_detail,
            where="平台端订单详情",
            labels=("租户名称", "申请人姓名", "申请人电话", "客户产品名"),
        )

        tenant_detail = await client.get(
            f"{PLATFORM}/tenants/{ctx['tenant'].id}", headers=platform
        )
        _assert_platform_sees_client_data(
            tenant_detail,
            where="平台端租户详情",
            labels=("租户名称", "租户联系人", "租户联系电话", "租户邮箱"),
        )

        body = order_detail.json()
        assert body["unitPrice"] == 13333.33
        assert body["totalAmount"] == 39999.99
        assert body["applicantName"] == SENTINELS["申请人姓名"]
        assert body["applicantPhone"] == SENTINELS["申请人电话"]
        assert body["tenantName"] == SENTINELS["租户名称"]
        assert body["clientProductName"] == SENTINELS["客户产品名"]

        tenant_body = tenant_detail.json()
        assert tenant_body["contactName"] == SENTINELS["租户联系人"]
        assert tenant_body["contactPhone"] == SENTINELS["租户联系电话"]
        assert tenant_body["email"] == SENTINELS["租户邮箱"]

    async def test_masked_name_stays_consistent_across_factory_endpoints(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """同一客户的脱敏名在列表 / 详情 / 抽检记录里必须**一致**。

        业务含义：工厂靠脱敏名判断「这几张工单是同一个客户下的」。
        如果每个端点各自脱敏（或某个端点忘了脱敏直接给真名），
        工厂对账时就会把同一客户当成两个——这正是「脱敏收口在一处」的价值。
        """
        ctx = await _build_stocked_order(
            client, auth, db, make_tenant, make_user, suffix="A003", merchant_account="13200001003"
        )
        factory = await _factories_headers(
            auth, db, factory_code="FAC-A003", account="13600001003"
        )
        fh = factory["headers"]

        dispatched = await client.post(
            f"{PLATFORM}/orders/{ctx['order']['id']}/dispatch",
            headers=ctx["platform"],
            json={"factoryId": factory["factory"].id},
        )
        assert dispatched.status_code == 201, dispatched.text
        factory_order_id = dispatched.json()["id"]

        listed = (await client.get(f"{FACTORY}/orders", headers=fh)).json()["records"][0]
        detail = (await client.get(f"{FACTORY}/orders/{factory_order_id}", headers=fh)).json()
        # 派单响应本身也是工单响应（与工厂端共用模型），脱敏名必须一致
        assert dispatched.json()["customerNameMasked"] == listed["customerNameMasked"]
        assert detail["customerNameMasked"] == listed["customerNameMasked"]
        assert listed["customerNameMasked"] == "S" + "*" * (len(SENTINELS["租户名称"]) - 2) + "b"


# ===========================================================================
# 二、对抗性：平台运营自己往 productionNote 里写客户名
# ===========================================================================


class TestProductionNoteAdversarial:
    """``productionNote`` 是**运营自己填的**内容，不属脱敏范围。"""

    async def test_production_note_is_echoed_verbatim_to_factory(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """对抗性记录：派单时的备注会**原样**出现在工厂端工单详情里。

        实测结论：``FactoryOrderDetailResponse.productionNote`` 映射自
        ``factory_orders.remark``（派单参数 ``productionNote``），
        因此运营若在备注里手写客户名，工厂端就会看到——**系统不会二次脱敏**。

        这是**已知的可接受面**（备注是「人写给人的说明」，语义上不可自动脱敏：
        无法区分「客户名」与「工艺术语」），但它必须被记录：
        一旦有人以为「工厂端所有字段都脱敏」，就会把客户信息写进备注。

        断言分两半：
        1. 备注里的哨兵**可以**出现在 ``productionNote`` 上（记录实际行为）；
        2. 备注**之外**的响应字段仍不含任何哨兵（脱敏范围没有因为备注而扩大）。
        """
        ctx = await _build_stocked_order(
            client, auth, db, make_tenant, make_user, suffix="A004", merchant_account="13200001004"
        )
        factory = await _factories_headers(
            auth, db, factory_code="FAC-A004", account="13600001004"
        )
        fh = factory["headers"]

        note = f"客户 {SENTINELS['申请人姓名']} 要求本周出货"
        dispatched = await client.post(
            f"{PLATFORM}/orders/{ctx['order']['id']}/dispatch",
            headers=ctx["platform"],
            json={"factoryId": factory["factory"].id, "productionNote": note},
        )
        assert dispatched.status_code == 201, dispatched.text
        factory_order_id = dispatched.json()["id"]

        detail = await client.get(f"{FACTORY}/orders/{factory_order_id}", headers=fh)
        assert detail.status_code == 200, detail.text
        body = detail.json()

        # ① 备注被原样回显（把「运营手写」这条通道显式记录下来）
        assert body["productionNote"] == note, (
            "派单备注应原样下发；若实现改为过滤备注，请更新本用例与验收结论"
        )

        # ② 除备注外，其它字段仍不得出现任何哨兵：
        #    把 productionNote 摘掉后再跑一遍红线扫描，证明脱敏范围没有扩大。
        body_without_note = {k: v for k, v in body.items() if k != "productionNote"}
        text = _json_text(body_without_note)
        for label, sentinel in SENTINELS.items():
            assert sentinel not in text, f"除 productionNote 外的字段泄漏了{label}"

        # ③ 派单响应（同一次请求的返回值）里 productionNote 也是备注本身，
        #    而其余敏感值一律不可见。
        dispatched_without_note = {
            k: v for k, v in dispatched.json().items() if k != "productionNote"
        }
        dtext = _json_text(dispatched_without_note)
        for label, sentinel in SENTINELS.items():
            assert sentinel not in dtext, f"派单响应（备注除外）泄漏了{label}"
        assert UNIT_PRICE_SENTINEL not in dtext
        assert TOTAL_AMOUNT_SENTINEL not in dtext

    async def test_blank_production_note_is_normalized_to_none(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """空白备注（``"   "``）归一为 ``None``，不落库成一条「看起来写了点什么」的记录。"""
        ctx = await _build_stocked_order(
            client, auth, db, make_tenant, make_user, suffix="A005", merchant_account="13200001005"
        )
        factory = await _factories_headers(
            auth, db, factory_code="FAC-A005", account="13600001005"
        )

        dispatched = await client.post(
            f"{PLATFORM}/orders/{ctx['order']['id']}/dispatch",
            headers=ctx["platform"],
            json={"factoryId": factory["factory"].id, "productionNote": "   "},
        )
        assert dispatched.status_code == 201, dispatched.text
        assert dispatched.json()["productionNote"] is None


# ===========================================================================
# 三、哨兵造数自检：扫描器必须真的能发现泄漏
# ===========================================================================


class TestScannerSelfCheck:
    """自检：``_assert_no_client_data`` 不能是一条永远通过的断言。"""

    def test_scanner_detects_leaked_sentinel_and_key(self) -> None:
        """把哨兵与禁发字段名塞进嵌套结构，扫描器必须报错。

        为什么要自检：递归扫描是一个**会静默失效**的工具——写错 key、
        写成 ``in`` 而不是 ``not in``，测试依然全绿。这里用断言反向证明
        它确实具备判别力（与 ``test_catalog.py`` 的扫描器自检同一手法）。
        """

        class _FakeResponse:
            status_code = 200
            text = ""

            def __init__(self, body: Any) -> None:
                self._body = body
                self.text = json.dumps(body, ensure_ascii=False)

            def json(self) -> Any:
                return self._body

        nested = {"records": [{"order": {"applicantPhone": "x", "note": SENTINELS["租户名称"]}}]}
        with pytest.raises(AssertionError):
            _assert_no_client_data(_FakeResponse(nested), where="自检")

        # 只有禁发 key、没有哨兵值时也必须失败
        with pytest.raises(AssertionError):
            _assert_no_client_data(_FakeResponse({"units": [{"totalAmount": None}]}), where="自检")

        # 干净的响应必须通过（反向确认不是「一律失败」）
        _assert_no_client_data(
            _FakeResponse({"customerNameMasked": "S****b", "quantity": 3}), where="自检"
        )


# ===========================================================================
# 四、已知可接受面之二：二维码载荷里的租户 / 产品**技术 ID**
# ===========================================================================


class TestQrPayloadSurface:
    """二维码载荷会把租户 ID 与客户产品 ID 一并下发给工厂。"""

    async def test_jd_qr_payload_contains_tenant_id_but_not_customer_identity(
        self, client: AsyncClient, auth: Any, db: AsyncSession, make_tenant: Any, make_user: Any
    ) -> None:
        """实测：工厂端二维码清单里的 JD 载荷含 ``tenantId`` / ``productId``（技术 ID）。

        ``FactoryOrderSerializer.DENIED_FIELDS`` 的口径是**名称与金额 / 联系方式**
        （``tenantName`` / ``clientProductName`` / ``unitPrice`` …），
        技术 ID 不在禁发名单里；但 JD 载荷是
        ``JD|{tenantId}|{productId}|{sn}|{sign}``，工厂因此拿到一个
        **跨工单稳定的客户标识**：把任意两张工单的载荷放在一起，
        就能判断它们是不是同一个客户——这比脱敏名强（脱敏名会随名字变化而变），
        也比「订单号」强（订单号是每单唯一的）。

        为什么记录为「语义可疑但不算缺陷」而不是缺陷：
        工厂本来就需要按客户对账（``customerNameMasked`` 与 ``orderNo`` 就是为此下发的），
        且二维码必须与平台端导出**逐字节一致**（工厂打印的就是同一批设备），
        去掉租户 ID 会让两边格式不一致。这里把它钉成已知行为，
        避免后续有人以为「工厂完全不知道客户之间的关联」。
        """
        ctx = await _build_stocked_order(
            client, auth, db, make_tenant, make_user, suffix="A006", merchant_account="13200001006"
        )
        factory = await _factories_headers(
            auth, db, factory_code="FAC-A006", account="13600001006"
        )
        fh = factory["headers"]

        dispatched = await client.post(
            f"{PLATFORM}/orders/{ctx['order']['id']}/dispatch",
            headers=ctx["platform"],
            json={"factoryId": factory["factory"].id},
        )
        assert dispatched.status_code == 201, dispatched.text
        factory_order_id = dispatched.json()["id"]

        response = await client.get(f"{FACTORY}/orders/{factory_order_id}/qrcodes", headers=fh)
        assert response.status_code == 200, response.text
        records = response.json()["records"]
        assert records, "工单下应当有设备二维码"

        for item in records:
            assert item["format"] == "JD"
            segments = item["payload"].split("|")
            assert len(segments) == 5
            # ① 技术 ID 确实随载荷下发（记录实际行为）
            assert segments[1] == ctx["tenant"].id
            assert segments[2] == ctx["catalog"]["product_id"]
            # ② 但载荷不含任何客户身份 / 金额（红线的范围没有扩大）
            for label, sentinel in SENTINELS.items():
                assert sentinel not in item["payload"], f"二维码载荷泄漏了{label}"
            assert UNIT_PRICE_SENTINEL not in item["payload"]
            assert TOTAL_AMOUNT_SENTINEL not in item["payload"]

        # ③ 载荷里出现的只有 ID，没有名称：把整个响应体再扫一遍
        _assert_no_client_data(response, where="二维码清单(载荷检查)")
