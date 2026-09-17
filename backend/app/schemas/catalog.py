"""目录域请求 / 响应模型。

契约约定
--------
* Python 字段用 ``snake_case``，对外 JSON 用 ``camelCase``——
  由 ``alias_generator=to_camel`` 自动完成，无需手写别名
  （避免 ``schemas/auth.py`` 那样逐字段 ``noqa: N815``）
* 请求体同样接受 camelCase，前端无需做字段名转换

安全红线
--------
本文件中**不存在**任何用于回显密钥的字段：

* ``CloudProviderResponse`` 只有 ``accessKeyHint`` / ``secretKeyHint``（掩码）
* ``MiniAppConfigResponse`` 只有 ``appSecretHint``

即「即使业务代码写错，也无法把明文密钥序列化出去」——
这是对早期实现「SecretKey 明文下发前端」问题的架构性防范。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.models.enums import (
    CloudProviderStatus,
    CloudVendor,
    EnableStatus,
    NetworkType,
    OtaSupport,
    TenantStatus,
    TestResult,
)


class ApiModel(BaseModel):
    """目录域模型基类：自动 snake_case ⇄ camelCase。"""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


def _ensure_json_object(value: Any) -> dict[str, Any] | None:
    """把「JSON 字符串或对象」统一成 dict（前端表单可能直接传字符串）。"""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("不是合法的 JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON 必须是对象（{}）")
        return parsed
    raise ValueError("格式不正确")


# ---------------------------------------------------------------------------
# 租户
# ---------------------------------------------------------------------------


class OptionItem(ApiModel):
    """通用下拉选项。"""

    value: str
    label: str


class TenantCounts(ApiModel):
    """租户关联数据计数（供详情页展示）。"""

    authorizations: int = 0
    client_products: int = 0
    users: int = 0


class TenantCreateRequest(ApiModel):
    """创建租户。"""

    code: str = Field(min_length=2, max_length=64, description="租户编码，全局唯一")
    name: str = Field(min_length=1, max_length=128)
    status: TenantStatus = Field(default=TenantStatus.ACTIVE)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    contact_name: str | None = Field(default=None, max_length=64)
    contact_phone: str | None = Field(default=None, max_length=32, description="联系电话（兼登录账号）")
    email: str | None = Field(default=None, max_length=128)
    industry: str | None = Field(default=None, max_length=64)
    remark: str | None = Field(default=None, max_length=2000)

    @field_validator("code")
    @classmethod
    def _upper_code(cls, value: str) -> str:
        """租户编码统一大写，避免 ``demo`` 与 ``DEMO`` 被当成两个租户。"""
        cleaned = value.strip().upper()
        if not cleaned.replace("-", "").replace("_", "").isalnum():
            raise ValueError("租户编码只能包含字母、数字、连字符与下划线")
        return cleaned

    @field_validator("name", "contact_name", "industry")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class TenantUpdateRequest(ApiModel):
    """更新租户（编码不可修改——它是登录页与运维排查的稳定标识）。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    timezone: str | None = Field(default=None, max_length=64)
    contact_name: str | None = Field(default=None, max_length=64)
    contact_phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=128)
    industry: str | None = Field(default=None, max_length=64)
    remark: str | None = Field(default=None, max_length=2000)


class TenantStatusRequest(ApiModel):
    """启用 / 禁用租户。"""

    status: TenantStatus
    reason: str | None = Field(default=None, max_length=256)


