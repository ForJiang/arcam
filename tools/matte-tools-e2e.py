#!/usr/bin/env python3
"""matte_tools 真图冒烟：装图自动修图链五步全跑（FEATURE-SPEC §1 门禁 4）。

用原版仓库自带的素材（黑底/白底 JPG、带透明 PNG）过整条链，打印每步结论。
用法：.venv/bin/python tools/matte-tools-e2e.py [素材目录]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arcam.matte_tools import (          # noqa: E402
    auto_prepare_char,
    content_bounds,
    cut_solid_bg,
)

DEFAULT_DIR = Path("/Users/jianghaoda/.zcode/workspace/default/oss-teardown/repo/chars/多的素材")


def run(path: Path) -> int:
    img = Image.open(path).convert("RGB")
    arr = np.asarray(img).astype(np.float64) / 255.0
    rgba = np.dstack([arr, np.ones(arr.shape[:2])])
    prep = auto_prepare_char(rgba)
    before = content_bounds(rgba)
    after = content_bounds(prep.rgba01)
    print(f"{path.name}")
    print(f"  原尺寸 {rgba.shape[1]}×{rgba.shape[0]} → 修后 {prep.rgba01.shape[1]}×{prep.rgba01.shape[0]}")
    print(f"  抠除占比 {('%0.1f%%' % (prep.cut_ratio * 100)) if prep.cut_ratio is not None else '—（未抠/门控拒）'}"
          f"｜裁边 {'是' if prep.trimmed else '否'}｜半身 {'是' if prep.half_body else '否'}")
    print(f"  内容占比 {('%0.1f%%' % (100 * before[2] * before[3] / (rgba.shape[0] * rgba.shape[1]))) if before else '—'}"
          f" → {('%0.1f%%' % (100 * after[2] * after[3] / (prep.rgba01.shape[0] * prep.rgba01.shape[1]))) if after else '—'}")
    print(f"  动作 {' + '.join(a.split('.')[-1] for a in prep.actions) or '（无）'}")
    # 单测 cut_solid_bg 的公开 API 也过一遍（返回占比）
    solo = cut_solid_bg(rgba)
    print(f"  cut_solid_bg 公开 API: {('%0.4f' % solo) if solo is not None else 'None'}")
    return 0


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    files = sorted(p for p in src.iterdir()
                   if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not files:
        print(f"{src} 里没有 jpg/png 素材")
        return 1
    for p in files:
        run(p)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
