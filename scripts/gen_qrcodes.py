#!/usr/bin/env python
"""生成演示二维码清单（P10 运维脚本）。

为什么要有这个脚本
==================
验收与演示时经常需要「把几台设备的二维码打出来扫一下」。如果每次都去
调用 ``GET /platform/orders/{id}/qrcodes``，就要求：服务已启动、有可登录的
账号、设备恰好挂在某个订单下——任何一条不满足就演示不下去。

本脚本把这件事变成一条离线命令：直接从库里读设备，产出**可打印、可核对**
的二维码与清单。

关键设计取舍
============

1. **载荷一律经 ``qrcode_service.build_qrcodes`` 生成**，脚本内不拼任何
   分隔符与签名。二维码是跨端契约（后端生成 / 前端展示 / 固件解析），
   格式与签名算法必须只有一处实现；脚本里再写一遍，等于制造第二个真相来源，
   而且它出错时「扫出来是乱码」比「报错」难发现得多。

2. **逐台调用而非整批调用**。``build_qrcodes`` 遇到无法生成二维码的设备
   （平台自有库存 ``tenant_id`` 为空，京东格式缺租户）会抛异常并说明原因。
   整批传入时一台坏设备就会让整批导出失败；逐台传入 + 捕获异常，
   则「好设备照常产出，坏设备逐条记入清单」——演示素材不会因为
   一台未分配设备而全军覆没，同时坏设备也不会被静默吞掉。

3. **输出到 ``data/qrcodes/``**（而非对象存储目录 ``data/storage/`` 内）：
   对象存储由存储层按 key 管理，运维产物混进去会污染业务对象命名空间；
   ``data/`` 已在 ``.gitignore`` 中，二维码（内含租户/产品信息）不会被误提交。

4. **PNG 渲染能力只探测一次**。``qrcode`` 库渲染 PNG 依赖 Pillow（或用纯
   Python 的 pypng）。开发环境缺这个可选依赖是常见的，此时按台报错会刷满
   屏并掩盖真正的问题；因此启动时探测一次，不可用就打印一条清晰提示，
   降级为「只产出文本清单」——清单里的载荷本身就是可核对的。

用法::

    make qrcodes
    # 或
    python scripts/gen_qrcodes.py [--limit N] [--sn SN]

参数:
    --limit N  最多生成 N 台（默认 50）。默认值是一道保险：库里的设备
               可能有上千台，误敲一次命令就生成上千张 PNG 既慢又无意义。
    --sn SN    只生成指定 SN 的这一台。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

# 让脚本能 import app 包（与 seed_demo.py / export_openapi.py 同一做法）
sys.path.insert(0, str(BACKEND_ROOT))

#: CSV 列顺序固定，便于把两次导出的清单直接 diff
CSV_COLUMNS = ("SN", "联网方式", "格式", "载荷", "文件名")

#: 默认最多生成台数
DEFAULT_LIMIT = 50


def _safe_filename(sn: str) -> str:
    """把 SN 转成安全文件名。

    SN 可能来自工厂导入，理论上能包含 ``/`` 或 ``..`` 这类字符；
    直接拼进路径会写到目录外。这里只做白名单替换，不做转义，
    因为文件名只需要「稳定 + 可读」，SN 原文始终完整保存在清单里。
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", sn)
    return cleaned.strip("._") or "device"


def _render_png(payload: str, path: Path) -> None:
    """用 ``qrcode`` 库渲染一张 PNG。

    纠错等级取 M（与 ``scripts/verify_qrcode.py`` 的参考实现一致），
    box_size=8 / border=4 是为了打印：屏幕上看小尺寸无所谓，
    纸上太小就扫不出来，白边不足则扫码器找不到定位图形。
    """
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    qr.make_image().save(path)


