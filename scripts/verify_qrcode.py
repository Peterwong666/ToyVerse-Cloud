#!/usr/bin/env python
"""二维码编码器正确性验证。

为什么需要这个脚本
------------------
前端为零依赖方案，二维码编码器是**手写实现**（`frontend/shared/ui/qrcode.js`）。
二维码涉及 Reed-Solomon 纠错、BCH 码、位序放置、掩码择优等多个易错环节，
任何一处出错都会导致「看起来像二维码但扫不出内容」——这是最难靠肉眼发现的问题。

因此这里用独立的第三方实现（`qrcode` 库）做**逐模块比对**，
把「手写编码器正确」变成一条可复现、可进 CI 的断言。

比对方式
--------
对每个载荷 × 全部 8 种掩码，比对两个实现的完整模块矩阵。
之所以固定掩码而不是比对自动选择的结果：掩码择优在“是否把格式信息计入罚分”
上各实现存在差异（`qrcode` 库评估掩码时不含格式信息），
但**任一掩码都是合法、可扫的**，因此固定掩码才能做严格等价断言。

用法::

    python scripts/verify_qrcode.py
    # 或
    make qr-verify
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
QR_MODULE = REPO_ROOT / "frontend" / "shared" / "ui" / "qrcode.js"

#: 覆盖各类真实场景：
#:   - 短载荷与空边界
#:   - 各版本容量边界（14 / 26 / 42 / 62 / 84 字节分别是 v1–v5 的容量上限）
#:   - 恰好填满容量（不触发填充码字）
#:   - 集贤 4G 二维码格式 JX|SN|IMEI|ICCID|deviceId
#:   - 京东 Wi-Fi 二维码格式 JD|tenant_id|product_id|sn|sign
#:   - 中文（UTF-8 多字节）
#:   - 长 URL
PAYLOADS: tuple[str, ...] = (
    "A",
    "demo-qr-1",
    "A" * 14,
    "A" * 26,
    "A" * 42,
    "A" * 62,
    "A" * 84,  # v5 恰好填满，不产生填充码字
    "JX|SN-TEST-0001|860123456789012|89860012345678901234|DEV-0001",
    "JD|t-001|cprod-abc123|SN-AAAA0001|8f3a2b1c9d4e5f60a1b2c3d4e5f60718",
    "SN-20260916-0001234567890123456789012345678901234567890123456789",
    "中文内容测试：小玩具，大智慧。ToyVerse Cloud 二维码编码验证。",
    "JX|867240051234567|89860612345678901234|DEV-2026-0001|小玩具大智慧",
    "https://example.com/platform/qr/parse?code=JD%7Ct-001%7Cpt-001%7CSN-0001%7Csign",
)


def _require_node() -> str:
    node = shutil.which("node")
    if not node:
        print("✗ 未找到 node，无法验证前端二维码编码器", file=sys.stderr)
        raise SystemExit(2)
    return node


def _require_reference():
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
        from qrcode.util import MODE_8BIT_BYTE, QRData
    except ImportError:
        print(
            "✗ 缺少参考实现。请先安装开发依赖：\n"
            "    pip install -r backend/requirements-dev.txt",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    return qrcode, ERROR_CORRECT_M, QRData, MODE_8BIT_BYTE


def mine(text: str, mask: int) -> dict:
    """调用前端编码器生成矩阵。"""
    script = (
        f"import {{ encode }} from {json.dumps(str(QR_MODULE))};\n"
        f"const r = encode({json.dumps(text)}, {{ mask: {mask} }});\n"
        "process.stdout.write(JSON.stringify({"
        "v: r.version, size: r.size, "
        'rows: r.modules.map(row => row.map(x => x ? "1" : "0").join(""))'
        "}));"
    )
    raw = subprocess.run(
        [_require_node(), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(raw)


def reference(text: str, mask: int, qrcode, error_m, qr_data, mode_byte) -> tuple[int, list[str]]:
    """调用第三方实现生成矩阵（强制字节模式，固定掩码）。"""
    qr = qrcode.QRCode(
        version=None,
        error_correction=error_m,
        box_size=1,
        border=0,
        mask_pattern=mask,
    )
    qr.add_data(qr_data(text.encode("utf-8"), mode=mode_byte))
    qr.make(fit=True)
    return qr.version, ["".join("1" if cell else "0" for cell in row) for row in qr.modules]


def main() -> int:
    if not QR_MODULE.exists():
        print(f"✗ 找不到编码器：{QR_MODULE}", file=sys.stderr)
        return 2

    qrcode, error_m, qr_data, mode_byte = _require_reference()

    print("=" * 72)
    print("二维码编码器正确性验证（与独立实现逐模块比对）")
    print("=" * 72)
    print(f"被测实现 : {QR_MODULE.relative_to(REPO_ROOT)}")
    print(f"参考实现 : qrcode {getattr(qrcode, '__version__', '?')}（字节模式，纠错等级 M）")
    print(f"载荷数量 : {len(PAYLOADS)}   比对组合 : {len(PAYLOADS) * 8}（每载荷 8 个掩码）")
    print()

    failures: list[str] = []
    checked = 0

    for text in PAYLOADS:
        label = text if len(text) <= 40 else f"{text[:37]}..."
        ok = True
        version = None

        for mask in range(8):
            try:
                got = mine(text, mask)
            except subprocess.CalledProcessError as exc:
                failures.append(f"{label} mask={mask} 编码异常：{exc.stderr.strip()}")
                ok = False
                break

            ref_version, ref_rows = reference(text, mask, qrcode, error_m, qr_data, mode_byte)
            version = got["v"]
            checked += 1

            if got["v"] != ref_version:
                failures.append(
                    f"{label} mask={mask} 版本不一致：本实现 v{got['v']}，参考 v{ref_version}"
                )
                ok = False
                break

            if ref_rows != got["rows"]:
                # 此处已确认矩阵不相等：用 strict=False 容忍长度差异，
                # 以便统计差异模块数后统一报错（长度不一致本身即视为失败）。
                diff = sum(
                    1
                    for a, b in zip(ref_rows, got["rows"], strict=False)
                    for x, y in zip(a, b, strict=False)
                    if x != y
                )
                failures.append(f"{label} mask={mask} 矩阵不一致，差异 {diff} 个模块")
                ok = False
                break

        status = "✔" if ok else "✗"
        print(f"  {status} v{version:<2} {len(text.encode()):>3}B  {label!r}")

    print()
    print("-" * 72)
    if failures:
        print(f"✗ 验证失败，共 {len(failures)} 项：")
        for item in failures[:20]:
            print(f"    {item}")
        return 1

    print(f"✔ 全部通过：{checked} 组矩阵逐模块一致，编码器与规范实现等价")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
