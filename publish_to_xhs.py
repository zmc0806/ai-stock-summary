#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书自动发布脚本（基于 xhs-mcp CLI）

依赖：
    npm install -g xhs-mcp
    npx xhs-mcp login   ← 首次登录，只需一次

用法：
    python publish_to_xhs.py                          # 发布最新文案
    python publish_to_xhs.py --dry-run                # 预览，不发布
    python publish_to_xhs.py --images /path/img.png   # 带配图发布
    python publish_to_xhs.py --auto-cover             # 自动生成封面图
"""

import os
import sys
import json
import argparse
import subprocess
from datetime import datetime

DEFAULT_PAYLOAD = "reports/xhs_payload_latest.json"

# ─── 自动生成封面图（可选，需要 Pillow） ──────────────────────────────────────

def try_generate_cover(payload: dict, output_path: str) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont
        import textwrap
    except ImportError:
        print("  [INFO] 未安装 Pillow，跳过封面生成。pip install Pillow 可启用。")
        return False

    try:
        W, H = 1080, 1080
        img = Image.new("RGB", (W, H), color=(15, 23, 42))
        draw = ImageDraw.Draw(img)

        draw.rectangle([(0, 0), (W, 160)], fill=(30, 41, 59))
        date_str = datetime.now().strftime("%Y.%m.%d")
        draw.rectangle([(60, 55), (340, 105)], fill=(99, 102, 241))

        font_paths = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "C:/Windows/Fonts/msyh.ttc",
        ]
        font_lg = font_sm = font_xs = None
        for fp in font_paths:
            if os.path.exists(fp):
                try:
                    font_lg = ImageFont.truetype(fp, 68)
                    font_sm = ImageFont.truetype(fp, 34)
                    font_xs = ImageFont.truetype(fp, 26)
                    break
                except Exception:
                    pass
        if font_lg is None:
            font_lg = font_sm = font_xs = ImageFont.load_default()

        draw.text((80, 63), f"📊 {date_str}  A股开盘前晨报", font=font_sm, fill="white")

        title = payload.get("title", "今日A股布局思路")
        y = 220
        for line in textwrap.wrap(title, width=9):
            draw.text((80, y), line, font=font_lg, fill="white")
            y += 90

        preview = payload.get("content", "")[:80] + "…"
        y = max(y + 50, 560)
        for line in textwrap.wrap(preview, width=24)[:4]:
            draw.text((80, y), line, font=font_xs, fill=(148, 163, 184))
            y += 40

        tags = payload.get("tags", [])[:4]
        draw.rectangle([(0, H - 130), (W, H)], fill=(30, 41, 59))
        draw.text((80, H - 110), "  ".join(f"#{t}" for t in tags),
                  font=font_xs, fill=(99, 102, 241))
        draw.text((80, H - 65), "A股财经  每日晨报", font=font_xs, fill=(71, 85, 105))

        img.save(output_path)
        print(f"  [OK] 封面图已生成 → {output_path}")
        return True
    except Exception as e:
        print(f"  [WARN] 封面图生成失败：{e}")
        return False


# ─── 发布核心逻辑 ──────────────────────────────────────────────────────────

def publish(payload: dict, image_paths: list, dry_run: bool = False) -> bool:
    title   = payload.get("title", "")
    content = payload.get("content", "")
    tags    = payload.get("tags", [])

    tags_line    = " ".join(f"#{t}" for t in tags)
    full_content = content.rstrip() + "\n\n" + tags_line

    print("\n" + "─" * 56)
    print("  📱 小红书发布预览")
    print(f"  标题  : {title}")
    print(f"  正文  : {full_content[:80]}...")
    print(f"  标签  : {tags_line}")
    print(f"  配图  : {image_paths if image_paths else '无（纯文字）'}")
    print("─" * 56 + "\n")

    if dry_run:
        print("[DRY-RUN] 预览完成，未实际发布。")
        return True

    # xhs-mcp publish 命令
    cmd = [
        "npx", "xhs-mcp", "publish",
        "--type",    "image",
        "--title",   title,
        "--content", full_content,
    ]
    if image_paths:
        cmd += ["--media", ",".join(image_paths)]
    if tags:
        cmd += ["--tags", ",".join(tags)]

    print(f"[RUN] npx xhs-mcp publish ...")
    try:
        result = subprocess.run(cmd, timeout=120)
        if result.returncode == 0:
            print("\n✅ 小红书发布成功！")
            return True
        else:
            print(f"\n[ERROR] 发布失败，退出码：{result.returncode}")
            print("  请确认已运行过：npx xhs-mcp login")
            return False
    except subprocess.TimeoutExpired:
        print("[ERROR] 超时（120s），浏览器可能未启动")
        return False
    except FileNotFoundError:
        print("[ERROR] 找不到 npx，请确认 Node.js 已安装：brew install node")
        return False
    except Exception as e:
        print(f"[ERROR] {e}")
        return False


# ─── 主程序 ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="小红书自动发布（xhs-mcp）")
    parser.add_argument("--payload",    default=DEFAULT_PAYLOAD)
    parser.add_argument("--images",     nargs="*", default=[])
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--auto-cover", action="store_true",
                        help="自动生成封面图（需要 pip install Pillow）")
    args = parser.parse_args()

    if not os.path.exists(args.payload):
        print(f"[ERROR] 找不到 {args.payload}")
        print("  请先运行：python intelligence_report_ashare.py")
        sys.exit(1)

    with open(args.payload, encoding="utf-8") as f:
        payload = json.load(f)

    print(f"[INFO] Payload 生成时间：{payload.get('generated_at', 'unknown')}")

    image_paths = list(args.images)

    if args.auto_cover and not image_paths:
        cover = "/tmp/xhs_cover.png"
        if try_generate_cover(payload, cover):
            image_paths = [cover]

    if not image_paths:
        print("[INFO] --media 为必填项，自动生成封面图...")
        cover = "/tmp/xhs_cover.png"
        if try_generate_cover(payload, cover):
            image_paths = [cover]
        else:
            print("[ERROR] 封面图生成失败，请用 --images 指定一张图片")
            print("        pip install Pillow  可启用自动封面")
            return False

    success = publish(payload, image_paths, dry_run=args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
