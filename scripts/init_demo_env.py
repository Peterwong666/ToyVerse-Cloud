"""Create a loopback-only demo configuration with fresh credentials."""

from __future__ import annotations

import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = (
    "JWT_SECRET_KEY",
    "QR_SIGN_SECRET",
    "PLATFORM_ADMIN_PASSWORD",
    "PLATFORM_OPERATOR_PASSWORD",
    "MERCHANT_ADMIN_PASSWORD",
    "FACTORY_ADMIN_PASSWORD",
)


def main() -> int:
    env_path = ROOT / ".env"
    sample = (ROOT / ".env.example").read_text(encoding="utf-8")
    lines = []
    for line in sample.splitlines():
        name = line.partition("=")[0]
        if name in SECRET_NAMES:
            line = f"{name}={secrets.token_urlsafe(48)}"
        elif name == "FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN":
            line = "FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN=false"
        lines.append(line)
    lines.extend(("", "APP_BIND_ADDRESS=127.0.0.1", "WORKERS=1"))
    try:
        with open(env_path, "x", encoding="utf-8", opener=_private_file) as stream:
            stream.write("\n".join(lines) + "\n")
    except FileExistsError:
        print(".env already exists. Use your existing configuration or a fresh checkout.")
        return 1
    print("Created .env. Login names and generated passwords are in that file.")
    return 0


def _private_file(path: str, flags: int) -> int:
    import os

    return os.open(path, flags, 0o600)


if __name__ == "__main__":
    raise SystemExit(main())
