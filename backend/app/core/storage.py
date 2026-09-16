"""对象存储抽象（P9）：知识库文件与固件包的落地。

为什么要有这一层
----------------
上传能力在两个地方被需要——知识库文件（商户端）与固件包（平台端）——
而两者的存储细节完全相同：**写文件、算摘要、按 key 取回、删除**。
如果各自直接 `open()` 写盘，将来换 S3 就要改两处，且极易只改一处。

设计取舍
--------
* **只实现 ``local``**：本阶段的目标是「克隆即跑」，本地磁盘后端天然满足；
  ``s3`` 分支**显式抛 `VENDOR_UNAVAILABLE`**（ADR-07 口径），而不是留下一个
  「看起来能配、配了报 NotImplementedError」的半成品。宁可启动期就明确
  「S3 未实现」，也不要让人以为它可用。
* **key 由调用方给，且必须经过校验**：key 里含 ``..`` 或绝对路径就会变成
  路径穿越（原型的 `server.py` 正是栽在这上面，见项目附录 B）。因此
  :func:`_safe_key` 是**唯一**的路径拼接入口，任何后端实现都必须先过它。
* **摘要在这里算**：``checksum`` 同时是「去重依据」与「完整性凭据」，
  让调用方各自算一遍必然出现算法不一致（一个 sha256、一个 md5）。

安全边界（写进 docstring 而不是只写在代码里）
--------------------------------------------
本地后端把文件放在 ``STORAGE_LOCAL_ROOT`` 之下，并保证：

1. 最终解析出的绝对路径**必须**以 root 为前缀（:func:`_safe_key` 校验）；
2. key 里不允许出现 ``..``、空段、绝对路径前缀（``/`` 开头、Windows 盘符）；
3. 文件名由调用方按业务规则生成（如 ``kb/{tenant}/{file_id}{ext}``），
   不使用用户上传的原始文件名——原始名只作为 ``filename`` 字段入库展示。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import settings
from app.core.errors import validation_error, vendor_unavailable
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 单次读取上传文件的上限（防御：UploadFile 是流式对象，不设上限会吃满内存）
CHUNK_SIZE = 1024 * 1024


@dataclass(slots=True)
class StoredObject:
    """一次落盘的结果。"""

    key: str
    size: int
    checksum: str


class StorageBackend(Protocol):
    """存储后端协议（结构性类型，无需继承）。"""

    def save(self, key: str, content: bytes) -> StoredObject: ...

    def read(self, key: str) -> bytes: ...

    def delete(self, key: str) -> bool: ...

    def exists(self, key: str) -> bool: ...


def _safe_key(key: str) -> str:
    """校验并归一化存储 key（**唯一的路径拼接入口**）。

    Raises:
        AppException: key 为空、含 ``..``、是绝对路径或含空段
            （``VALIDATION_ERROR``，属于调用方的编程错误，不该静默修正）。
    """
    candidate = (key or "").strip().replace("\\", "/")
    if not candidate:
        raise validation_error("存储 key 不能为空")
    if candidate.startswith("/") or ":" in candidate.split("/")[0]:
        raise validation_error("存储 key 不能是绝对路径")
    parts = list(candidate.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise validation_error("存储 key 不能包含空段或 .. 路径段")
    return "/".join(parts)


def sha256_hex(content: bytes) -> str:
    """内容摘要（去重与完整性校验的统一算法）。"""
    return hashlib.sha256(content).hexdigest()


class LocalStorage:
    """本地磁盘后端。

    目录结构就是 key 的层级（``data/storage/kb/t-001/xxx.txt``），
    运维可以直接用文件管理工具查看，不需要任何额外工具——这是「克隆即跑」
    目标下的最优选择。
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or settings.storage_root).resolve()

    def _resolve(self, key: str) -> Path:
        """把 key 解析为磁盘路径，并**再次**确认它在 root 之内。"""
        safe = _safe_key(key)
        target = (self.root / safe).resolve()
        if not str(target).startswith(str(self.root)):
            # 双保险：即使 _safe_key 被绕过，这里也拒绝越界写入
            raise validation_error("存储路径越界")
        return target

    def save(self, key: str, content: bytes) -> StoredObject:
        """写入并返回摘要。父目录不存在时自动创建。"""
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        checksum = sha256_hex(content)
        logger.info("存储写入 %s（%d 字节）", key, len(content))
        return StoredObject(key=_safe_key(key), size=len(content), checksum=checksum)

    def read(self, key: str) -> bytes:
        target = self._resolve(key)
        if not target.is_file():
            from app.core.errors import not_found

            raise not_found(f"存储对象不存在：{key}")
        return target.read_bytes()

    def delete(self, key: str) -> bool:
        """删除；对象不存在返回 ``False``（**不报错**）。

        删除是幂等动作：文件已经不在，调用方想要的终态已经达成。
        让「重复删除」抛错只会逼调用方写 try/except，反而掩盖真错误。
        """
        target = self._resolve(key)
        if not target.is_file():
            return False
        target.unlink()
        logger.info("存储删除 %s", key)
        return True

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()


