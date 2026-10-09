"""arcam 命令行 —— 原版桌面端四件事（出图 / 立绘层 / 素材包 / 抠图）加离线视频、LiDAR 深度。

子命令一览（每个都有中文 --help）::

    arcam render PHOTO CHAR -o OUT.png [--scene PATH] [摆放参数] [--depth DEPTH]  # 出图
    arcam char-layer CHAR -o OUT.png [摆放参数]                    # 仅 RGBA 立绘层
    arcam bundle PHOTO CHAR -o OUT.zip [摆放参数]                   # 三件套 zip（确定性时间戳）
    arcam matte IMG -o OUT.png [--model PATH]                       # ISNet-anime 抠图
    arcam video TRACK.json CHAR -o OUTDIR [--bg 单图或目录] [--tone] # 逐帧渲染
    arcam check TRACK.json                                          # 轨道自检
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
import sys
import zipfile
from pathlib import Path

import numpy as np

from arcam.compose import _merge_env, compose_char_layer, compose_shot
from arcam.geometry import Camera
from arcam.i18n import LANGS
from arcam.matting import anime_cut
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


def _add_lang(p: argparse.ArgumentParser) -> None:
    """语言开关（FEATURE-SPEC §3）：只影响人话输出，不改任何数值行为。"""
    p.add_argument("--lang", default="zh", choices=list(LANGS), metavar="语言",
                   help="人话输出语言：zh 中文 / en English（默认 zh；数值行为不变）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arcam",
        description="让她走出屏幕（Python 离线版）—— 透视投影立绘合成管线："
                    "出图 / 立绘层 / 素材包 / 抠图 / 轨道视频 / LiDAR 深度。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  arcam render photo.jpg char.png -o shot.png --scene scene.json
  arcam render photo.jpg char.png -o shot.png --depth scan_0001.png
  arcam char-layer char.png -o layer.png --mode free --size-pct 70
  arcam bundle photo.jpg char.png -o pack.zip --pitch 12
  arcam matte photo.jpg -o cut.png --model isnet-anime-q8.onnx
  arcam prep char.png -o clean.png            # 装图自动修图链（抠底/裁边/柔边/微糊）
    arcam video track.json char.png -o frames/ --bg bg/ --tone
    arcam check track.json
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
    p.add_argument("--overlay", default=None, choices=("grid", "horizon", "figure", "hud"),
                   metavar="层", help="出图后叠一层 HUD 取景装饰（默认不叠；原版那些"
                                      "只在实时预览里画，出图不带——要排查构图时才开）")
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
                    "--out 给目录时同步落盘 frame_%06d.depth。模块缺失才提示退出 0。")
    ps.add_argument("--port", type=int, default=7707, metavar="端口",
                    help="监听端口（默认 7707）")
    ps.add_argument("--out", metavar="DIR", default=None,
                   help="帧序列输出目录（默认不落盘）")
    ps.set_defaults(func=cmd_lidar_serve)

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
                sample_light=not _light_off(args))


def _light_off(args) -> bool:
    """--no-light：关掉采光（light=None → bakeLit 原样返回）。"""
    return bool(getattr(args, "no_light", False))


def _placement(char01, subj: Subject, cam: Camera, args):
    """单独算一次落位给 build_scene 用。

    只传当前模式收得下的 kwargs：ground 分支不收 size_pct/edge_bar、
    free 分支不收 char_scale/shot（签名见 SPEC §3.2 的两个分支）。
    shot/edge_bar/rot_basis 必须与 _compose_kw 一致，否则 scene.json 记的参数和
    实际渲染不符 —— 这三个历史上各漏过一次，改动本函数务必与 _compose_kw 对拍。
    """
    if args.mode == "free":
        return current_placement(char01, subj, cam, mode="free",
                                 size_pct=args.size_pct, edge_bar=args.edge_bar,
                                 rot_basis=args.rot_basis)
    return current_placement(char01, subj, cam, mode="ground",
                             char_scale=args.char_scale, rot_basis=args.rot_basis,
                             shot=args.shot)


def _scene_dict(args, char01, subj: Subject, cam: Camera, rect, placement) -> dict:
    """buildScene 的 1:1 同构调用（index.html:3729）。

    shot/char_scale 必须透传：buildScene 把它们记进配方（index.html:3756/3760），
    漏传会让 scene.json 与实际渲染参数不符。
    """
    return build_scene(rect, placement, subj, cam, frame_key=args.frame,
                       char_rgba01=char01, char_name=Path(args.char).name,
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
    overlay_note = ""
    if args.overlay:
        # HUD 只叠指定的一层（原版出图不带 HUD，这是排查构图的显式开关）
        from arcam.hud import draw_figure, draw_grid, draw_horizon, draw_hud_text
        canvas = img_u8.copy()
        if args.overlay == "grid":
            canvas = draw_grid(canvas, cam_eff, subj_eff.lateral_m)
        elif args.overlay == "horizon":
            canvas = draw_horizon(canvas, cam_eff, "地平线")
        elif args.overlay == "figure":
            canvas = draw_figure(canvas, cam_eff, subj_eff)
        else:
            canvas, hints = draw_hud_text(canvas, cam_eff, subj_eff, has_char=True,
                                          canvas_size=(width, height))
            if hints:
                overlay_note = "；构图提示：" + "；".join(hints)
        img_u8 = canvas
    save_png(img_u8, out)
    print(f"已出图 {out}（{width}×{height}）"
          + (f"｜已叠 HUD 层：{args.overlay}{overlay_note}" if args.overlay else ""))
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
    """从 scene.json 提摆放段（字段名与配方一致，单位一致）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cam = data.get("camera", {})
    subj = data.get("subject", {})
    ch = data.get("character", {})
    out = {
        "h": cam.get("heightM"), "hp": subj.get("heightM"),
        "d": subj.get("distanceM"), "x": subj.get("lateralM"),
        "feq": cam.get("focal35mm"), "pitch": cam.get("pitchDeg"),
        "roll": cam.get("rollDeg"), "shot": data.get("shot"),
        "char_scale": data.get("charScale"), "frame": data.get("frame", {}).get("aspect"),
        "mode": ch.get("mode"),
        "size_pct": ch.get("image", {}).get("height") and None,
        "edge_bar": data.get("edge", {}).get("bar"),
        "rot_basis": "frame" if ch.get("rotationDeg") == 0 and ch.get("mode") == "free"
                     and data.get("edge", {}).get("rotationLocked") else "world",
    }
    return {k: v for k, v in out.items() if v is not None}


