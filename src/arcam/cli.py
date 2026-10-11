"""arcam 命令行 —— 原版桌面端四件事（出图 / 立绘层 / 素材包 / 抠图）加离线视频、LiDAR 深度。

子命令一览（每个都有中文 --help；共 15 个，stats 现抓本表不硬编码）::

    arcam render PHOTO CHAR -o OUT.png [--scene PATH] [摆放参数] [--depth DEPTH]  # 出图
    arcam char-layer CHAR -o OUT.png [摆放参数]                    # 仅 RGBA 立绘层
    arcam bundle PHOTO CHAR -o OUT.zip [摆放参数]                   # 三件套 zip（确定性时间戳）
    arcam matte IMG -o OUT.png [--model PATH]                       # ISNet-anime 抠图
    arcam prep CHAR -o OUT.png [--no-solid/--no-trim/--no-soften/--no-blur]  # 装图自动修图链
    arcam video TRACK.json CHAR -o OUTDIR [--bg 单图或目录] [--tone] [--gif]  # 逐帧渲染
    arcam check TRACK.json                                          # 轨道自检
    arcam recipe save|list|show|apply NAME ...                      # 摆放配方库
    arcam watch PHOTO CHAR -o OUT.png [--once] [--interval 秒]       # 素材变了自动重出图
    arcam multi PHOTO CHAR... -o OUT.png [--spec 'K=V;K=V']          # 多立绘叠同一照片
    arcam contact-sheet PHOTO CHAR -o OUT.png --vary K=V,V,V        # 参数对比网格
    arcam batch PHOTO_DIR CHAR -o OUTDIR [--depth-dir DIR] [--ext png|jpg]  # 批量出图
    arcam stats [--json]                                            # 项目自报体检表
    arcam lidar inspect DEPTH [--json]                              # LiDAR 深度诊断（h/pitch/roll）
    arcam lidar serve --port 7707 [--out DIR]                       # 实时流接收（Ctrl-C 停止）
    arcam web [--host 127.0.0.1] [--port 8765] [--no-open]          # 本地网页界面（标准库 HTTP 服务）

退出码：0 正常；1 运行期失败（缺文件、素材不合法、轨道有问题）；2 用法错误（argparse）。
报错一律人话，不静默失败 —— 对齐原版 saveNote 的坏消息必提示（index.html:3370）。

LiDAR 环境接入契约：``docs/LIDAR-SPEC.md``。``--depth`` 给深度帧时，相机高/俯仰/
滚转/人物纵深由深度测出（LIDAR-SPEC §0），**显式**摆放参数仍然优先；显式与测出冲突
时打印一行「深度测得 …（显式值优先）」。显式值的判定口径：argparse 里这几个开关
默认 None，用了才是显式——compose 侧只收 override dict，不解析 argv。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np

from arcam.compose import _merge_env, _over, compose_char_layer, compose_shot
from arcam.frames import frame_rect
from arcam.geometry import Camera
from arcam.i18n import LANGS, TXT, t
from arcam.matting import anime_cut
from arcam.numeric import js_round
from arcam.placement import FRAMES, SHOTS, Subject, current_placement
from arcam.scene import build_scene, save_scene
from arcam.video import (
    GIF_NAME,
    MP4_NAME,
    encode_jpeg,
    encode_png,
    frame_count,
    image_to_rgb01,
    image_to_rgba01,
    load_track,
    render_track,
    save_png,
    track_issues,
)

ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
"""zip 内文件时间戳写死 1980-01-01 —— 原版 zipStore 同样写死 0x21（index.html:3309），
同样输入永远出同样字节，确定性复现。"""

#: ``lidar sim --push`` 的帧规格下限：``simulate_scene_server``（arcam.lidar_sim）
#: 按 ``synth_ground_frame`` 的默认 256×192 / seed 0 合成，没有尺寸与种子参数。
#: 与 build_parser 里 sim 组 ``--width/--height/--seed`` 的默认值一致——改了
#: parser 默认值这里要同步（v1.8 审计：曾静默丢掉用户给的 --width/--height/--seed）。
_SIM_PUSH_FIXED_SPEC = {"width": 256, "height": 192, "seed": 0}


class CliError(Exception):
    """人话错误：main() 捕获后打到 stderr，退出码 1。"""


def _add_out(p: argparse.ArgumentParser) -> None:
    p.add_argument("-o", "--out", required=True, metavar="路径",
                   help="输出文件路径（-o 短横形式等价）")


def _add_placement(p: argparse.ArgumentParser) -> None:
    """摆放参数组：每个开关都是原版 S 的一个滑块（index.html:1985–1986 bind）。

    ``--x/--d/--h/--pitch/--roll`` 的 argparse 默认值是 **None**（不是原版 S 默认）：
    这几个量 LiDAR 深度也能测出（LIDAR-SPEC §0），"用户显式给过" 与 "没给、用默认
    或测出值" 需要区分，口径就是 "is None"。给了 ``--depth`` 时非 None 的键进
    ``env_overrides`` 显式优先；没给时 compose 侧从测出值/原版默认补齐。未给
    ``--depth`` 的路径由 :func:`_pose` 把 None 还原成 S 默认值，行为与从前一致。
    """
    g = p.add_argument_group("摆放参数（默认值 = 原版 S 的默认值，index.html:921–937）")
    g.add_argument("--x", type=float, default=None, metavar="米",
                   help="人物横向偏移（S.x，+右，默认 0）")
    g.add_argument("--d", type=float, default=None, metavar="米",
                   help="人物到相机的纵向距离（S.d，默认 4）")
    g.add_argument("--h", type=float, default=None, metavar="米",
                   help="相机离地高（S.h，默认 1.6）")
    g.add_argument("--hp", type=float, default=1.65, metavar="米",
                   help="人物身高（S.Hp，默认 1.65）")
    g.add_argument("--feq", type=float, default=23.0, metavar="mm",
                   help="等效 35mm 焦距（S.feq，默认 23）")
    g.add_argument("--pitch", type=float, default=None, metavar="度",
                   help="俯角（S.pitch，低头为正，默认 0）")
    g.add_argument("--roll", type=float, default=None, metavar="度",
                   help="滚转角（S.roll，画面顺时针，默认 0）")
    g.add_argument("--shot", default="全身", choices=list(SHOTS), metavar="景别",
                   help="景别：" + "，".join(f"{k}={v}" for k, v in SHOTS.items())
                        + "（下沿离地高度/身高，默认 全身）")
    g.add_argument("--char-scale", type=float, default=1.0, metavar="倍",
                   help="立绘倍率（S.charScale，只乘投影跨度，默认 1）")
    g.add_argument("--frame", default="满幅", choices=list(FRAMES), metavar="画幅",
                   help="出图画幅（FRAMES 中文 key，默认 满幅=整块画布）")
    g.add_argument("--rot-basis", default="world", choices=("world", "frame"),
                   help="立绘对正基准：world 跟世界竖直 / frame 永远画面竖直（S.rotBasis）")
    g.add_argument("--mode", default="ground", choices=("ground", "free"),
                   help="摆放模式：ground 贴地 / free 自由半身（S.mode）")
    g.add_argument("--size-pct", type=float, default=60.0, metavar="%",
                   help="自由模式立绘占画面高度百分比（S.sizePct，默认 60）")
    g.add_argument("--edge-bar", type=int, default=0, choices=(0, 1, 2, 3, 4),
                   metavar="0-4", help="框条吸附（S.edgeBar，0 不吸/1 上/2 下/3 左/4 右，"
                                       "仅自由模式，吸附时旋转锁 0）")
    g.add_argument("--tone", action="store_true",
                   help="色调匹配（原版 TONE_ON，只有照片路径生效，index.html:3690）")
    g.add_argument("--no-light", action="store_true",
                   help="关掉环境光烘焙（bakeLit；只出原色立绘）")
    g.add_argument("--no-grain", action="store_true",
                   help="关掉颗粒匹配（ bakeLit 里的颗粒那一段，光仍烘）")
    g.add_argument("--no-shadow", action="store_true",
                   help="关掉接触阴影（原版 LIGHT_SHD，index.html:1144）")
    g.add_argument("--shadow-az", type=float, default=0.0, metavar="度",
                   help="阴影光线方位角（默认 0 = 影子正落脚下，与原版一致；非 0 = "
                        "斜射拖偏。原版 2D 阴影无方向，这是离线侧新增控制）")
    g.add_argument("--shadow-opacity", type=_nonneg, default=1.0, metavar="倍",
                   help="阴影浓度倍率（默认 1.0，乘在原版 SH_ALPHA 上；0 = 不铺阴影）")
    g.add_argument("--grain-scale", type=_nonneg, default=1.0, metavar="倍",
                   help="颗粒强度倍率（默认 1.0 = 原版逐位一致；乘在 bakeLit 的 gAmp 上，"
                        "只改颗粒那段，曲线/白平衡/饱和不动；0 = 不撒颗粒）")


def _nonneg(value: float) -> float:
    """argparse ``type=``：非负浮点（0 合法、负数报用法错误 exit 2）。

    ``--shadow-opacity``/``--grain-scale`` 的 0 是有语义的合法值（关阴影 /
    关颗粒，网页两侧滑块最左档就是 0），负数则是静默失效：乘在 gAmp/SH_ALPHA
    上被夹成 0，出图与 0 逐字节相同、零提示（v1.9.0 对抗复核实测）。webapp.py
    对同两键本就 400，CLI 不能另立一套口径。
    """
    v = float(value)
    if v < 0.0:
        raise argparse.ArgumentTypeError(f"应 >= 0，得到 {v:g}")
    return v


def _add_lang(p: argparse.ArgumentParser) -> None:
    """语言开关（FEATURE-SPEC §3）：只影响人话输出，不改任何数值行为。"""
    p.add_argument("--lang", default="zh", choices=list(LANGS), metavar="语言",
                   help="人话输出语言：zh 中文 / en English（默认 zh；数值行为不变）")


#: ``--overlay`` 的合法层名与 **canonical 叠放次序**（grid → horizon → figure →
#: hud）——与原版取景区四个辅助开关的排列一致（网页侧 webui/index.html:127-129
#: 前三项 + HUD 读数，各自独立）。用户书写顺序不改变实际叠放次序：
#: ``--overlay hud,grid`` 仍按 grid+hud 叠。
OVERLAY_LAYERS = ("grid", "horizon", "figure", "hud")


def _overlay_layers(value) -> tuple[str, ...]:
    """``--overlay`` 的值 → canonical 序、去重（保首次出现）的层名元组。

    兼两职：build_parser 里当 argparse ``type=`` 用（坏值抛
    :class:`argparse.ArgumentTypeError` → 用法错误 exit 2，不抛 traceback）；
    ``_apply_overlay``/提示文案里当归一化用（顺带收裸字符串的 API 直调）。
    逗号分段、逐段去空白；空段与未知名都报错并列合法值；重复名去重。
    返回顺序恒为 :data:`OVERLAY_LAYERS` 的 canonical 序，与书写顺序无关。
    """
    if value is None:
        return ()
    parts = ([p.strip() for p in value.split(",")] if isinstance(value, str)
             else [str(p).strip() for p in value])
    if any(not p for p in parts):
        raise argparse.ArgumentTypeError(
            f"层名不能为空（--overlay 用逗号组合多层，如 grid,horizon；合法值："
            f"{'/'.join(OVERLAY_LAYERS)}；收到 {value!r}）")
    bad = [p for p in parts if p not in OVERLAY_LAYERS]
    if bad:
        raise argparse.ArgumentTypeError(
            f"未知层名 {'/'.join(bad)}（合法值：{'/'.join(OVERLAY_LAYERS)}）")
    return tuple(layer for layer in OVERLAY_LAYERS if layer in set(parts))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arcam",
        description="让她走出屏幕（Python 离线版）—— 透视投影立绘合成管线："
                    "出图 / 立绘层 / 素材包 / 抠图 / 装图修图 / 轨道视频 / 配方 /"
                    "批量 / 多立绘 / 对比网格 / LiDAR 深度 / 本地网页。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  arcam render photo.jpg char.png -o shot.png --scene scene.json
  arcam render photo.jpg char.png -o shot.png --depth scan_0001.png
  arcam char-layer char.png -o layer.png --mode free --size-pct 70
  arcam bundle photo.jpg char.png -o pack.zip --pitch 12
  arcam matte photo.jpg -o cut.png --model isnet-anime-q8.onnx
  arcam prep char.png -o clean.png            # 装图自动修图链（抠底/裁边/柔边/微糊）
  arcam video track.json char.png -o frames/ --bg bg/ --tone --gif
  arcam check track.json
  arcam recipe save demo --pitch 15 --mode free   # 配方存/列/看/取（apply 打可粘参数）
  arcam watch photo.jpg char.png -o shot.png --once
  arcam multi photo.jpg a.png b.png -o two.png --spec 'x=1.2;d=3.2;scale=0.7'
  arcam contact-sheet photo.jpg char.png -o grid.png --vary d=3,4,5 --vary pitch=0,15
  arcam batch photos/ char.png -o out/ --depth-dir depths/ --scene
  arcam stats                                    # 项目体检表（规模/测试/子命令）
  arcam lidar inspect scan_0001.png [--json]
  arcam web [--host 127.0.0.1] [--port 8765] [--no-open]   # 本地网页界面

退出码：0 正常，1 运行期失败（缺文件/素材不合法/轨道有问题），2 用法错误。""")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="命令")

    p = sub.add_parser("prep", help="立绘自动修图：纯色底抠图 + 裁边 + 柔边 + 镜头微糊",
                       description="装图后自动做的修图链（index.html:2400-2440 的 1:1 对译，"
                                   "docs/FEATURE-SPEC §1）：① 纯色底抠底（占比 5%~85% 门控）"
                                   "② 自动裁边 ③ 柔边 ④ 镜头微糊 ⑤ 半身判定。"
                                   "任一步失败静默跳过；动过手且内容 <8px 整体回退原图。"
                                   "半身结论会提示（那种图不能贴地，应走 --mode free）。")
    p.add_argument("char", metavar="CHAR", help="立绘（带 alpha 的 PNG 最佳；纯色底 JPG 也能抠）")
    _add_out(p)
    p.add_argument("--no-solid", action="store_true", help="跳过①纯色底抠底")
    p.add_argument("--no-trim", action="store_true", help="跳过②自动裁边")
    p.add_argument("--no-soften", action="store_true", help="跳过③柔边整理")
    p.add_argument("--no-blur", action="store_true", help="跳过④镜头微糊")
    _add_lang(p)
    p.set_defaults(func=cmd_prep)

    p = sub.add_parser("render", help="单张出图（可选顺带写 scene.json 配方）",
                       description="把立绘合成进照片，单张出图。"
                                   "--scene 给的路径会顺带写 scene.json（1:1 同构原版配方）。"
                                   "--depth 给 LiDAR 深度帧：相机高/俯仰/滚转/人物纵深改为"
                                   "深度测出，显式摆放参数仍优先（docs/LIDAR-SPEC §0/§4）。")
    p.add_argument("photo", metavar="PHOTO", help="背景/照片（JPEG/PNG/…）")
    p.add_argument("char", metavar="CHAR", help="立绘（带 alpha 的 PNG 最佳）")
    _add_out(p)
    p.add_argument("--scene", metavar="PATH", help="同时写 scene.json 到该路径")
    p.add_argument("--depth", metavar="DEPTH", default=None,
                   help="LiDAR 深度帧：16-bit PNG/TIF（毫米）/ .npy（米）/ .depth（magic "
                        "ARCD）/ 人像 HEIC；E1-E4 全开，显式参数仍优先")
    p.add_argument("--overlay", default=None, type=_overlay_layers,
                   metavar="层[,层...]",
                   help="出图后叠 HUD 取景装饰层，可用逗号组合多层（如 grid,horizon,figure；"
                        "合法值 grid/horizon/figure/hud，重复名只算一次）。实际叠放次序"
                        "恒为 grid→horizon→figure→hud（与原版取景四开关排列一致），"
                        "与书写顺序无关。默认不叠；原版那些只在实时预览里画，出图不带"
                        "——要排查构图时才开")
    _add_placement(p)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("recipe", help="摆放配方：存 / 列 / 看 / 取（参数预设库）",
                       description="把一组摆放参数存成命名配方，之后任意照片/立绘都能套。"
                                   "配方就是 scene.json 的摆放段（同字段同单位），存到 "
                                   "~/.arcam/recipes/NAME.json（$ARCAM_RECIPES 可改根目录）。"
                                   "apply 子命令打印可直接粘的 CLI 参数，也能 --set 临时改单项。")
    rsub = p.add_subparsers(dest="recipe_cmd", required=True, metavar="动作")
    rp = rsub.add_parser("save", help="存一个配方（当前 CLI 参数或 --from scene.json）")
    rp.add_argument("name", metavar="NAME", help="配方名（文件名安全字符）")
    rp.add_argument("--from", dest="from_file", metavar="SCENE", default=None,
                    help="从 scene.json 提取摆放段（不给则用当前命令行参数）")
    _add_placement(rp)
    rp.set_defaults(func=cmd_recipe_save)
    rsub.add_parser("list", help="列出所有配方").set_defaults(func=cmd_recipe_list)
    rp = rsub.add_parser("show", help="看一个配方的内容")
    rp.add_argument("name", metavar="NAME")
    rp.add_argument("--json", action="store_true", help="原样输出 JSON")
    rp.set_defaults(func=cmd_recipe_show)
    rp = rsub.add_parser("apply", help="生成可粘的 CLI 参数（或 --set 改单项后输出）")
    rp.add_argument("name", metavar="NAME")
    rp.add_argument("--set", action="append", default=[], metavar="K=V",
                    help="临时改一项（可多次）：--set pitch=15 --set mode=free")
    rp.set_defaults(func=cmd_recipe_apply)
    _add_lang(p)

    p = sub.add_parser("watch", help="监视文件夹：素材或轨道变了就自动重出图",
                       description="盯着 PHOTO（或 --track 轨道文件），改动落地后自动重跑出图——"
                                   "调参时不用反复敲命令。Ctrl-C 退出；--once 只跑一遍（等于带日志的 render）。")
    p.add_argument("photo", metavar="PHOTO", help="要监视的照片（改了它就重渲）")
    p.add_argument("char", metavar="CHAR", help="立绘（改了它也重渲）")
    _add_out(p)
    p.add_argument("--interval", type=float, default=1.0, metavar="秒",
                   help="轮询间隔（默认 1.0；文件系统事件在各平台可靠性不一，轮询最稳）")
    p.add_argument("--once", action="store_true", help="只跑一遍就退出（不进入监视循环）")
    p.add_argument("--overlay", default=None, type=_overlay_layers,
                   metavar="层[,层...]",
                   help="出图后叠 HUD 取景装饰层，可用逗号组合多层（如 grid,horizon；"
                        "合法值 grid/horizon/figure/hud，重复名只算一次）。实际叠放次序"
                        "恒为 grid→horizon→figure→hud（与原版取景四开关排列一致），"
                        "与书写顺序无关（默认不叠）")
    _add_placement(p)
    _add_lang(p)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("multi", help="多立绘合成：一张照片里站多个人（各带各的站位）",
                       description="按顺序把多张立绘叠进同一照片。每张立绘用 --spec 给一套"
                                   "站位覆盖（x/d/scale/shot/mode/size_pct/edge_bar/rot_basis），"
                                   "不给就用主参数。层与层之间 source-over 顺序合成，"
                                   "后画的盖先画的（和手工逐一合成等价）。")
    p.add_argument("photo", metavar="PHOTO", help="背景照片")
    p.add_argument("chars", metavar="CHAR", nargs="+", help="立绘（至少一张；多张按顺序叠）")
    _add_out(p)
    p.add_argument("--spec", action="append", default=[], metavar="K=V;K=V",
                   help="第 N 张立绘的站位覆盖（按 --spec 出现顺序对应第 2、3… 张；"
                        "第一张用主参数）。例：--spec 'x=0.6;d=3.2;scale=0.8'")
    p.add_argument("--scene", metavar="PATH", help="同时写 scene.json（记第一张的落位）")
    _add_placement(p)
    _add_lang(p)
    p.set_defaults(func=cmd_multi)

    p = sub.add_parser("stats", help="项目自报：规模/测试/功能矩阵一句话",
                       description="数一数源码行数、测试条数、子命令与模块，打印项目体检表。"
                                   "给 README 和发布说明用，免手工维护。")
    p.add_argument("--json", action="store_true", help="输出 JSON（脚本好用）")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("contact-sheet", help="参数对比网格：同一构图扫一组参数出一张总览图",
                       description="把同一照片/立绘在一组参数取值下各渲一张，拼成带标注的"
                                   "对比网格——调参时肉眼挑最满意的那一档，比逐张出图快。"
                                   "--vary 可给多次（笛卡尔积，上限 24 格防止手滑）。")
    p.add_argument("photo", metavar="PHOTO", help="背景照片")
    p.add_argument("char", metavar="CHAR", help="立绘")
    _add_out(p)
    p.add_argument("--vary", action="append", required=True, metavar="K=V,V,V",
                   help="要扫的参数与取值：--vary d=3,4,5 --vary pitch=0,15,30")
    p.add_argument("--cols", type=int, default=0, metavar="N",
                   help="每行几格（默认按格数自动开方）")
    p.add_argument("--cell-w", type=int, default=480, metavar="PX",
                   help="单格宽（默认 480；高按画幅等比）")
    _add_placement(p)
    _add_lang(p)
    p.set_defaults(func=cmd_contact_sheet)

    p = sub.add_parser("batch", help="批量出图：一个文件夹的照片 × 同一立绘同一组参数",
                       description="把 PHOTO_DIR 里每张照片按同一组摆放参数各出一张图。"
                                   "适用于「同一机位拍一批、要一致合成」的场合：参数只调一遍，"
                                   "批量套用。--depth-dir 给同名深度帧时逐张匹配（可选）。"
                                   "输出 OUTDIR/shot_<照片名>.<ext>，每张顺带写 scene.json。")
    p.add_argument("photo_dir", metavar="PHOTO_DIR", help="照片目录（jpg/png/webp…）")
    p.add_argument("char", metavar="CHAR", help="立绘（带 alpha 的 PNG 最佳）")
    p.add_argument("-o", "--out", required=True, metavar="OUTDIR", help="输出目录")
    p.add_argument("--depth-dir", metavar="DIR", default=None,
                   help="深度帧目录（与照片同名不同后缀时按 stem 匹配；可选）")
    p.add_argument("--ext", default="png", choices=("png", "jpg"), metavar="格式",
                   help="输出格式：png（默认，无损）/ jpg（q95，文件小）")
    p.add_argument("--scene", action="store_true",
                   help="每张顺带写 scene.json（默认不写；批量时配方意义不大）")
    _add_placement(p)
    _add_lang(p)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("char-layer", help="仅输出 RGBA 立绘层（和照片逐像素对应）",
                       description="不铺背景、只出摆好位的透明立绘层，"
                                   "直接盖在对应 shot.jpg 上严丝合缝（index.html:3709）。")
    p.add_argument("char", metavar="CHAR", help="立绘（带 alpha 的 PNG 最佳）")
    _add_out(p)
    p.add_argument("--width", type=int, default=1080, metavar="px", help="层画布宽（默认 1080）")
    p.add_argument("--height", type=int, default=1920, metavar="px", help="层画布高（默认 1920）")
    _add_placement(p)
    p.set_defaults(func=cmd_char_layer)

    p = sub.add_parser("bundle", help="素材包 zip：shot.jpg(q0.95) + scene.json + character.png",
                       description="三件套打包（原版导出素材包同款，index.html:3820）。"
                                   "zip 内时间戳固定 1980-01-01，确定性复现。")
    p.add_argument("photo", metavar="PHOTO", help="背景/照片（JPEG/PNG/…）")
    p.add_argument("char", metavar="CHAR", help="立绘（带 alpha 的 PNG 最佳）")
    _add_out(p)
    _add_placement(p)
    p.set_defaults(func=cmd_bundle)

    p = sub.add_parser("matte", help="ISNet-anime 抠图（MODEL 可透传）",
                       description="纯前端同款抠图（cutout.js:670–747 animeCut）。"
                                   "模型缺失时错误信息会带下载指引。")
    p.add_argument("image", metavar="IMG", help="输入图片")
    _add_out(p)
    p.add_argument("--model", metavar="PATH", default=None,
                   help="isnet-anime-q8.onnx 路径；缺省按 参数→$ARCAM_ISNET_MODEL→"
                        "仓库 anime/→~/.cache/arcam/ 顺序解析")
    p.set_defaults(func=cmd_matte)

    p = sub.add_parser("video", help="按位姿轨道逐帧渲染出帧序列",
                       description="track.json 定义时间轴与关键帧，逐帧走同一条 composeShot 管线；"
                                   "--bg 给单图（静止背景）或帧目录（按文件名排序）。")
    p.add_argument("track", metavar="TRACK.json", help="轨道文件")
    p.add_argument("char", metavar="CHAR", help="立绘（带 alpha 的 PNG 最佳）")
    p.add_argument("-o", "--out", required=True, metavar="OUTDIR", help="输出目录")
    p.add_argument("--bg", metavar="IMG_OR_DIR", default=None,
                   help="背景：单张图片（每帧复用）或帧目录（frame_000001.png…顺序）；必填")
    p.add_argument("--tone", action="store_true", help="色调匹配开关")
    p.add_argument("--gif", action="store_true",
                   help="追加写动画 GIF（video.gif，纯 PIL 零依赖；256 色逐帧自适应）")
    p.set_defaults(func=cmd_video)

    p = sub.add_parser("check", help="轨道合法性自检",
                       description="读轨道并自检（时间戳单调、参数范围），打印摘要；"
                                   "合法退出 0，发现任一问题退出 1。")
    p.add_argument("track", metavar="TRACK.json", help="轨道文件")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("lidar", help="LiDAR 深度：环境量诊断 / 实时流接收",
                       description="iPhone 激光雷达深度接入的离线工具（契约见 "
                                   "docs/LIDAR-SPEC.md）：inspect 读一帧深度、拟合地面平面、"
                                   "报告相机环境量；serve 收实时流帧（协议见 spec §6）。")
    lidar_sub = p.add_subparsers(dest="lidar_cmd", required=True, metavar="子命令")

    pi = lidar_sub.add_parser(
        "inspect", help="读深度帧并拟合地面平面，报告相机环境量",
        description="load_depth → fit_ground_plane → camera_env_from_plane；"
                    "打印 h/pitch/roll、平面 inliers/rms、有效覆盖率。"
                    "--json 输出同构 dict（同一组字段，便于脚本消费）。")
    pi.add_argument("depth", metavar="DEPTH",
                    help="深度文件：16-bit PNG/TIF（毫米）/ .npy（米）/ .depth（ARCD）/ 人像 HEIC")
    pi.add_argument("--json", action="store_true",
                    help="输出 JSON（与文本同一组字段）而不是人话文本")
    pi.set_defaults(func=cmd_lidar_inspect)

    ps = lidar_sub.add_parser(
        "serve", help="实时流接收（TCP，协议见 docs/LIDAR-SPEC §6）",
        description="实时接收 iPhone 端推来的深度帧（arcam.lidar_live.serve，"
                    "LIDAR-SPEC §6 协议）：逐帧打印一行摘要，Ctrl-C 停止；"
                    "--out 给目录时同步落盘 frame_%%06d.depth。模块缺失才提示退出 0。")
    ps.add_argument("--port", type=int, default=7707, metavar="端口",
                    help="监听端口（默认 7707）")
    ps.add_argument("--out", metavar="DIR", default=None,
                   help="帧序列输出目录（默认不落盘）")
    ps.set_defaults(func=cmd_lidar_serve)

    pg = lidar_sub.add_parser(
        "sim", help="合成深度帧：落盘 .depth 序列，或推给正在监听的 serve",
        description="按已知真值（相机高/俯角/滚转）合成深度相机坐标的地面点云——"
                    "没有真机时验证整条 LiDAR 链路（sim → serve → inspect → "
                    "render --depth）。默认把帧写进 --out 目录（frame_%%06d.depth，"
                    "ARCD 格式，内参外参齐备）；--push 给端口时改为推给正在跑的 "
                    "arcam lidar serve。噪声由 (seed, index) 播种 → 同参同帧可复现。")
    pg.add_argument("--h", type=float, default=1.6, metavar="米",
                    help="相机离地高（真值，默认 1.6）")
    pg.add_argument("--pitch", type=float, default=12.0, metavar="度",
                    help="俯角（真值，默认 12）")
    pg.add_argument("--roll", type=float, default=8.0, metavar="度",
                    help="滚转角（真值，默认 8）")
    pg.add_argument("--width", type=int, default=256, metavar="PX",
                    help="深度帧宽（默认 256）")
    pg.add_argument("--height", type=int, default=192, metavar="PX",
                    help="深度帧高（默认 192）")
    pg.add_argument("--frames", type=int, default=5, metavar="N",
                    help="合成几帧（默认 5）")
    pg.add_argument("--seed", type=int, default=0, metavar="N",
                    help="随机种子（默认 0；同参同种子同帧可复现）")
    pg.add_argument("--out", metavar="DIR", default=None,
                    help="帧序列输出目录（frame_%%06d.depth）；与 --push 二选一")
    pg.add_argument("--push", type=int, default=None, metavar="端口",
                    help="推给 127.0.0.1:端口 上正在监听的 arcam lidar serve（不落盘）")
    pg.add_argument("--json", action="store_true",
                    help="只打印第一帧的 meta（JSON），不写盘不推流")
    pg.set_defaults(func=cmd_lidar_sim)

    p = sub.add_parser("web", help="本地网页界面（标准库 HTTP 服务，仅本机回环）",
                       description="起本机回环（默认 127.0.0.1:8765）网页服务：照片/立绘上传、"
                                   "预览渲染（摆放参数全部可调）、抠图、LiDAR 深度诊断与深度"
                                   "驱动渲染、成品下载（PNG / 立绘层 / 素材包 zip）。"
                                   "零第三方 web 框架，数据只在内存、进程退出即清空；"
                                   "契约见 docs/WEB-SPEC.md。")
    p.add_argument("--host", default="127.0.0.1", metavar="地址",
                   help="监听地址（默认 127.0.0.1，只绑本机回环，不做网络暴露）")
    p.add_argument("--port", type=int, default=8765, metavar="端口",
                   help="监听端口（默认 8765；被占用时换个端口）")
    p.add_argument("--no-open", action="store_true",
                   help="不自动打开浏览器（自己贴地址）")
    p.add_argument("--open", action="store_true",
                   help="自动打开浏览器（默认行为；启动器显式传入）")
    p.set_defaults(func=cmd_web)
    return parser


