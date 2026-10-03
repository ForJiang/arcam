"""前端 webui 静态检查（不起真服务；真服务冒烟由编排者按 WEB-SPEC §5 做）。

覆盖（对应 W2 交付要求）：
- index.html / style.css / app.js 存在且 index.html 引用 /static/style.css、/static/app.js；
- HTML 可被 html.parser 解析、非 void 标签配平、关键 DOM id 齐全；
- app.js 里每个 getElementById/$ 字面量 id 都能在 index.html 找到（契约外发现的第一道闸）；
- app.js 引用 WEB-SPEC §1 全部 6 个 API 路由、300ms 防抖常量、15 个摆参键；
- HTML 下拉/滑块默认值与 cli.py 的 S 默认一致（全身/满幅/ground/world/0/60...）；
- style.css 有 :root token、var() 自洽、<900px 响应式；
- app.js 无外链（零 CDN）、不用 innerHTML/alert（文件名与后端错误提示不可信）。
"""

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "src" / "arcam" / "webui"
DOCS = ROOT / "docs"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "source", "track", "wbr"}

# W2 功能点 -> 关键 DOM id（一个不能少）
REQUIRED_IDS = [
    # ① 照片上传 ② 立绘上传 ③ 深度帧上传（drag&drop + 点击选择）
    "drop-photo", "file-photo", "photo-name",
    "drop-char", "file-char", "char-name",
    "drop-depth", "file-depth", "depth-name",
    # ④ 抠图 ⑤ LiDAR 诊断（+ 重新渲染）
    "btn-matte", "btn-lidar", "btn-rerender",
    # ⑥ 预览 canvas + env 展示
    "preview-canvas", "preview-empty", "env-box",
    # ⑦ 参数面板：7 个数字+滑块双控件
    "p-x-num", "p-x-rng", "p-d-num", "p-d-rng", "p-h-num", "p-h-rng",
    "p-hp-num", "p-hp-rng", "p-feq-num", "p-feq-rng",
    "p-pitch-num", "p-pitch-rng", "p-roll-num", "p-roll-rng",
    # ⑦ 下拉 / 滑块 / edge-bar / tone
    "p-shot", "p-frame", "p-mode", "p-rot",
    "p-char-scale-rng", "p-char-scale-val",
    "p-size-pct-rng", "p-size-pct-val",
    "row-edge-bar", "p-edge-bar", "p-tone",
    # ⑧ 三个下载钮 + ⑨ scene.json 折叠展示 + ⑩ 状态条
    "btn-png", "btn-layer", "btn-bundle", "btn-copy-scene",
    "scene-details", "scene-json",
    "status-bar", "status-msg", "status-time", "local-chip",
    "input-hint",
]

# WEB-SPEC §1 路由子集（前端必须全部对接）
API_ROUTES = [
    "/api/upload", "/api/render", "/api/export",
    "/api/matte", "/api/lidar/inspect", "/api/matte-result-as-char",
]

# cli.py _add_placement 的 S 默认（WEB-SPEC §2）
HTML_DEFAULTS = [
    ('<option value="全身" selected>', "shot 默认 全身"),
    ('<option value="满幅" selected>', "frame 默认 满幅"),
    ('<option value="world" selected>', "rot_basis 默认 world"),
    ('<option value="ground" selected>', "mode 默认 ground"),
    ('<option value="0" selected>', "edge_bar 默认 0"),
]


class _IdCollector(HTMLParser):
    """收集 id 属性并检查非 void 标签配平。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "id" and v:
                self.ids.add(v)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"多余的 </{tag}>")
            return
        if self.stack[-1] != tag:
            self.errors.append(f"标签配平失败：期待 </{self.stack[-1]}> 实得 </{tag}>")
            return
        self.stack.pop()


def _strip_js_noise(src):
    """去掉注释与字符串，供大括号/圆括号/方括号配平启发式使用。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"'(?:\\.|[^'\\])*'", "''", src)
    src = re.sub(r'"(?:\\.|[^"\\])*"', '""', src)
    return src


