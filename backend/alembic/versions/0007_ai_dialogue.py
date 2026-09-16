"""AI 与对话域：ai_providers / role_presets / knowledge_bases / kb_files /
voice_profiles / ai_configs / dialogue_sessions / dialogue_messages

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-16

八张表按依赖顺序建立（被引用的先建）：

    ai_providers                          平台级供应商清单（密钥密文 + 默认标记）
    role_presets                          角色预设（tenant_id 为空 = 平台内置）
    knowledge_bases                       知识库（租户级）
        └ kb_files                        知识库文件（上传状态与解析状态分离）
    voice_profiles                        音色档案（含训练任务状态）
    ai_configs                            客户产品的 AI 配置（供应商解析第 1 层）
    dialogue_sessions                     对话会话（租户级）
        └ dialogue_messages               对话消息（含流式耗时与安全标记）

两个需要说明的决定
------------------

1. ``dialogue_sessions.device_id`` / ``end_user_id`` **不建外键**：
   ``devices`` 与 ``end_users`` 表要到 P4 / P9 才落地，此处加外键会让
   本迁移依赖尚不存在的表（PostgreSQL 会直接拒绝建表）。它们先作为
   松散引用存在，待对应阶段落地后用新迁移补外键。
2. ``downgrade()`` 严格按依赖逆序：先 drop 索引 → 再 drop 表，
   且顺序与 ``upgrade()`` 相反，保证任意一次 ``downgrade -1`` 都能干净回滚。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base  # noqa: F401 - 自定义类型 UTCDateTime 需要此导入

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    """创建时间 / 更新时间两列（与 db.base.TimestampMixin 保持一致）。"""
    return [
        sa.Column(
            "created_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.db.base.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    """升级。"""
    # ------------------------------------------------------------
    # 1. AI 供应商（平台级）
    # ------------------------------------------------------------
    op.create_table(
        "ai_providers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("vendor", sa.String(length=32), nullable=True),
        sa.Column("api_base", sa.String(length=256), nullable=True),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("secret_key_enc", sa.Text(), nullable=True),
        sa.Column("api_key_hint", sa.String(length=32), nullable=True),
        sa.Column("secret_key_hint", sa.String(length=32), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("last_health_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("last_health_status", sa.String(length=32), nullable=True),
        sa.Column("last_health_message", sa.String(length=512), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_providers")),
    )
    with op.batch_alter_table("ai_providers", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ai_providers_code"), ["code"], unique=True)
        batch_op.create_index(batch_op.f("ix_ai_providers_is_default"), ["is_default"], unique=False)
        batch_op.create_index(batch_op.f("ix_ai_providers_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_ai_providers_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_ai_providers_vendor"), ["vendor"], unique=False)

    # ------------------------------------------------------------
    # 2. 角色预设（tenant_id 为空 = 平台内置）
    # ------------------------------------------------------------
    op.create_table(
        "role_presets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("persona", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("greeting", sa.String(length=512), nullable=True),
        sa.Column("age_group", sa.String(length=32), nullable=True),
        sa.Column("tone", sa.String(length=32), nullable=True),
        sa.Column("keywords", sa.JSON(), nullable=True),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_presets")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_role_presets_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "code", name="uq_role_presets_tenant_code"),
    )
    with op.batch_alter_table("role_presets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_role_presets_code"), ["code"], unique=False)
        batch_op.create_index(batch_op.f("ix_role_presets_is_builtin"), ["is_builtin"], unique=False)
        batch_op.create_index(batch_op.f("ix_role_presets_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_role_presets_tenant_id"), ["tenant_id"], unique=False)

    # ------------------------------------------------------------
    # 3. 知识库（租户级）
    # ------------------------------------------------------------
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("doc_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.String(length=64), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_bases")),
        sa.ForeignKeyConstraint(
            ["client_product_id"],
            ["client_products.id"],
            name=op.f("fk_knowledge_bases_client_product_id_client_products"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_knowledge_bases_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("knowledge_bases", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_knowledge_bases_client_product_id"), ["client_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_knowledge_bases_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_knowledge_bases_tenant_id"), ["tenant_id"], unique=False)

    # ------------------------------------------------------------
    # 4. 知识库文件
    # ------------------------------------------------------------
    op.create_table(
        "kb_files",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=256), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("storage_path", sa.String(length=512), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_files")),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_kb_files_knowledge_base_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_kb_files_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("kb_files", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_kb_files_knowledge_base_id"), ["knowledge_base_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_kb_files_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_kb_files_tenant_id"), ["tenant_id"], unique=False)

    # ------------------------------------------------------------
    # 5. 音色档案
    # ------------------------------------------------------------
    op.create_table(
        "voice_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider_code", sa.String(length=64), nullable=True),
        sa.Column("external_voice_id", sa.String(length=128), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("sample_url", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("train_status", sa.String(length=32), nullable=True),
        sa.Column("train_task_id", sa.String(length=128), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_profiles")),
        sa.ForeignKeyConstraint(
            ["client_product_id"],
            ["client_products.id"],
            name=op.f("fk_voice_profiles_client_product_id_client_products"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_voice_profiles_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("voice_profiles", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_voice_profiles_client_product_id"), ["client_product_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_voice_profiles_provider_code"), ["provider_code"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_voice_profiles_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_voice_profiles_tenant_id"), ["tenant_id"], unique=False)

    # ------------------------------------------------------------
    # 6. 客户产品的 AI 配置（供应商解析第 1 层的落点）
    # ------------------------------------------------------------
    op.create_table(
        "ai_configs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=False),
        sa.Column("provider_code", sa.String(length=64), nullable=True),
        sa.Column("role_preset_code", sa.String(length=64), nullable=True),
        sa.Column("knowledge_base_id", sa.String(length=36), nullable=True),
        sa.Column("voice_profile_id", sa.String(length=36), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("greeting", sa.String(length=512), nullable=True),
        sa.Column("temperature", sa.Integer(), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=True),
        sa.Column("safety_enabled", sa.Boolean(), nullable=False),
        sa.Column("safety_sensitive_words", sa.Boolean(), nullable=False),
        sa.Column("safety_llm_review", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_configs")),
        sa.ForeignKeyConstraint(
            ["client_product_id"],
            ["client_products.id"],
            name=op.f("fk_ai_configs_client_product_id_client_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_ai_configs_knowledge_base_id_knowledge_bases"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_ai_configs_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["voice_profile_id"],
            ["voice_profiles.id"],
            name=op.f("fk_ai_configs_voice_profile_id_voice_profiles"),
            ondelete="SET NULL",
        ),
    )
    with op.batch_alter_table("ai_configs", schema=None) as batch_op:
        # unique + index 组合 → 唯一索引：一个客户产品只有一份 AI 配置
        batch_op.create_index(
            batch_op.f("ix_ai_configs_client_product_id"), ["client_product_id"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_ai_configs_provider_code"), ["provider_code"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_ai_configs_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_ai_configs_tenant_id"), ["tenant_id"], unique=False)

    # ------------------------------------------------------------
    # 7. 对话会话
    # ------------------------------------------------------------
    op.create_table(
        "dialogue_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("client_product_id", sa.String(length=36), nullable=True),
        # 松散引用：devices 表 P4 落地、end_users 表 P9 落地，届时不改主键即可补外键
        sa.Column("device_id", sa.String(length=36), nullable=True),
        sa.Column("end_user_id", sa.String(length=36), nullable=True),
        sa.Column("provider_code", sa.String(length=64), nullable=True),
        sa.Column("role_preset_code", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("total_latency_ms", sa.Integer(), nullable=False),
        sa.Column("started_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("ended_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("close_reason", sa.String(length=128), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dialogue_sessions")),
        sa.ForeignKeyConstraint(
            ["client_product_id"],
            ["client_products.id"],
            name=op.f("fk_dialogue_sessions_client_product_id_client_products"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_dialogue_sessions_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
    )
    with op.batch_alter_table("dialogue_sessions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_dialogue_sessions_client_product_id"), ["client_product_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_dialogue_sessions_device_id"), ["device_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_dialogue_sessions_end_user_id"), ["end_user_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_dialogue_sessions_provider_code"), ["provider_code"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_dialogue_sessions_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_dialogue_sessions_tenant_id"), ["tenant_id"], unique=False
        )

    # ------------------------------------------------------------
    # 8. 对话消息
    # ------------------------------------------------------------
    op.create_table(
        "dialogue_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content_type", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("audio_url", sa.String(length=512), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("provider_code", sa.String(length=64), nullable=True),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("safety_flag", sa.String(length=32), nullable=True),
        sa.Column("safety_detail", sa.JSON(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dialogue_messages")),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["dialogue_sessions.id"],
            name=op.f("fk_dialogue_messages_session_id_dialogue_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_dialogue_messages_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("session_id", "seq", name="uq_dialogue_messages_session_seq"),
    )
    with op.batch_alter_table("dialogue_messages", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_dialogue_messages_role"), ["role"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_dialogue_messages_safety_flag"), ["safety_flag"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_dialogue_messages_session_id"), ["session_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_dialogue_messages_tenant_id"), ["tenant_id"], unique=False
        )


def downgrade() -> None:
    """回滚（严格按依赖逆序：先 drop 索引 → 再 drop 表）。"""
    with op.batch_alter_table("dialogue_messages", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_dialogue_messages_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_dialogue_messages_session_id"))
        batch_op.drop_index(batch_op.f("ix_dialogue_messages_safety_flag"))
        batch_op.drop_index(batch_op.f("ix_dialogue_messages_role"))
    op.drop_table("dialogue_messages")

    with op.batch_alter_table("dialogue_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_dialogue_sessions_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_dialogue_sessions_status"))
        batch_op.drop_index(batch_op.f("ix_dialogue_sessions_provider_code"))
        batch_op.drop_index(batch_op.f("ix_dialogue_sessions_end_user_id"))
        batch_op.drop_index(batch_op.f("ix_dialogue_sessions_device_id"))
        batch_op.drop_index(batch_op.f("ix_dialogue_sessions_client_product_id"))
    op.drop_table("dialogue_sessions")

    with op.batch_alter_table("ai_configs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ai_configs_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_ai_configs_status"))
        batch_op.drop_index(batch_op.f("ix_ai_configs_provider_code"))
        batch_op.drop_index(batch_op.f("ix_ai_configs_client_product_id"))
    op.drop_table("ai_configs")

    with op.batch_alter_table("voice_profiles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_voice_profiles_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_voice_profiles_status"))
        batch_op.drop_index(batch_op.f("ix_voice_profiles_provider_code"))
        batch_op.drop_index(batch_op.f("ix_voice_profiles_client_product_id"))
    op.drop_table("voice_profiles")

    with op.batch_alter_table("kb_files", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_kb_files_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_kb_files_status"))
        batch_op.drop_index(batch_op.f("ix_kb_files_knowledge_base_id"))
    op.drop_table("kb_files")

    with op.batch_alter_table("knowledge_bases", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_knowledge_bases_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_knowledge_bases_status"))
        batch_op.drop_index(batch_op.f("ix_knowledge_bases_client_product_id"))
    op.drop_table("knowledge_bases")

    with op.batch_alter_table("role_presets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_role_presets_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_role_presets_status"))
        batch_op.drop_index(batch_op.f("ix_role_presets_is_builtin"))
        batch_op.drop_index(batch_op.f("ix_role_presets_code"))
    op.drop_table("role_presets")

    with op.batch_alter_table("ai_providers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ai_providers_vendor"))
        batch_op.drop_index(batch_op.f("ix_ai_providers_status"))
        batch_op.drop_index(batch_op.f("ix_ai_providers_kind"))
        batch_op.drop_index(batch_op.f("ix_ai_providers_is_default"))
        batch_op.drop_index(batch_op.f("ix_ai_providers_code"))
    op.drop_table("ai_providers")
