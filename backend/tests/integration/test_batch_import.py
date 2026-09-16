"""集成测试：P4 设备批次 CSV 导入（上传 → 预检 → 导入 → 错误报告 → 幂等重跑）。

覆盖本阶段验收标准
------------------
* ① **完整链路**：上传 CSV → 预检计数（``totalRows`` / ``validRows`` / ``invalidRows``
  / ``duplicatedRows``）→ 导入 → ``importedRows`` 与设备表新增数一致、批次与明细行
  状态为 ``IMPORTED``、明细行回填 ``deviceId``。
* ② ★ **幂等重跑**：同一份文件字节再上传 → 返回**同一个批次**（不新建、不报错）；
  同一批次再 ``import`` → 设备数不变（不重复造设备）。
* ③ **无效行判定**：缺 ``sn`` → ``INVALID``；文件内重复 ``sn`` → ``SKIPPED``
  且计入 ``duplicatedRows``；``sn`` 与库内既有设备冲突 → ``INVALID``
  （实现口径：与「文件内重复」区分开，见 ``batch_service._precheck``）。
* ④ **错误报告**：``error-report`` 返回带 BOM 的 ``text/csv``，含出错行号；
  ``lines?status=INVALID`` 只返回无效行。
* ⑤ ★ **安全限额**：超过 5000 行 / 超过 5 MB 一律**拒绝**而非静默截断；
  单字段超长按 ``MAX_FIELD_LENGTH`` 截断且不导致 500。
* ⑥ **健壮性**：空文件 / 只有表头 / 缺少 SN 列 → 400 语义化错误，绝不 500。
* ⑦ 批次域为平台独占：商户 / 工厂端访问 → 403。

测试写法对齐 ``tests/integration/test_catalog.py``：camelCase 断言、
中文注释说明「为什么这样断言」。
"""

from __future__ import annotations

import csv
import io
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.db.base import utcnow
from app.models.catalog import ClientProduct, ProductTemplate
from app.models.device import Device, DeviceBatch
from app.models.enums import (
    ActivationStatus,
    AssetStatus,
    BatchLineStatus,
    BatchStatus,
    BindStatus,
    OnlineStatus,
    OrderStatus,
)
from app.models.identity import Tenant
from app.models.order import Order
from app.services.batch_service import (
    CSV_BOM,
    FIELD_MAX_LENGTHS,
    MAX_BATCH_ROWS,
    MAX_UPLOAD_BYTES,
)
from tests.conftest import API_PREFIX

pytestmark = pytest.mark.integration

PLATFORM = f"{API_PREFIX}/platform"

#: 标准表头（小写，与工厂导出的常见形态一致）
HEADER = ("sn", "imei", "iccid", "mac")


# ---------------------------------------------------------------------------
# 辅助函数（模块级，避免污染 conftest）
# ---------------------------------------------------------------------------


