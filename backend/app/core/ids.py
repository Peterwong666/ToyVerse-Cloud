"""ID 生成工具。

项目使用**带前缀的字符串主键**（如 ``t-001``、``d-3f2a1b``），而非自增整数：

* 便于在日志、二维码、审计记录中一眼识别实体类型
* 便于跨环境迁移数据而不发生主键冲突
* 与参考实现的 Schema 保持一致的 ``VARCHAR(36)`` 主键风格

种子数据使用可读的固定 ID（``t-001``），运行时新建实体使用随机后缀。
"""

from __future__ import annotations

import secrets

#: 各实体的 ID 前缀
PREFIX = {
    # 身份与租户
    "tenant": "t",
    "role": "r",
    "user": "u",
    "org": "o",
    "position": "p",
    "factory": "fa",
    "refresh_token": "rt",
    # 目录与产品
    "cloud_provider": "cp",
    "product_template": "pt",
    "product_authorization": "pa",
    "client_product": "cprod",
    "miniapp_config": "mp",
    # 订单与工厂
    "order": "ord",
    "factory_order": "fo",
    "burn_report": "br",
    "inspection": "ins",
    # 设备与批次
    "device": "d",
    "device_credential": "dc",
    "device_batch": "db",
    "device_batch_line": "dbl",
    "device_event": "de",
    "device_binding": "bnd",
    # 分配
    "allocation_order": "ao",
    "allocation_item": "ai",
    # 终端与 AI
    "end_user": "eu",
    "recharge_plan": "rp",
    "recharge_order": "ro",
    "content_item": "ci",
    "ai_provider": "aip",
    "ai_config": "aic",
    "knowledge_base": "kb",
    "kb_file": "kbf",
    "voice_profile": "vp",
    "role_preset": "rps",
    "dialogue_session": "ds",
    "dialogue_message": "dm",
    # 运营与审计
    "ota_package": "otap",
    "ota_record": "otar",
    "audit_log": "log",
    "outbox_event": "evt",
}


def new_id(entity: str, *, length: int = 10) -> str:
    """为指定实体生成一个新的 ID。

    Args:
        entity: 实体名（见 :data:`PREFIX` 的键）。
        length: 随机部分长度。

    Raises:
        KeyError: 实体名未在前缀表中登记。

    Example:
        >>> new_id("device")       # doctest: +SKIP
        'd-3f2a1b9c4e'
    """
    prefix = PREFIX[entity]
    return f"{prefix}-{secrets.token_hex(length // 2)}"


def new_uuid() -> str:
    """生成标准 UUID4 十六进制串（36 位以内的通用主键）。"""
    import uuid

    return str(uuid.uuid4())