# ---------- 内部小工具 ----------

def _need(path, what: str) -> Path:
    p = Path(path)
    if not p.is_file():
        raise CliError(f"找不到{what}：{p}")
    return p


#: 原版 S 的默认值（index.html:921-937）—— 这些开关 argparse 默认 None，
#: 在 _pose 里还原成原版默认，"is None" 才是显式判定口径（见 _add_placement）。
_POSE_DEFAULTS = {'h': 1.6, 'd': 4.0, 'x': 0.0, 'pitch': 0.0, 'roll': 0.0}


def _pose(args, width: int, height: int) -> tuple[Camera, Subject]:
    h = _POSE_DEFAULTS['h'] if args.h is None else args.h
    d = _POSE_DEFAULTS['d'] if args.d is None else args.d
    x = _POSE_DEFAULTS['x'] if args.x is None else args.x
    pitch = _POSE_DEFAULTS['pitch'] if args.pitch is None else args.pitch
    roll = _POSE_DEFAULTS['roll'] if args.roll is None else args.roll
    # feq<=0 会让 focal=0，place 里 up/focal 直接 ZeroDivisionError（A1 X2）；
    # NaN/Inf 则静默污染整条投影链。在这里一次性挡成人话错误。
    if not math.isfinite(args.feq) or args.feq <= 0:
        raise CliError(f"--feq 必须是有限正数（等效焦距 mm，收到 {args.feq!r}）")
    cam = Camera(width, height, h, args.feq, pitch, roll)
    subj = Subject(args.hp, d, x)
    return cam, subj


