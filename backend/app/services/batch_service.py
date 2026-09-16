"""设备批次 CSV 导入服务。

完整链路
========

    上传 → SHA-256 去重 → 预检 → 导入 → 进度 → 错误报告 → 幂等重跑

每一步的落地方式
----------------

* **上传 + 预检**（:func:`create_batch`）：解析 CSV 后立刻逐行判定并落
  ``device_batch_lines``，行状态即预检结论。若只有「预检报告」而没有明细行，
  导入时就只能重新解析一遍文件——同一份数据两处判定，迟早会不一致。
* **SHA-256 去重**：``device_batches.file_sha256`` 唯一。重复上传同一份文件
  **直接返回已存在的批次**（不抛错、不重复导入）——这是「幂等重跑」的基础：
  前端重试、网络重发、用户手抖点两次，结果都一致。
* **导入**（:func:`import_batch`）：只为 ``VALID`` 行建设备，建完把行标为
  ``IMPORTED`` 并回填 ``device_id``。因此**失败后重跑只补没建过的行**，
  不会产生重复设备。
* **错误报告**：``error_report`` 存结构化明细，:func:`build_error_csv`
  输出带 BOM 的 CSV（Excel 直接双击不乱码）。

安全边界（防超大文件打爆内存）
------------------------------
* 单文件数据行上限 :data:`MAX_BATCH_ROWS`，超限直接拒绝而不是截断
  （截断会让人以为「就导入了这些」，是最危险的静默失败）；
* 单元格长度上限 :data:`MAX_FIELD_LENGTH`，超长即截断并记录；
* 全程使用标准库 ``csv`` 解析，**不使用** ``eval`` / ``exec`` 之类动态求值。
"""

from __future__ import annotations