class S3Storage:
    """S3 后端**占位**：显式安全失败，绝不假装可用。

    为什么保留这个类而不是干脆不写：配置文件里有 ``STORAGE_BACKEND=s3``
    这个取值（与 ``.env.example`` 一致）。若它没有对应实现，运维配了之后
    会在**第一次上传**时才炸出 ``ImportError``/``AttributeError``；
    留一个明确报 ``VENDOR_UNAVAILABLE`` 的实现，能在上传那一刻就说清
    「S3 后端尚未实现，请改用 local 或自行实现 StorageBackend」。
    这是 ADR-07「宁可失败也不伪造成功」在存储层的同一口径。
    """

    def _fail(self) -> None:
        raise vendor_unavailable(
            "S3 存储后端尚未实现（本阶段仅提供本地磁盘后端），请将 STORAGE_BACKEND 设为 local",
            vendor="s3",
        )

    def save(self, key: str, content: bytes) -> StoredObject:
        self._fail()
        raise AssertionError("unreachable")

    def read(self, key: str) -> bytes:
        self._fail()
        raise AssertionError("unreachable")

    def delete(self, key: str) -> bool:
        self._fail()
        raise AssertionError("unreachable")

    def exists(self, key: str) -> bool:
        self._fail()
        raise AssertionError("unreachable")


def get_storage() -> StorageBackend:
    """按配置返回存储后端。"""
    if settings.STORAGE_BACKEND == "s3":
        return S3Storage()
    return LocalStorage()


def build_object_key(*, prefix: str, owner: str, object_id: str, filename: str) -> str:
    """生成业务 key：``{prefix}/{owner}/{object_id}{扩展名}``。

    **不使用用户上传的原始文件名**（只取扩展名）：原始名可能含路径分隔符、
    控制字符或超长内容，直接拼进路径就是路径穿越与磁盘污染的门票。
    原始名作为 ``filename`` 字段入库，仅用于展示。

    扩展名一并**归一化**（小写、只保留字母数字），拿不到扩展名时返回无后缀的 key。
    """
    safe_prefix = _safe_key(prefix)
    safe_owner = _safe_key(owner)
    safe_id = _safe_key(object_id)
    suffix = ""
    if "." in filename:
        raw_ext = filename.rsplit(".", 1)[-1].strip().lower()
        cleaned = "".join(ch for ch in raw_ext if ch.isalnum())
        if cleaned:
            suffix = f".{cleaned}"
    return f"{safe_prefix}/{safe_owner}/{safe_id}{suffix}"


__all__ = [
    "CHUNK_SIZE",
    "LocalStorage",
    "S3Storage",
    "StorageBackend",
    "StoredObject",
    "build_object_key",
    "get_storage",
    "sha256_hex",
]