def _env_overrides(args) -> dict:
    """用户**显式**给过的环境量（LIDAR-SPEC §0 优先级链的"显式"端）。

    只收 ``_add_placement`` 里默认 None 的那五个开关；argparse 没给过 = 键不存在。
    compose 侧收 override dict，不解析 argv（契约见 docs/LIDAR-SPEC §4）。
    """
    ov: dict = {}
    for key in ('h', 'pitch', 'roll', 'd', 'x'):
        value = getattr(args, key, None)
        if value is not None:
            ov[key] = float(value)
    return ov


def _load_depth_arg(path, what: str):
    """懒加载 LiDAR 深度帧；模块缺失 / 文件读不懂 → 人话 CliError（退出 1）。"""
    p = _need(path, what)
    try:
        from arcam.depth import load_depth
    except ImportError as exc:
        raise CliError(
            f"读不到 LiDAR 深度模块：{exc}（arcam.depth 应随包一起提供；"
            "若安装被裁剪过，契约见 docs/LIDAR-SPEC.md §2）") from None
    try:
        return load_depth(p)
    except ValueError as e:
        raise CliError(str(e)) from None


def _lidar_env_of(depth):
    """fit_ground_plane → camera_env_from_plane（LIDAR-SPEC §4①）。

    懒加载 arcam.plane：模块缺失或点数不够（ValueError）都转人话 CliError。
    """
    try:
        from arcam.plane import camera_env_from_plane, fit_ground_plane
    except ImportError as exc:
        raise CliError(
            f"读不到 LiDAR 平面拟合模块：{exc}（arcam.plane 应随包一起提供；"
            "若安装被裁剪过，契约见 docs/LIDAR-SPEC.md §3）") from None
    plane = fit_ground_plane(depth)
    return camera_env_from_plane(plane, roll_deg=depth.roll_deg)