class AccountCreateRequest(ApiModel):
    """为租户创建登录账号。"""

    account: str = Field(min_length=4, max_length=64, description="登录账号（建议用手机号）")
    nickname: str | None = Field(default=None, max_length=128)
    role_code: str = Field(default="MERCHANT_ADMIN", max_length=64)
    password: str | None = Field(
        default=None, max_length=128, description="留空则自动生成强随机密码并仅返回一次"
    )

    @field_validator("account")
    @classmethod
    def _strip_account(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("账号不能为空")
        return cleaned


class ResetPasswordRequest(ApiModel):
    """重置账号密码。"""

    account: str = Field(min_length=4, max_length=64)
    new_password: str | None = Field(
        default=None, max_length=128, description="留空则自动生成强随机密码并仅返回一次"
    )


class PasswordResetResult(ApiModel):
    """重置密码结果。

    ``password`` 只在**本次响应**中出现一次，且账号被置为「下次登录必须改密」。
    """

    account: str
    password: str | None = Field(
        default=None, description="自动生成的新密码（仅返回一次，请立即转交客户）"
    )
    generated: bool = Field(description="是否由服务端自动生成")
    must_change_password: bool = True


class AccountBrief(ApiModel):
    """租户下的账号摘要（不含任何密码字段）。"""

    id: str
    account: str
    nickname: str | None = None
    role_code: str
    status: str
    last_login_at: datetime | None = None
    must_change_password: bool = False


class AccountCreateResult(ApiModel):
    """新建租户账号的结果。

    ``password`` 为服务端自动生成的初始密码，**只在本次响应中出现一次**；
    若调用方自行指定了密码，则该字段为 ``null``（服务端不回显调用方提供的密码）。
    """

    account: AccountBrief
    password: str | None = Field(default=None, description="自动生成的初始密码（仅返回一次）")
    generated: bool = Field(description="密码是否由服务端自动生成")


class TenantResponse(ApiModel):
    """租户列表项。"""

    id: str
    code: str
    name: str
    status: str
    timezone: str
    contact_name: str | None = None
    contact_phone: str | None = None
    email: str | None = None
    industry: str | None = None
    remark: str | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status == TenantStatus.ACTIVE


class TenantDetailResponse(TenantResponse):
    """租户详情：附加计数与关联清单。

    修复 P-03 的验收视角——「建后客户详情可见」：
    这里直接返回该租户的授权与客户产品清单。
    """

    counts: TenantCounts = Field(default_factory=TenantCounts)
    accounts: list[AccountBrief] = Field(default_factory=list)
    authorizations: list[AuthorizationResponse] = Field(default_factory=list)
    client_products: list[ClientProductResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 云服务商
# ---------------------------------------------------------------------------


class CloudProviderCreateRequest(ApiModel):
    """创建云服务商。

    ``accessKey`` / ``secretKey`` 为**明文入参**，服务层加密后落库；
    响应中永不回显（只在 ``*Hint`` 中给前 4 位掩码）。
    """

    code: str = Field(min_length=2, max_length=64, description="厂商编码，全局唯一")
    name: str = Field(min_length=1, max_length=128)
    vendor: CloudVendor = Field(default=CloudVendor.JIXIAN)
    network_type: NetworkType = Field(default=NetworkType.FOUR_G)
    api_base: str | None = Field(default=None, max_length=256)
    access_key: str | None = Field(default=None, max_length=256, description="明文入参，加密存储")
    secret_key: str | None = Field(default=None, max_length=512, description="明文入参，加密存储")
    extra_config: dict[str, Any] | None = Field(default=None, description="厂商特有参数")
    ota_support: OtaSupport = Field(default=OtaSupport.SUPPORTED)
    remark: str | None = Field(default=None, max_length=2000)

    @field_validator("code")
    @classmethod
    def _upper_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("extra_config", mode="before")
    @classmethod
    def _parse_extra(cls, value: Any) -> dict[str, Any] | None:
        return _ensure_json_object(value)


class CloudProviderUpdateRequest(ApiModel):
    """更新云服务商。

    密钥字段**留空表示保持原值**（避免「改名字却把密钥清空」这类误操作）。
    """

    name: str | None = Field(default=None, min_length=1, max_length=128)
    api_base: str | None = Field(default=None, max_length=256)
    access_key: str | None = Field(default=None, max_length=256)
    secret_key: str | None = Field(default=None, max_length=512)
    extra_config: dict[str, Any] | None = None
    ota_support: OtaSupport | None = None
    remark: str | None = Field(default=None, max_length=2000)

    @field_validator("extra_config", mode="before")
    @classmethod
    def _parse_extra(cls, value: Any) -> dict[str, Any] | None:
        return _ensure_json_object(value)

    @model_validator(mode="after")
    def _require_some_change(self) -> CloudProviderUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("请至少提供一个待更新字段")
        return self


class CloudProviderResponse(ApiModel):
    """云服务商响应。**含掩码提示，绝不含密文与明文密钥。**"""

    id: str
    code: str
    name: str
    vendor: str
    network_type: str
    api_base: str | None = None
    access_key_hint: str | None = Field(default=None, description="AccessKey 掩码（前 4 位 + ****）")
    secret_key_hint: str | None = Field(default=None, description="SecretKey 掩码（前 4 位 + ****）")
    extra_config: dict[str, Any] | None = None
    status: str = CloudProviderStatus.NOT_CONNECTED
    ota_support: str = OtaSupport.SUPPORTED
    has_credentials: bool = False
    last_tested_at: datetime | None = None
    last_test_ok: bool | None = None
    last_test_message: str | None = None
    template_count: int = 0
    remark: str | None = None
    created_at: datetime
    updated_at: datetime


class CloudTestResponse(ApiModel):
    """连通性检测结果。

    未配置密钥或接口地址时返回 ``result=NOT_CONFIGURED`` 且 ``ok=false``，
    **绝不伪造成功**（ADR-07）。
    """

    result: TestResult
    ok: bool
    message: str
    latency_ms: int | None = None
    vendor: str
    tested_at: datetime


# ---------------------------------------------------------------------------
# 产品模板与授权
# ---------------------------------------------------------------------------


class TemplateCreateRequest(ApiModel):
    """创建产品模板。"""

    code: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    category: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=64)
    chip: str | None = Field(default=None, max_length=64)
    network_type: NetworkType = Field(default=NetworkType.WIFI)
    cloud_provider_id: str | None = Field(default=None, description="关联的云服务商")
    firmware_version: str | None = Field(default=None, max_length=64)
    reference_price: float | None = Field(default=None, ge=0, le=9_999_999, description="参考单价（元）")
    ai_features: dict[str, Any] | None = Field(default=None)
    specs: dict[str, Any] | None = Field(default=None)
    status: EnableStatus = Field(default=EnableStatus.ENABLED)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("code")
    @classmethod
    def _upper_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("ai_features", "specs", mode="before")
    @classmethod
    def _parse_json(cls, value: Any) -> dict[str, Any] | None:
        return _ensure_json_object(value)


class TemplateUpdateRequest(ApiModel):
    """更新产品模板（编码不可修改）。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    category: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=64)
    chip: str | None = Field(default=None, max_length=64)
    network_type: NetworkType | None = None
    cloud_provider_id: str | None = None
    firmware_version: str | None = Field(default=None, max_length=64)
    reference_price: float | None = Field(default=None, ge=0, le=9_999_999)
    ai_features: dict[str, Any] | None = None
    specs: dict[str, Any] | None = None
    status: EnableStatus | None = None
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("ai_features", "specs", mode="before")
    @classmethod
    def _parse_json(cls, value: Any) -> dict[str, Any] | None:
        return _ensure_json_object(value)


class TemplateResponse(ApiModel):
    """产品模板响应。"""

    id: str
    code: str
    name: str
    category: str | None = None
    model: str | None = None
    chip: str | None = None
    network_type: str
    cloud_provider_id: str | None = None
    cloud_provider_name: str | None = None
    firmware_version: str | None = None
    reference_price: float | None = None
    ai_features: dict[str, Any] | None = None
    specs: dict[str, Any] | None = None
    status: str
    description: str | None = None
    authorization_count: int = 0
    client_product_count: int = 0
    created_at: datetime
    updated_at: datetime


class AuthorizeRequest(ApiModel):
    """授权模板给租户（幂等：已授权的租户会被跳过而非报错）。"""

    tenant_ids: list[str] = Field(min_length=1, description="目标租户 ID 列表")
    max_devices: int | None = Field(default=None, ge=0, description="可创建设备上限，留空不限")
    expires_at: datetime | None = Field(default=None)
    remark: str | None = Field(default=None, max_length=2000)

    @field_validator("tenant_ids")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            cleaned = item.strip()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        if not seen:
            raise ValueError("请至少选择一个租户")
        return seen


class AuthorizeResult(ApiModel):
    """授权结果。

    ``skipped`` 为本次已存在（幂等跳过）的租户，
    ``denied`` 为不存在的租户——便于前端给出精确反馈，而不是笼统失败。
    """

    template_id: str
    created: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    denied: list[str] = Field(default_factory=list)
    total_authorized: int = 0


class AuthorizationResponse(ApiModel):
    """授权记录。"""

    id: str
    tenant_id: str
    tenant_code: str | None = None
    tenant_name: str | None = None
    template_id: str
    template_code: str | None = None
    template_name: str | None = None
    status: str
    authorized_at: datetime | None = None
    authorized_by: str | None = None
    expires_at: datetime | None = None
    max_devices: int | None = None
    client_product_count: int = 0
    remark: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# 客户产品与小程序配置
# ---------------------------------------------------------------------------


class ClientProductCreateRequest(ApiModel):
    """创建客户产品。

    ★ P-03 修复点：``tenant_id`` 与 ``template_id`` 均为**必填**，
    且服务层会校验「该租户已获得该模板授权」，否则返回
    ``PRODUCT_NOT_AUTHORIZED``。因此不可能再出现「产品未绑定客户」。
    """

    tenant_id: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    code: str = Field(min_length=2, max_length=64, description="产品编码，全局唯一")
    name: str | None = Field(default=None, max_length=128, description="留空则沿用模板名称")
    firmware_version: str | None = Field(default=None, max_length=64, description="留空则沿用模板版本")
    status: EnableStatus = Field(default=EnableStatus.ENABLED)
    remark: str | None = Field(default=None, max_length=2000)

    @field_validator("code")
    @classmethod
    def _upper_code(cls, value: str) -> str:
        return value.strip().upper()


class ClientProductUpdateRequest(ApiModel):
    """更新客户产品（租户与模板不可变更——它们决定了产品归属与授权依据）。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    firmware_version: str | None = Field(default=None, max_length=64)
    status: EnableStatus | None = None
    ai_enabled: bool | None = None
    remark: str | None = Field(default=None, max_length=2000)


class ClientProductBrief(ApiModel):
    """客户产品摘要（嵌入租户详情等场景）。"""

    id: str
    code: str
    name: str
    tenant_id: str
    template_id: str
    network_type: str
    status: str
    ai_enabled: bool = False
    created_at: datetime


class ClientProductResponse(ClientProductBrief):
    """客户产品响应。"""

    tenant_code: str | None = None
    tenant_name: str | None = None
    template_code: str | None = None
    template_name: str | None = None
    cloud_provider_id: str | None = None
    cloud_provider_name: str | None = None
    firmware_version: str | None = None
    has_miniapp_config: bool = False
    miniapp_config: MiniAppConfigResponse | None = None
    remark: str | None = None
    updated_at: datetime


class MiniAppConfigRequest(ApiModel):
    """创建 / 更新小程序配置（upsert 语义）。

    ``app_secret`` 为明文入参，留空表示保持原值；响应只回掩码。
    """

    app_name: str = Field(min_length=1, max_length=128)
    app_id: str | None = Field(default=None, max_length=64)
    app_secret: str | None = Field(default=None, max_length=512)
    original_id: str | None = Field(default=None, max_length=64)
    theme_color: str | None = Field(default=None, max_length=16)
    logo_url: str | None = Field(default=None, max_length=512)
    share_title: str | None = Field(default=None, max_length=128)
    share_desc: str | None = Field(default=None, max_length=256)
    service_phone: str | None = Field(default=None, max_length=32)
    service_qr_url: str | None = Field(default=None, max_length=512)
    version: str | None = Field(default=None, max_length=32)
    status: EnableStatus = Field(default=EnableStatus.DISABLED)
    remark: str | None = Field(default=None, max_length=2000)


class MiniAppConfigResponse(ApiModel):
    """小程序配置响应。**只有掩码，没有 AppSecret。**"""

    id: str
    client_product_id: str
    tenant_id: str
    app_name: str
    app_id: str | None = None
    app_secret_hint: str | None = Field(default=None, description="AppSecret 掩码（前 4 位 + ****）")
    has_app_secret: bool = False
    original_id: str | None = None
    theme_color: str | None = None
    logo_url: str | None = None
    share_title: str | None = None
    share_desc: str | None = None
    service_phone: str | None = None
    service_qr_url: str | None = None
    version: str | None = None
    status: str
    published_at: datetime | None = None
    remark: str | None = None
    created_at: datetime
    updated_at: datetime


#: 需要在运行时解析的前向引用（TenantDetailResponse 引用了后定义的模型）
TenantDetailResponse.model_rebuild()
ClientProductResponse.model_rebuild()
