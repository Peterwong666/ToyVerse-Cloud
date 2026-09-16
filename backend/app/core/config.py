"""应用配置。

所有配置项通过环境变量或 `.env` 文件注入，代码中不含任何硬编码默认口令。
启动时会执行安全校验（见 `validate_security`），不通过则拒绝启动。
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# 路径解析
#
# ★ 为什么这里不能只靠「`__file__` 上溯几级」推算
# ------------------------------------------------
# 本地布局是 `<repo>/backend/app/core/config.py`（上溯三级 = 仓库根），
# 而**容器里的布局是 `/app/app/core/config.py`——没有 `backend/` 这一层**
# （镜像把 `backend/app` 直接拷成 `/app/app`）。同样上溯三级会数到 `/`，
# 于是 `FRONTEND_ROOT` 变成 `/frontend`：静态资源挂载被静默跳过，
# **整个 UI 全部 404，而 API 一切正常**（实测踩到：启动日志里只有一条
# 「前端目录不存在」的 WARNING，直到跑冒烟才发现）。
#
# 因此改为「环境变量 → 本地布局的推算 → 向上找标记文件」三级兜底：
# 显式配置优先，其次兼容既有本地布局，最后靠标记文件（`frontend/index.html`
# 或 `backend/app`）把两种布局都认出来。
# ---------------------------------------------------------------------------


def _guess_repo_root(start: Path) -> Path:
    """从 ``start`` 向上找仓库根。

    判据：既含 ``backend/app``（源码布局）或含 ``frontend/index.html``（资产布局）。
    两级都找不到时回退到「按本地布局上溯三级」，保证行为不比原来更差。
    """
    for candidate in (start, *start.parents):
        if (candidate / "backend" / "app").is_dir():
            return candidate
    for candidate in (start, *start.parents):
        if (candidate / "frontend" / "index.html").is_file():
            return candidate
    return start.parents[2] if len(start.parents) > 2 else start


#: 后端目录（`backend/` 或容器里的 `/app`）
BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]

#: 仓库根目录（本地为仓库根；容器里为 `/app`，即应用工作目录）
REPO_ROOT: Path = _guess_repo_root(Path(__file__).resolve())

#: 数据目录（SQLite 文件与本地对象存储都落在这里）。
#:
#: ★ 必须可配置：容器里 `/app` 归 root 所有、进程以非 root 运行，
#: 默认的 `<repo>/data`（= `/app/data`）**建不出来**（PermissionError）。
#: 而「用非 root 跑」是安全底线，不能为了省事改成 root。
#: 因此容器部署把 `DATA_DIR` 指向可写卷（compose 里设为 `/data`，
#: 与 `DATABASE_URL` / `STORAGE_LOCAL_ROOT` 保持同一处）。
DATA_ROOT: Path = Path(os.environ.get("DATA_DIR", "").strip() or (REPO_ROOT / "data"))

#: 存储根目录（``STORAGE_LOCAL_ROOT`` 的相对路径以 DATA_ROOT 为基准）
DATA_ROOT_EXPLICIT = bool(os.environ.get("DATA_DIR", "").strip())

#: 前端静态资源目录。
#:
#: 环境变量 ``FRONTEND_DIR`` 可覆盖（容器与自定义部署建议显式指定），
#: 未设置时在候选目录里取第一个真实存在的：
#: ``<repo>/frontend``（本地）与 ``/app/frontend``（镜像布局）。
def _resolve_frontend_root() -> Path:
    """定位前端静态资源目录（环境变量 → 候选目录 → 兜底默认值）。"""
    override = os.environ.get("FRONTEND_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    candidates = (REPO_ROOT / "frontend", BACKEND_ROOT.parent / "frontend")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    # 都不存在时返回首选路径：挂载逻辑会打一条明确的 WARNING，便于定位
    return candidates[0]


FRONTEND_ROOT: Path = _resolve_frontend_root()


def _env_files() -> tuple[Path, ...]:
    """按优先级返回候选 `.env` 路径（靠后的覆盖靠前的）。"""
    return (REPO_ROOT / ".env", BACKEND_ROOT / ".env")


# ---------------------------------------------------------------------------
# 安全校验失败
# ---------------------------------------------------------------------------


class InsecureConfigurationError(RuntimeError):
    """配置不满足安全要求，拒绝启动。"""


# ---------------------------------------------------------------------------
# 主配置
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """应用配置。"""

    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------- 应用基础 ----------------
    APP_NAME: str = "ToyVerse Cloud"
    APP_ENV: Literal["development", "testing", "staging", "production"] = "development"
    DEBUG: bool = False

    HOST: str = "0.0.0.0"
    PORT: int = 8000

    CORS_ALLOWED_ORIGINS: str = "http://localhost:8000,http://127.0.0.1:8000"

    # ---------------- 数据库 ----------------
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/toyverse.db"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_POOL_MAX_OVERFLOW: int = 20
    DATABASE_POOL_RECYCLE: int = 1800
    DATABASE_ECHO: bool = False

    # ---------------- 鉴权 ----------------
    JWT_SECRET_KEY: str = "dev-only-insecure-secret-change-me-at-least-32-bytes"
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "toyverse-cloud"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    BCRYPT_ROUNDS: int = Field(default=12, ge=4, le=16)
    MIN_PASSWORD_LENGTH: int = Field(default=8, ge=6, le=128)
    FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN: bool = True

    # ---- 外部密钥的落库加密（见 app/core/crypto.py） ----
    #: 云服务商 SecretKey / 小程序 AppSecret 的对称加密密钥。
    #: 留空时由 ``JWT_SECRET_KEY`` 派生（历史 .env 无需改动即可启动）；
    #: 生产环境应显式配置独立密钥。
    SECRET_ENCRYPTION_KEY: str = ""

    MAX_LOGIN_FAILURES: int = Field(default=5, ge=1)
    LOCKOUT_MINUTES: int = Field(default=15, ge=1)

    # ---------------- 初始管理员账号 ----------------
    #: 平台超级管理员：``admin``（不使用手机号，也与任何第三方参考站账号无关）
    PLATFORM_ADMIN_ACCOUNT: str = "admin"
    PLATFORM_ADMIN_PASSWORD: str = ""
    PLATFORM_ADMIN_NICKNAME: str = "平台管理员"

    #: 平台运营：可处理产品 / 订单 / 设备等日常业务，但不能改
    #: 「客户档案 / 云服务商 / 产品模板」这类平台级配置（权限见 core/permissions.py）
    PLATFORM_OPERATOR_ACCOUNT: str = ""
    PLATFORM_OPERATOR_PASSWORD: str = ""
    PLATFORM_OPERATOR_NICKNAME: str = "平台运营"

    MERCHANT_ADMIN_ACCOUNT: str = "15555555555"
    MERCHANT_ADMIN_PASSWORD: str = ""
    MERCHANT_ADMIN_NICKNAME: str = "商户管理员"

    FACTORY_ADMIN_ACCOUNT: str = "13600000000"
    FACTORY_ADMIN_PASSWORD: str = ""
    FACTORY_ADMIN_NICKNAME: str = "工厂管理员"

    # ---------------- 演示数据 ----------------
    #: 是否写入演示数据（租户 / 模板 / 客户产品 / 订单 / 设备 / AI 配置 …）
    SEED_DEMO_DATA: bool = True
    #: 是否**在应用启动时**写入演示数据。
    #:
    #: ★ 多 worker 部署必须设为 false。应用的 lifespan 会**每个 worker 各执行一次**，
    #: 于是 `--workers 4` 会有 4 个进程同时往同一个 SQLite 里灌同一批种子：
    #: 通常只有一个能成功，其余报错（实测：容器启动日志里 1 次成功 + 3 次
    #: 「写入演示数据失败」）。虽然看起来「数据最后还是有了」，但那是**偶然**——
    #: 赢得竞争的 worker 可能只写完一半。
    #:
    #: 正确做法：容器入口脚本在 fork 之前执行一次
    #: `python -m app.db.seed`（见 `deploy/Dockerfile.backend` 的 CMD），
    #: 并把本开关设为 false。单进程开发（`make dev`）保持 true 即可。
    SEED_AT_STARTUP: bool = True
    DEMO_QR_SALT: str = "toyverse-demo-salt"

    # ---------------- 二维码 ----------------
    QR_SIGN_SECRET: str = "dev-only-qr-sign-secret-change-me"
    QR_CONFIRM_TOKEN_TTL_SECONDS: int = Field(default=300, ge=30)

    # ---------------- 设备在线判定 ----------------
    HEARTBEAT_INTERVAL_SECONDS: int = Field(default=30, ge=5)
    ONLINE_WINDOW_SECONDS: int = Field(default=180, ge=30)

    # ---------------- AI ----------------
    AI_DEFAULT_PROVIDER: str = "mock"
    MOCK_ASR_MODE: Literal["echo", "fixed"] = "echo"
    MOCK_TTS_MODE: Literal["text", "wav"] = "text"
    MOCK_LATENCY_MS: int = Field(default=120, ge=0, le=10_000)
    MOCK_RANDOM_SEED: int = 20260916

    # 集贤系统（4G）
    JIXIAN_API_BASE: str = ""
    JIXIAN_ACCESS_KEY: str = ""
    JIXIAN_SECRET_KEY: str = ""
    JIXIAN_VENDOR_ID: str = ""
    JIXIAN_APP_ID: str = ""
    JIXIAN_TIMEOUT_SECONDS: int = 10

    # 京东云 JoyInside（Wi-Fi）
    JOYINSIDE_API_BASE: str = ""
    JOYINSIDE_ACCESS_KEY: str = ""
    JOYINSIDE_SECRET_KEY: str = ""
    JOYINSIDE_TENANT_ID: str = ""
    JOYINSIDE_APP_ID: str = ""
    JOYINSIDE_TIMEOUT_SECONDS: int = 10

    # 火山引擎（豆包）
    VOLCANO_API_BASE: str = ""
    VOLCANO_ACCESS_KEY: str = ""
    VOLCANO_SECRET_KEY: str = ""
    VOLCANO_APP_ID: str = ""
    VOLCANO_MODEL_ENDPOINT: str = ""
    VOLCANO_TIMEOUT_SECONDS: int = 30

    # 百度智能云
    BAIDU_API_BASE: str = ""
    BAIDU_API_KEY: str = ""
    BAIDU_SECRET_KEY: str = ""
    BAIDU_APP_ID: str = ""
    BAIDU_TIMEOUT_SECONDS: int = 30

    # ---------------- 对象存储 ----------------
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    STORAGE_LOCAL_ROOT: str = "./data/storage"
    STORAGE_PRESIGNED_TTL_SECONDS: int = 3600

    S3_ENDPOINT: str = ""
    S3_REGION: str = "us-east-1"
    S3_BUCKET: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""

    # ---------------- 知识库文件与固件包（P9） ----------------
    #: 单个知识库文件的上限（字节）。默认 10MB：知识库是给检索用的文本素材，
    #: 更大的文件通常是误传（音视频应走内容库而不是知识库）。
    KB_FILE_MAX_BYTES: int = Field(default=10 * 1024 * 1024, ge=1024)
    #: 允许的知识库文件扩展名（小写，不含点）。
    #:
    #: 用**白名单**而不是黑名单：解析器只认识这几种格式，放进来别的类型
    #: 只会得到一个「上传成功但解析失败」的文件（:class:`KbFileStatus` 里
    #: `PENDING` 与 `PARSED` 是分开的两个事实，就是为了暴露这种沉默故障）。
    KB_ALLOWED_EXTENSIONS: str = "txt,md,csv,json,pdf,docx"
    #: 单个固件包的上限（字节）。默认 128MB。
    OTA_PACKAGE_MAX_BYTES: int = Field(default=128 * 1024 * 1024, ge=1024)
    #: 允许的固件包扩展名
    OTA_ALLOWED_EXTENSIONS: str = "bin,img,hex,zip,gz"

    # ---------------- 终端用户小程序（P8） ----------------
    #: 短信通道。
    #:
    #: * ``mock`` —— 不真实发短信，**验证码在响应里回显**（响应带 ``mock: true``）。
    #:   这是作品集项目「无真实厂商密钥也能跑通全流程」的取舍，与 P7 的离线
    #:   AI 模拟引擎同一思路：能力可演示，但**必须显式标注是模拟的**。
    #: * ``none`` —— 发送验证码接口直接 503，**安全失败**（ADR-07 口径）。
    #:
    #: 生产环境禁止 ``mock``（见 ``validate_security``）：把验证码回显给调用方
    #: 等于短信验证码形同虚设。
    MINIAPP_SMS_PROVIDER: Literal["mock", "none"] = "mock"
    #: 支付通道。``mock`` 时支付接口把订单直接置为已支付并标注 ``mock: true``；
    #: ``none`` 时 503。生产环境禁止 ``mock``。
    PAYMENT_PROVIDER: Literal["mock", "none"] = "mock"
    #: 短信验证码有效期（秒）
    MINIAPP_LOGIN_CODE_TTL_SECONDS: int = Field(default=300, ge=30)
    #: 同一手机号连续输错验证码的上限（超过即作废该验证码，需重新获取）
    MINIAPP_LOGIN_MAX_ATTEMPTS: int = Field(default=5, ge=1)
    #: 终端用户令牌有效期（分钟）。默认 30 天：家长不会为了给玩具配网而反复登录。
    END_USER_TOKEN_EXPIRE_MINUTES: int = Field(default=43_200, ge=5)
    #: 单次对话会话的消息上限（防止一次会话把 messages 表写爆）
    DIALOGUE_SESSION_MESSAGE_LIMIT: int = Field(default=200, ge=10)
    #: 内容安全三开关的默认值（商户端可在 P9 按产品覆盖）
    CONTENT_SAFETY_TEXT: bool = True
    CONTENT_SAFETY_AUDIO: bool = True
    CONTENT_SAFETY_VISUAL: bool = False
    S3_PATH_STYLE: bool = True

    # ---------------- 可观测性 ----------------
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False
    METRICS_ENABLED: bool = True

    # ================= 派生属性 =================

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def cors_origins(self) -> list[str]:
        """解析 CORS 白名单。"""
        return [origin.strip() for origin in self.CORS_ALLOWED_ORIGINS.split(",") if origin.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def resolved_database_url(self) -> str:
        """把相对路径的 SQLite URL 解析为绝对路径。

        这样无论从哪个工作目录启动，数据库文件位置都是确定的。
        """
        if not self.is_sqlite:
            return self.DATABASE_URL

        prefix = "sqlite+aiosqlite:///"
        if not self.DATABASE_URL.startswith(prefix):
            return self.DATABASE_URL

        raw_path = self.DATABASE_URL[len(prefix) :]
        if raw_path.startswith("/"):  # 已是绝对路径
            return self.DATABASE_URL

        absolute = (REPO_ROOT / raw_path.lstrip("./")).resolve()
        return f"{prefix}{absolute}"

    @property
    def storage_root(self) -> Path:
        """本地对象存储根目录（绝对路径）。"""
        raw = Path(self.STORAGE_LOCAL_ROOT)
        if raw.is_absolute():
            return raw
        return (REPO_ROOT / str(raw).lstrip("./")).resolve()

    @property
    def ai_provider_credentials(self) -> dict[str, dict[str, str]]:
        """厂商密钥表。值为空的厂商即视为「未接入」。"""
        return {
            "jixian": {
                "api_base": self.JIXIAN_API_BASE,
                "access_key": self.JIXIAN_ACCESS_KEY,
                "secret_key": self.JIXIAN_SECRET_KEY,
                "vendor_id": self.JIXIAN_VENDOR_ID,
                "app_id": self.JIXIAN_APP_ID,
            },
            "joyinside": {
                "api_base": self.JOYINSIDE_API_BASE,
                "access_key": self.JOYINSIDE_ACCESS_KEY,
                "secret_key": self.JOYINSIDE_SECRET_KEY,
                "tenant_id": self.JOYINSIDE_TENANT_ID,
                "app_id": self.JOYINSIDE_APP_ID,
            },
            "volcano": {
                "api_base": self.VOLCANO_API_BASE,
                "access_key": self.VOLCANO_ACCESS_KEY,
                "secret_key": self.VOLCANO_SECRET_KEY,
                "app_id": self.VOLCANO_APP_ID,
                "model_endpoint": self.VOLCANO_MODEL_ENDPOINT,
            },
            "baidu": {
                "api_base": self.BAIDU_API_BASE,
                "api_key": self.BAIDU_API_KEY,
                "secret_key": self.BAIDU_SECRET_KEY,
                "app_id": self.BAIDU_APP_ID,
            },
        }

    # ================= 校验 =================

    @field_validator("LOG_LEVEL")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    def validate_security(self) -> None:
        """启动期安全校验。任一不通过即拒绝启动。

        这是对历史项目中「硬编码弱口令」问题的架构性防范。
        """
        problems: list[str] = []

        # 1) JWT 密钥强度
        weak_markers = ("change-me", "changeme", "insecure", "placeholder", "example", "secret-key")
        if len(self.JWT_SECRET_KEY.encode()) < 32:
            problems.append(
                f"JWT_SECRET_KEY 长度不足 32 字节（当前 {len(self.JWT_SECRET_KEY.encode())} 字节）"
            )
        if any(marker in self.JWT_SECRET_KEY.lower() for marker in weak_markers):
            problems.append("JWT_SECRET_KEY 仍为占位符，请替换为强随机值")

        # 2) 二维码签名密钥
        if len(self.QR_SIGN_SECRET.encode()) < 16:
            problems.append("QR_SIGN_SECRET 长度不足 16 字节")
        if any(marker in self.QR_SIGN_SECRET.lower() for marker in weak_markers):
            problems.append("QR_SIGN_SECRET 仍为占位符，请替换为强随机值")

        # 2.5) 外部密钥的落库加密密钥
        encryption_key = self.SECRET_ENCRYPTION_KEY.strip()
        if encryption_key:
            if len(encryption_key.encode()) < 32:
                problems.append(
                    f"SECRET_ENCRYPTION_KEY 长度不足 32 字节（当前 {len(encryption_key.encode())} 字节）"
                )
            if any(marker in encryption_key.lower() for marker in weak_markers):
                problems.append("SECRET_ENCRYPTION_KEY 仍为占位符，请替换为强随机值")
        elif self.is_production:
            problems.append(
                "生产环境必须显式配置 SECRET_ENCRYPTION_KEY（留空时由 JWT_SECRET_KEY 派生"
                "，更换 JWT 密钥会导致已存密钥全部无法解密）"
            )

        # 3) 管理员口令必须显式设置且足够强
        admins = {
            "PLATFORM_ADMIN": (self.PLATFORM_ADMIN_ACCOUNT, self.PLATFORM_ADMIN_PASSWORD),
            "MERCHANT_ADMIN": (self.MERCHANT_ADMIN_ACCOUNT, self.MERCHANT_ADMIN_PASSWORD),
            "FACTORY_ADMIN": (self.FACTORY_ADMIN_ACCOUNT, self.FACTORY_ADMIN_PASSWORD),
        }
        # 平台运营账号是可选的（不配置则不写入种子数据）；
        # 一旦配置了账号，就必须同时给它一个合规口令。
        if self.PLATFORM_OPERATOR_ACCOUNT.strip():
            admins["PLATFORM_OPERATOR"] = (
                self.PLATFORM_OPERATOR_ACCOUNT,
                self.PLATFORM_OPERATOR_PASSWORD,
            )
        for label, (account, password) in admins.items():
            if not password:
                problems.append(f"{label}_PASSWORD 未设置（账号 {account}）")
                continue
            if len(password) < self.MIN_PASSWORD_LENGTH:
                problems.append(
                    f"{label}_PASSWORD 长度不足 {self.MIN_PASSWORD_LENGTH} 位"
                )
            if password.lower() in {"admin", "password", "12345678", "admin@2024"}:
                problems.append(f"{label}_PASSWORD 为常见弱口令，请更换")

        # 4) 禁用演示数据仅在生产环境强制要求
        if self.is_production:
            if self.DEBUG:
                problems.append("生产环境（APP_ENV=production）不得开启 DEBUG")
            if self.SEED_DEMO_DATA:
                problems.append("生产环境不得开启 SEED_DEMO_DATA，请设为 false")
            if any("localhost" in o or "127.0.0.1" in o for o in self.cors_origins):
                problems.append("生产环境 CORS_ALLOWED_ORIGINS 不得包含 localhost")

        # 5) 终端用户小程序的模拟通道不得在生产环境启用
        #
        # 这两项与「DEBUG / SEED_DEMO_DATA / localhost CORS」同一性质：
        # 在开发与验收环境是**必要的可演示性**，在生产环境是**直接的安全漏洞**。
        # 因此不在代码里悄悄降级，而是让服务在启动期就拒绝起来。
        if self.is_production:
            if self.MINIAPP_SMS_PROVIDER == "mock":
                problems.append(
                    "生产环境不得使用 MINIAPP_SMS_PROVIDER=mock"
                    "（验证码会回显在响应里，短信校验形同虚设）"
                )
            if self.PAYMENT_PROVIDER == "mock":
                problems.append(
                    "生产环境不得使用 PAYMENT_PROVIDER=mock（支付会被直接置为成功）"
                )

        if problems:
            detail = "\n".join(f"  - {item}" for item in problems)
            raise InsecureConfigurationError(
                "配置未通过安全校验，服务拒绝启动：\n"
                f"{detail}\n\n"
                "修复建议：\n"
                "  1. 复制 .env.example 为 .env\n"
                '  2. 生成强密钥：python -c "import secrets;print(secrets.token_urlsafe(48))"\n'
                "  3. 为三个管理员账号设置强密码\n"
            )


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回配置单例。"""
    return Settings()