def _csv_bytes(rows: list[list[str]], header: tuple[str, ...] = HEADER) -> bytes:
    """构造 CSV 字节（真实换行 + UTF-8，贴近工厂导出文件）。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _row(sn: str, index: int = 1) -> list[str]:
    """构造一行合法数据（IMEI / ICCID / MAC 形态合法，避免引入无关变量）。"""
    return [
        sn,
        f"8600000000000{index:03d}",
        f"8986000000000000{index:03d}",
        f"AA:BB:CC:00:01:{index:02X}",
    ]


async def _upload(
    client: AsyncClient,
    headers: dict[str, str],
    content: bytes,
    *,
    file_name: str = "sn.csv",
    order_id: str | None = None,
) -> Any:
    """上传批次（不预设状态码，便于失败用例断言 4xx）。"""
    params = {"orderId": order_id} if order_id else None
    return await client.post(
        f"{PLATFORM}/batches",
        headers=headers,
        params=params,
        files={"file": (file_name, content, "text/csv")},
    )


async def _upload_ok(
    client: AsyncClient, headers: dict[str, str], content: bytes, **kwargs: Any
) -> dict[str, Any]:
    """上传批次并断言 201，返回响应体。"""
    response = await _upload(client, headers, content, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


async def _import(
    client: AsyncClient, headers: dict[str, str], batch_id: str
) -> Any:
    """导入批次（不预设状态码）。"""
    return await client.post(f"{PLATFORM}/batches/{batch_id}/import", headers=headers)


async def _lines(
    client: AsyncClient, headers: dict[str, str], batch_id: str, *, status: str | None = None
) -> dict[str, Any]:
    """取批次明细（可按行状态筛选）。"""
    params = {"status": status} if status else None
    response = await client.get(
        f"{PLATFORM}/batches/{batch_id}/lines", headers=headers, params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _device_count(db: AsyncSession, *, sn: str | None = None) -> int:
    """统计设备数（可限定 SN），用于「导入数量一致」与「不重复造设备」的断言。"""
    stmt = select(func.count()).select_from(Device)
    if sn is not None:
        stmt = stmt.where(Device.sn == sn)
    return int((await db.execute(stmt)).scalar_one())


async def _batch_count(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count()).select_from(DeviceBatch))).scalar_one())


async def _make_existing_device(db: AsyncSession, *, sn: str) -> Device:
    """预置一台设备（用于验证「SN 与库内既有设备冲突」的判定）。"""
    device = Device(
        id=new_id("device"),
        sn=sn,
        network_type="4G",
        asset_status=str(AssetStatus.GENERATED),
        activation_status=str(ActivationStatus.NOT_ACTIVATED),
        online_status=str(OnlineStatus.NEVER_ONLINE),
        bind_status=str(BindStatus.UNBOUND),
        generated_at=utcnow(),
    )
    db.add(device)
    await db.flush()
    return device


async def _make_two_orders(db: AsyncSession) -> tuple[Order, Order]:
    """直接落「同一租户下的两张订单」（批次与订单的归属绑定用）。

    走库级造数而不是 HTTP：本组用例要验证的是**文件摘要幂等**与订单归属的关系，
    审核 / 生成等前置流程不构成变量。
    """
    template = ProductTemplate(
        id=new_id("product_template"), code="BATCH-TPL", name="批次模板", network_type="4G"
    )
    tenant_id = new_id("tenant")
    db.add(Tenant(id=tenant_id, code=new_id("tenant").upper(), name="批次订单租户", status="ACTIVE"))
    db.add(template)
    await db.flush()

    product = ClientProduct(
        id=new_id("client_product"),
        tenant_id=tenant_id,
        template_id=template.id,
        code="BATCH-PROD",
        name="批次产品",
        network_type="4G",
    )
    db.add(product)
    await db.flush()

    orders = [
        Order(
            id=new_id("order"),
            order_no=f"ORD-BATCH-{index:02d}",
            tenant_id=tenant_id,
            client_product_id=product.id,
            quantity=2,
            status=str(OrderStatus.PENDING_AUDIT),
            network_type="4G",
        )
        for index in (1, 2)
    ]
    db.add_all(orders)
    await db.flush()
    return orders[0], orders[1]


# ===========================================================================
# 一、完整链路
# ===========================================================================


class TestBatchImportHappyPath:
    """上传 → 预检 → 导入全链路，计数与落库结果一致。"""

    async def test_precheck_then_import_produces_devices(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        platform = await auth.platform_headers()
        content = _csv_bytes([_row(f"SN-BATCH-{i:02d}", i) for i in range(1, 5)])

        batch = await _upload_ok(client, platform, content, file_name="factory-4g.csv")

        # ---- ① 预检结论 ----
        assert batch["status"] == str(BatchStatus.PRE_CHECKED)
        assert batch["fileName"] == "factory-4g.csv"
        assert batch["totalRows"] == 4
        assert batch["validRows"] == 4
        assert batch["invalidRows"] == 0
        assert batch["duplicatedRows"] == 0
        assert batch["importedRows"] == 0
        assert batch["importedRatio"] == 0.0
        assert batch["errorReportAvailable"] is False
        assert batch["fileSha256"], "文件摘要必须落库（幂等重跑的依据）"
        # 未关联订单时批次默认 4G（与工厂导入 4G 模组清单的主场景一致）
        assert batch["networkType"] == "4G"
        assert batch["tenantId"] is None

        # 批次列表可查（预检结论不是「只在创建响应里」）
        listed = await client.get(
            f"{PLATFORM}/batches", headers=platform, params={"keyword": batch["batchNo"]}
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["total"] == 1

        # 预检明细：行号采用电子表格行号（表头为第 1 行）
        precheck_lines = await _lines(client, platform, batch["id"])
        assert precheck_lines["total"] == 4
        assert [item["rowNo"] for item in precheck_lines["records"]] == [2, 3, 4, 5]
        assert {item["status"] for item in precheck_lines["records"]} == {
            str(BatchLineStatus.VALID)
        }
        assert all(item["deviceId"] is None for item in precheck_lines["records"])
        assert all(item["imei"] for item in precheck_lines["records"])

        # ---- ② 导入 ----
        imported = await _import(client, platform, batch["id"])
        assert imported.status_code == 200, imported.text
        body = imported.json()
        assert body["status"] == str(BatchStatus.IMPORTED)
        assert body["importedRows"] == 4
        assert body["importedRatio"] == 1.0
        assert body["finishedAt"] is not None

        # ---- ③ 设备表新增数与 importedRows 一致 ----
        assert await _device_count(db) == 4
        devices = list(
            (
                await db.execute(select(Device).where(Device.batch_id == batch["id"]))
            ).scalars()
        )
        assert len(devices) == 4
        assert {device.sn for device in devices} == {f"SN-BATCH-{i:02d}" for i in range(1, 5)}
        assert all(device.asset_status == str(AssetStatus.GENERATED) for device in devices)
        assert all(device.tenant_id is None for device in devices), "批次导入先入平台库存"
        assert all(device.order_id is None for device in devices)

        # ---- ④ 明细行状态与回填 ----
        imported_lines = await _lines(client, platform, batch["id"], status="IMPORTED")
        assert imported_lines["total"] == 4
        assert all(item["deviceId"] for item in imported_lines["records"])
        # 回填的 deviceId 必须真实存在于设备表（不是随手写的串）
        assert {item["deviceId"] for item in imported_lines["records"]} == {
            device.id for device in devices
        }

        # ---- ⑤ 每个新建设备都有 IMPORTED 事件（时间线可追溯）----
        events = await client.get(
            f"{PLATFORM}/devices/{devices[0].id}/events", headers=platform
        )
        assert events.status_code == 200, events.text
        imported_events = [
            item for item in events.json()["records"] if item["eventType"] == "IMPORTED"
        ]
        assert len(imported_events) == 1
        assert imported_events[0]["dimension"] == "asset"
        assert imported_events[0]["toStatus"] == str(AssetStatus.GENERATED)
        assert imported_events[0]["detail"]["batchNo"] == batch["batchNo"]

        # 设备列表能按 SN 检索到（keyword 走 SN/IMEI/MAC）
        found = await client.get(
            f"{PLATFORM}/devices", headers=platform, params={"keyword": devices[0].sn}
        )
        assert found.json()["total"] == 1


# ===========================================================================
# 二、★ 幂等重跑
# ===========================================================================


class TestBatchIdempotency:
    """同一份文件、同一个批次的重复操作都必须收敛到同一结果。"""

    async def test_reupload_same_bytes_reuses_batch_and_import_is_idempotent(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        platform = await auth.platform_headers()
        content = _csv_bytes([_row("SN-IDEM-01", 1), _row("SN-IDEM-02", 2)])

        first = await _upload_ok(client, platform, content)
        second = await _upload_ok(client, platform, content, file_name="另一个文件名.csv")

        assert second["id"] == first["id"], "同一份文件（SHA-256 相同）必须复用批次"
        assert second["batchNo"] == first["batchNo"]
        assert second["fileSha256"] == first["fileSha256"]
        assert await _batch_count(db) == 1, "重复上传不得新建批次"
        # 幂等复用不应把文件名改掉（首次为准）
        assert second["fileName"] == first["fileName"]

        assert (await _import(client, platform, first["id"])).status_code == 200
        assert await _device_count(db) == 2

        # 再导入一次：幂等返回，设备数不变
        again = await _import(client, platform, first["id"])
        assert again.status_code == 200, again.text
        assert again.json()["status"] == str(BatchStatus.IMPORTED)
        assert again.json()["importedRows"] == 2
        assert await _device_count(db) == 2, "重复导入绝不能重复造设备"

        # 导入完成后再上传同一文件：仍然复用该批次，且状态已推进到 IMPORTED
        third = await _upload_ok(client, platform, content)
        assert third["id"] == first["id"]
        assert third["status"] == str(BatchStatus.IMPORTED)
        assert await _batch_count(db) == 1

    async def test_same_file_for_another_order_is_not_silently_reused(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """★ 同一份文件换订单上传，不得把新订单的意图接到旧订单的批次上。

        ``file_sha256`` 全局唯一是幂等重跑的基础，但 ``create_batch`` 复用已有批次时
        **不校验 ``orderId``**：一旦运维把同一份工厂清单为另一张订单上传，
        拿到的仍是旧批次（``orderId`` / ``tenantId`` 都是旧的），随后导入的设备
        会静默挂到旧订单下——这正是「静默错关联」类故障的典型形态。
        """
        platform = await auth.platform_headers()
        order_1, order_2 = await _make_two_orders(db)
        content = _csv_bytes([_row("SN-REUSE-01", 1), _row("SN-REUSE-02", 2)])

        first = await _upload_ok(client, platform, content, order_id=order_1.id)
        assert first["orderId"] == order_1.id
        assert first["tenantId"] == order_1.tenant_id

        response = await _upload(client, platform, content, order_id=order_2.id)
        assert response.status_code in (201, 409), response.text

        body = response.json()
        if response.status_code == 201:
            assert body["id"] != first["id"], "不同订单应得到自己的批次"
            assert body["orderId"] == order_2.id, (
                f"该批次被绑定到 orderId={body['orderId']!r}，"
                f"而本次请求的是 orderId={order_2.id!r}——同摘要复用忽略了订单归属"
            )
        else:
            assert body.get("code"), "冲突响应必须带统一错误码"


# ===========================================================================
# 三、无效 / 重复行
# ===========================================================================


class TestInvalidAndDuplicatedRows:
    """预检判定：缺 SN / 文件内重复 / 与库内冲突，三者语义必须可区分。"""

    async def test_missing_duplicated_and_existing_rows_are_classified(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        platform = await auth.platform_headers()
        await _make_existing_device(db, sn="SN-BATCH-EXIST")

        content = _csv_bytes(
            [
                _row("SN-BATCH-A", 1),  # 行 2：有效
                ["", "8600000000000009", "8986000000000009", ""],  # 行 3：缺 SN
                _row("SN-BATCH-A", 3),  # 行 4：文件内重复 → SKIPPED
                _row("SN-BATCH-EXIST", 4),  # 行 5：库内已存在 → INVALID
                _row("SN-BATCH-B", 5),  # 行 6：有效
            ]
        )

        batch = await _upload_ok(client, platform, content)

        assert batch["totalRows"] == 5
        assert batch["validRows"] == 2, "只有 SN-BATCH-A / SN-BATCH-B 可导入"
        assert batch["invalidRows"] == 3
        # duplicatedRows 的语义是**文件内部**重复；「库内已存在」属于无效行，
        # 只计入 invalidRows（两个口径不同，混在一起会让字段名失去意义）
        assert batch["duplicatedRows"] == 1, "只有第 5 行是文件内重复"
        assert batch["errorReportAvailable"] is True

        # 明细：三类判定各自命中，且行号可直接定位
        lines = await _lines(client, platform, batch["id"])
        by_row = {item["rowNo"]: item for item in lines["records"]}
        assert by_row[2]["status"] == str(BatchLineStatus.VALID)
        assert by_row[3]["status"] == str(BatchLineStatus.INVALID)
        assert by_row[3]["errorMessage"] == "缺少 SN"
        assert by_row[4]["status"] == str(BatchLineStatus.SKIPPED)
        assert by_row[4]["errorMessage"] == "文件内重复，已跳过"
        assert by_row[5]["status"] == str(BatchLineStatus.INVALID)
        assert by_row[5]["errorMessage"] == "SN 已存在于设备库"
        assert by_row[6]["status"] == str(BatchLineStatus.VALID)

        # 按行状态筛选
        invalid_only = await _lines(client, platform, batch["id"], status="INVALID")
        assert invalid_only["total"] == 2
        assert {item["rowNo"] for item in invalid_only["records"]} == {3, 5}
        skipped_only = await _lines(client, platform, batch["id"], status="SKIPPED")
        assert skipped_only["total"] == 1
        assert skipped_only["records"][0]["sn"] == "SN-BATCH-A"

        # 导入只落有效行，且不重复落已有的 SN
        imported = await _import(client, platform, batch["id"])
        assert imported.status_code == 200, imported.text
        assert imported.json()["importedRows"] == 2
        assert await _device_count(db, sn="SN-BATCH-A") == 1, "文件内重复不得造出第二台"
        assert await _device_count(db, sn="SN-BATCH-B") == 1
        assert await _device_count(db, sn="SN-BATCH-EXIST") == 1, "库内既有设备不得被覆盖或复制"
        assert await _device_count(db) == 3


# ===========================================================================
# 四、错误报告
# ===========================================================================


class TestErrorReport:
    """错误报告是「给 Excel 直接打开」的产物：BOM + 行号 + 原因。"""

    async def test_error_report_is_bom_csv_with_row_numbers(
        self, client: AsyncClient, auth: Any
    ) -> None:
        platform = await auth.platform_headers()
        content = _csv_bytes(
            [
                _row("SN-REPORT-01", 1),
                # 行 3：有其它字段但缺 SN（整行全空会被当作空行跳过，故不构成「缺 SN」用例）
                ["", "8600000000000009", "8986000000000009", ""],
                _row("SN-REPORT-01", 3),  # 行 4：文件内重复
            ]
        )
        batch = await _upload_ok(client, platform, content)
        assert batch["errorReportAvailable"] is True

        response = await client.get(
            f"{PLATFORM}/batches/{batch['id']}/error-report", headers=platform
        )
        assert response.status_code == 200, response.text
        assert "text/csv" in response.headers["content-type"]
        assert "attachment" in response.headers.get("content-disposition", "")

        text = response.content.decode("utf-8")
        assert text.startswith(CSV_BOM), "必须带 BOM，否则 Excel 打开中文乱码"
        assert "行号" in text and "错误原因" in text
        # 出错行号与原因都要出现（第 3、4 行）
        assert "3" in text and "4" in text
        assert "缺少 SN" in text
        assert "文件内重复" in text
        assert "SN-REPORT-01" in text

        # 错误行不能被算作可导入
        assert batch["validRows"] == 1
        assert batch["invalidRows"] == 2

    async def test_error_report_for_clean_batch_is_header_only(
        self, client: AsyncClient, auth: Any
    ) -> None:
        """无错误时仍返回合法 CSV（只有表头），而不是 404 或空响应。"""
        platform = await auth.platform_headers()
        batch = await _upload_ok(client, platform, _csv_bytes([_row("SN-CLEAN-01", 1)]))
        assert batch["errorReportAvailable"] is False

        response = await client.get(
            f"{PLATFORM}/batches/{batch['id']}/error-report", headers=platform
        )
        assert response.status_code == 200, response.text
        text = response.content.decode("utf-8")
        assert text.startswith(CSV_BOM)
        assert text.strip().lstrip(CSV_BOM).splitlines() == ["行号,SN,错误原因"]


# ===========================================================================
# 五、★ 安全限额
# ===========================================================================


class TestBatchSafetyLimits:
    """限额必须「拒绝」而不是「静默截断」——静默截断是最危险的失败形态。"""

    async def test_more_than_max_rows_is_rejected_not_truncated(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        platform = await auth.platform_headers()
        rows = [_row(f"SN-OVER-{i:05d}", i % 900 + 1) for i in range(MAX_BATCH_ROWS + 1)]
        content = _csv_bytes(rows)

        response = await _upload(client, platform, content)

        assert response.status_code == 400, response.text
        body = response.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert str(MAX_BATCH_ROWS) in body["message"], "错误信息要说明上限，便于用户拆分文件"
        assert await _batch_count(db) == 0, "被拒绝的上传不得留下半个批次"

    async def test_oversize_file_is_rejected(self, client: AsyncClient, auth: Any) -> None:
        """行数限制挡不住「一行特别长」，因此还要有字节数上限。"""
        platform = await auth.platform_headers()
        content = b"sn,imei\n" + b"A" * (MAX_UPLOAD_BYTES + 1)

        response = await _upload(client, platform, content)

        assert response.status_code == 400, response.text
        assert response.json()["code"] == "VALIDATION_ERROR"
        assert "MB" in response.json()["message"]

    async def test_overlong_field_is_truncated_without_500(
        self, client: AsyncClient, auth: Any
    ) -> None:
        """单字段超长按**该字段自己的上限**截断（并记日志），绝不 500。

        各字段上限与数据库列长一致（``sn`` 64、``imei/iccid/mac`` 32）——
        SQLite 不校验长度所以截断到 128 也看不出问题，
        但换 PostgreSQL 就会写库报错。
        """
        platform = await auth.platform_headers()
        sn_limit = FIELD_MAX_LENGTHS["sn"]
        overlong = "S" * (sn_limit + 72)
        batch = await _upload_ok(client, platform, _csv_bytes([[overlong, "8600", "8986", ""]]))

        assert batch["totalRows"] == 1
        assert batch["validRows"] == 1, "截断后 SN 非空，仍属可导入行"
        lines = await _lines(client, platform, batch["id"])
        sn = lines["records"][0]["sn"]
        assert len(sn) == sn_limit
        assert sn == "S" * sn_limit

        # 导入超长 SN 的设备不得导致 5xx（截断行为本身已在上面钉死）
        imported = await _import(client, platform, batch["id"])
        assert imported.status_code < 500, imported.text


# ===========================================================================
# 六、健壮性：空文件 / 只有表头 / 缺列
# ===========================================================================


class TestMalformedUploads:
    """畸形输入必须是 400 语义化错误，不能是 500。"""

    @pytest.mark.parametrize(
        ("label", "content", "expected_fragment"),
        [
            ("空字节文件", b"", "空"),
            ("纯空白文件", b"   \n\n", "空"),
            ("只有表头", b"sn,imei,iccid,mac\n", "没有数据行"),
            ("只有表头与空行", b"sn,imei\n\n\n", "没有数据行"),
        ],
    )
    async def test_empty_like_files_are_rejected_gracefully(
        self,
        client: AsyncClient,
        auth: Any,
        db: AsyncSession,
        label: str,
        content: bytes,
        expected_fragment: str,
    ) -> None:
        platform = await auth.platform_headers()
        response = await _upload(client, platform, content)

        assert response.status_code == 400, f"{label}：{response.text}"
        body = response.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert expected_fragment in body["message"]
        assert await _batch_count(db) == 0

    async def test_missing_sn_column_reports_the_seen_headers(
        self, client: AsyncClient, auth: Any, db: AsyncSession
    ) -> None:
        """缺 SN 列时把实际表头回显出来——用户才知道错在哪一列。"""
        platform = await auth.platform_headers()
        response = await _upload(client, platform, b"imei,iccid\n8600000000000001,8986000000000000001\n")

        assert response.status_code == 400, response.text
        body = response.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert "SN" in body["message"]
        assert body["details"]["headers"] == ["imei", "iccid"]
        assert await _batch_count(db) == 0

    async def test_unknown_headers_do_not_crash(self, client: AsyncClient, auth: Any) -> None:
        """多出无关列（工厂常带备注列）不应影响解析。"""
        platform = await auth.platform_headers()
        response = await _upload(
            client,
            platform,
            b"sn,remark,extra\nSN-EXTRA-01,\xe5\xa4\x87\xe6\xb3\xa8,1\n",
        )
        assert response.status_code == 201, response.text
        assert response.json()["validRows"] == 1

    async def test_blank_lines_are_skipped_without_shifting_row_numbers(
        self, client: AsyncClient, auth: Any
    ) -> None:
        """空行不计入 ``totalRows``（Excel 常在末尾留空行），但行号保持电子表格口径。"""
        platform = await auth.platform_headers()
        content = b"sn,imei,iccid,mac\nSN-BLANK-01,8600,8986,AA\n\n\nSN-BLANK-02,8601,8987,BB\n"

        batch = await _upload_ok(client, platform, content)

        assert batch["totalRows"] == 2, "空行不算数据行"
        assert batch["validRows"] == 2
        lines = await _lines(client, platform, batch["id"])
        assert [item["rowNo"] for item in lines["records"]] == [2, 5], (
            "行号必须忠于原始文件（跳过空行后第 2 条数据在第 5 行），否则错误报告无法定位"
        )


# ===========================================================================
# 七、解析容错（编码 / 表头别名）
# ===========================================================================


class TestParsingTolerance:
    """工厂导出的 CSV 表头与编码五花八门：能容错就不能拒之门外。"""

    async def test_gbk_encoded_file_with_chinese_headers_is_accepted(
        self, client: AsyncClient, auth: Any
    ) -> None:
        """GBK 编码 + 中文表头（国内工厂最常见的导出形态）。"""
        platform = await auth.platform_headers()
        content = "序列号,IMEI,ICCID,MAC地址\nSN-GBK-01,8600000000000001,8986000000000000001,AA:BB\n".encode(
            "gbk"
        )

        batch = await _upload_ok(client, platform, content)

        assert batch["totalRows"] == 1
        assert batch["validRows"] == 1
        lines = await _lines(client, platform, batch["id"])
        assert lines["records"][0]["sn"] == "SN-GBK-01"
        assert lines["records"][0]["imei"] == "8600000000000001"
        assert lines["records"][0]["mac"] == "AA:BB"

    @pytest.mark.parametrize(
        "header",
        [
            "SN,IMEI,ICCID,MAC",  # 全大写
            "serial,imei,iccid,mac",  # 英文别名
            "设备序列号,imei,iccid,mac",  # 中文别名
            "device_sn,imei,iccid,mac",  # 下划线形式
        ],
    )
    async def test_header_alias_variants_are_normalized(
        self, client: AsyncClient, auth: Any, header: str
    ) -> None:
        platform = await auth.platform_headers()
        content = f"{header}\nSN-HDR-01,8600000000000001,8986000000000000001,AA:BB\n".encode()

        batch = await _upload_ok(client, platform, content)

        assert batch["totalRows"] == 1
        assert batch["validRows"] == 1, f"表头 {header!r} 应被识别"

    async def test_utf8_bom_header_is_accepted(self, client: AsyncClient, auth: Any) -> None:
        """Excel 另存为 UTF-8 CSV 会在文件头带 BOM，表头不能被它污染。"""
        platform = await auth.platform_headers()
        content = "\ufeffSN,IMEI,ICCID,MAC\nSN-BOM-01,8600000000000001,8986000000000000001,AA:BB\n".encode()

        batch = await _upload_ok(client, platform, content)

        assert batch["validRows"] == 1
        lines = await _lines(client, platform, batch["id"])
        assert lines["records"][0]["sn"] == "SN-BOM-01"


# ===========================================================================
# 八、平台独占
# ===========================================================================


class TestBatchEndpointsArePlatformOnly:
    """批次域（含错误报告）是平台独占能力。"""

    @pytest.mark.parametrize("role", ["merchant", "factory"])
    async def test_non_platform_role_cannot_touch_batches(
        self, client: AsyncClient, auth: Any, role: str
    ) -> None:
        headers = (
            await auth.merchant_headers() if role == "merchant" else await auth.factory_headers()
        )

        listing = await client.get(f"{PLATFORM}/batches", headers=headers)
        assert listing.status_code == 403, listing.text
        assert listing.json()["code"] == "PERMISSION_DENIED"

        upload = await _upload(client, headers, _csv_bytes([_row("SN-HACK-01", 1)]))
        assert upload.status_code == 403, upload.text
        assert upload.json()["code"] == "PERMISSION_DENIED"

        report = await client.get(
            f"{PLATFORM}/batches/device_batch-does-not-exist/error-report", headers=headers
        )
        assert report.status_code == 403, "权限校验必须先于「资源是否存在」，避免探测"