def _files():
    return {
        "index.html": (WEBUI / "index.html").read_text(encoding="utf-8"),
        "style.css": (WEBUI / "style.css").read_text(encoding="utf-8"),
        "app.js": (WEBUI / "app.js").read_text(encoding="utf-8"),
    }


def test_files_exist_and_referenced():
    html, css, js = _files()["index.html"], _files()["style.css"], _files()["app.js"]
    for name, text in (("index.html", html), ("style.css", css), ("app.js", js)):
        assert len(text) > 1000, f"{name} 内容过少，疑似没写完"
    # webapp 静态路由是 /static/*（WEB-SPEC §1）
    assert '/static/style.css' in html, "index.html 未引用 /static/style.css"
    assert '/static/app.js' in html, "index.html 未引用 /static/app.js"
    assert 'rel="stylesheet"' in html
    # 零 CDN：不允许任何外链资源（协议相对/绝对 URL；注释里的"零 CDN"字样除外）
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', html), "index.html 出现外链资源"
    assert "http://" not in css and "https://" not in css, "style.css 出现外链"
    assert "http://" not in js and "https://" not in js, "app.js 出现外链"
    for host in ("unpkg", "jsdelivr", "cdnjs", "googleapis"):
        assert host not in (html + css + js).lower(), f"发现 CDN 宿主痕迹: {host}"


def test_html_parses_tags_balanced_ids_complete():
    files = _files()
    html = files["index.html"]
    p = _IdCollector()
    p.feed(html)
    p.close()
    assert not p.errors, f"HTML 结构问题：{p.errors}"
    assert not p.stack, f"HTML 有未闭合标签：{p.stack}"
    for id_ in REQUIRED_IDS:
        assert id_ in p.ids, f"index.html 缺少必需 DOM id: {id_}"
    # 单页骨架：恰好一个 canvas / script / stylesheet link
    assert html.count("<canvas") == 1
    assert html.count("<script") == 1
    assert html.count('rel="stylesheet"') == 1


def test_js_dom_references_resolve():
    files = _files()
    html, js = files["index.html"], files["app.js"]
    p = _IdCollector()
    p.feed(html)
    ids = p.ids
    refs = set(re.findall(r"getElementById\('([^']+)'\)", js))
    refs |= set(re.findall(r"\$\('([^']+)'\)", js))
    assert refs, "app.js 里没解析到任何 DOM id 引用（正则或写法变了？）"
    missing = sorted(refs - ids)
    assert not missing, f"app.js 引用了不存在的 DOM id: {missing}"


def test_api_routes_all_wired():
    js = _files()["app.js"]
    for route in API_ROUTES:
        assert route in js, f"app.js 未对接 {route}"
    assert re.search(r"DEBOUNCE_MS\s*=\s*300", js), "缺少 300ms 防抖常量"
    # 上传字段名与 WEB-SPEC §1 一致：uploadFile 按 kind 透传，LiDAR 诊断字面量 depth
    assert re.search(r"fd\.append\(kind,\s*file\)", js), "上传未按 kind 字段透传"
    assert re.search(r"fd\.append\('depth'", js), "LiDAR 诊断未上传 depth 字段"
    for kind in ("photo", "char", "depth"):
        assert f"'{kind}'" in js, f"上传种类 {kind} 缺失"
    # 显式参数口径 + 诊断自动带 depth_uploaded
    assert "depth_uploaded" in js and "state.touched" in js
    # 错误不清空参数：失败路径只走状态条
    assert "showApiError" in js
    # 抠图成功后设为当前立绘
    assert re.search(r"apiJson\('/api/matte-result-as-char'", js)