def generate_secret(length: int = 48) -> str:
    """生成强随机密钥（供脚本与文档使用）。"""
    return secrets.token_urlsafe(length)


def ensure_runtime_dirs() -> None:
    """创建运行时所需目录。

    包括本地对象存储根目录，以及 SQLite 数据库文件所在目录。
    供应用启动与 Alembic 迁移共同调用——两者都需要这些目录已存在。
    """
    try:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)

        if settings.STORAGE_BACKEND == "local":
            settings.storage_root.mkdir(parents=True, exist_ok=True)

        if settings.is_sqlite:
            url = settings.resolved_database_url
            prefix = "sqlite+aiosqlite:///"
            if url.startswith(prefix):
                db_file = Path(url[len(prefix) :])
                if db_file.parent:
                    db_file.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        # 把「原始 traceback」升级成「可执行的报错」：
        # 这类失败 100% 发生在容器/自定义部署里，而根因永远是「目录不可写」，
        # 报错里直接给出该改哪个变量比让人去读堆栈有用得多。
        raise InsecureConfigurationError(
            "数据目录不可写，服务拒绝启动：\n"
            f"  - 尝试创建的目录：{exc.filename}\n"
            f"  - 当前 DATA_ROOT：{DATA_ROOT}\n"
            "修复方式：把环境变量 DATA_DIR 指向进程可写的目录"
            "（容器部署通常挂载一个卷，例如 DATA_DIR=/data），"
            "或调整该目录的属主与权限。\n"
            "注意：不要为了绕过它而用 root 运行服务——非 root 运行是安全底线。"
        ) from exc


settings = get_settings()