def cmd_recipe_save(args) -> int:
    from arcam.i18n import t
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
    axes = []
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
        values = [v.strip() for v in raw.split(",") if v.strip()]
        if not values:
            raise CliError(f"--vary {key} 没给取值")
        axes.append((key, values))
    return axes


def cmd_contact_sheet(args) -> int:
    """参数对比网格（FEATURE-SPEC §7）：笛卡尔积逐格渲染 + 带标注拼图。

    每格标注 = 该格**非默认**的取值（默认值来自命令行其余参数）——一眼看出
    这一格改了什么。格内不出 HUD（对比的是构图本身）。
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

    # 基准参数（命令行给的其余摆放参数）
    base_params = dict(frame_key=args.frame, mode=args.mode, size_pct=args.size_pct,
                       char_scale=args.char_scale, rot_basis=args.rot_basis,
                       shot=args.shot, edge_bar=args.edge_bar,
                       shadow=not args.no_shadow, grain=not args.no_grain,
                       sample_light=not args.no_light)

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
    from arcam.i18n import t
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
    if depth_dir is not None and not depth_dir.is_dir():
        raise CliError(f"深度帧目录不存在：{depth_dir}")
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
                    try:
                        depth = load_depth(cand)
                        break
                    except Exception:
                        continue
            if depth is not None:
                from arcam.plane import fit_ground_plane, camera_env_from_plane
                env = camera_env_from_plane(fit_ground_plane(depth))
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
                  + ("（深度驱动）" if depth is not None else ""))
        except Exception as exc:
            failed.append((photo.name, f"{type(exc).__name__}: {exc}"))
            print(f"  [{i}/{len(photos)}] {photo.name} → 失败：{exc}")
    print(t("note.batchDone", ok_count, len(photos), lang=args.lang)
          if ok_count == len(photos)
          else f"批处理完成：{ok_count}/{len(photos)} 成功"
               + (f"，{len(failed)} 张失败（见上）" if failed else ""))
    return 0 if not failed else 1

def cmd_char_layer(args) -> int:
    char_p = _need(args.char, "立绘")
    char01 = image_to_rgba01(char_p)
    cam, subj = _pose(args, args.width, args.height)
    # shot/edge_bar/rot_basis 与 _compose_kw 同一套：char-layer 收了这些开关就得生效，
    # 否则「层盖回去严丝合缝」对不上（render 侧一直透传，这里曾整组漏掉）。
    layer, _rect = compose_char_layer(char01, subj, cam, frame_key=args.frame,
                                      mode=args.mode,
                                      size_pct=args.size_pct,
                                      char_scale=args.char_scale,
                                      rot_basis=args.rot_basis,
                                      shot=args.shot, edge_bar=args.edge_bar,
                                      tone=args.tone)
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
    """
    from arcam.i18n import t
    from arcam.matte_tools import auto_trim, lens_blur, soften_edge
    char_p = _need(args.char, "立绘")
    char01 = image_to_rgba01(char_p)
    lang = args.lang

    arr = char01
    actions: list[str] = []
    if not args.no_solid:
        try:
            out, ratio = _flood(arr)
            if out is not None:
                arr = out
                actions.append(t("note.autoCut", f"{ratio * 100:.0f}", lang=lang))
        except Exception:
            pass
    if not args.no_trim:
        try:
            res = auto_trim(arr)
            if res is not None:
                arr = res[0]
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
            arr = lens_blur(arr, 0.62)
            actions.append(t("note.autoBlur", lang=lang))
        except Exception:
            pass

    from arcam.matte_tools import is_half_body
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
        print("  · " + t("note.chHint", lang=lang).split("；")[0])
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