def _compose_kw(args) -> dict:
    return dict(frame_key=args.frame, mode=args.mode, size_pct=args.size_pct,
                char_scale=args.char_scale, rot_basis=args.rot_basis,
                tone=args.tone, shot=args.shot, edge_bar=args.edge_bar,
                shadow=not getattr(args, "no_shadow", False),
                grain=not getattr(args, "no_grain", False),
                sample_light=not _light_off(args),
                shadow_az=getattr(args, "shadow_az", 0.0),
                shadow_opacity=getattr(args, "shadow_opacity", 1.0),
                grain_scale=getattr(args, "grain_scale", 1.0))


def _light_off(args) -> bool:
    """--no-light：关掉采光（light=None → bakeLit 原样返回）。"""
    return bool(getattr(args, "no_light", False))


def _placement(char01, subj: Subject, cam: Camera, args):
    """单独算一次落位给 build_scene 用（与渲染链 :func:`arcam.compose._placement`
    **同参**——scene.json 记的必须是本遍实际落位，否则配方和出图对不上）。

    只传当前模式收得下的 kwargs：ground 分支不收 size_pct/edge_bar/rect、
    free 分支不收 char_scale/shot（签名见 SPEC §3.2 的两个分支）。
    ⚠ free 分支必须收 ``rect=frame_rect(...)``：贴边吸附的目标框（原版 snapRect）。
      漏传它 = 吸附在 scene.json 里整个失效（渲染吸了、配方记没吸）——v1.5 审计
      由 B1/B4 两个分区各自 e2e 复现（1:1+edge_bar=2 差 48px、16:9+edge_bar=2
      差 189px），凡 free + 非满幅 + edge_bar≠0 的 render/char-layer/bundle/
      multi 全部中招。webapp 侧（webapp.py:723）v1.1 就已修对，这里补齐。
    shot/edge_bar/rot_basis/rect 必须与 _compose_kw 一致，改动本函数务必与
    _compose_kw 对拍（这四个历史上各漏过一次）。
    """
    if args.mode == "free":
        return current_placement(char01, subj, cam, mode="free",
                                 size_pct=args.size_pct, edge_bar=args.edge_bar,
                                 rot_basis=args.rot_basis,
                                 rect=frame_rect(args.frame, cam.width, cam.height))
    return current_placement(char01, subj, cam, mode="ground",
                             char_scale=args.char_scale, rot_basis=args.rot_basis,
                             shot=args.shot)


def _scene_dict(args, char01, subj: Subject, cam: Camera, rect, placement,
                char_name: str | None = None) -> dict:
    """buildScene 的 1:1 同构调用（index.html:3729）。

    shot/char_scale 必须透传：buildScene 把它们记进配方（index.html:3756/3760），
    漏传会让 scene.json 与实际渲染参数不符。

    ``char_name`` 给多立绘命令用：它的位置参数叫 ``chars``（没有 ``args.char``），
    由调用方把第一张立绘的文件名传进来（v1.5 审计修复前 ``multi --scene`` 直接
    AttributeError 崩掉）。
    """
    return build_scene(rect, placement, subj, cam, frame_key=args.frame,
                       char_rgba01=char01,
                       char_name=char_name if char_name is not None
                       else Path(args.char).name,
                       mode=args.mode, edge_bar=args.edge_bar,
                       shot=args.shot, char_scale=args.char_scale)


# ---------- 子命令 ----------