import csv
import hashlib
import io
import secrets
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.core.errors import (
    batch_file_conflict,
    internal_error,
    invalid_state_transition,
    not_found,
    validation_error,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.scope import assert_visible, scoped
from app.models.device import Device, DeviceBatch, DeviceBatchLine
from app.models.enums import AssetStatus, AuditAction, BatchLineStatus, BatchStatus, NetworkType
from app.models.order import Order
from app.schemas.batch import BatchLineResponse, BatchResponse
from app.services import audit_service, device_service

logger = get_logger(__name__)

#: 单文件允许的最大数据行数（不含表头）。5000 台一次导入对演示与中小批量足够，
#: 同时把最坏内存占用压在可控范围内。
MAX_BATCH_ROWS = 5000
#: 单个文件允许的最大字节数（5 MB）。行数限制挡不住「一行特别长」的情况，
#: 因此在读入内存后立刻再做一次字节数校验。
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
#: 单个单元格最大长度（表头/原始行等非落地字段的兜底上限）
MAX_FIELD_LENGTH = 128
#: 各落地字段的最大长度，**必须与数据库列长一致**：
#: ``devices.sn`` 与 ``device_batch_lines.sn`` 是 ``String(64)``、
#: imei/iccid/mac 是 ``String(32)``。SQLite 不校验长度所以短了看不出来，
#: 但换 PostgreSQL（ADR-05 要求支持）超长会直接写库报错。
FIELD_MAX_LENGTHS: dict[str, int] = {"sn": 64, "imei": 32, "iccid": 32, "mac": 32}
#: 批次号随机后缀字符集
BATCH_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
#: 批次号生成重试次数
BATCH_NO_ATTEMPTS = 5

#: 列名容错表：归一化后的表头 → 规范字段名。
#: 为什么做容错？工厂/供应商给的 CSV 表头五花八门（中英文、大小写、空格），
#: 要求对方改表头不现实；且列顺序不固定，按序号取值必然错位。
HEADER_ALIASES: dict[str, str] = {
    "sn": "sn",
    "序列号": "sn",
    "设备序列号": "sn",
    "serial": "sn",
    "serialno": "sn",
    "devicesn": "sn",
    "imei": "imei",
    "iccid": "iccid",
    "sim": "iccid",
    "mac": "mac",
    "mac地址": "mac",
}

#: CSV 输出的 BOM：没有它 Excel 会用本地编码打开 UTF-8 文件，中文变乱码
CSV_BOM = "\ufeff"

#: 错误报告 CSV 的表头
ERROR_CSV_HEADER = ("行号", "SN", "错误原因")

#: 批次列表允许的排序字段
ALLOWED_SORT_FIELDS: frozenset[str] = frozenset({"created_at", "batch_no", "total_rows"})


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _normalize_header(value: str) -> str:
    """归一化表头：去 BOM / 空白 / 下划线 / 连字符，转小写。"""
    cleaned = (value or "").strip().lstrip(CSV_BOM)
    for token in (" ", "_", "-", "／", "/"):
        cleaned = cleaned.replace(token, "")
    return cleaned.lower()


def _decode(content: bytes) -> str:
    """把上传内容解码为文本。

    先试 UTF-8（含 BOM），失败再试 GBK——国内工厂导出的 CSV 多数是 GBK，
    两种都是「正常文件」，不该因为编码把整批数据拒之门外。
    最后兜底 latin-1（不会抛异常），保证错误以「行内容不对」的形式
    暴露在预检报告里，而不是一个让人摸不着头脑的解码异常。
    """
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def _clean_cell(value: str | None, limit: int = MAX_FIELD_LENGTH) -> str:
    """清洗单元格：去空白并按给定上限截断。"""
    return (value or "").strip()[:limit]


def parse_csv(content: bytes) -> list[dict[str, Any]]:
    """解析 CSV 为「行记录」列表。

    Args:
        content: 文件原始字节。

    Returns:
        每项形如 ``{"row_no": 2, "sn": "...", "imei": ..., "iccid": ..., "mac": ...,
        "raw": {...}}``；``row_no`` 采用**电子表格行号**（表头为第 1 行），
        这样错误报告里的行号可以直接拿去定位。

    Raises:
        AppException: 空文件、缺少 SN 列、行数超限（``VALIDATION_ERROR``）。
    """
    if not content.strip():
        raise validation_error("文件内容为空")

    text = _decode(content)
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header_row = next(reader)
    except StopIteration as exc:  # pragma: no cover - 空内容已在上面拦下
        raise validation_error("文件内容为空") from exc

    indexes: dict[str, int] = {}
    for index, raw_header in enumerate(header_row):
        field = HEADER_ALIASES.get(_normalize_header(raw_header))
        # 同名列只取第一处：避免后面的空列覆盖掉真正有数据的列
        if field and field not in indexes:
            indexes[field] = index

    if "sn" not in indexes:
        raise validation_error(
            "CSV 缺少 SN 列（支持表头：SN / 序列号 / serial）",
            details={"field": "sn", "headers": [_clean_cell(h) for h in header_row][:20]},
        )

    rows: list[dict[str, Any]] = []
    truncated_fields = 0
    truncated_names: set[str] = set()
    for row_no, raw_row in enumerate(reader, start=2):
        if not any((cell or "").strip() for cell in raw_row):
            continue  # 跳过空行：Excel 常在末尾留空行
        if len(rows) >= MAX_BATCH_ROWS:
            raise validation_error(
                f"文件行数超过上限 {MAX_BATCH_ROWS} 行，请拆分后再上传"
            )

        record: dict[str, Any] = {"row_no": row_no}
        for field, index in indexes.items():
            original = raw_row[index] if index < len(raw_row) else ""
            limit = FIELD_MAX_LENGTHS.get(field, MAX_FIELD_LENGTH)
            cleaned = _clean_cell(original, limit)
            if len((original or "").strip()) > limit:
                truncated_fields += 1
                truncated_names.add(field)
            record[field] = cleaned or None
        record["raw"] = {
            _clean_cell(header_row[i]): _clean_cell(cell)
            for i, cell in enumerate(raw_row)
            if i < len(header_row)
        }
        rows.append(record)

    if not rows:
        raise validation_error("文件中没有数据行")
    if truncated_fields:
        # 记录是哪些字段被截断：只报个数字，运维无法判断是 SN 被截了（要紧）
        # 还是备注类字段被截了（无所谓）
        logger.warning(
            "CSV 中有 %d 个超长单元格被截断（字段：%s）",
            truncated_fields,
            "、".join(sorted(truncated_names)) or "-",
        )
    return rows


# ---------------------------------------------------------------------------
# 单号
# ---------------------------------------------------------------------------


def generate_batch_no(now: datetime | None = None) -> str:
    """生成批次号 ``BATCH-YYYYMMDD-XXXX``。"""
    stamp = (now or utcnow()).strftime("%Y%m%d")
    suffix = "".join(secrets.choice(BATCH_ALPHABET) for _ in range(4))
    return f"BATCH-{stamp}-{suffix}"


async def _next_batch_no(session: AsyncSession) -> str:
    """取一个未被占用的批次号。"""
    for _ in range(BATCH_NO_ATTEMPTS):
        candidate = generate_batch_no()
        exists = (
            await session.execute(select(DeviceBatch.id).where(DeviceBatch.batch_no == candidate))
        ).scalar_one_or_none()
        if exists is None:
            return candidate
    raise internal_error("批次号生成失败，请重试")


# ---------------------------------------------------------------------------
# 转换
# ---------------------------------------------------------------------------


def batch_to_response(batch: DeviceBatch) -> BatchResponse:
    """ORM → 批次响应（含派生的导入比例与「是否有错误明细」）。"""
    response = BatchResponse.model_validate(batch)
    denominator = batch.valid_rows or batch.total_rows
    response.imported_ratio = round(batch.imported_rows / denominator, 4) if denominator else 0.0
    report = batch.error_report or {}
    response.error_report_available = bool(report.get("rows"))
    return response


def line_to_response(line: DeviceBatchLine) -> BatchLineResponse:
    """ORM → 明细行响应。"""
    return BatchLineResponse.model_validate(line)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def get_batch(session: AsyncSession, batch_id: str) -> DeviceBatch:
    """按 ID 取批次。

    Raises:
        AppException: 批次不存在。
    """
    batch = (
        await session.execute(select(DeviceBatch).where(DeviceBatch.id == batch_id))
    ).scalar_one_or_none()
    if batch is None:
        raise not_found("批次不存在")
    return batch


async def list_batches(
    session: AsyncSession,
    auth: AuthContext,
    *,
    offset: int = 0,
    limit: int = 20,
    status: str | None = None,
    keyword: str | None = None,
    sort_by: str | None = None,
    order: str = "desc",
) -> tuple[list[DeviceBatch], int]:
    """分页查询批次（租户过滤经 :func:`scoped`）。"""
    conditions: list[ColumnElement[bool]] = []
    if status:
        conditions.append(DeviceBatch.status == status)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(DeviceBatch.batch_no.like(pattern) | DeviceBatch.file_name.like(pattern))

    if sort_by and sort_by not in ALLOWED_SORT_FIELDS:
        raise validation_error(f"不支持的排序字段：{sort_by}")

    total = int(
        (
            await session.execute(
                scoped(select(func.count()).select_from(DeviceBatch), DeviceBatch, auth).where(
                    *conditions
                )
            )
        ).scalar_one()
    )

    column = getattr(DeviceBatch, sort_by) if sort_by else DeviceBatch.created_at
    stmt = (
        scoped(select(DeviceBatch), DeviceBatch, auth)
        .where(*conditions)
        .order_by(column.desc() if order == "desc" else column.asc())
        .offset(offset)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all()), total


async def list_lines(
    session: AsyncSession,
    auth: AuthContext,
    batch_id: str,
    *,
    offset: int = 0,
    limit: int = 20,
    status: str | None = None,
) -> tuple[list[DeviceBatchLine], int]:
    """分页查询批次明细（先校验批次可见性）。"""
    batch = await get_batch(session, batch_id)
    assert_visible(batch.tenant_id, auth, resource="批次")

    conditions: list[ColumnElement[bool]] = [DeviceBatchLine.batch_id == batch_id]
    if status:
        conditions.append(DeviceBatchLine.status == status)

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(DeviceBatchLine).where(*conditions)
            )
        ).scalar_one()
    )
    stmt = (
        select(DeviceBatchLine)
        .where(*conditions)
        .order_by(DeviceBatchLine.row_no.asc())
        .offset(offset)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all()), total


