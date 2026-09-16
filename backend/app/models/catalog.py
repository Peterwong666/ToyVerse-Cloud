"""目录域模型：云服务商 / 产品模板 / 产品授权 / 客户产品 / 小程序配置。

领域关系（自上而下逐层收窄）
----------------------------

    CloudProvider（云服务商，平台级）
        ↑ 被引用
    ProductTemplate（产品模板，平台级）
        ↑ 授权（ProductAuthorization：模板 × 租户）
    ClientProduct（客户产品，租户级）★ 商户端「我的产品」的数据来源
        ↓ 1:1
    MiniAppConfig（小程序配置，租户级）

三个关键设计
------------
1. **客户产品由模板派生，且必须绑定租户**——修正 P-03
   （原型「新建产品未绑定客户」，导致客户详情页看不到产品）。
   ``client_products.tenant_id`` 为 ``NOT NULL``，从数据库层面杜绝这类漂移。
2. **快照字段**：客户产品落库时把模板的 ``network_type`` / ``cloud_provider_id``
   **复制一份**。模板后续被修改不会追溯影响已交付的产品，
   这是「历史数据可解释」的基本要求。
3. **密钥只存密文 + 掩码提示**：``access_key_enc`` / ``secret_key_enc`` 存密文，
   ``access_key_hint`` 存写入时生成的掩码（前 4 位 + ``****``）。
   列表页只读 hint，不需要解密，把解密面收敛到「调用厂商接口」这一处。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UTCDateTime
from app.models.enums import (
    CloudProviderStatus,
    CloudVendor,
    EnableStatus,
    NetworkType,
    OtaSupport,
)


class CloudProvider(Base, TimestampMixin):
    """云服务商账号配置（平台级）。

    一个云服务商 = 一份可以「生成设备 / 激活设备」的厂商账号。
    集贤（4G）与京东云 JoyInside（Wi-Fi）是最初的两个真实厂商。
    """

    __tablename__ = "cloud_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    vendor: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CloudVendor.JIXIAN, index=True
    )
    network_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NetworkType.FOUR_G, index=True
    )

    api_base: Mapped[str | None] = mapped_column(String(256), doc="厂商接口基址")
    access_key_enc: Mapped[str | None] = mapped_column(Text, doc="AccessKey 密文")
    secret_key_enc: Mapped[str | None] = mapped_column(Text, doc="SecretKey 密文（绝不下发）")
    access_key_hint: Mapped[str | None] = mapped_column(
        String(32), doc="AccessKey 掩码提示（前 4 位 + ****），供列表展示"
    )
    secret_key_hint: Mapped[str | None] = mapped_column(String(32), doc="SecretKey 掩码提示")
    extra_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, doc="厂商特有参数（vendor_id / app_id / tenant_id 等）"
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CloudProviderStatus.NOT_CONNECTED, index=True
    )
    ota_support: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OtaSupport.SUPPORTED
    )

    last_tested_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, doc="最近一次连通性检测时间"
    )
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, doc="最近一次检测是否通过")
    last_test_message: Mapped[str | None] = mapped_column(String(512))

    remark: Mapped[str | None] = mapped_column(Text)

    # ---- 关系 ----
    templates: Mapped[list[ProductTemplate]] = relationship(
        back_populates="cloud_provider", lazy="selectin"
    )

    @property
    def has_credentials(self) -> bool:
        """是否已录入完整密钥（未录入的厂商一律安全失败，见 ADR-07）。"""
        return bool(self.access_key_enc and self.secret_key_enc)

    def __repr__(self) -> str:
        return f"<CloudProvider {self.code} vendor={self.vendor} status={self.status}>"


class ProductTemplate(Base, TimestampMixin):
    """产品模板（平台级）。

    平台把「一款可销售的智能玩具」抽象为模板（型号 / 芯片方案 / 联网方式 /
    云服务商 / 固件版本），再授权给租户，租户据此生成自己的客户产品。
    """

    __tablename__ = "product_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), index=True, doc="产品品类")

    model: Mapped[str | None] = mapped_column(String(64), doc="型号")
    chip: Mapped[str | None] = mapped_column(String(64), doc="芯片 / 模组方案，如 ESP32-S3")
    network_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NetworkType.WIFI, index=True
    )
    cloud_provider_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("cloud_providers.id", ondelete="RESTRICT"), index=True
    )
    firmware_version: Mapped[str | None] = mapped_column(String(64))

    reference_price: Mapped[float | None] = mapped_column(
        Numeric(10, 2), doc="参考单价（元）；工厂端一律不下发金额（P6）"
    )
    ai_features: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, doc="AI 能力开关快照，如 {story: true, music: true, chat: true}"
    )
    specs: Mapped[dict[str, Any] | None] = mapped_column(JSON, doc="规格参数（尺寸 / 材质 / 电池）")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.ENABLED, index=True
    )
    description: Mapped[str | None] = mapped_column(Text)

    # ---- 关系 ----
    cloud_provider: Mapped[CloudProvider | None] = relationship(back_populates="templates")
    authorizations: Mapped[list[ProductAuthorization]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ProductTemplate {self.code} {self.name}>"


class ProductAuthorization(Base, TimestampMixin):
    """产品授权：把某个模板授权给某个租户。

    授权是「租户能否基于该模板创建客户产品」的**唯一依据**
    （见 :func:`app.services.catalog_service.create_client_product`）。
    """

    __tablename__ = "product_authorizations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "template_id", name="uq_product_authorizations_tenant_template"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.ENABLED, index=True
    )
    authorized_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    authorized_by: Mapped[str | None] = mapped_column(String(64), doc="授权操作者账号")
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, doc="到期时间，为空表示长期有效")
    max_devices: Mapped[int | None] = mapped_column(
        Integer, doc="可创建设备数量上限，为空表示不限"
    )
    remark: Mapped[str | None] = mapped_column(Text)

    # ---- 关系 ----
    template: Mapped[ProductTemplate] = relationship(back_populates="authorizations")

    def __repr__(self) -> str:
        return f"<ProductAuthorization tenant={self.tenant_id} template={self.template_id}>"


class ClientProduct(Base, TimestampMixin):
    """客户产品（租户级）——商户端「我的产品」。

    ★ 修复 P-03：``tenant_id`` 为 ``NOT NULL``，创建时必须显式指定租户，
    且该租户必须已获得对应模板的授权，因此不可能再出现「产品未绑定客户」。
    """

    __tablename__ = "client_products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_templates.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    # ---- 快照字段（创建时从模板复制，模板后续变更不追溯影响） ----
    network_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    cloud_provider_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("cloud_providers.id", ondelete="RESTRICT"), index=True
    )
    firmware_version: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.ENABLED, index=True
    )
    ai_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, doc="是否已配置 AI（P9 落地）"
    )
    remark: Mapped[str | None] = mapped_column(Text)

    # ---- 关系 ----
    miniapp_config: Mapped[MiniAppConfig | None] = relationship(
        back_populates="client_product",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<ClientProduct {self.code} tenant={self.tenant_id}>"


class MiniAppConfig(Base, TimestampMixin):
    """小程序配置（与客户产品 1:1）。

    ``app_secret_enc`` 与云服务商密钥同等待遇：加密落库、响应只回掩码。
    """

    __tablename__ = "miniapp_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_product_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("client_products.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    app_name: Mapped[str] = mapped_column(String(128), nullable=False)
    app_id: Mapped[str | None] = mapped_column(String(64), doc="微信小程序 AppID")
    app_secret_enc: Mapped[str | None] = mapped_column(Text, doc="AppSecret 密文（绝不下发）")
    app_secret_hint: Mapped[str | None] = mapped_column(String(32), doc="AppSecret 掩码提示")
    original_id: Mapped[str | None] = mapped_column(String(64), doc="原始 ID（gh_ 开头）")

    theme_color: Mapped[str | None] = mapped_column(String(16), doc="主题色，如 #4F46E5")
    logo_url: Mapped[str | None] = mapped_column(String(512))
    share_title: Mapped[str | None] = mapped_column(String(128))
    share_desc: Mapped[str | None] = mapped_column(String(256))
    service_phone: Mapped[str | None] = mapped_column(String(32), doc="客服电话")
    service_qr_url: Mapped[str | None] = mapped_column(String(512), doc="客服二维码图片地址")

    version: Mapped[str | None] = mapped_column(String(32), doc="小程序版本号")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.DISABLED, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    remark: Mapped[str | None] = mapped_column(Text)

    # ---- 关系 ----
    client_product: Mapped[ClientProduct] = relationship(back_populates="miniapp_config")

    def __repr__(self) -> str:
        return f"<MiniAppConfig {self.app_name} product={self.client_product_id}>"