def cmd_render(args) -> int:
    photo = _need(args.photo, "背景照片")
    char_p = _need(args.char, "立绘")
    src01 = image_to_rgb01(photo)
    char01 = image_to_rgba01(char_p)
    height, width = src01.shape[:2]
    cam, subj = _pose(args, width, height)
    depth = _load_depth_arg(args.depth, "深度文件") if args.depth else None
    kw = _compose_kw(args)
    if depth is not None:
        env = _lidar_env_of(depth)
        cam_eff, subj_eff, notes = _merge_env(cam, subj, _env_overrides(args), env,
                                              depth=depth, mode=args.mode,
                                              shot=args.shot)
        # compose_shot 内部会再合并一次：这里把 effective 值全量显式喂进去，
        # 两遍结果逐位一致（E3 在显式 d/x 存在时被覆盖），scene.json 记的
        # 也就是这一遍实际落位用的值
        kw.update(depth=depth, env_overrides={
            'h': cam_eff.height_m, 'pitch': cam_eff.pitch_deg,
            'roll': cam_eff.roll_deg, 'd': subj_eff.distance_m,
            'x': subj_eff.lateral_m})
        conflicts = [k for k in ('h', 'pitch', 'roll', 'd')
                     if notes[k]['source'] == 'explicit']
        suffix = (f"（显式值优先：{'、'.join(conflicts)}）" if conflicts
                  else "（已用于摆位）")
        print(f"深度测得 h={env.height_m:.3f}m pitch={env.pitch_deg:.2f}° "
              f"roll={env.roll_deg:.2f}°{suffix}")
    else:
        cam_eff, subj_eff = cam, subj
    shot = compose_shot(src01, char01, subj, cam, **kw)
    out = Path(args.out)
    # compose_shot 出 uint8 (0..255)；浮点口径也收（clip+四舍五入到字节），
    # 与 video._to_pil 的量化时点一致，避免双重量化把图洗白。
    base_arr = np.asarray(shot.shot)
    if base_arr.dtype == np.uint8:
        img_u8 = base_arr
    else:
        img_u8 = np.clip(base_arr.astype(np.float64) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    overlay_layers = _overlay_layers(args.overlay)
    overlay_note = ""
    if overlay_layers:
        # HUD 按 canonical 序逐层叠（原版出图不带 HUD，这是排查构图的显式开关）
        img_u8, overlay_note = _apply_overlay(
            img_u8, args, cam_eff, subj_eff, width, height,
            placement=_placement(char01, subj_eff, cam_eff, args))
    save_png(img_u8, out)
    # 提示里列**实际叠的层序**（canonical 序，不是用户书写序）；单层时与从前逐字相同
    print(f"已出图 {out}（{width}×{height}）"
          + (f"｜已叠 HUD 层：{'+'.join(overlay_layers)}{overlay_note}"
             if overlay_layers else ""))
    if args.scene:
        scene_path = Path(args.scene)
        if scene_path.parent and not scene_path.parent.exists():
            scene_path.parent.mkdir(parents=True, exist_ok=True)
        save_scene(_scene_dict(args, char01, subj_eff, cam_eff, shot.rect,
                               _placement(char01, subj_eff, cam_eff, args)), scene_path)
        print(f"配方 {scene_path}（version 1，与原版 scene.json 同构）")
    return 0




# ─────────────────────────── 配方（recipe） ───────────────────────────

#: 配方根目录：默认 ~/.arcam/recipes，$ARCAM_RECIPES 可改（数据仍在本机）。
RECIPE_ROOT = Path(os.environ.get("ARCAM_RECIPES",
                                  str(Path.home() / ".arcam" / "recipes")))

#: 配方字段 → CLI 旗标（apply 打印用；None = 不该出现在 CLI 里的内部字段）
_RECIPE_FLAGS = {
    "h": "--h", "hp": "--hp", "d": "--d", "x": "--x", "feq": "--feq",
    "pitch": "--pitch", "roll": "--roll", "shot": "--shot",
    "char_scale": "--char-scale", "frame": "--frame", "mode": "--mode",
    "rot_basis": "--rot-basis", "size_pct": "--size-pct",
    "edge_bar": "--edge-bar",
}


def _recipe_path(name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    if not safe:
        raise CliError(f"配方名 {name!r} 不含可用字符（字母数字/-/_）")
    return RECIPE_ROOT / f"{safe}.json"


def _recipe_of_args(args) -> dict:
    """从命令行参数摘出摆放段（只收配方字段，其余一律不进）。"""
    out = {}
    for key in _RECIPE_FLAGS:
        v = getattr(args, key, None)
        if v is not None:
            out[key] = v
    return out


def _recipe_from_scene(path: Path) -> dict:
    """从 scene.json 提摆放段（字段名与配方一致，单位一致）。

    ⚠ 原版配方（index.html:3729-3789 buildScene）**不记** ``sizePct`` 与
    ``rotBasis`` 两个量，只能尽量反推：
    - ``size_pct``：无从反推（``character.image`` 记的是立绘 PNG 的像素尺寸，
      与"占画面高百分之几"不是一回事）→ 恒为 None，配方里就不带这一项；
    - ``rot_basis``：frame 基准下 ``rotationDeg`` 恒 0，world 基准下它等于 roll。
      所以**只有 roll≠0 时可判定**（roll=0 时两种基准渲染逐位相同，回落 world
      无损）；吸附（edge_bar≠0）本身也把旋转锁成 0，与基准无关，推断成 frame
      也不会改变渲染结果。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cam = data.get("camera", {})
    subj = data.get("subject", {})
    ch = data.get("character", {})
    rot_zero = ch.get("rotationDeg") == 0
    roll_given = cam.get("rollDeg") not in (None, 0)
    out = {
        "h": cam.get("heightM"), "hp": subj.get("heightM"),
        "d": subj.get("distanceM"), "x": subj.get("lateralM"),
        "feq": cam.get("focal35mm"), "pitch": cam.get("pitchDeg"),
        "roll": cam.get("rollDeg"), "shot": data.get("shot"),
        "char_scale": data.get("charScale"), "frame": data.get("frame", {}).get("aspect"),
        "mode": ch.get("mode"),
        "size_pct": None,
        "edge_bar": data.get("edge", {}).get("bar"),
        "rot_basis": "frame" if (rot_zero and roll_given) else "world",
    }
    return {k: v for k, v in out.items() if v is not None}


def cmd_recipe_save(args) -> int:
    if args.from_file:
        p = _need(args.from_file, "scene.json")
        recipe = _recipe_from_scene(p)
    else:
        recipe = _recipe_of_args(args)
    if not recipe:
        raise CliError("配方是空的：给摆放参数（--pitch 12 等）或用 --from scene.json")
    path = _recipe_path(args.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": args.name, "params": recipe},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"配方已存 {path}（{len(recipe)} 项）")
    print("  " + " ".join(f"{_RECIPE_FLAGS[k]}={v}" for k, v in recipe.items()
                          if k in _RECIPE_FLAGS))
    return 0


def cmd_recipe_list(args) -> int:
    root = RECIPE_ROOT
    if not root.is_dir() or not any(root.glob("*.json")):
        print("（还没有配方——用 arcam recipe save NAME --pitch 12 存一个）")
        return 0
    for p in sorted(root.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            params = data.get("params", {})
        except Exception:
            print(f"  {p.stem}（读不出来：{p.name}）")
            continue
        brief = " ".join(f"{k}={v}" for k, v in list(params.items())[:5])
        print(f"  {p.stem:<20} {len(params):>2} 项  {brief}")
    return 0


def cmd_recipe_show(args) -> int:
    path = _recipe_path(args.name)
    if not path.is_file():
        raise CliError(f"没有叫 {args.name} 的配方（arcam recipe list 看现有）")
    data = json.loads(path.read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"配方 {args.name}（{path}）")
    for k, v in data.get("params", {}).items():
        flag = _RECIPE_FLAGS.get(k, k)
        print(f"  {flag:<16} {v}")
    return 0


def cmd_recipe_apply(args) -> int:
    path = _recipe_path(args.name)
    if not path.is_file():
        raise CliError(f"没有叫 {args.name} 的配方（arcam recipe list 看现有）")
    params = dict(json.loads(path.read_text(encoding="utf-8")).get("params", {}))
    for item in args.set:
        if "=" not in item:
            raise CliError(f"--set 要 K=V 形式，收到 {item!r}")
        k, v = item.split("=", 1)
        if k not in _RECIPE_FLAGS:
            raise CliError(f"配方里没有 {k!r} 这一项（可用：{'/'.join(_RECIPE_FLAGS)}）")
        params[k] = v
    flags = []
    for k, v in params.items():
        flag = _RECIPE_FLAGS.get(k)
        if flag:
            flags.append(f"{flag} {v}")
    print(" ".join(flags))
    return 0


#: 对比网格上限（笛卡尔积格数；防 --vary 给太宽把内存/时间炸掉）
_SHEET_MAX_CELLS = 24


def _parse_vary(items: list[str]) -> list[tuple[str, list[str]]]:
    """--vary K=V,V,V → [(key, [values...])]；键必须在摆放参数里。"""
    axes: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for item in items:
        if "=" not in item:
            raise CliError(f"--vary 要 K=V,V 形式，收到 {item!r}")
        key, raw = item.split("=", 1)
        key = key.strip()
        valid = {"x", "d", "h", "hp", "feq", "pitch", "roll", "shot",
                 "char_scale", "frame", "mode", "size_pct", "edge_bar",
                 "rot_basis"}
        if key not in valid:
            raise CliError(f"--vary 的键 {key!r} 不是摆放参数（可用：{'/'.join(sorted(valid))}）")
        if key in seen:
            # 同一个参数扫两轴：笛卡尔积照算，但后一轴把前一轴的值整个覆写，
            # 格子成倍重复而标签看不出问题——直接拦掉
            raise CliError(f"--vary 的键 {key!r} 给了两次（同一参数只应扫一轴）")
        seen.add(key)
        values = [v.strip() for v in raw.split(",") if v.strip()]
        if not values:
            raise CliError(f"--vary {key} 没给取值")
        axes.append((key, values))
    return axes



# ─────────────────────────── watch / multi / stats ───────────────────────────

def _apply_overlay(img_u8: np.ndarray, args, cam: Camera, subj: Subject,
                   width: int, height: int, placement=None) -> tuple[np.ndarray, str]:
    """按 ``--overlay`` 逐层叠 HUD 取景装饰，返回 (画后的图, 附加提示文案)。

    原版出图**不带** HUD（index.html:3487-3490：那些只在实时预览的 draw() 里画），
    这是排查构图的显式开关。``--overlay`` 可逗号组合多层（如 ``grid,horizon``），
    四层（grid/horizon/figure/hud）各自 1:1 对译 :mod:`arcam.hud`，按
    :data:`OVERLAY_LAYERS` 的 canonical 序依次叠——书写顺序不影响叠放次序；
    render 与 watch 共用本函数——曾有两份逐字重复的分支，改一处漏一处
    （v1.8 审计收敛）。

    ``hud`` 层的构图提示必须喂**本遍实际落位**（placement）与 ``--shot/--mode``：
    不喂的话三分线/顶满/偏小/浮空那几条永远不触发（原版 compositionHints 读的
    就是当前摆位）。``--lang`` 同时决定 HUD 文案语言（地平线标签走 i18n 的
    ``horizon`` 键，中文下与原来的硬编码字符串逐字相同）。
    """
    from arcam.hud import draw_figure, draw_grid, draw_horizon, draw_hud_text
    lang = getattr(args, "lang", "zh")
    canvas = img_u8.copy()
    note = ""
    for layer in _overlay_layers(args.overlay):
        if layer == "grid":
            canvas = draw_grid(canvas, cam, subj.lateral_m)
        elif layer == "horizon":
            canvas = draw_horizon(canvas, cam, t("horizon", lang=lang))
        elif layer == "figure":
            canvas = draw_figure(canvas, cam, subj)
        else:  # hud
            canvas, hints = draw_hud_text(canvas, cam, subj, has_char=True,
                                          canvas_size=(width, height), lang=lang,
                                          shot=args.shot, mode=args.mode,
                                          placement=placement)
            if hints:
                note = "；构图提示：" + "；".join(hints)
    return canvas, note


def _render_one(args, photo_p: Path, char01, out: Path, overlay_note: list) -> int:
    """render 的核心一遍（watch/multi 共用）：返回退出码。"""
    src01 = image_to_rgb01(photo_p)
    height, width = src01.shape[:2]
    cam, subj = _pose(args, width, height)
    kw = _compose_kw(args)
    shot = compose_shot(src01, char01, subj, cam, **kw)
    base_arr = np.asarray(shot.shot)
    img_u8 = base_arr if base_arr.dtype == np.uint8 else \
        np.clip(base_arr.astype(np.float64) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    if getattr(args, "overlay", None):
        img_u8, note = _apply_overlay(img_u8, args, cam, subj, width, height,
                                      placement=_placement(char01, subj, cam, args))
        if note:
            overlay_note.append(note)
    save_png(img_u8, out)
    return 0


def _sig_of(paths: list[Path]) -> tuple:
    """(路径, mtime_ns, size) 签名——变了就重渲。"""
    sig = []
    for p in paths:
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(p), None, None))
    return tuple(sig)


def cmd_watch(args) -> int:
    """监视素材变化自动重出图（轮询式；跨平台最稳）。

    ⚠ 人话输出目前全是中文：``--lang`` 收下但语言表里没有监视循环的键
    （加键会牵动 webui/docs 两侧同源表，见审计报告）。
    """
    photo_p = _need(args.photo, "背景照片")
    char_p = _need(args.char, "立绘")
    out = Path(args.out)
    watched = [photo_p, char_p]
    note: list = []

    def run_once(reason: str) -> None:
        t0 = time.perf_counter()
        try:
            char01 = image_to_rgba01(char_p)
            note.clear()
            _render_one(args, photo_p, char01, out, note)
            ms = (time.perf_counter() - t0) * 1000.0
            tail = ("｜" + note[0]) if note else ""
            print(f"[{_stamp()}] {reason} → {out.name}（{ms:.0f} ms{tail}）", flush=True)
        except Exception as exc:
            print(f"[{_stamp()}] {reason} → 失败：{exc}", flush=True)

    run_once("首次")
    if args.once:
        return 0
    print(f"监视中（{photo_p.name} / {char_p.name}，每 {args.interval:g}s 轮询）—— Ctrl-C 退出",
          flush=True)
    last = _sig_of(watched)
    try:
        while True:
            time.sleep(max(0.2, args.interval))
            now = _sig_of(watched)
            if now != last:
                changed = [Path(a[0]).name for a, b in zip(now, last) if a != b]
                run_once("变了：" + "、".join(changed))
                last = _sig_of(watched)
    except KeyboardInterrupt:
        print("\n已停止监视。")
    return 0


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


#: multi 的 spec 键 → 参数名（与摆放参数同名；scale 是 char_scale 的简写）
_MULTI_SPEC_KEYS = {
    "x": "x", "d": "d", "h": "h", "hp": "hp", "feq": "feq",
    "pitch": "pitch", "roll": "roll", "shot": "shot",
    "scale": "char_scale", "char_scale": "char_scale", "frame": "frame",
    "mode": "mode", "size_pct": "size_pct", "edge_bar": "edge_bar",
    "rot_basis": "rot_basis", "tone": "tone",
}


def _parse_spec(text: str) -> dict:
    """'x=0.6;d=3.2;scale=0.8' → {x:0.6, d:3.2, char_scale:0.8}（人话报错）。"""
    out: dict = {}
    for item in text.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise CliError(f"spec 段 {item!r} 不是 K=V 形式")
        k, v = item.split("=", 1)
        k = k.strip()
        if k not in _MULTI_SPEC_KEYS:
            raise CliError(f"spec 键 {k!r} 不认识（可用：{'/'.join(sorted(_MULTI_SPEC_KEYS))}）")
        key = _MULTI_SPEC_KEYS[k]
        if key in ("shot", "frame", "mode", "rot_basis"):
            out[key] = v.strip()
        elif key == "tone":
            out[key] = v.strip().lower() in ("1", "true", "yes", "on")
        elif key == "edge_bar":
            out[key] = int(v)
        else:
            out[key] = float(v)
    return out


class _SpecArgs:
    """把 spec dict 套到主 args 上得到的替身（只覆盖 spec 里有的键）。"""

    def __init__(self, base, overrides: dict):
        self._base = base
        self._over = overrides

    def __getattr__(self, name):
        if name in self._over:
            return self._over[name]
        return getattr(self._base, name)


def _paste_layer(base_rgb01: np.ndarray, layer_rgba01: np.ndarray, rect) -> np.ndarray:
    """把裁切框里的立绘层按 ``R.x/R.y`` 贴回整幅照片（compose._crop_offset 的逆）。

    compose_char_layer 出来的层尺寸是**裁切后**的 off 画布（与素材包里那张
    character.png 一致），不是整幅。满幅时两者恰好同尺寸、偏移 (0,0)，非满幅时
    不按偏移贴就会形状不匹配——v1.5 审计修复前 ``multi --frame 4:5`` 直接 numpy
    广播报错（且报的是内部形状，不是人话）。
    """
    height, width = base_rgb01.shape[:2]
    full = np.zeros((height, width, 4), dtype=np.float64)
    ox, oy = js_round(rect.x), js_round(rect.y)
    h, w = int(layer_rgba01.shape[0]), int(layer_rgba01.shape[1])
    x0, y0 = max(0, ox), max(0, oy)
    x1, y1 = min(width, ox + w), min(height, oy + h)
    if x1 > x0 and y1 > y0:
        full[y0:y1, x0:x1] = layer_rgba01[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
    return _over(base_rgb01, full)


def cmd_multi(args) -> int:
    """多立绘合成：逐张出立绘层，按顺序 source-over 叠到照片上。

    为什么不直接改 compose 画多个人：compose_shot 的契约是「一张立绘一套
    落位」（六步管线、char_layer 语义都建立在单一主体上）。多立绘 = 多次调用
    + 层合成，数学上与「一气画多个人」等价（source-over 对 alpha 可结合），
    且不动冻结的单主体管线。

    ⚠ 本命令的人话输出目前全是中文：``--lang`` 收下但无处可用（语言表里没有
    多立绘的键，加键会牵动 webui/docs 两侧同源表，见审计报告）。
    """
    if len(args.spec) > max(0, len(args.chars) - 1):
        raise CliError(f"--spec 给了 {len(args.spec)} 套但只有 {len(args.chars) - 1} 张"
                       "附加立绘需要覆盖（第一张用主参数）")
    photo_p = _need(args.photo, "背景照片")
    src01 = image_to_rgb01(photo_p)
    height, width = src01.shape[:2]

    base = np.clip(np.asarray(src01, dtype=np.float64), 0.0, 1.0)
    layers = []
    for i, char_path in enumerate(args.chars):
        char_p = _need(char_path, f"立绘 {i + 1}")
        char01 = image_to_rgba01(char_p)
        # 第一张用主参数；第 i 张用第 i-1 个 spec 覆盖
        eff_args = args if i == 0 or not args.spec else _SpecArgs(args, _parse_spec(args.spec[i - 1]))
        cam, subj = _pose(eff_args, width, height)
        kw = _compose_kw(eff_args)
        # 立绘层路径：src=None → 只出立绘+阴影的透明层（同裁切框）
        layer, rect = compose_char_layer(char01, subj, cam, **kw)
        layers.append((layer, rect, char_p.name))
        print(f"  层 {i + 1}/{len(args.chars)}：{char_p.name}"
              + (f"（spec：{args.spec[i - 1]}）" if i > 0 and args.spec else ""))

    # 顺序 source-over：后画的盖先画的（与逐一合成等价）。每层按自己的裁切框
    # 偏移贴回整幅——满幅时偏移为 0，行为与逐位相同
    out_img = base
    for layer, rect, _name in layers:
        out_img = _paste_layer(out_img, layer, rect)
    img_u8 = np.clip(out_img * 255.0 + 0.5, 0, 255).astype(np.uint8) \
        if out_img.dtype != np.uint8 else np.asarray(out_img)
    out = Path(args.out)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    save_png(img_u8, out)
    print(f"已合成 {len(layers)} 层 → {out}（{width}×{height}）")
    if args.scene:
        # scene.json 记第一张的落位（多主体的配方格式原版没有，如实记主体一）
        char01 = image_to_rgba01(_need(args.chars[0], "立绘 1"))
        cam, subj = _pose(args, width, height)
        shot = compose_shot(src01, char01, subj, cam, **_compose_kw(args))
        save_scene(_scene_dict(args, char01, subj, cam, shot.rect,
                               _placement(char01, subj, cam, args),
                               char_name=Path(args.chars[0]).name),
                   Path(args.scene))
        print(f"配方 {args.scene}（记第一张立绘的落位；多主体无原版格式）")
    return 0


def cmd_stats(args) -> int:
    """项目自报：规模/测试/子命令/模块一张表。"""
    import arcam
    # 源码树定位：src/arcam/cli.py → 仓库根；安装态没有 src/ 树，退回已安装
    # 的 arcam 包目录（import arcam 的位置）——计数仍然有意义。
    root = Path(__file__).resolve().parents[2]
    src = root / "src" / "arcam"
    if not src.is_dir():
        src = Path(arcam.__file__).resolve().parent
    py_files = sorted(src.rglob("*.py"))
    loc = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in py_files)
    tests_dir = root / "tests"
    test_files = sorted(tests_dir.glob("test_*.py")) if tests_dir.is_dir() else []
    test_loc = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in test_files)
    js_files = (sorted((src / "webui").glob("*.js")) if (src / "webui").is_dir() else []) \
        + (sorted((root / "docs").glob("*.js")) if (root / "docs").is_dir() else [])
    js_loc = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in js_files)
    subs = sorted(_subcommand_names())
    info = {
        "version": _project_version(root),
        "python_modules": len(py_files),
        "python_loc": loc,
        "test_files": len(test_files),
        "test_loc": test_loc,
        "js_files": len(js_files),
        "js_loc": js_loc,
        "public_api": len(arcam.__all__),
        "i18n_entries": len(TXT),
        "subcommands": subs,
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    print(f"arcam {info['version']}")
    print(f"  Python  {info['python_modules']:>3} 个模块 / {info['python_loc']:>5} 行")
    print(f"  测试    {info['test_files']:>3} 个文件 / {info['test_loc']:>5} 行")
    print(f"  网页    {info['js_files']:>3} 个 JS   / {info['js_loc']:>5} 行")
    print(f"  公开 API {info['public_api']} 个导出 · 语言表 {info['i18n_entries']} 条")
    print(f"  子命令  {len(subs)} 个：{' '.join(subs)}")
    return 0


def _subcommand_names() -> list[str]:
    """从 build_parser 现抓顶层子命令名（不硬编码，新增命令自动进 stats）。

    顶层 subparsers 的 ``dest`` 就是 ``"cmd"``（recipe/lidar 的二级分组是
    ``recipe_cmd``/``lidar_cmd``，不会被误抓）。
    """
    parser = build_parser()
    for action in parser._actions:
        if action.dest == "cmd" and isinstance(getattr(action, "choices", None), dict):
            return sorted(action.choices)
    return []


def _project_version(root: Path) -> str:
    """版本号：**源码树在就以 pyproject.toml 为准**，纯安装态才用 importlib.metadata。

    可编辑安装（``pip install -e .``）的 metadata 停留在**安装那一刻**的版本：
    实测 pyproject 已到 1.7.0、``importlib.metadata`` 还报 1.2.0，``stats``
    于是把旧版本号当现版本打出来（v1.8 审计修复）。旁边找得到 pyproject 就说明
    这是仓库检出（editable 或直接跑），repo 里的 pyproject 永远比 metadata 新；
    真·wheel 安装旁边没有 pyproject，才回落到 metadata。都不可得给 '?'。
    """
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            txt = pyproject.read_text(encoding="utf-8")
        except OSError:
            txt = None
        if txt is not None:
            m = re.search(r'^version\s*=\s*"([^"]+)"', txt, re.M)
            if m:
                return m.group(1)
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("arcam")
    except Exception:
        pass
    return "?"

def cmd_contact_sheet(args) -> int:
    """参数对比网格（FEATURE-SPEC §7）：笛卡尔积逐格渲染 + 带标注拼图。

    每格标注 = 该格在 ``--vary`` 各轴上的取值（没扫的轴就是命令行基准值，
    不进标签）——一眼看出这一格改了什么。格内不出 HUD（对比的是构图本身）。
    """
    from PIL import Image, ImageDraw
    photo = _need(args.photo, "背景照片")
    char_p = _need(args.char, "立绘")
    src01 = image_to_rgb01(photo)
    char01 = image_to_rgba01(char_p)
    height, width = src01.shape[:2]
    base_cam, base_subj = _pose(args, width, height)

    axes = _parse_vary(args.vary)
    total = 1
    for _key, values in axes:
        total *= len(values)
    if total > _SHEET_MAX_CELLS:
        raise CliError(f"笛卡尔积 {total} 格超过上限 {_SHEET_MAX_CELLS}——"
                       "减少 --vary 的轴或每轴取值数")

    # 笛卡尔积（保持 --vary 顺序：第一个轴变化最慢）
    combos: list[dict] = [{}]
    for key, values in axes:
        combos = [dict(c, **{key: v}) for c in combos for v in values]

    # 基准参数（命令行给的其余摆放参数 + 光影三门）——直接复用 _compose_kw：
    # 它就是 compose_shot 开关的唯一来源，另抄一份必然漏（--tone 就这样静默失效过，
    # v1.5 审计修复）
    base_params = _compose_kw(args)

    def _coerce(key: str, v: str):
        if key in ("shot", "frame", "mode", "rot_basis"):
            return v
        if key == "edge_bar":
            return int(v)
        return float(v)

    cells = []
    for combo in combos:
        cam = base_cam
        subj = base_subj
        kw = dict(base_params)
        compose_keys = ("frame_key", "mode", "size_pct", "char_scale",
                        "rot_basis", "shot", "edge_bar")
        for key, v in combo.items():
            # 只有 compose 自己的键进 kw；h/feq/pitch/roll/hp/d/x 走下面的
            # Camera/Subject 重建（它们是相机与人物位姿，不是 compose 开关）
            if key in compose_keys:
                kw[key] = _coerce(key, v)
            if key == "h":
                cam = Camera(cam.width, cam.height, float(v), cam.feq_mm,
                             cam.pitch_deg, cam.roll_deg)
            elif key == "feq":
                cam = Camera(cam.width, cam.height, cam.height_m, float(v),
                             cam.pitch_deg, cam.roll_deg)
            elif key == "pitch":
                cam = Camera(cam.width, cam.height, cam.height_m, cam.feq_mm,
                             float(v), cam.roll_deg)
            elif key == "roll":
                cam = Camera(cam.width, cam.height, cam.height_m, cam.feq_mm,
                             cam.pitch_deg, float(v))
            elif key == "hp":
                subj = Subject(float(v), subj.distance_m, subj.lateral_m)
            elif key == "d":
                subj = Subject(subj.height_m, float(v), subj.lateral_m)
            elif key == "x":
                subj = Subject(subj.height_m, subj.distance_m, float(v))
        shot = compose_shot(src01, char01, subj, cam, **kw)
        img = np.clip(shot.shot * 255.0 + 0.5, 0, 255).astype(np.uint8) \
            if shot.shot.dtype != np.uint8 else np.asarray(shot.shot)
        label = "  ".join(f"{k}={v}" for k, v in combo.items())
        cells.append((img, label))

    # 拼图：等比缩到 cell_w，标签条 26px
    from arcam.hud import _font
    cell_w = max(80, int(args.cell_w))
    label_h = 26
    scaled = []
    cell_h = 0
    for img, label in cells:
        h, w = img.shape[:2]
        cw = cell_w
        ch = max(1, int(round(h * (cw / w))))
        im = Image.fromarray(img, "RGB").resize((cw, ch), Image.BILINEAR)
        scaled.append((im, label))
        cell_h = max(cell_h, ch)
    cols = args.cols if args.cols > 0 else int(np.ceil(np.sqrt(len(scaled))))
    cols = max(1, min(cols, len(scaled)))
    rows = int(np.ceil(len(scaled) / cols))
    sheet = Image.new("RGB", (cols * cell_w, rows * (cell_h + label_h)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = _font(13)
    for i, (im, label) in enumerate(scaled):
        r, c = divmod(i, cols)
        x, y = c * cell_w, r * (cell_h + label_h)
        sheet.paste(im, (x, y + label_h))
        draw.rectangle([x, y, x + cell_w - 1, y + label_h], fill=(40, 40, 40))
        draw.text((x + 6, y + 5), label, font=font, fill=(235, 235, 235))
    out = Path(args.out)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, format="PNG")
    print(f"对比网格 {out}（{len(cells)} 格 × {cell_w}px，{cols} 列 {rows} 行）")
    for i, (_im, label) in enumerate(scaled, 1):
        print(f"  [{i}] {label}")
    return 0

def cmd_batch(args) -> int:
    """批量出图：PHOTO_DIR 里每张照片 × 同一立绘同一组参数（FEATURE-SPEC §7）。

    输出 ``OUTDIR/shot_<照片 stem>.<ext>``；``--scene`` 时每张顺带写同名
    scene.json。深度帧按 **stem** 匹配（``scan_0001`` ↔ ``scan_0001.depth``），
    匹配不到的照片走无深度路径（不报错——批量任务不该因一张缺深度整批失败）。
    单张失败只记一条继续跑，最后汇总（退出码 0 = 全部成功，1 = 有失败）。
    """
    src_dir = Path(args.photo_dir)
    if not src_dir.is_dir():
        raise CliError(f"照片目录不存在：{src_dir}")
    photos = sorted(p for p in src_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in
                    (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"))
    if not photos:
        raise CliError(f"{src_dir} 里没有图片文件（jpg/png/webp/bmp/tif）")
    char_p = _need(args.char, "立绘")
    char01 = image_to_rgba01(char_p)
    depth_dir = Path(args.depth_dir) if args.depth_dir else None
    if depth_dir is not None:
        if not depth_dir.is_dir():
            raise CliError(f"深度帧目录不存在：{depth_dir}")
        # 深度帧读取与 render --depth 同一条懒加载路径：模块被裁剪过时在这里就
        # 人话报错，不进循环——循环里的宽松 except 会把 NameError/ImportError 当成
        # "这个候选文件读不了" 吞掉，整批照片静默退回无深度渲染（v1.5 审计修复）。
        try:
            from arcam.depth import load_depth
        except ImportError as exc:
            raise CliError(
                f"读不到 LiDAR 深度模块：{exc}（arcam.depth 应随包一起提供；"
                "若安装被裁剪过，契约见 docs/LIDAR-SPEC.md §2）") from None
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    kw = _compose_kw(args)
    ok_count, failed = 0, []
    for i, photo in enumerate(photos, 1):
        try:
            src01 = image_to_rgb01(photo)
            height, width = src01.shape[:2]
            cam, subj = _pose(args, width, height)
            depth = None
            if depth_dir is not None:
                for cand in depth_dir.glob(photo.stem + ".*"):
                    # 只放过"文件读不懂"（同名但不是深度帧）；NameError 之类的
                    # 实现错误必须抛出去，不能被当成下一个候选安静跳过
                    try:
                        depth = load_depth(cand)
                        break
                    except (ValueError, OSError):
                        continue
            if depth is not None:
                from arcam.plane import fit_ground_plane, camera_env_from_plane
                # roll_deg 必须透传：ARKit 给了横滚就先反旋再解 pitch，与
                # render --depth / lidar inspect / compose._lidar_env 同一口径
                # （漏传时同一帧会解出另一组 roll/pitch，v1.5 审计修复）
                env = camera_env_from_plane(fit_ground_plane(depth),
                                            roll_deg=depth.roll_deg)
                cam, subj, _notes = _merge_env(cam, subj, _env_overrides(args), env,
                                               depth=depth, mode=args.mode,
                                               shot=args.shot)
                kw_eff = dict(kw, depth=depth, env_overrides={
                    'h': cam.height_m, 'pitch': cam.pitch_deg,
                    'roll': cam.roll_deg, 'd': subj.distance_m,
                    'x': subj.lateral_m})
            else:
                kw_eff = kw
            shot = compose_shot(src01, char01, subj, cam, **kw_eff)
            stem = f"shot_{photo.stem}"
            if args.ext == "jpg":
                out_path = out_dir / f"{stem}.jpg"
                out_path.write_bytes(encode_jpeg(shot.shot, 95))
            else:
                out_path = out_dir / f"{stem}.png"
                save_png(shot.shot, out_path)
            if args.scene:
                save_scene(_scene_dict(args, char01, subj, cam, shot.rect,
                                       _placement(char01, subj, cam, args)),
                           out_dir / f"{stem}.json")
            ok_count += 1
            print(f"  [{i}/{len(photos)}] {photo.name} → {out_path.name}"
                  + (t("note.batchDepth", lang=args.lang) if depth is not None else ""))
        except Exception as exc:
            failed.append((photo.name, f"{type(exc).__name__}: {exc}"))
            print(f"  [{i}/{len(photos)}] {photo.name} → "
                  f"{t('note.batchFailed', lang=args.lang)}：{exc}")
    print(t("note.batchDone", ok_count, len(photos), lang=args.lang)
          if ok_count == len(photos)
          else t("note.batchPartial", ok_count, len(photos), len(failed),
                 lang=args.lang))
    return 0 if not failed else 1

def cmd_char_layer(args) -> int:
    char_p = _need(args.char, "立绘")
    char01 = image_to_rgba01(char_p)
    cam, subj = _pose(args, args.width, args.height)
    # 与 render 同一套 _compose_kw：char-layer 收了 --shot/--edge-bar/--rot-basis
    # 和光影三门（--no-shadow/--no-grain/--no-light）就必须生效，否则「层盖回去
    # 严丝合缝」对不上（这几组历史上各整组漏过一次；v1.5 审计又抓到时漏的是
    # 三门——shadow 决定层里那团接触阴影在不在，用户开了 --no-shadow 层却照画）。
    layer, _rect = compose_char_layer(char01, subj, cam, **_compose_kw(args))
    out = Path(args.out)
    save_png(layer, out)
    print(f"立绘层 {out}（{layer.shape[1]}×{layer.shape[0]} RGBA"
          f"—— 直接盖在对应出图上严丝合缝）")
    return 0


def cmd_bundle(args) -> int:
    photo = _need(args.photo, "背景照片")
    char_p = _need(args.char, "立绘")
    src01 = image_to_rgb01(photo)
    char01 = image_to_rgba01(char_p)
    height, width = src01.shape[:2]
    cam, subj = _pose(args, width, height)
    shot = compose_shot(src01, char01, subj, cam, **_compose_kw(args))
    # 立绘层用 composeShot 自己带出来的那一份：同一裁切框、同一套光
    # （原版 buildCharCanvas = composeShot(null, LAST_SHOT_LIGHT)，index.html:3709。
    #  合成路径自带 frame_key，与 --frame 一致。）
    layer = shot.char_layer
    if layer is None:
        raise CliError("这一遍没有产出立绘层（立绘缺失或落位失败）")
    placement = _placement(char01, subj, cam, args)
    scene_json = json.dumps(_scene_dict(args, char01, subj, cam, shot.rect, placement),
                            ensure_ascii=False, indent=2).encode("utf-8")
    out = Path(args.out)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    parts = (
        ("shot.jpg", encode_jpeg(shot.shot, 95)),
        ("scene.json", scene_json),
        ("character.png", encode_png(layer)),
    )
    with zipfile.ZipFile(out, "w") as zf:
        for name, data in parts:
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    print(f"素材包 {out}（{', '.join(n for n, _ in parts)}；"
          f"JPEG q0.95，时间戳固定 1980-01-01）")
    return 0


def cmd_matte(args) -> int:
    img = _need(args.image, "图片")
    src01 = image_to_rgb01(img)
    try:
        result = anime_cut(src01, args.model)
    except ImportError as e:
        raise CliError(f"{e}\n（装一下再试：pip install onnxruntime，"
                       f"或 pip install -e \".[matting]\"）") from None
    except FileNotFoundError as e:
        raise CliError(f"{e}\n（下载 isnet-anime-q8.onnx 后用 --model PATH 指过来）") from None
    except ValueError as e:
        raise CliError(str(e)) from None
    out = Path(args.out)
    save_png(result.rgba, out)
    print(f"已抠图 {out}（前景占比 {result.fg:.1%}，推理 {result.ms:.0f} ms）")
    return 0


def cmd_prep(args) -> int:
    """立绘自动修图链（FEATURE-SPEC §1；index.html:2400-2440 的 1:1 对译）。

    四步各有关断（--no-solid/--no-trim/--no-soften/--no-blur），关掉的步从
    auto_prepare_char 的动作链里摘掉——实现方式是逐步调用同模块的公开函数，
    顺序与原版一致；任一步抛错静默跳过（修图不能把"出图"搞挂）。

    ⚠ 兜底（index.html:2435-2439）也必须在：动过手（抠或裁）且最终内容 bbox
    任一边 < 8px → 整条回退原图、动作链清空。曾整条漏掉——5×5 的立绘会被
    输出成 5×5 的"修好"的图，而 webapp /api/prep（走 auto_prepare_char）同
    一张图回退原图，两条入口结论相反（v1.8 审计修复）。
    """
    from arcam.matte_tools import (_MIN_CONTENT_SIDE, auto_trim, content_bounds,
                                   is_half_body, lens_blur, soften_edge)
    char_p = _need(args.char, "立绘")
    char01 = image_to_rgba01(char_p)
    lang = args.lang

    arr = char01
    actions: list[str] = []
    cut_done = trim_done = False
    if not args.no_solid:
        try:
            out, ratio = _flood(arr)
            if out is not None:
                arr = out
                cut_done = True
                actions.append(t("note.autoCut", f"{ratio * 100:.0f}", lang=lang))
        except Exception:
            pass
    if not args.no_trim:
        try:
            res = auto_trim(arr)
            if res is not None:
                arr = res[0]
                trim_done = True
                actions.append(t("note.autoTrim", lang=lang))
        except Exception:
            pass
    if not args.no_soften:
        try:
            soft, changed = soften_edge(arr, erode=1, feather=2)
            if changed:
                arr = soft
                actions.append(t("note.autoSoft", lang=lang))
        except Exception:
            pass
    if not args.no_blur:
        try:
            blurred = lens_blur(arr, 0.62)
            # 与 auto_prepare_char 同口径：没变化就不算做过这一步（不虚报动作）
            if not np.array_equal(blurred, arr):
                arr = blurred
                actions.append(t("note.autoBlur", lang=lang))
        except Exception:
            pass

    # ⑥ 兜底：裁完什么都不剩（或只剩一条缝）→ 整条回退原图（原版逐字）。
    #    回退即"什么都没发生"：动作链清空，只留一条还原提示；半身判定在
    #    回退后的原图上重算（auto_prepare_char 同一口径）。
    if cut_done or trim_done:
        bounds = content_bounds(arr)
        if bounds is None or bounds[2] < _MIN_CONTENT_SIDE or bounds[3] < _MIN_CONTENT_SIDE:
            arr = char01
            actions.clear()
            actions.append(t("note.chReset", lang=lang))

    half = is_half_body(arr)
    out = Path(args.out)
    save_png(arr, out)
    print(f"已修图 {out}（{arr.shape[1]}×{arr.shape[0]} RGBA）")
    for line in actions:
        print(f"  · {line}")
    if half:
        print(f"  · {t('note.halfAuto', lang=lang)}")
        print("    （半身图建议配 --mode free 出图；贴地会把下沿钉在地面上）")
    else:
        # note.chHint 是「自由模式…；贴地模式…」两句：只报第一句（与语言无关地
        # 截到第一个分句——中文用全角分号、英文用句点空格，两种都要认）
        print("  · " + re.split(r"[；]|\. ", t("note.chHint", lang=lang))[0])
    return 0


def _flood(arr):
    """cut_solid_bg 的数组版（matte_tools._flood_solid 的稳定转发）。

    单独包一层是为了 cmd_prep 的步进式调用不必 import 私有名；占比门控
    （5%~85%）与 auto_prepare_char 完全一致。
    """
    from arcam.matte_tools import CUT_RATIO_MAX, CUT_RATIO_MIN, _flood_solid
    out, ratio = _flood_solid(arr, 32.0, 1.2)
    if out is None or not (CUT_RATIO_MIN <= ratio <= CUT_RATIO_MAX):
        return None, 0.0
    return out, ratio


def cmd_video(args) -> int:
    track = load_track(args.track)
    problems = track_issues(track)
    if problems:
        raise CliError("轨道有问题：\n  - " + "\n  - ".join(problems))
    if not args.bg:
        raise CliError("video 需要 --bg 指定背景（单张图片或帧目录）——"
                       "原版导出的每一帧都铺着实时画面，离线侧就是这段素材")
    char_p = _need(args.char, "立绘")
    char01 = image_to_rgba01(char_p)
    out_dir = Path(args.out)
    frames = render_track(args.bg, track, char01, out_dir, tone=args.tone, gif=args.gif)
    extras = []
    mp4 = out_dir / MP4_NAME
    if mp4.is_file():
        extras.append(mp4.name)
    gif_p = out_dir / GIF_NAME
    if args.gif and gif_p.is_file():
        extras.append(gif_p.name)
    tail = (" + " + " + ".join(extras)) if extras else "（未装 imageio/imageio-ffmpeg，只有 PNG 序列）"
    print(f"已渲染 {len(frames)} 帧 → {out_dir}{tail}")
    return 0


def _print_summary(track) -> None:
    n = frame_count(track)
    k0, k1 = track.keys[0], track.keys[-1]
    print(f"轨道 {track.width}×{track.height} @ {track.fps:g}fps"
          f"（{n} 帧 / {(n - 1) / track.fps:.3f}s）")
    print(f"  画幅 {track.frame_key} · 模式 {track.mode} · 景别 {track.shot}"
          f" · 倍率 {track.char_scale:g} · 占比 {track.size_pct:g}%")
    print(f"  键 {len(track.keys)} 个（t {k0.t:g} → {k1.t:g}s）")
    print(f"  横向 {k0.lateral_m:g} → {k1.lateral_m:g} m · 距离 {k0.distance_m:g}"
          f" → {k1.distance_m:g} m")
    print(f"  俯角 {k0.pitch_deg:g} → {k1.pitch_deg:g}° · 滚转 {k0.roll_deg:g}"
          f" → {k1.roll_deg:g}°")


def cmd_check(args) -> int:
    track = load_track(args.track)
    problems = track_issues(track)
    _print_summary(track)
    if problems:
        print(f"发现 {len(problems)} 个问题：", file=sys.stderr)
        for m in problems:
            print(f"  - {m}", file=sys.stderr)
        return 1
    print("轨道合法。")
    return 0



def cmd_lidar_sim(args) -> int:
    """合成深度帧（lidar_sim.synth_ground_frame）：落盘 .depth 或推给 serve。

    三模态：``--json`` 只打印第一帧 meta；``--push`` 推给正在监听的 serve；
    否则（默认或 ``--out``）落盘 ``frame_%06d.depth``。``--width/--height/
    --seed`` 只作用于本命令自己合成的帧——``--push`` 走
    :func:`arcam.lidar_sim.simulate_scene_server`，它按 ``synth_ground_frame``
    的默认规格（256×192、seed 0）合成、不接受尺寸/种子参数，所以推流模式下
    给非默认值直接人话报错，不静默丢参数（v1.8 审计修复）。
    """
    from arcam.lidar_sim import synth_ground_frame

    if args.push is not None and args.out is not None:
        raise CliError("--out 与 --push 二选一（落盘 或 推流，别同时给）")
    if args.frames < 1:
        raise CliError(f"--frames 至少 1（收到 {args.frames}）")
    if args.width < 8 or args.height < 8:
        raise CliError(f"深度帧尺寸太小：{args.width}×{args.height}（至少 8×8）")
    if args.push is not None:
        off = [k for k, v in _SIM_PUSH_FIXED_SPEC.items()
               if getattr(args, k) != v]
        if off:
            raise CliError(
                f"--push 推流模式下不支持 {'/'.join('--' + k for k in off)}："
                f"推流由 arcam.lidar_sim.simulate_scene_server 按固定规格 "
                f"{_SIM_PUSH_FIXED_SPEC['width']}×{_SIM_PUSH_FIXED_SPEC['height']}"
                f"（seed {_SIM_PUSH_FIXED_SPEC['seed']}）合成。要自定义尺寸/种子"
                "请用落盘模式（--out DIR 或缺省）再 arcam lidar inspect 验证，"
                "或让 serve 侧支持尺寸参数")

    meta0, depth0 = synth_ground_frame(h=args.h, pitch_deg=args.pitch,
                                       roll_deg=args.roll, width=args.width,
                                       height=args.height, index=0, seed=args.seed)
    if args.json:
        print(json.dumps(meta0, ensure_ascii=False, indent=2))
        return 0

    if args.push is not None:
        # 推流模式：起 daemon 线程扮演设备，主线程等它推完
        from arcam.lidar_sim import simulate_scene_server
        try:
            th = simulate_scene_server(args.push, h=args.h, pitch=args.pitch,
                                       roll=args.roll, frames=args.frames)
        except OSError as exc:
            raise CliError(f"连不上 127.0.0.1:{args.push}：{exc}\n"
                           "先在另一个终端跑 arcam lidar serve --port <同端口>") from None
        th.join(timeout=30.0 + args.frames)
        print(f"已推送 {args.frames} 帧到 127.0.0.1:{args.push}"
              f"（h={args.h} pitch={args.pitch} roll={args.roll}）")
        return 0

    # 落盘模式：frame_%06d.depth（ARCD，内参外参齐备）
    from arcam.lidar_live import save_depth_file
    out_dir = Path(args.out) if args.out else Path(f"lidar_sim_{args.width}x{args.height}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(args.frames):
        meta, depth = synth_ground_frame(h=args.h, pitch_deg=args.pitch,
                                         roll_deg=args.roll, width=args.width,
                                         height=args.height, index=i, seed=args.seed)
        save_depth_file(out_dir / f"frame_{i:06d}.depth", meta, depth)
    valid = float(np.isfinite(depth0).mean())
    print(f"已合成 {args.frames} 帧 → {out_dir}/frame_000000.depth …"
          f"（{args.width}×{args.height}，有效覆盖率 {valid:.1%}，真值 h={args.h}"
          f" pitch={args.pitch} roll={args.roll}）")
    print(f"  验证闭环：arcam lidar inspect {out_dir}/frame_000000.depth")
    return 0

def cmd_lidar_inspect(args) -> int:
    """LiDAR 深度诊断：读帧 → 拟合地面平面 → 报告相机环境量（LIDAR-SPEC §6）。

    人话文本与 ``--json`` 是**同一组字段**（同构 dict），脚本侧直接吃 JSON；
    深度读不懂 / 平面拟不出来（点数不足等）都是人话报错、退出 1。
    """
    try:
        from arcam.depth import load_depth
        from arcam.plane import camera_env_from_plane, fit_ground_plane
    except ImportError as exc:
        raise CliError(
            f"读不到 LiDAR 模块：{exc}（arcam.depth/arcam.plane 应随包一起提供；"
            "若安装被裁剪过，契约见 docs/LIDAR-SPEC.md §2/§3）") from None
    depth_p = _need(args.depth, "深度文件")
    try:
        frame = load_depth(depth_p)
        plane = fit_ground_plane(frame)
        env = camera_env_from_plane(plane, roll_deg=frame.roll_deg)
    except ValueError as e:
        raise CliError(str(e)) from None
    coverage = float(np.isfinite(np.asarray(frame.depth_m, dtype=np.float64)).mean())
    height, width = int(frame.depth_m.shape[0]), int(frame.depth_m.shape[1])
    info = {
        "file": str(depth_p),
        "source": frame.source,
        "width": width,
        "height": height,
        "coverage": round(coverage, 4),
        "inliers": int(plane.inliers),
        "rms": float(plane.rms),
        "height_m": float(env.height_m),
        "pitch_deg": float(env.pitch_deg),
        "roll_deg": float(env.roll_deg),
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    print(f"深度 {depth_p}（{width}×{height}，{frame.source or '无来源描述'}）")
    print(f"  有效覆盖率 coverage={coverage:.1%}")
    print(f"  地面平面 inliers={plane.inliers} rms={plane.rms:.3f}m")
    print(f"  相机高 h={env.height_m:.3f}m")
    print(f"  俯仰 pitch={env.pitch_deg:.2f}° 滚转 roll={env.roll_deg:.2f}°")
    if frame.roll_deg is not None:
        print(f"  （ARKit 给定 roll={frame.roll_deg:.2f}°，已先反旋再解 pitch）")
    return 0


def _live_frame_line(n: int, meta: dict, depth) -> str:
    """实时流每帧的一行人话摘要；meta 缺字段就跳过（协议只说"至少含"哪些键）。"""
    height, width = int(depth.shape[0]), int(depth.shape[1])
    env = " ".join(
        f"{label}={meta[key]:g}"
        for label, key in (("h", "height_m"), ("pitch", "pitch_deg"), ("roll", "roll_deg"))
        if isinstance(meta.get(key), (int, float))
    )
    return f"第 {n} 帧 {width}×{height}" + (f" {env}" if env else "")


def cmd_lidar_serve(args) -> int:
    """实时流接收（LIDAR-SPEC §6；实现在 arcam.lidar_live）。

    ``lidar_live.serve`` 返回的是**惰性 generator**（调用时只 bind+listen），
    所以这里必须**迭代**它才真的在收帧 —— 早先只调不迭代，命令 bind 完就退出、
    一帧都收不到（表现为"秒退且端口没人听"）。

    模块不存在 / 没有可调用的 serve() → 打印"实时流模块未就绪"并正常退出（0），
    不崩、不吞错；端口被占用等运行期错误转 CliError（退出 1）。

    ``lidar_live.serve(port, host, out_dir, max_frames)`` 的参数名以它为准
    （``out_dir`` 不是 ``out``）；本命令只透传 ``--port``/``--out``，host 用它的
    默认 127.0.0.1（回环，不做网络暴露）。
    """
    try:
        import arcam.lidar_live as live
    except ImportError:
        print("实时流模块未就绪（arcam lidar serve 需要 arcam/lidar_live.py）。"
              "实时流协议见 docs/LIDAR-SPEC.md §6：TCP，每帧 = [u32 BE meta_len]"
              "[JSON][u32 BE len][depth float32 米]。")
        return 0
    serve = getattr(live, "serve", None)
    if not callable(serve):
        print("实时流模块未就绪（arcam/lidar_live.py 里没有可调用的 serve()）。")
        return 0
    kw = {"port": int(args.port)}
    if args.out is not None:
        kw["out_dir"] = str(args.out)
        print(f"深度帧将写入 {args.out}（frame_%06d.depth）", flush=True)
    n = 0
    try:
        print(f"实时流监听 127.0.0.1:{int(args.port)}，等待设备连接（Ctrl-C 停止）",
              flush=True)
        for meta, depth in serve(**kw):          # 必须迭代：generator 只在被消费时才 accept
            n += 1
            print(_live_frame_line(n, meta, depth), flush=True)
    except KeyboardInterrupt:
        print(f"\n已停止，共接收 {n} 帧。", flush=True)
        return 0
    except (ValueError, OSError) as e:
        raise CliError(str(e)) from None
    print(f"连接已关闭，共接收 {n} 帧。", flush=True)
    return 0


def cmd_web(args) -> int:
    """web 子命令：转交 :func:`arcam.webapp.main`（契约 = docs/WEB-SPEC.md §1）。

    webapp 懒加载：其他子命令不为一个本地网页服务多付 import 成本；``--host``
    ``--port`` ``--no-open`` 原样翻译成 webapp.main 的 argv。端口占用 / 起不来
    服务由 webapp.main 打人话并返回 1（与 CLI 退出码约定一致）。
    """
    from arcam import webapp
    argv = ["--host", str(args.host), "--port", str(args.port)]
    if args.no_open:
        argv.append("--no-open")
    return webapp.main(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。退出码 0 正常 / 1 运行期失败 / 2 用法错误（argparse 自带）。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CliError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"错误：找不到文件 {e}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