# ---------------------------------------------------------------------------
# 上传 + 预检
# ---------------------------------------------------------------------------


async def create_batch(
    session: AsyncSession,
    auth: AuthContext,
    *,
    file_name: str,
    content: bytes,
    order_id: str | None = None,
    request: Request | None = None,
) -> DeviceBatch:
    """上传并预检批次（幂等：同一份文件重复上传直接返回原批次）。

    Args:
        session: 数据库会话。
        auth: 认证上下文（平台端）。
        file_name: 原始文件名。
        content: 文件字节。
        order_id: 可选关联订单；提供时联网方式与租户继承自订单。
        request: 用于提取客户端 IP。

    Returns:
        新建的批次（``PRE_CHECKED``）或已存在的同摘要批次。

    Raises:
        AppException: 文件不合法（``VALIDATION_ERROR``）。
    """
    if len(content) > MAX_UPLOAD_BYTES:
        raise validation_error(
            f"文件大小超过上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB，请拆分后再上传"
        )

    sha256 = hashlib.sha256(content).hexdigest()

    existing = (
        await session.execute(select(DeviceBatch).where(DeviceBatch.file_sha256 == sha256))
    ).scalar_one_or_none()
    if existing is not None:
        # 幂等：**同一订单**重复上传同一份文件时复用既有批次，不重复导入。
        # 但若本次带了不同的 orderId，就绝不能复用——否则导入出来的设备会
        # 静默挂到旧订单/旧租户上（归属错误，且用户从界面上看不出来）。
        if (existing.order_id or None) != (order_id or None):
            raise batch_file_conflict(
                f"该文件已用于批次 {existing.batch_no}（订单 {existing.order_id or '无'}），"
                "不能用于另一个订单。请修改文件内容或改选正确的订单。",
                details={
                    "existingBatchNo": existing.batch_no,
                    "existingOrderId": existing.order_id,
                    "requestedOrderId": order_id,
                },
            )
        logger.info("批次 %s 已存在（文件摘要相同且订单一致），直接复用", existing.batch_no)
        return existing

    order: Order | None = None
    if order_id:
        order = (
            await session.execute(select(Order).where(Order.id == order_id))
        ).scalar_one_or_none()
        if order is None:
            raise not_found("关联订单不存在")

    rows = parse_csv(content)

    batch = DeviceBatch(
        id=new_id("device_batch"),
        batch_no=await _next_batch_no(session),
        order_id=order.id if order else None,
        tenant_id=order.tenant_id if order else None,
        network_type=order.network_type if order else str(NetworkType.FOUR_G),
        file_name=_clean_cell(file_name) or None,
        file_sha256=sha256,
        status=str(BatchStatus.PRE_CHECKED),
        created_by=auth.account,
        started_at=utcnow(),
    )
    session.add(batch)
    await session.flush()

    stats = await _precheck(session, batch, rows)
    batch.total_rows = stats["total"]
    batch.valid_rows = stats["valid"]
    batch.invalid_rows = stats["invalid"]
    batch.duplicated_rows = stats["duplicated"]
    batch.error_report = {"total": len(stats["errors"]), "rows": stats["errors"]}
    batch.finished_at = utcnow()
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        actor=auth,
        resource_type="device_batch",
        resource_id=batch.id,
        summary=(
            f"上传设备批次 {batch.batch_no}：共 {batch.total_rows} 行，"
            f"有效 {batch.valid_rows}，无效 {batch.invalid_rows}"
        ),
        detail={
            "fileName": batch.file_name,
            "fileSha256": sha256,
            "total": batch.total_rows,
            "valid": batch.valid_rows,
            "invalid": batch.invalid_rows,
            "duplicated": batch.duplicated_rows,
        },
        request=request,
    )
    await session.commit()
    logger.info(
        "批次 %s 预检完成：%d 行（有效 %d / 无效 %d）",
        batch.batch_no,
        batch.total_rows,
        batch.valid_rows,
        batch.invalid_rows,
    )
    return batch


