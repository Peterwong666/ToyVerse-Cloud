"""AI 与对话域模型（P7）。

八张表的职责分层
----------------

    ai_providers       平台级：可用供应商清单（含密钥密文与「默认供应商」标记）
    role_presets       角色预设（平台内置 tenant_id=NULL，租户可自定义）
    knowledge_bases    知识库（租户级，可挂到某个客户产品）
        ↓ 1:N
    kb_files           知识库文件（解析状态独立于上传状态）
    voice_profiles     音色档案（对应火山的声音复刻训练任务）
    ai_configs         客户产品的 AI 配置（★ 解析顺序第 1 层的落点）
    dialogue_sessions  对话会话（租户级）
        ↓ 1:N
    dialogue_messages  对话消息（含流式耗时与安全过滤命中标记）

三条设计约束
------------
1. **密钥只存密文**：``api_key_enc`` 与 ``app.models.catalog`` 同等待遇——
   只存密文 + 掩码提示，任何响应都不回显明文（见 :mod:`app.ai.registry`）。
2. **租户级表一律带 ``tenant_id``**：``ai_configs`` / ``dialogue_sessions`` /
   ``knowledge_bases`` / ``kb_files`` / ``voice_profiles`` 都显式携带，
   便于 ADR-08 的租户过滤在 repository 层一刀切完成。
3. **刻意不给 ``device_id`` / ``end_user_id`` 建外键**：``devices`` 与
   ``end_users`` 表要到 P4 / P9 才落地，P7 期间若加外键会让迁移依赖一张
   尚不存在的表（PostgreSQL 会直接拒绝建表，SQLite 只是延迟到写入才报错）。
   因此这两个字段先作为**松散引用**保留，待对应阶段落地后补外键。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UTCDateTime
from app.models.enums import (
    AiProviderKind,
    AiProviderStatus,
    DialogueSessionStatus,
    EnableStatus,
    KbFileStatus,
    MessageContentType,
    MessageRole,
)


class AiProvider(Base, TimestampMixin):
    """AI 供应商（平台级）。

    一条记录 = 一份「平台已经录入密钥、可用于对话/设备能力」的厂商账号。
    ``code`` 与 :mod:`app.ai.registry` 中的注册键一一对应，注册表启动时
    用 :func:`app.ai.registry.build_default_registry` 从配置构造实例。
    """

    __tablename__ = "ai_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AiProviderKind.MODEL, index=True
    )
    vendor: Mapped[str | None] = mapped_column(String(32), index=True, doc="对应 CloudVendor")

    api_base: Mapped[str | None] = mapped_column(String(256))
    api_key_enc: Mapped[str | None] = mapped_column(Text, doc="AccessKey / APIKey 密文")
    secret_key_enc: Mapped[str | None] = mapped_column(Text, doc="SecretKey 密文（绝不下发）")
    api_key_hint: Mapped[str | None] = mapped_column(String(32), doc="掩码提示（前 4 位 + ****）")
    secret_key_hint: Mapped[str | None] = mapped_column(String(32))
    config: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, doc="厂商特有参数（app_id / cluster_id / model 等）"
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AiProviderStatus.ACTIVE, index=True
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True, doc="是否为平台默认供应商"
    )

    last_health_at: Mapped[datetime | None] = mapped_column(UTCDateTime, doc="最近一次健康探测时间")
    last_health_status: Mapped[str | None] = mapped_column(String(32))
    last_health_message: Mapped[str | None] = mapped_column(String(512))

    description: Mapped[str | None] = mapped_column(Text)
    remark: Mapped[str | None] = mapped_column(Text)

    @property
    def has_credentials(self) -> bool:
        """是否已录入密钥。未录入即视为「未接入」，调用必须安全失败（ADR-07）。"""
        return bool(self.api_key_enc and self.secret_key_enc)

    def __repr__(self) -> str:
        return f"<AiProvider {self.code} kind={self.kind} default={self.is_default}>"


class RolePreset(Base, TimestampMixin):
    """角色预设（人设）。

    ``tenant_id`` 可为空：为空的记录是**平台内置角色**（所有租户可见、不可删），
    非空则是租户自定义角色。这样既有开箱即用的人设库，也允许租户微调。
    """

    __tablename__ = "role_presets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_role_presets_tenant_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, doc="为空表示平台内置"
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    persona: Mapped[str | None] = mapped_column(Text, doc="人设描述（供 LLM 系统提示）")
    system_prompt: Mapped[str | None] = mapped_column(Text)
    greeting: Mapped[str | None] = mapped_column(String(512), doc="开场白")
    age_group: Mapped[str | None] = mapped_column(String(32), doc="适龄段，如 3-6 岁")
    tone: Mapped[str | None] = mapped_column(String(32), doc="语气风格")
    keywords: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, doc="mock 引擎用于改写回复的关键词表"
    )

    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.ENABLED, index=True
    )
    remark: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<RolePreset {self.code} builtin={self.is_builtin}>"


class KnowledgeBase(Base, TimestampMixin):
    """知识库（租户级）。"""

    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="SET NULL"), index=True
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.ENABLED, index=True
    )

    doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(64))
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    remark: Mapped[str | None] = mapped_column(Text)

    files: Mapped[list[KbFile]] = relationship(
        back_populates="knowledge_base", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<KnowledgeBase {self.name} tenant={self.tenant_id}>"


class KbFile(Base, TimestampMixin):
    """知识库文件。

    ``status`` 使用 :class:`KbFileStatus`：上传成功（``PENDING``）与解析成功
    （``PARSED``）是两个独立事实——只有后者才能被检索命中。
    """

    __tablename__ = "kb_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    storage_path: Mapped[str | None] = mapped_column(String(512), doc="对象存储路径（走 STORAGE_BACKEND）")
    checksum: Mapped[str | None] = mapped_column(String(64), doc="内容摘要，用于去重")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=KbFileStatus.PENDING, index=True
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(String(512))

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="files")

    def __repr__(self) -> str:
        return f"<KbFile {self.filename} status={self.status}>"


class VoiceProfile(Base, TimestampMixin):
    """音色档案。

    ``external_voice_id`` 是厂商侧音色标识：火山对应 ``TrainTTSVoiceType``
    产出的 ``voice_type``；``train_status`` 对应 ``BatchListVoiceTrainStatus``
    的批量任务状态。把「训练中」显式建模，是为了让运营看板能显示进度而不是
    只能看到「配好了 / 没配好」。
    """

    __tablename__ = "voice_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="SET NULL"), index=True
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_code: Mapped[str | None] = mapped_column(String(64), index=True)
    external_voice_id: Mapped[str | None] = mapped_column(String(128), doc="厂商侧音色 ID / voice_type")
    language: Mapped[str | None] = mapped_column(String(16), default="zh-CN")
    sample_url: Mapped[str | None] = mapped_column(String(512), doc="训练样本音频地址")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.DISABLED, index=True
    )
    train_status: Mapped[str | None] = mapped_column(String(32))
    train_task_id: Mapped[str | None] = mapped_column(String(128), doc="厂商侧训练任务 ID")
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    remark: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<VoiceProfile {self.name} provider={self.provider_code}>"


class AiConfig(Base, TimestampMixin):
    """客户产品的 AI 配置（租户级）。

    ★ 本表是**供应商解析顺序的第 1 层**：``provider_code`` 一旦有值，
    优先于模板厂商与平台默认供应商（见 :func:`app.ai.registry.resolve_provider`）。
    把「覆盖默认」的能力放在产品维度而不是租户维度，是因为同一个租户可能
    同时售卖接入不同大模型的多款玩具。
    """

    __tablename__ = "ai_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("client_products.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        doc="一个客户产品只有一份 AI 配置（1:1）",
    )

    provider_code: Mapped[str | None] = mapped_column(
        String(64), index=True, doc="指定供应商；为空则按解析顺序继续下探"
    )
    role_preset_code: Mapped[str | None] = mapped_column(String(64))
    knowledge_base_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="SET NULL")
    )
    voice_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("voice_profiles.id", ondelete="SET NULL")
    )

    system_prompt: Mapped[str | None] = mapped_column(Text)
    greeting: Mapped[str | None] = mapped_column(String(512))
    temperature: Mapped[int | None] = mapped_column(Integer, doc="采样温度（放大 100 倍存整数避免浮点漂移）")
    max_tokens: Mapped[int | None] = mapped_column(Integer)

    #: 内容安全三开关（P8 在 ``assistant.delta`` 前生效，命中写 safety_flag）
    safety_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    safety_sensitive_words: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    safety_llm_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EnableStatus.DISABLED, index=True
    )
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    remark: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<AiConfig product={self.client_product_id} provider={self.provider_code}>"


class DialogueSession(Base, TimestampMixin):
    """对话会话（租户级）。

    ``device_id`` / ``end_user_id`` 刻意不建外键：对应表尚未落地（见模块 docstring）。
    """

    __tablename__ = "dialogue_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("client_products.id", ondelete="SET NULL"), index=True
    )
    device_id: Mapped[str | None] = mapped_column(String(36), index=True, doc="松散引用（devices 表 P4 落地）")
    end_user_id: Mapped[str | None] = mapped_column(String(36), index=True, doc="松散引用（end_users 表 P9 落地）")

    provider_code: Mapped[str | None] = mapped_column(String(64), index=True)
    role_preset_code: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DialogueSessionStatus.ACTIVE, index=True
    )

    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, doc="累计端到端耗时，供 P9 看板算均值"
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    close_reason: Mapped[str | None] = mapped_column(String(128))

    messages: Mapped[list[DialogueMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<DialogueSession {self.id} status={self.status} provider={self.provider_code}>"


class DialogueMessage(Base, TimestampMixin):
    """对话消息。

    为流式对话与 P8/P9 预留的三个字段：

    * ``latency_ms`` —— 流式回复的端到端耗时，验收指标「首字延迟 / 端到端延迟」的数据来源
    * ``content_type`` —— 区分文本与音频（语音玩具的用户输入天然是音频）
    * ``safety_flag`` —— 内容安全过滤命中标记；为空表示未命中
    """

    __tablename__ = "dialogue_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_dialogue_messages_session_seq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dialogue_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    seq: Mapped[int] = mapped_column(Integer, nullable=False, doc="会话内序号，从 1 开始")
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MessageRole.USER, index=True
    )
    content_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MessageContentType.TEXT
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    audio_url: Mapped[str | None] = mapped_column(String(512))

    latency_ms: Mapped[int | None] = mapped_column(Integer)
    provider_code: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, doc="流式分块数，用于验证「真的分多块返回」"
    )

    safety_flag: Mapped[str | None] = mapped_column(
        String(32), index=True, doc="安全过滤命中标记；为空表示未命中"
    )
    safety_detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    #: 本段回复所依据的内容库条目（P9 新增）。
    #:
    #: 由供应商在流式分块里**显式声明**内容标题，服务层据此回填本列
    #: （见 :class:`app.ai.base.ChatChunk` 的说明）。运营看板的「内容热度排行」
    #: 就建立在这一列的真实 GROUP BY 上——而不是靠「回复文本里出现了《某某》」
    #: 这类文本嗅探，后者会被用户自己说出的书名与角色前缀污染。
    content_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("content_items.id", ondelete="SET NULL"), index=True
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, doc="厂商原始响应（排查用，脱敏后存）")

    session: Mapped[DialogueSession] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<DialogueMessage seq={self.seq} role={self.role}>"