def _probe_png_support() -> tuple[bool, str]:
    """探测 PNG 渲染能力，返回 ``(是否可用, 失败原因)``。

    真渲染一张再丢掉，比 ``import PIL`` 更可靠：``qrcode`` 的 PNG 后端
    在运行时才解析（Pillow / pypng 二者之一即可），只查 import 会漏判。
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _render_png("ToyVerse-Cloud-PNG-Probe", Path(tmp) / "probe.png")
    except Exception as exc:  # 可选依赖缺失有很多种表现，一律降级而非中断
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


async def _load_devices(sn: str | None, limit: int) -> list[Any]:
    """从数据库读取设备。

    按 SN 排序：让同一批设备每次导出的清单顺序一致，
    这样 ``diff manifest.csv`` 才能用于「这次和上次有什么变化」。
    """
    from sqlalchemy import select

    from app.db.session import SessionLocal, dispose_engine
    from app.models.device import Device

    try:
        async with SessionLocal() as session:
            stmt = select(Device)
            if sn:
                stmt = stmt.where(Device.sn == sn)
            stmt = stmt.order_by(Device.sn).limit(limit)
            return list((await session.execute(stmt)).scalars().all())
    finally:
        await dispose_engine()


def _build_items(devices: list[Any]) -> tuple[list[Any], list[tuple[str, str]]]:
    """把设备翻译成二维码记录，返回 ``(成功列表, 跳过列表)``。

    逐台调用 ``build_qrcodes``（见模块 docstring 的取舍 2）。
    """
    from app.core.errors import AppException
    from app.services.qrcode_service import build_qrcodes

    items: list[Any] = []
    skipped: list[tuple[str, str]] = []
    for device in devices:
        try:
            items.extend(build_qrcodes([device]))
        except AppException as exc:
            # 只捕获业务异常：真正的编程错误（AttributeError 之类）应当暴露
            skipped.append((str(getattr(device, "sn", "?")), exc.message))
    return items, skipped


def _write_manifests(out_dir: Path, rows: list[dict[str, str]]) -> None:
    """写出 CSV 与 JSON 两份清单。

    两份都写：CSV 给人看（可直接进打印清单与 Excel），
    JSON 给机器用（后续脚本/CI 校验，字段类型与转义无歧义）。
    """
    with (out_dir / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as fp:
        # utf-8-sig：表头是中文，带 BOM 才能被 Excel 正确识别编码
        writer = csv.DictWriter(fp, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)

    (out_dir / "manifest.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成演示二维码清单（PNG + manifest.csv/json）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"最多生成多少台设备（默认 {DEFAULT_LIMIT}）",
    )
    parser.add_argument("--sn", help="只生成指定 SN 的设备")
    args = parser.parse_args()

    if args.limit < 1:
        print("✗ --limit 必须大于 0")
        return 2

    from app.core.config import DATA_ROOT

    out_dir = DATA_ROOT / "qrcodes"

    print("=" * 68)
    print("ToyVerse Cloud — 生成演示二维码清单")
    print("=" * 68)

    devices = asyncio.run(_load_devices(args.sn, args.limit))

    if not devices:
        # 以退出码 1 结束：脚本「什么都没做成」必须让人和 CI 都能察觉，
        # 静默返回 0 会让流水线里的 make qrcodes 假装成功。
        if args.sn:
            print(f"✗ 未找到 SN 为 {args.sn} 的设备。")
        else:
            print("✗ 数据库中没有设备。请先执行 `make seed`。")
        return 1

    print(f"数据库设备: {len(devices)} 台（按 SN 排序，最多 {args.limit} 台）")
    print()

    items, skipped = _build_items(devices)
    if skipped:
        print(f"⚠️  {len(skipped)} 台设备无法生成二维码（已跳过，未中断导出）：")
        for sn, reason in skipped:
            print(f"    - {sn}: {reason}")
        print()

    if not items:
        print("✗ 没有任何设备成功生成二维码，请检查上面的跳过原因。")
        return 1

    png_ok, png_error = _probe_png_support()
    if not png_ok:
        print("⚠️  未能渲染 PNG：当前虚拟环境缺少二维码图片后端（Pillow 或 pypng）。")
        print(f"    原因：{png_error}")
        print("    修复：backend/.venv/bin/pip install pillow")
        print("    本次仅输出文本清单（载荷本身可直接用于扫码/核对）。")
        print()

    # 幂等：文件名只由 SN 决定，重复执行覆盖同名文件，不会堆出 xxx-1.png
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str]] = []
    png_count = 0
    for item in items:
        filename = f"{_safe_filename(item.sn)}.png"
        if png_ok:
            try:
                _render_png(item.payload, out_dir / filename)
                png_count += 1
            except Exception as exc:
                # 单张渲染失败不该毁掉整批：记下来，清单里仍保留该行
                print(f"    ⚠️  {item.sn} 渲染 PNG 失败：{type(exc).__name__}: {exc}")
                filename = ""
        else:
            filename = ""

        rows.append(
            {
                "SN": item.sn,
                "联网方式": item.network_type,
                "格式": str(item.format),
                "载荷": item.payload,
                "文件名": filename,
            }
        )

    _write_manifests(out_dir, rows)

    # 按格式分组的数量：一眼看出这次导出覆盖了几种方案（4G 与 Wi-Fi 格式不同）
    by_format: dict[str, int] = {}
    for item in items:
        key = str(item.format)
        by_format[key] = by_format.get(key, 0) + 1

    print("-" * 68)
    print(f"✔ 完成：共生成 {len(rows)} 条二维码记录（跳过 {len(skipped)} 台）")
    print(f"  PNG 图片  : {png_count} 张" + ("" if png_ok else "（后端不可用，未生成）"))
    print(f"  输出目录  : {out_dir}")
    print(f"  文本清单  : {out_dir / 'manifest.csv'}")
    print(f"  机器清单  : {out_dir / 'manifest.json'}")
    print("  按格式分组: " + "，".join(f"{k} {v} 张" for k, v in sorted(by_format.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
