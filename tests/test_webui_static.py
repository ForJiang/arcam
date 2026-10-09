"""前端 webui 静态检查（不起真服务；真服务冒烟由编排者按 WEB-SPEC §5 做）。

覆盖（W2 交付要求 + A5 前端双界面审计加固）：
- 本地版 ``src/arcam/webui/`` 与 Pages 版 ``docs/`` 两套三件套存在且引用关系正确；
- index.html 可被 html.parser 解析、非 void 标签配平、关键 DOM id 齐全；
- app.js 里每个 DOM id 引用（含 ``'p-' + key + '-num'`` 动态拼接、``bindDrop`` /
  ``setName`` / ``bindSelect`` / ``bindSlider`` 的字面量参数）都能在 index.html 找到；
- 本地版 app.js 对接 WEB-SPEC §1 全部 6 个 API 路由、300ms 防抖、15 个摆参键，
  且键名与 webapp.py 的 ``PARAM_DEFAULTS`` 逐字一致；
- Pages 版 app.js 动态 import 两个管线模块 + matte.js，参数键走 arcam-render.js 的
  camelCase；手写 zip 的起止标记在位；
- HTML 下拉/滑块默认值与 cli.py 的 S 默认一致（两版逐字相同）；
- style.css 有 :root token、var() 自洽（无未定义引用、无定义了没人用的死 token）、
  <900px 响应式；两套 CSS 都查；
- 两套 app.js 均无外链（零 CDN）、不用 innerHTML/alert、不 fetch 绝对 URL；
- 死代码守卫：HTML 里没有任何 id 既不被 JS 也不被 CSS 引用。
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

# params schema 十五键（WEB-SPEC §2 / cli.py 逐字同名）；depth_uploaded 由诊断状态自动带
PARAM_KEYS = ["x", "d", "h", "hp", "feq", "pitch", "roll", "shot",
              "char_scale", "frame", "rot_basis", "mode", "size_pct",
              "edge_bar", "tone",
              # 光影三开关（原版 LIGHT_ON/GRN/SHD；v1.3.0 接入，web 与 CLI 同语义）
              "light", "grain", "shadow"]

# bindNumeric 动态拼 id 的七个量（'p-' + key + '-num'/'-rng'）
NUMERIC_KEYS = ["x", "d", "h", "hp", "feq", "pitch", "roll"]

# Pages 版 app.js 必须对接的三个 JS 模块（index.html 只引 app.js 本身）
PAGES_MODULES = ["./arcam-core.js", "./arcam-render.js", "./matte.js"]

# arcam-render.js normalizeParams 实际读的 camelCase 键（SITE-SPEC §1）
RENDER_KEYS = ["frame", "mode", "shot", "sizePct", "charScale",
               "rotBasis", "edgeBar", "tone"]

# 两版页面必须逐字一致的参数控件（LiDAR 专属块、抠图钮显隐除外，SITE-SPEC §3）
SHARED_CONTROL_IDS = [
    "p-x-num", "p-x-rng", "p-d-num", "p-d-rng", "p-h-num", "p-h-rng",
    "p-hp-num", "p-hp-rng", "p-feq-num", "p-feq-rng",
    "p-pitch-num", "p-pitch-rng", "p-roll-num", "p-roll-rng",
    "p-shot", "p-frame", "p-mode", "p-rot", "row-edge-bar", "p-edge-bar",
    "p-char-scale-rng", "p-char-scale-val",
    "p-size-pct-rng", "p-size-pct-val", "p-tone",
    "btn-rerender", "btn-png", "btn-layer", "btn-bundle",
    "btn-copy-scene", "preview-canvas", "preview-empty",
    "scene-details", "scene-json", "status-msg", "status-time",
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


def _pages_files():
    return {
        "index.html": (DOCS / "index.html").read_text(encoding="utf-8"),
        "style.css": (DOCS / "style.css").read_text(encoding="utf-8"),
        "app.js": (DOCS / "app.js").read_text(encoding="utf-8"),
        "arcam-core.js": (DOCS / "arcam-core.js").read_text(encoding="utf-8"),
        "arcam-render.js": (DOCS / "arcam-render.js").read_text(encoding="utf-8"),
        "matte.js": (DOCS / "matte.js").read_text(encoding="utf-8"),
    }


def _ids_of(html):
    p = _IdCollector()
    p.feed(html)
    p.close()
    return p.ids


def _js_dom_refs(js):
    """app.js 引用的全部 DOM id（含动态拼接与辅助函数的字面量参数）。"""
    refs = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", js))
    refs |= set(re.findall(r"\$\(['\"]([^'\"]+)['\"]\)", js))
    # 动态拼接：bindNumeric 里 'p-' + key + '-num'/'-rng'
    for suffix in set(re.findall(r"'p-'\s*\+\s*key\s*\+\s*'([^']+)'", js)):
        for key in NUMERIC_KEYS:
            refs.add("p-" + key + suffix)
    # 位置感知的 binder 提取（这些函数的 id 参数位置各不相同，动态 $() 看不到）：
    #   bind3(id, key, …)            首参是 id；其输出 span 是 'o' + id
    #   bindDrop(id, inputId, kind)   前两参是 id
    #   bindSelect(key, selectId)     次参是 id（首参是 param key）
    #   bindSlider(key, rngId, valId, fmt)  后两参是 id
    def _args_of(fn):
        out = []
        for m in re.finditer(r"\b" + fn + r"\(([^)]*)\)", js):
            out.append(re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)))
        return out
    for a in _args_of("bind3"):
        if a:
            refs.add(a[0]); refs.add("o" + a[0])
    for a in _args_of("bindDrop"):
        refs.update(a[:2])
    for a in _args_of("bindSelect"):
        if len(a) > 1:
            refs.add(a[1])
    for a in _args_of("bindSlider"):
        refs.update(a[1:3])
    # 辅助函数的字面量 id 参数
    for a, b in re.findall(r"bindDrop\('([^']+)',\s*'([^']+)'", js):
        refs.update((a, b))
    refs |= set(re.findall(r"setName\('([^']+)'", js))
    refs |= set(re.findall(r"bindSelect\('[^']+',\s*'([^']+)'", js))
    for a, b in re.findall(r"bindSlider\('[^']+',\s*'([^']+)',\s*'([^']+)'", js):
        refs.update((a, b))
    return refs


def _css_id_refs(css):
    return set(re.findall(r"#([A-Za-z][\w-]*)", css))


def _s_defaults(js):
    m = re.search(r"S_DEFAULTS\s*=\s*\{(.*?)\}", js, flags=re.S)
    assert m, "app.js 里找不到 S_DEFAULTS"
    return " ".join(m.group(1).split())


def _ranges(js):
    m = re.search(r"RANGES\s*=\s*\{(.*?)\};", js, flags=re.S)
    assert m, "app.js 里找不到 RANGES"
    return " ".join(m.group(1).split())


def _tag_with_id(html, dom_id):
    """取带该 id 的标签开标签（input/select/div…），用于两版逐字比对。"""
    m = re.search(r"<(\w+)([^>]*\bid=\"" + re.escape(dom_id) + r"\"[^>]*)>", html)
    assert m, f"HTML 里没有 id={dom_id}"
    return f"<{m.group(1)}{m.group(2)}>"


def _select_block(html, dom_id):
    m = re.search(r'<select[^>]*\bid="' + re.escape(dom_id) + r'"[^>]*>.*?</select>',
                  html, flags=re.S)
    assert m, f"HTML 里没有 select id={dom_id}"
    return " ".join(m.group(0).split())


def _fn_body(js, name):
    """取 JS 函数体（`function name(...) { … }`，按花括号配平切）。"""
    i = js.find("function " + name + "(")
    assert i >= 0, f"app.js 里找不到 function {name}"
    depth, j = 0, js.find("{", i)
    for k in range(j, len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[j:k + 1]
    raise AssertionError(f"function {name} 花括号不配平")


# ---------------------------------------------------------------- 本地版 webui

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
    assert not re.search(r'(?:src|href)\s*=\s*["\']/', html.replace('/static/', '')), \
        "index.html 引用了 /static/ 之外的绝对路径资源"
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
    # 单页骨架：预览 canvas + HUD 浮层 canvas（FEATURE-SPEC §4 的 HUD 常驻
    # 浮层，出图不带 HUD）；i18n/hud/live/app 四模块 + 一个样式表
    assert html.count("<canvas") == 2
    # webui：i18n/hud/live/app 四模块（app.js 是普通 script）
    assert html.count("<script") == 4
    assert html.count('rel="stylesheet"') == 1


def test_js_dom_references_resolve():
    files = _files()
    html, js = files["index.html"], files["app.js"]
    ids = _ids_of(html)
    refs = _js_dom_refs(js)
    assert refs, "app.js 里没解析到任何 DOM id 引用（正则或写法变了？）"
    missing = sorted(refs - ids)
    assert not missing, f"app.js 引用了不存在的 DOM id: {missing}"


def test_no_dead_dom_ids():
    """死代码守卫：HTML 里的 id 必须被 JS 或 CSS 引用到（未引用的一律删）。

    ⚠ 扫描**目录下全部 .js**（不止 app.js）：webui 拆成 i18n/hud/live/app 四个
    模块后，相机与 HUD 的 id 引用落在 live.js/hud.js 里；docs 侧同理。
    """
    for label, folder in (("webui", WEBUI), ("docs", DOCS)):
        html = (folder / "index.html").read_text(encoding="utf-8")
        js = "\n".join(p.read_text(encoding="utf-8")
                       for p in sorted(folder.glob("*.js")))
        css = (folder / "style.css").read_text(encoding="utf-8")
        dead = sorted(_ids_of(html) - _js_dom_refs(js) - _css_id_refs(css))
        assert not dead, f"{label}/index.html 有未引用的死 DOM id: {dead}"


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
    for k in PARAM_KEYS:
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


def test_param_keys_match_webapp():
    """前端参数键必须与 webapp.py 的 PARAM_DEFAULTS 逐字一致（键名漂移 → 400）。"""
    src = (ROOT / "src" / "arcam" / "webapp.py").read_text(encoding="utf-8")
    m = re.search(r"PARAM_DEFAULTS[^=]*=\s*\{(.*?)\}", src, flags=re.S)
    assert m, "webapp.py 里找不到 PARAM_DEFAULTS"
    backend = set(re.findall(r'"([a-z_]+)"\s*:', m.group(1)))
    assert backend == set(PARAM_KEYS) | {"depth_uploaded"}, \
        f"PARAM_DEFAULTS 与 WEB-SPEC §2 不符：{sorted(backend)}"
    for label, js in (("webui", _files()["app.js"]), ("docs", _pages_files()["app.js"])):
        got = set(re.findall(r"([a-z_]+)\s*:", _s_defaults(js)))
        assert got == set(PARAM_KEYS), f"{label}/app.js 的 S_DEFAULTS 键不一致：{sorted(got)}"


def test_css_tokens_and_responsive():
    css = _files()["style.css"]
    root = re.search(r":root\s*\{(.*?)\}", css, flags=re.S)
    assert root, "style.css 缺少 :root token 块"
    defined = set(re.findall(r"(--[\w-]+)\s*:", root.group(1)))
    used = set(re.findall(r"var\((--[\w-]+)\)", css))
    undefined = sorted(used - defined)
    assert not undefined, f"CSS 引用了未定义 token: {undefined}"
    assert len(defined) >= 8, "token 数量异常偏少"
    # token 名/值以 docs/WEB-STYLE.md §2 为准（该文档已在库，逐值比对见
    # test_dark_mode_tokens_complete 与 test_pages_css_matches_local_tokens）
    # <900px 上下堆叠（单列）
    assert re.search(r"@media\s*\(\s*max-width:\s*900px\s*\)", css), "缺 900px 响应式断点"
    assert re.search(r"@media[^{]*\{[^}]*grid-template-columns:\s*1fr", css, flags=re.S), \
        "900px 断点应把两栏改为单列"


def test_css_tokens_fully_used():
    """死代码守卫：:root 里定义的 token 必须被 var() 引用（WEB-STYLE 的用途表）。"""
    for label, folder in (("webui", WEBUI), ("docs", DOCS)):
        css = (folder / "style.css").read_text(encoding="utf-8")
        root = re.search(r":root\s*\{(.*?)\}", css, flags=re.S)
        defined = set(re.findall(r"(--[\w-]+)\s*:", root.group(1)))
        used = set(re.findall(r"var\((--[\w-]+)\)", css))
        dead = sorted(defined - used)
        assert not dead, f"{label}/style.css 有定义了没人用的死 token: {dead}"


def test_subtitle_bottom_margin():
    """副标题与 forjiang-crypto 的 .sub 同规格：margin 0 0 20px（两处都要在）。"""
    for label, folder in (("webui", WEBUI), ("docs", DOCS)):
        css = (folder / "style.css").read_text(encoding="utf-8")
        m = re.search(r"#app-sub\s*\{(.*?)\}", css, flags=re.S)
        assert m, f"{label}/style.css 缺少 #app-sub 规则"
        assert re.search(r"margin:\s*0\s+0\s+20px", m.group(1)), \
            f"{label}/style.css 的 #app-sub 下边距不是 20px"


def test_dark_mode_tokens_complete():
    """深浅两套 token 一一对应（暗色不许漏项，也不许多项/引入新名）。"""
    core_colors = {"--bg", "--surface", "--text", "--muted", "--border",
                   "--accent", "--danger", "--ok", "--info", "--hint"}
    for label, folder in (("webui", WEBUI), ("docs", DOCS)):
        css = (folder / "style.css").read_text(encoding="utf-8")
        roots = re.findall(r":root\s*\{(.*?)\}", css, flags=re.S)
        assert len(roots) >= 2, f"{label}/style.css 缺暗色 token 块"
        light = set(re.findall(r"(--[\w-]+)\s*:", roots[0]))
        dark = set(re.findall(r"(--[\w-]+)\s*:", roots[1]))
        extra = sorted(dark - light)
        assert not extra, f"{label} 暗色块引入了亮色没有的 token: {extra}"
        missing = sorted(core_colors - dark)
        assert not missing, f"{label} 暗色块漏了核心色 token: {missing}"
        assert "prefers-color-scheme: dark" in css


def test_js_no_external_and_hardening():
    js = _files()["app.js"]
    assert "http://" not in js and "https://" not in js, "app.js 出现外链，违反零 CDN"
    assert "innerHTML" not in js, "禁止 innerHTML：文件名/后端错误文本不可信"
    assert "alert(" not in js, "禁止 alert，错误一律进状态条"
    assert not re.search(r"fetch\(\s*['\"]https?://", js), "app.js 不允许 fetch 绝对 URL"
    clean = _strip_js_noise(js)
    for a, b in (("(", ")"), ("{", "}"), ("[", "]")):
        assert clean.count(a) == clean.count(b), \
            f"app.js 括号配平异常（启发式检查）：{a}{b} {clean.count(a)}/{clean.count(b)}"


# ---------------------------------------------------------------- Pages 版 docs/

def test_pages_files_exist_and_referenced():
    files = _pages_files()
    html = files["index.html"]
    for name, text in files.items():
        assert len(text) > 1000, f"docs/{name} 内容过少，疑似没写完"
    # Pages 只伺服静态文件：同目录相对路径，零绝对路径、零外链（SITE-SPEC §0）
    assert './style.css' in html and 'src="./app.js"' in html
    assert 'type="module"' in html
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', html), "index.html 出现外链资源"
    assert not re.search(r'(?:src|href)\s*=\s*["\']/', html), "index.html 出现绝对路径资源"
    # index.html 引 7 个 script：i18n/matte-tools/hud/live/mmd.js 五个普通 script
    # （window 挂钩，按依赖顺序加载）+ app.js 一个 module + importmap 一个。
    # C1 第一阶段新增的两个：mmd.js 是 3D 立绘运行时（index.html 里的经典 script，
    # 内部按需动态 import three）；importmap 给它（以及 GLTFLoader/three-mmd 里的
    # 裸模块说明符 'three'）映射到本地 ./mmd/three.module.js —— 零外链的前提。
    # 两个管线模块（arcam-core/arcam-render）仍由 app.js 动态 import，不写死在 html 里。
    assert html.count("<script") == 7
    for direct in ("i18n.js", "matte-tools.js", "hud.js", "live.js", "mmd.js"):
        assert f'"./{direct}"' in html, f"index.html 缺 {direct} 引用"
    # importmap 必须在任何动态 import 之前落地，且只映射同目录本地路径
    assert html.count('type="importmap"') == 1
    assert html.index('type="importmap"') < html.index('src="./')
    assert './mmd/three.module.js' in html and '"three/addons/":"./mmd/"' in html
    # mmd.js 用 document.currentScript 拼资源路径 —— 必须是经典 script，不能 module
    assert re.search(r'<script src="\./mmd\.js" defer></script>', html), \
        "mmd.js 必须是经典 script（module 下 document.currentScript 是 null）"
    assert html.count('rel="stylesheet"') == 1
    js = files["app.js"]
    for mod in PAGES_MODULES:
        assert f"'{mod}'" in js, f"app.js 未动态 import {mod}"
    # 抠图挂钩点与懒加载（SITE-SPEC §3/§4）
    assert "window.arcamMatte" in js and "refreshMatteBtn" in js
    assert "'arcam:matte-ready'" in js
    # 手写 store 式 zip 的起止标记在位（三件套语义）
    assert "[ZIP-BEGIN]" in js and "[ZIP-END]" in js
    assert "'shot.jpg'" in js and "'scene.json'" in js and "'character.png'" in js
    assert "application/zip" in js


def test_pages_html_parses_and_ids_resolve():
    files = _pages_files()
    html, js = files["index.html"], files["app.js"]
    p = _IdCollector()
    p.feed(html)
    p.close()
    assert not p.errors, f"docs/index.html 结构问题：{p.errors}"
    assert not p.stack, f"docs/index.html 有未闭合标签：{p.stack}"
    # Pages 版无 LiDAR 深度帧/env：这些 id 不该出现
    for absent in ("drop-depth", "file-depth", "depth-name", "btn-lidar", "env-box"):
        assert absent not in p.ids, f"Pages 版不应有 LiDAR 专属 id: {absent}"
    # 共用的控件/状态 id 一个不能少
    for id_ in SHARED_CONTROL_IDS:
        assert id_ in p.ids, f"docs/index.html 缺少共用 DOM id: {id_}"
    refs = _js_dom_refs(js)
    assert refs, "docs/app.js 里没解析到任何 DOM id 引用"
    missing = sorted(refs - p.ids)
    assert not missing, f"docs/app.js 引用了不存在的 DOM id: {missing}"


def test_pages_js_no_external_and_hardening():
    files = _pages_files()
    js, matte = files["app.js"], files["matte.js"]
    # 资源引用一律同目录相对路径：不允许 src/href/fetch()/import() 绝对 URL
    # （错误文案里出现的 http://localhost 提示语不算外链）
    for name, text in (("app.js", js), ("matte.js", matte)):
        for pat in (r'src\s*=\s*["\']https?://', r'href\s*=\s*["\']https?://',
                    r'fetch\(\s*["\']https?://', r'import\(\s*["\']https?://'):
            assert not re.search(pat, text), f"docs/{name} 出现外链资源: {pat}"
        assert "innerHTML" not in text, f"docs/{name} 用了 innerHTML"
        assert "alert(" not in text, f"docs/{name} 用了 alert"
        for host in ("unpkg", "jsdelivr", "cdnjs", "googleapis"):
            assert host not in text.lower(), f"docs/{name} 发现 CDN 宿主痕迹: {host}"
    # 防抖 + 过期响应哨兵 + 参数越界预检（与本地版同口径）
    assert re.search(r"DEBOUNCE_MS\s*=\s*300", js), "Pages 版缺 300ms 防抖常量"
    assert "renderSeq" in js, "Pages 版缺过期渲染哨兵"
    assert re.search(r"core\.SHOTS\[", js) and re.search(r"core\.FRAMES", js), \
        "Pages 版渲染前应校验 shot/frame 是否在 core 的表里"
    # 抠图失败不静默返回原图（SITE-SPEC §4）
    assert "throw new Error" in matte


def test_pages_param_schema():
    files = _pages_files()
    js = files["app.js"]
    # 页面状态用 snake_case 十五键（与本地版同一套 S_DEFAULTS）
    for k in PARAM_KEYS:
        assert re.search(r"\b" + re.escape(k) + r"\s*:", js), f"docs/app.js 缺参数键 {k}"
    # composeParams 把十五键映射到 arcam-render.js normalizeParams 读的 camelCase
    for k in RENDER_KEYS:
        assert re.search(r"\b" + re.escape(k) + r"\s*:", js), f"docs/app.js 缺渲染键 {k}"
    # frame 另收 frameKey 别名（arcam-render.js normalizeParams）
    assert "frameKey" in files["arcam-render.js"]
    # 导出三件套 + 立绘层入口都接上了
    for fn in ("composeShot", "composeCharLayer", "buildBundleZip"):
        assert fn in js, f"docs/app.js 未使用 {fn}"
    # 立绘层只能从 composeCharLayer 拿：composeShot 的返回值是 {shot,rect,scene,light}，
    # 里面没有层字段——在 doExportBundle 里读 r.charLayer 会永远落空、白跑一遍合成
    # （先去注释再查：注释里提到 Python 的 compose_char_layer 不算）
    bundle = _strip_js_noise(_fn_body(js, "doExportBundle"))
    assert "composeCharLayer(" in bundle, "doExportBundle 应走 composeCharLayer 取立绘层"
    assert "charLayer" not in bundle and "char_layer" not in bundle, \
        "doExportBundle 从 composeShot 的返回值里读不存在的立绘层字段"
    # 本地版独有的三件套参数键不会漏传到渲染侧
    for frag in ("sizePct: p.size_pct", "charScale: p.char_scale",
                 "rotBasis: p.rot_basis", "edgeBar: p.edge_bar"):
        assert frag in js, f"composeParams 缺映射: {frag}"


def test_pages_css_matches_local_tokens():
    """Pages 版 style.css 与本地版同一套 token（SITE-SPEC §0：照抄本地版）。"""
    local = _files()["style.css"]
    pages = _pages_files()["style.css"]
    local_root = re.search(r":root\s*\{(.*?)\}", local, flags=re.S).group(1)
    pages_root = re.search(r":root\s*\{(.*?)\}", pages, flags=re.S).group(1)

    def tokens(block):
        return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))

    assert tokens(local_root) == tokens(pages_root), "两套 :root token 不一致"
    # 副标题 20px 下边距、<900px 响应式两版都在
    assert re.search(r"#app-sub\s*\{[^}]*margin:\s*0\s+0\s+20px", pages, flags=re.S)
    assert re.search(r"@media\s*\(\s*max-width:\s*900px\s*\)", pages), "Pages 版缺 900px 断点"
    assert re.search(r"@media[^{]*\{[^}]*grid-template-columns:\s*1fr", pages, flags=re.S)


def test_param_controls_match_between_versions():
    """两版页面的共用参数控件逐字相同（SITE-SPEC §3：布局/控件 id 保持一致）。"""
    wh = _files()["index.html"]
    dh = _pages_files()["index.html"]
    for cid in SHARED_CONTROL_IDS:
        assert _tag_with_id(wh, cid) == _tag_with_id(dh, cid), \
            f"控件 {cid} 在两版页面里不一致"
    for sel in ("p-shot", "p-frame", "p-mode", "p-rot", "p-edge-bar"):
        assert _select_block(wh, sel) == _select_block(dh, sel), f"下拉 {sel} 两版不一致"
    # 抠图钮：本地版常显；Pages 版初始 hidden，window.arcamMatte 就绪才显示（SITE-SPEC §3）。
    # 判据是"标签上没有 hidden 属性"（data-t 等新属性不影响可见性）。
    matte_tag = _tag_with_id(wh, "btn-matte")
    assert "hidden" not in matte_tag, "本地版抠图钮应常显"
    assert "hidden" in _tag_with_id(dh, "btn-matte"), "Pages 版抠图钮应初始隐藏"
    assert re.search(r'id="btn-matte"[^>]*\bhidden\b', dh), "Pages 版抠图钮应默认隐藏"
    # 两版 S_DEFAULTS / RANGES 逐字相同
    assert _s_defaults(_files()["app.js"]) == _s_defaults(_pages_files()["app.js"]), \
        "两版 S_DEFAULTS 不一致"
    assert _ranges(_files()["app.js"]) == _ranges(_pages_files()["app.js"]), \
        "两版 RANGES 不一致"


def test_golden_fixture_present():
    """JS 侧数值基准必须在（SITE-SPEC §0 的验收基准）。

    placement 组含 free 模式 rot_basis=frame / edge_bar 吸附向量（审计补的
    断链回归钉）：这两个参数曾是 compose/webapp 整组漏传的重灾区，golden
    缺覆盖就等于 JS/Python 分叉也发现不了。
    """
    g = DOCS / "tests" / "golden.json"
    assert g.is_file(), "docs/tests/golden.json 缺失"
    import json
    data = json.loads(g.read_text(encoding="utf-8"))
    counts = {k: len(data[k]) for k in ("focal", "project", "place", "placement", "scene")}
    assert counts == {"focal": 40, "project": 200, "place": 120,
                      "placement": 102, "scene": 3}, f"golden 分组数量变了：{counts}"
    free = [v for v in data["placement"] if v["kind"] == "free"]
    assert any(v.get("rot_basis") == "frame" for v in free), \
        "free 向量缺 rot_basis=frame 回归钉"
    assert any(v.get("edge_bar") for v in free), "free 向量缺 edge_bar 吸附回归钉"


def test_i18n_tables_match_between_python_and_js():
    """两侧语言表逐条一致（i18n.js 由 i18n.py 同源生成；漂移=某一侧漏翻）。

    Python 侧 src/arcam/i18n.py 的 TXT 与 webui/i18n.js 的 ARCAM_TXT 必须
    键集合相同、每条两列都逐字相同。生成器是一次性脚本，手改任一侧就会漂移。
    """
    import json
    import re
    from arcam.i18n import TXT as PY_TXT

    js_src = (WEBUI / "i18n.js").read_text(encoding="utf-8")
    m = re.search(r"window\.ARCAM_TXT = \{(.*?)\n\};", js_src, re.S)
    assert m, "webui/i18n.js 里找不到 ARCAM_TXT"
    # JS 对象字面量带尾逗号（JSON 不容）——剥掉再解析
    js_txt = json.loads("{" + re.sub(r",\s*$", "", m.group(1).strip()) + "}")
    assert set(js_txt) == set(PY_TXT), \
        f"两侧键集合不同：仅 JS {sorted(set(js_txt) - set(PY_TXT))[:5]}，" \
        f"仅 Python {sorted(set(PY_TXT) - set(js_txt))[:5]}"
    for key, (zh, en) in PY_TXT.items():
        assert list(js_txt[key]) == [zh, en], f"{key} 两侧文案不同"
    assert len(PY_TXT) == 182, f"表条数变了：{len(PY_TXT)}（原版 160 + webui 17 + CLI 1；3D 五键原版就有）"


def test_mmd_3d_wired():
    """MMD 3D 层接线（FEATURE-SPEC §5）：vendor 在位、UI 齐全、钩子接通。

    真模型 E2E 需要用户自带 .zip/.glb/.pmx（原版模型也不随仓库分发），
    这里钉住**接线**：mmd.js 与 mmd/ 资源被 index.html 引用、app.js 用真实
    API（modelNames/modelIndex/poseNames/params）接线、composeShot 有 charDraw 钩子。
    """
    html = (DOCS / "index.html").read_text(encoding="utf-8")
    js = (DOCS / "app.js").read_text(encoding="utf-8")
    render = (DOCS / "arcam-render.js").read_text(encoding="utf-8")
    # ① vendor 与 script 引用
    assert (DOCS / "mmd.js").is_file(), "docs/mmd.js 缺失"
    assert (DOCS / "mmd" / "three.module.js").is_file(), "docs/mmd/three.module.js 缺失"
    assert (DOCS / "mmd" / "poses.json").is_file(), "docs/mmd/poses.json 缺失"
    assert 'src="./mmd.js"' in html, "index.html 没引 mmd.js"
    # ② 3D UI（原版移植的那套 id）
    for cid in ("t3d", "f3d", "f3dFile", "f3dDir", "model3dRow", "pose3dRow",
                "az3d", "el3d", "focus3d", "dof3d", "d3dLight", "d3dNote"):
        assert f'id="{cid}"' in html, f"3D UI 缺 {cid}"
    # ③ app.js 用 mmd.js 的真实 API 接线
    for call in ("ARCAM_MMD", "modelNames()", "modelIndex()", "poseNames()",
                 "switchModel(", "M3D.render(", "M3D.canvas()"):
        assert call in js, f"app.js 缺 3D 调用 {call}"
    # ④ composeShot 的 charDraw 钩子（3D 替换 drawInto 的唯一入口）
    assert "charDraw" in render and "p.charDraw" in render, \
        "arcam-render.js 的 composeShot 没有 charDraw 钩子"
    assert "charDraw" in js and "draw3DInto" in js, "app.js 没接 charDraw"