def test_param_schema_matches_cli():
    files = _files()
    html, js = files["index.html"], files["app.js"]
    # params schema 十五键（WEB-SPEC §2 / cli.py 逐字同名）
    keys = ["x", "d", "h", "hp", "feq", "pitch", "roll", "shot",
            "char_scale", "frame", "rot_basis", "mode", "size_pct",
            "edge_bar", "tone"]
    for k in keys:
        assert re.search(r"\b" + re.escape(k) + r"\s*:", js), f"app.js 缺参数键 {k}"
    # shot/frame/rot/mode/edge 的 CLI choices 在 HTML 里齐全
    for opt in ("全身", "膝上", "腰上", "胸上", "头肩"):
        assert f'<option value="{opt}"' in html, f"shot 选项缺 {opt}"
    for opt in ("自由", "1:1", "4:5", "16:9", "9:16", "满幅"):
        assert f'<option value="{opt}"' in html, f"frame 选项缺 {opt}"
    assert '<option value="free">' in html and '<option value="frame">' in html
    for v in ("0", "1", "2", "3", "4"):
        assert f'<option value="{v}"' in html, f"edge-bar 选项缺 {v}"
    # 默认值 = S 默认
    for frag, why in HTML_DEFAULTS:
        assert frag in html, f"HTML 默认值与 S 不符：{why}"
    for frag, why in [
        ('id="p-x-num" step="0.05" value="0"', "x 默认 0"),
        ('id="p-d-num" step="0.05" value="4"', "d 默认 4"),
        ('id="p-h-num" step="0.05" value="1.6"', "h 默认 1.6"),
        ('id="p-hp-num" step="0.01" value="1.65"', "hp 默认 1.65"),
        ('id="p-feq-num" step="0.5" value="23"', "feq 默认 23"),
        ('id="p-pitch-num" step="0.5" value="0"', "pitch 默认 0"),
        ('id="p-roll-num" step="0.5" value="0"', "roll 默认 0"),
        ('id="p-char-scale-rng" min="0.1" max="3" step="0.05" value="1"', "char_scale 默认 1"),
        ('id="p-size-pct-rng" min="10" max="100" step="1" value="60"', "size_pct 默认 60"),
    ]:
        assert frag in html, f"HTML 默认值与 S 不符：{why}"
    # edge-bar 行默认隐藏（ground 模式）
    assert re.search(r'id="row-edge-bar"[^>]*hidden', html), "edge-bar 行应默认 hidden"


def test_css_tokens_and_responsive():
    css = _files()["style.css"]
    root = re.search(r":root\s*\{(.*?)\}", css, flags=re.S)
    assert root, "style.css 缺少 :root token 块"
    defined = set(re.findall(r"(--[\w-]+)\s*:", root.group(1)))
    used = set(re.findall(r"var\((--[\w-]+)\)", css))
    undefined = sorted(used - defined)
    assert not undefined, f"CSS 引用了未定义 token: {undefined}"
    assert len(defined) >= 8, "token 数量异常偏少"
    # WEB-STYLE.md 若已产出，token 名应由它决定；未产出时才锁中性默认
    if not (DOCS / "WEB-STYLE.md").exists():
        required = {"--bg", "--surface", "--surface2", "--text", "--muted",
                    "--accent", "--border", "--danger", "--ok"}
        missing = sorted(required - defined)
        assert not missing, f"WEB-STYLE.md 未就绪，应使用中性默认 token，缺: {missing}"
    # <900px 上下堆叠（单列）
    assert re.search(r"@media\s*\(\s*max-width:\s*900px\s*\)", css), "缺 900px 响应式断点"
    assert re.search(r"@media[^{]*\{[^}]*grid-template-columns:\s*1fr", css, flags=re.S), \
        "900px 断点应把两栏改为单列"


def test_js_no_external_and_hardening():
    js = _files()["app.js"]
    assert "http://" not in js and "https://" not in js, "app.js 出现外链，违反零 CDN"
    assert "innerHTML" not in js, "禁止 innerHTML：文件名/后端错误文本不可信"
    assert "alert(" not in js, "禁止 alert，错误一律进状态条"
    clean = _strip_js_noise(js)
    for a, b in (("(", ")"), ("{", "}"), ("[", "]")):
        assert clean.count(a) == clean.count(b), \
            f"app.js 括号配平异常（启发式检查）：{a}{b} {clean.count(a)}/{clean.count(b)}"