async def _precheck(
    session: AsyncSession, batch: DeviceBatch, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """逐行预检并写明细。

    判定优先级：**缺 SN > 库内已存在 > 文件内重复 > 有效**。

    为什么先把「文件内重复」判为 ``SKIPPED`` 而不是 ``INVALID``？
    同一个 SN 在文件里出现两次，通常只是工厂导出时的重复行，
    第一次是好的、第二次是多余的——标成 ``SKIPPED`` 才能表达
    「不是数据错误，只是被跳过」，用户不必去改文件。
    """
    sns = [row.get("sn") for row in rows]
    existing: set[str] = set()
    present = [sn for sn in sns if sn]
    if present:
        existing = set(
            (await session.execute(select(Device.sn).where(Device.sn.in_(present))))
            .scalars()
            .all()
        )

    seen: set[str] = set()
    valid = invalid = duplicated = 0
    errors: list[dict[str, Any]] = []

    for row in rows:
        sn = row.get("sn")
        status = BatchLineStatus.VALID
        message: str | None = None

        if not sn:
            status = BatchLineStatus.INVALID
            message = "缺少 SN"
        elif sn in existing:
            # 库内已存在属于「无效行」，不计入 duplicatedRows
            # （duplicatedRows 的语义是**文件内部**重复，两者口径不同）
            status = BatchLineStatus.INVALID
            message = "SN 已存在于设备库"
        elif sn in seen:
            status = BatchLineStatus.SKIPPED
            message = "文件内重复，已跳过"
            duplicated += 1
        else:
            seen.add(sn)

        if status is BatchLineStatus.VALID:
            valid += 1
        else:
            invalid += 1
            errors.append({"rowNo": row["row_no"], "sn": sn, "reason": message})

        session.add(
            DeviceBatchLine(
                id=new_id("device_batch_line"),
                batch_id=batch.id,
                row_no=row["row_no"],
                sn=sn,
                imei=row.get("imei"),
                iccid=row.get("iccid"),
                mac=row.get("mac"),
                raw=row.get("raw"),
                status=str(status),
                error_message=message,
            )
        )

    await session.flush()
    return {
        "total": len(rows),
        "valid": valid,
        "invalid": invalid,
        "duplicated": duplicated,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


async def import_batch(
    session: AsyncSession,
    auth: AuthContext,
    *,
    batch_id: str,
    request: Request | None = None,
) -> DeviceBatch:
    """把预检通过的明细行导入为设备（幂等：``IMPORTED`` 直接返回）。

    * 只为 ``VALID`` 行建设备（``asset_status=GENERATED``，``tenant_id`` 留空
      = 平台自有库存，与批次导入「先入平台库存、再分配」的业务顺序一致）；
    * 建完后把行标为 ``IMPORTED`` 并回填 ``device_id``——因此失败重跑只补未建的行。

    Raises:
        AppException: 批次状态不允许导入（``INVALID_STATE_TRANSITION``）。
    """
    batch = await get_batch(session, batch_id)
    # 与 list_lines 保持一致：读路径统一经 assert_visible 收口（ADR-08）。
    # 当前批次端点是平台独占、不可利用，但不变式不能破——
    # 否则将来对商户端开放时这里就是一个现成的越权读。
    assert_visible(batch.tenant_id, auth, resource="批次")

    if BatchStatus(batch.status) is BatchStatus.IMPORTED:
        logger.info("批次 %s 已导入，幂等返回", batch.batch_no)
        return batch
    if BatchStatus(batch.status) not in (
        BatchStatus.PRE_CHECKED,
        BatchStatus.IMPORTING,
        BatchStatus.FAILED,
    ):
        raise invalid_state_transition(
            f"批次当前状态为 {batch.status}，不允许导入",
            current=batch.status,
            target=str(BatchStatus.IMPORTING),
        )

    batch.status = str(BatchStatus.IMPORTING)
    batch.started_at = batch.started_at or utcnow()
    await session.flush()

    lines = list(
        (
            await session.execute(
                select(DeviceBatchLine)
                .where(
                    DeviceBatchLine.batch_id == batch.id,
                    DeviceBatchLine.status == str(BatchLineStatus.VALID),
                )
                .order_by(DeviceBatchLine.row_no.asc())
            )
        )
        .scalars()
        .all()
    )

    now = utcnow()
    created = 0
    for line in lines:
        device = Device(
            id=new_id("device"),
            tenant_id=batch.tenant_id,
            order_id=batch.order_id,
            batch_id=batch.id,
            sn=str(line.sn),
            imei=line.imei,
            iccid=line.iccid,
            mac=line.mac,
            network_type=batch.network_type,
            asset_status=str(AssetStatus.GENERATED),
            generated_at=now,
            remark=f"批次 {batch.batch_no} 导入",
        )
        session.add(device)
        await session.flush()

        line.status = str(BatchLineStatus.IMPORTED)
        line.device_id = device.id
        created += 1

        await device_service.record_event(
            session,
            device,
            event_type=device_service.EVENT_IMPORTED,
            dimension="asset",
            from_status=None,
            to_status=str(AssetStatus.GENERATED),
            actor=auth,
            summary=f"批次 {batch.batch_no} 导入",
            detail={"batchNo": batch.batch_no, "rowNo": line.row_no},
            request=request,
        )

    # 必须先 flush：会话配置了 autoflush=False，行状态的改动还挂在内存里，
    # 直接做聚合查询会统计到旧值（表现为「导入了 2 行却只记 1 行」）。
    await session.flush()
    imported_rows = int(
        (
            await session.execute(
                select(func.count())
                .select_from(DeviceBatchLine)
                .where(
                    DeviceBatchLine.batch_id == batch.id,
                    DeviceBatchLine.status == str(BatchLineStatus.IMPORTED),
                )
            )
        ).scalar_one()
    )
    batch.imported_rows = imported_rows
    batch.status = str(BatchStatus.IMPORTED)
    batch.finished_at = utcnow()
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        actor=auth,
        resource_type="device_batch",
        resource_id=batch.id,
        summary=f"批次 {batch.batch_no} 导入完成：新增 {created} 台设备，累计 {imported_rows} 台",
        detail={"batchNo": batch.batch_no, "created": created, "imported": imported_rows},
        request=request,
    )
    await session.commit()
    logger.info("批次 %s 导入完成：新增 %d 台，累计 %d 台", batch.batch_no, created, imported_rows)
    return batch


# ---------------------------------------------------------------------------
# 错误报告
# ---------------------------------------------------------------------------


def build_error_csv(batch: DeviceBatch) -> str:
    """把批次的错误明细导出为 CSV 文本（含 BOM，Excel 打开不乱码）。

    用 ``\\r\\n`` 作为行结束符：RFC 4180 的推荐值，也是 Excel 最保险的选择。

    Args:
        batch: 批次对象。

    Returns:
        完整 CSV 文本（含 BOM）。
    """
    report = batch.error_report or {}
    rows = report.get("rows") or []

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(ERROR_CSV_HEADER)
    for item in rows:
        writer.writerow([item.get("rowNo"), item.get("sn") or "", item.get("reason") or ""])
    return CSV_BOM + buffer.getvalue()


__all__ = [
    "ALLOWED_SORT_FIELDS",
    "CSV_BOM",
    "ERROR_CSV_HEADER",
    "MAX_BATCH_ROWS",
    "MAX_UPLOAD_BYTES",
    "MAX_FIELD_LENGTH",
    "batch_to_response",
    "build_error_csv",
    "create_batch",
    "generate_batch_no",
    "get_batch",
    "import_batch",
    "line_to_response",
    "list_batches",
    "list_lines",
    "parse_csv",
]
