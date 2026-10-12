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

# cli.py _add_placement 的 S 默认（WEB-SPEC §2）。用正则不用整标签字面量：
# option 上可以挂 data-t（i18n 预接线，见 test_datat_keys_resolve），字面量
# 会因属性增减误红；两版 option 的逐字一致另由 _select_block 门禁盯。
HTML_DEFAULTS = [
    (r'<option value="全身"\s+selected', "shot 默认 全身"),
    (r'<option value="满幅"\s+selected', "frame 默认 满幅"),
    (r'<option value="world"\s+selected', "rot_basis 默认 world"),
    (r'<option value="ground"\s+selected', "mode 默认 ground"),
    (r'<option value="0"\s+selected', "edge_bar 默认 0"),
]

# params schema 十五键（WEB-SPEC §2 / cli.py 逐字同名）；depth_uploaded 由诊断状态自动带
PARAM_KEYS = ["x", "d", "h", "hp", "feq", "pitch", "roll", "shot",
              "char_scale", "frame", "rot_basis", "mode", "size_pct",
              "edge_bar", "tone",
              # 光影三开关（原版 LIGHT_ON/GRN/SHD；v1.3.0 接入，web 与 CLI 同语义）
              "light", "grain", "shadow",
              # 阴影方向/浓度（v1.6.0 新增创作控制，默认 = 原版行为）
              "shadow_az", "shadow_opacity",
              # 颗粒强度倍率（v1.9.0）：乘在 bake_lit 颗粒幅度上，1.0 = 原版逐位一致
              "grain_scale",
              # 3D 路径（v1.7.0）：后端只出照片底，MMD 层客户端合成
              "base_only"]

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


def _strip_comments(src, html=False):
    """只剥注释、**保留字符串字面量**，供「接线是否真在代码里」的子串断言用。

    为什么需要它：裸 ``"bindSlider('grain_scale', …)" in js`` 会被整段注释
    骗过 —— 把那两行注释掉（括号仍配平）测试照样绿，滑块变成死控件，正是
    「控件存在但到不了执行点」这个最高发 bug 类的检测漏洞（v1.9.0 对抗复核
    变异实测复现）。``_strip_js_noise`` 连字符串一起抹，不能用于子串断言。
    ``html=True`` 时连 ``<!-- -->`` 一起剥（注释掉的控件同样不该算数）。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        if html and src.startswith("<!--", i):
            j = src.find("-->", i)
            i = n if j < 0 else j + 3
            continue
        c = src[i]
        if c in "'\"`":                      # 字符串/模板字面量整体跳过
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    j += 1
                    break
                j += 1
            out.append(src[i:j])
            i = j
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


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


def _enclosing_fn(js, pos):
    """返回包含位置 pos 的最近**命名**函数声明首行（`async function x(` / `function x(`）。

    匿名回调（`.then(function (res) {`）不匹配：那些位置归到外层命名函数，
    对本仓库的调用点判定够用（真正的匿名回调里的下载点另有专项测试盯）。
    """
    best = None
    for m in re.finditer(r"^(async\s+)?function\s+(\w+)\s*\(", js, flags=re.M):
        if m.start() < pos:
            best = m
        else:
            break
    assert best is not None, f"位置 {pos} 不在任何命名函数里"
    return best.group(0)


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
    # webui：importmap + i18n/hud/live/md/app（app.js 普通 script；importmap 不是 src script
    # 但也算 <script 标签，mmd.js 是 3D 运行时，v1.7.0 加入）
    assert html.count("<script") == 6
    assert html.count('rel="stylesheet"') == 1


def test_js_dom_references_resolve():
    """每个 .js 引用的全部 DOM id 都必须能在 index.html 找到（两侧、全部模块）。

    2026-10-11 W5 审计补宽：原来只查 app.js —— live.js/hud.js/matte-tools.js/
    mmd.js/i18n.js 里的 id 引用（`$('cam-video')` 这类）没有任何门禁盯着，
    「JS 引了但 HTML 没有」正好是静默失效高发类，故按目录扫全部 .js。
    """
    for label, folder in (("webui", WEBUI), ("docs", DOCS)):
        html = (folder / "index.html").read_text(encoding="utf-8")
        ids = _ids_of(html)
        for js in sorted(folder.glob("*.js")):
            src = js.read_text(encoding="utf-8")
            refs = _js_dom_refs(src)
            # app.js 是页面主脚本，必须有 id 引用；hud.js 这类纯绘制模块
            # （ctx 由调用方传进来）一个 id 都不碰是正常的，不强制非空
            assert refs or js.name != "app.js", \
                f"{label}/{js.name} 里没解析到任何 DOM id 引用（正则或写法变了？）"
            missing = sorted(refs - ids)
            assert not missing, f"{label}/{js.name} 引用了不存在的 DOM id: {missing}"


def test_state_fields_all_written():
    """死状态字段守卫：app.js 里读到的 ``state.X`` 必须有真实写入点。

    踩过的坑（v1.5.0 B5 审计实修）：webui/app.js 读 ``state.charName`` 决定修图三钮
    能不能点、HUD 有没有立绘，但上传分支只写过 ``state.char`` —— ``charName`` 从来
    没被赋值，三个按钮整个点不动、HUD 永远报「还没选立绘」，而静态检查完全看不见。
    自赋值（``state.X = state.X``）不算写入，那种行本身就是死代码。
    """
    for label, js in (("webui", _files()["app.js"]), ("docs", _pages_files()["app.js"])):
        clean = _strip_js_noise(js)
        m = re.search(r"\bstate\s*=\s*\{(.*?)\n\};", js, flags=re.S)
        assert m, f"{label}/app.js 里找不到 state 字面量"
        declared = set(re.findall(r"^\s{2}(\w+):", m.group(1), flags=re.M))
        reads = set(re.findall(r"\bstate\.(\w+)", clean))
        # 写入点：state.X = …（排除自赋值 state.X = state.X）
        writes = set()
        for mm in re.finditer(r"\bstate\.(\w+)\s*=(?!=)", clean):
            if re.search(r"\bstate\." + mm.group(1) + r"\s*=\s*state\." + mm.group(1) + r"\b", clean):
                continue
            writes.add(mm.group(1))
        dead = sorted(reads - writes - declared)
        assert not dead, f"{label}/app.js 有只读不写的 state 字段（静默失效高发区）: {dead}"


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
    # （option 可挂 data-t，判据放宽到「标签存在」；逐字一致走 _select_block）
    for opt in ("全身", "膝上", "腰上", "胸上", "头肩"):
        assert re.search(r'<option value="%s"[^>]*>' % re.escape(opt), html), \
            f"shot 选项缺 {opt}"
    for opt in ("自由", "1:1", "4:5", "16:9", "9:16", "满幅"):
        assert re.search(r'<option value="%s"[^>]*>' % re.escape(opt), html), \
            f"frame 选项缺 {opt}"
    assert re.search(r'<option value="free"[^>]*>', html) and \
        re.search(r'<option value="frame"[^>]*>', html)
    for v in ("0", "1", "2", "3", "4"):
        assert re.search(r'<option value="%s"[^>]*>' % v, html), f"edge-bar 选项缺 {v}"
    # 默认值 = S 默认（HTML_DEFAULTS 是正则，见其定义处注释）
    for frag, why in HTML_DEFAULTS:
        assert re.search(frag, html), f"HTML 默认值与 S 不符：{why}"
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


def test_grain_scale_slider_wired():
    """颗粒强度滑块（v1.9.0）：HTML 控件 + app.js 绑定 + 记忆写回三段齐全。

    「参数存在于代码但永远到不了执行点」是本项目最高发 bug 类，所以逐段钉死：
    滑块 id 在 HTML、bindSlider 把它绑到 grain_scale 参数键、
    syncControlsFromParams 把它写回控件（缺任何一段 = 假控件）。
    """
    html, js = _files()["index.html"], _files()["app.js"]
    # 剥注释后再断言：注释掉的控件/接线不算数（见 _strip_comments 的说明）
    html, js = _strip_comments(html, html=True), _strip_comments(js)
    assert 'id="p-grain-scale-rng" min="0" max="3" step="0.05" value="1"' in html, \
        "index.html 缺颗粒强度滑块（量程 0–3、步进 0.05、默认 1）"
    assert 'id="p-grain-scale-val"' in html, "index.html 缺颗粒强度数值 span"
    assert "bindSlider('grain_scale', 'p-grain-scale-rng', 'p-grain-scale-val'" in js, \
        "app.js 未把颗粒强度滑块绑到 grain_scale 参数键"
    # 参数记忆恢复后必须写回控件，否则界面与 state.params 不一致
    body = _strip_comments(_fn_body(js, "syncControlsFromParams"))
    assert re.search(r"\$\('p-grain-scale-rng'\)\.value\s*=\s*state\.params\.grain_scale",
                     body), "syncControlsFromParams 未写回颗粒强度滑块"
    assert re.search(r"\$\('p-grain-scale-val'\)\.textContent\s*=\s*"
                     r"state\.params\.grain_scale\.toFixed\(2\)", body), \
        "syncControlsFromParams 未写回颗粒强度数值 span"


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


def test_save_blob_fallback_chain():
    """保存兜底链（原版 index.html:3407-3453 移植）：share → picker → download。

    静态钉死四件事：三段兜底与顺序、AbortError 静默取消、doExport 不再有
    内联 a.download 重复实现、所有 saveBlob 调用点都带 await（漏 await 会
    静默丢下载——saveBlob 现在是 async，调用处不 await 就抓不到取消）。
    """
    js = _files()["app.js"]
    raw = _fn_body(js, "saveBlob")
    # ① 三段兜底齐全且顺序正确（share 最早、<a download> 垫底）
    for frag in ("navigator.canShare", "navigator.share", "showSaveFilePicker",
                 "createWritable", "URL.createObjectURL"):
        assert frag in raw, f"saveBlob 缺兜底段 {frag}"
    order = [raw.index("navigator.canShare"), raw.index("showSaveFilePicker"),
             raw.index("URL.createObjectURL")]
    assert order == sorted(order), "saveBlob 兜底顺序应为 share → picker → download"
    # 安卓 WebView 桥明确不移植（本项目没有安卓壳，原版 index.html:3417-3426）
    assert "ArcLog" not in raw, "saveBlob 不应出现安卓桥 ArcLog"
    assert "new File([blob], name, { type: blob.type || 'application/octet-stream' })" in raw, \
        "saveBlob 未用 File 包装 blob（blob.type 为空时兜底 octet-stream）"
    assert "navigator.canShare({ files: [file] })" in raw and \
        "navigator.share({ files: [file] })" in raw, "share 段缺 files 传参"
    assert "window.showSaveFilePicker({ suggestedName: name })" in raw, \
        "picker 段缺 suggestedName"
    # ② 用户取消静默返回；四返回值都在
    assert "AbortError" in raw and "'cancel'" in raw, "取消应静默返回 'cancel'"
    for ret in ("'share'", "'picker'", "'download'"):
        assert ret in raw, f"saveBlob 缺返回值 {ret}"
    # ③ doExport 收敛到 saveBlob，不再内联一份下载代码
    exp = _fn_body(js, "doExport")
    assert re.search(r"await\s+saveBlob\(", exp), "doExport 应 await saveBlob"
    assert "createElement" not in exp and ".download" not in exp, \
        "doExport 里还有内联下载代码（重复实现，应收敛到 saveBlob）"
    # ④ 调用点数 = await 调用点数（定义行那处不算）：漏一个 await 就是静默失效
    calls = len(re.findall(r"\bsaveBlob\(", js))
    awaited = len(re.findall(r"await\s+saveBlob\(", js))
    assert calls == awaited + 1, f"saveBlob 共 {calls} 处引用、仅 {awaited} 处带 await"
    # ⑤ 每个调用点都落在 async 函数里：saveBlob 是 async，同步函数里连 await
    #  都写不了，Promise 会被静默丢掉（用户点下载没反应）
    for m in re.finditer(r"\bsaveBlob\(", js):
        if js[:m.start()].rstrip().endswith("function"):
            continue                      # 定义行本身
        head = _enclosing_fn(js, m.start())
        assert head.startswith("async function"), \
            f"saveBlob 调用点在非 async 函数里（{head.strip()}），await 写不了"


def test_batch_wired_once():
    """批量出图只能有一条接线（v1.6.0 双接线事故回归）。

    踩过的坑：init 里内联防 change 调 doBatch，bindBatch() 又各自绑了一份
    change —— 选一次文件发两次 /api/batch、下两个 zip、状态条还被覆盖。
    这里钉死唯一接线 + 唯一 fetch 点。
    """
    js = _strip_comments(_files()["app.js"])
    assert "function bindBatch" not in js and re.search(r"\bbindBatch\(\)", js) is None, \
        "bindBatch 是与 init 里 doBatch 接线重复的死代码（双触发批量）"
    assert re.search(r"\$\('file-photos'\)\.addEventListener\('change'", js), \
        "init 里应保留 file-photos 的 change 接线（调 doBatch）"
    assert len(re.findall(r"\$\('file-photos'\)\.addEventListener\('change'", js)) == 1, \
        "file-photos 的 change 只能绑一次（双绑 = 批量跑两遍）"
    assert js.count("fetch('/api/batch'") == 1, "/api/batch 应只有一个 fetch 点"
    assert "function doBatch(files)" in js, "doBatch 是批量出图的唯一实现"


def test_frame_read_css_both_sides():
    """取景框读数样式两版都要有（v1.9.0 W5 审计实修：docs 侧整条漏掉）。

    踩过的坑：docs/index.html:267 有 `<span class="frame-read">`、app.js 的
    updateFrameRead 每帧刷它，但 docs/style.css 没有对应规则 —— span 掉回
    正文 13px/正文色且可换行，与本地版观感不一致（SITE-SPEC §3 要求两版
    控件一致）。这里两版都钉死，防再次漂移。
    """
    for label, folder in (("webui", WEBUI), ("docs", DOCS)):
        css = (folder / "style.css").read_text(encoding="utf-8")
        assert re.search(r"\.frame-read\s*\{", css), \
            f"{label}/style.css 缺 .frame-read 规则（frame-read span 会掉回正文样式）"


def test_docs_hud_shots_single_source():
    """docs/hud.js 的景别表必须来自 arcam-core.js（单一来源）。

    webui 版没有 arcam-core（后端渲染），那份自带 SHOTS 是固有差异；docs 侧
    再本地抄一份就是两份真值 —— 改一处漏一处，两侧景别口径悄悄分叉。
    """
    hud = (DOCS / "hud.js").read_text(encoding="utf-8")
    assert re.search(r'import\s*\{[^}]*\bSHOTS\b[^}]*\}\s*from\s*"\./arcam-core\.js"', hud), \
        "docs/hud.js 未从 arcam-core.js import SHOTS"
    assert not re.search(r"\bvar\s+SHOTS\s*=\s*\{", hud), \
        "docs/hud.js 又本地定义了一份 SHOTS（应收敛到 arcam-core.js 的 import）"


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
    # 条数口径（实测，别按印象写）：原版 index.html:600-830 TXT 逐条搬来 160 条
    # （其中 4 条改了键名，如 kind.closeTip/sec.input 这类对不上原名的）+ webui/docs
    # 历次新增：v1.3.0 起 26 条（sec.input/dz.*/btn.png/fold.fx/hint.fxWeb 等）
    # → 190；v1.10.0 英文支持补全再加 102 条（live.*/hud.* 构图提示/app.js 状态
    # 文案）→ 292。改这条断言前先跑：
    #   .venv/bin/python -c "from arcam.i18n import TXT; print(len(TXT))"
    assert len(PY_TXT) == 338, f"表条数变了：{len(PY_TXT)}（292 + 网页全面中文化 51 − 5 个零引用死键）"


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


def test_no_python_style_comments_in_js():
    """JS 文件里不许有 Python 风格 ``#`` 注释（SITE-SPEC §0 的零构建红线）。

    踩过的坑：给 arcam-render.js 加 shadow 门时一行注释写成了 ``#`` —— 整个
    module 语法错误、动态 import 失败、Pages 版**全部功能不可用**（状态条报
    「运算模块未就绪」）。这种笔误单测看不见，只有真加载才炸，所以在静态门禁
    里按行扫：行首（缩进后）是 ``#`` 的一律红。URL 片段（``http://x#y``）在
    字符串里、不在行首，不受影响。
    """
    import shutil
    import subprocess
    folders = [WEBUI, DOCS]
    for folder in folders:
        for js in sorted(folder.glob("*.js")):
            text = js.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    raise AssertionError(
                        f"{js.relative_to(ROOT)}:{i} 是 Python 风格注释：{line.strip()[:60]}")
            # 有 node 就再做一次真语法检查（module 语义：复制成 .mjs）
            node = shutil.which("node")
            if node:
                import tempfile
                with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
                    f.write(text)
                    tmp = f.name
                try:
                    r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
                    assert r.returncode == 0, \
                        f"{js.relative_to(ROOT)} 语法错误：{r.stderr.strip()[:200]}"
                finally:
                    Path(tmp).unlink(missing_ok=True)


def test_webui_has_mmd_3d_stack():
    """本机网页版 3D 立绘栈（v1.7.0）：vendor 在位、importmap/script、UI 与接线齐全。

    与 Pages 版（docs/）同源；分工差异写在这里：后端只出照片底
    （base_only），WebGL 层客户端合成。
    """
    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    js = (WEBUI / "app.js").read_text(encoding="utf-8")
    # ① vendor 文件
    for f in ("mmd.js", "mmd/three.module.js", "mmd/poses.json"):
        assert (WEBUI / f).is_file(), f"webui/{f} 缺失"
    # ② importmap 必须在任何 script 之前（裸模块说明符 'three' 的唯一解析方式）
    assert 'type="importmap"' in html
    assert html.index('type="importmap"') < html.index('src="/static/i18n.js"')
    assert '/static/mmd/three.module.js' in html
    assert 'src="/static/mmd.js"' in html
    # ③ UI 控件（与 docs 版同一套 id）
    for cid in ("t3d", "f3d", "f3dFile", "f3dDir", "model3dRow", "pose3dRow",
                "az3d", "el3d", "focus3d", "dof3d", "d3dLight", "d3dNote"):
        assert f'id="{cid}"' in html, f"3D UI 缺 {cid}"
    # ④ app.js 接线：render 带 base_only、drawMmdLayer 合成、attachMmdKey 挂配方
    for token in ("window.ARCAM_MMD", "base_only = true", "drawMmdLayer",
                  "attachMmdKey", "refresh3dRows", "do3DImport", "bind3D();",
                  "buildBundleZip"):
        assert token in js, f"app.js 缺 3D 接线 {token}"
    # ⑤ 客户端 zip writer（3D 导出用；从 docs/app.js 同源移植）
    assert "function crc32(" in js and "ZIP_DOS_DATE" in js
    # ⑥ docs 侧也要有同一套（两侧同源的门禁）
    dhtml = (DOCS / "index.html").read_text(encoding="utf-8")
    djs = (DOCS / "app.js").read_text(encoding="utf-8")
    assert 'src="./mmd.js"' in dhtml and 'type="importmap"' in dhtml
    for token in ("window.ARCAM_MMD", "draw3DInto", "attachMmdKey", "bind3D();"):
        assert token in djs, f"docs/app.js 缺 3D 接线 {token}"


# ---------------------------------------------------------------- v1.12.0 W5 审计新增门禁

#: i18n 预接线 pending 清单（v1.12.0 W5 深度打磨轮；键名/文案/用途见
#: artifacts/W5-web/DONE.md §5）。这些键**已在 HTML/JS 里接线**（data-t 或
#: mmd.js 的 T(key, fallback)），但键本身还没落地——src/arcam/i18n.py 归编排者
#: （.ai/COLLABORATION.md §2 / docs/AUDIT-CHARTER.md §3 红线，Worker 不许自己加）。
#: 协议（三条断言共同维持，缺一不可）：
#:   ① 键既不在语言表也不在 pending → 红（typo / 漏报告，双语无漏的机械化检查）；
#:   ② 键已落地却没从 pending 删除 → 红（逼着清理，不让 pending 清单腐烂）；
#:   ③ pending 里的键在两侧 HTML/JS 里找不到接线 → 红（防只报不接、防键名抄错）。
#: 编排者按 DONE.md 落地全部键后，本集合应被删空。
PENDING_I18N_KEYS: set[str] = set()
# v1.12.0：W5 预接的 51 个键已由编排者全部落地（页面骨架/参数面板/opt.*/mmd.*/
# err.network），登记表清空。机制保留——将来再预接新键时照样先登记后落地，
# 「落地了却没删登记」和「登记了却没落地」两个方向都有门禁（见下方测试）。

#: 只报告、不预接线的键（DONE.md §5 同清单）：标签里嵌了 `.val` 读数 span
#: （char-scale/size-pct/阴影方位/阴影浓度/颗粒强度五行），data-t 的
#: textContent 覆写会把 span 冲掉——要翻得连同标记结构一起改，属编排者裁决项。
REPORT_ONLY_I18N_KEYS = {
    "param.charScale", "param.sizePct", "param.shadowAz", "param.shadowOp",
    "param.grainScale",
}

# JS 里取语言文案的调用形态（t/tt/liveT/arcamT + mmd.js 的 T 助手）。
# 只认**字面量键**：t('upload.' + kind) 这类动态拼接匹配不到（故意——
# 动态键无法静态核对，其基表 upload.* 由 data-t 门禁间接覆盖）。
_I18N_CALL_RE = re.compile(r"\b(?:t|tt|liveT|arcamT|T)\((['\"])([^'\"]+)\1\s*[\),]")


def _i18n_table(folder):
    """i18n.js 的 ARCAM_TXT 键集合（JSON 解析，``\\uXXXX`` 转义自动还原）。"""
    import json
    src = (folder / "i18n.js").read_text(encoding="utf-8")
    m = re.search(r"window\.ARCAM_TXT = \{(.*?)\n\};", src, re.S)
    assert m, f"{folder.name}/i18n.js 里找不到 ARCAM_TXT"
    return set(json.loads("{" + re.sub(r",\s*$", "", m.group(1).strip()) + "}").keys())


def test_no_duplicate_dom_ids():
    """HTML 里不许有重复 id（v1.12.0 审计实修：btn-batch 两个 index.html 各重一次）。

    踩过的坑：「批量出图」钮写了两遍、共用同一个 id——getElementById 只返回
    第一个，第二个钮整个点不动，而既有门禁（id 解析 / 死 id 扫描）对重复 id
    完全无感（引用的字符串在、元素也在、两个都能被 querySelectorAll 刷到）。
    重复 id 同时是非法 HTML（querySelector 行为未定义）。两边都钉死。
    """
    for label, folder in (("webui", WEBUI), ("docs", DOCS)):
        html = (folder / "index.html").read_text(encoding="utf-8")
        ids = re.findall(r'\bid="([^"]+)"', html)
        dup = sorted({i for i in ids if ids.count(i) > 1})
        assert not dup, f"{label}/index.html 有重复 DOM id: {dup}"


def test_datat_keys_resolve():
    """每个 data-t / data-t-title 键都必须能在语言表解析，或在 pending 清单里。

    抓两类事故：① 键名 typo——切英文后 applyLang 的守卫会跳过覆写，标签静默
    停在中文（裸键会被显示出来，但守卫让 typo 完全不可见）；② 新文案没进
    i18n 表（「双语无漏」的机械化检查）。反向断言 pending 不许腐烂：键一旦
    落地就必须从 PENDING_I18N_KEYS 删除。
    """
    for label, folder in (("webui", WEBUI), ("docs", DOCS)):
        html = (folder / "index.html").read_text(encoding="utf-8")
        table = _i18n_table(folder)
        keys = set(re.findall(r'data-t="([^"]+)"', html))
        keys |= set(re.findall(r'data-t-title="([^"]+)"', html))
        missing = sorted(keys - table - PENDING_I18N_KEYS)
        assert not missing, \
            f"{label}/index.html 有解析不了的 i18n 键（typo 或漏报告给编排者）: {missing}"
        landed = sorted(keys & table & PENDING_I18N_KEYS)
        assert not landed, \
            f"{label}: 这些 pending 键已落地，请从 PENDING_I18N_KEYS 删除: {landed}"


def test_js_i18n_keys_resolve():
    """JS 里每个语言文案调用（t/tt/liveT/arcamT/T）的字面量键都要能解析。

    与 test_datat_keys_resolve 互补：那条守 HTML 静态文案，这条守运行时
    拼接的文案（状态条/提示条/HUD 读数/3D 错误）。先剥注释再扫——注释里
    提到的键名（如 hud.js 文档里的 t("hud.*")）不算数。动态拼接键
    （t('upload.' + kind)）静态核不了，其基表由 data-t 门禁间接覆盖。
    """
    for label, folder in (("webui", WEBUI), ("docs", DOCS)):
        table = _i18n_table(folder)
        used = set()
        for js in sorted(folder.glob("*.js")):
            src = _strip_comments(js.read_text(encoding="utf-8"))
            used |= {m.group(2) for m in _I18N_CALL_RE.finditer(src)}
        missing = sorted(used - table - PENDING_I18N_KEYS)
        assert not missing, \
            f"{label}: JS 里调了语言表没有的键（typo 或漏报告给编排者）: {missing}"
        landed = sorted(used & table & PENDING_I18N_KEYS)
        assert not landed, f"{label}: 这些 pending 键已落地，请从 PENDING_I18N_KEYS 删除: {landed}"


def test_pending_i18n_keys_are_wired():
    """pending 清单里的每个键都必须在两侧 HTML/JS 里真接着线。

    防两种腐烂：① 只在 DONE.md 报了、HTML/JS 没接线（编排者落了键也不生效，
    白等一轮）；② 键名在报告与接线之间抄错（落地后静默不翻）。判据是**原文
    出现**（子串）而非调用形态——mmd.js 的 T(kind === 'zip' ? 'mmd.noPmxZip'
    : …) 是三元形式，调用正则抓不到，子串能抓到。
    """
    sources = {}
    for label, folder in (("webui", WEBUI), ("docs", DOCS)):
        blob = (folder / "index.html").read_text(encoding="utf-8")
        blob += "\n".join(p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.js")))
        sources[label] = blob
    for key in sorted(PENDING_I18N_KEYS):
        hits = [label for label, blob in sources.items() if f'"{key}"' in blob or f"'{key}'" in blob]
        assert hits, f"pending 键 {key} 在两侧 HTML/JS 里都找不到接线（只报不接）"
    # 只报告的键同样要在 DONE.md 清单里有着落（防 REPORT_ONLY 集合腐烂）
    for key in sorted(REPORT_ONLY_I18N_KEYS):
        assert key.startswith("param."), f"REPORT_ONLY_I18N_KEYS 出现非参数面板键: {key}"


def test_docs_i18n_byte_identical():
    """docs/i18n.js 与 webui/i18n.js 逐字节相同（两侧同源；i18n.py 同源生成）。

    既有门禁只逐条比对了 webui 侧与 Python 的表（test_i18n_tables_match…），
    docs 侧只抽查了 hint.fxWeb 一条——整表漂移（某条只在一边改）发现不了。
    """
    a = (WEBUI / "i18n.js").read_bytes()
    b = (DOCS / "i18n.js").read_bytes()
    assert a == b, "两侧 i18n.js 不一致（同源文件漂移，某一侧漏改）"


def test_zip_writer_sections_match():
    """两侧 app.js 的手写 zip writer 段一致（store 式最小 zip，v1.7.0 同源移植）。

    3D 素材包 / 批量 zip 都走它；两侧各一份实现，改一边漏一边 = 两版产物
    结构分叉。剥注释后比对（docs 版多一行 JSDoc 注释，无行为差异）。
    """
    def section(js, end_marker):
        i = js.find("const ZIP_DOS_TIME")
        assert i >= 0, "app.js 里找不到 zip writer 段（const ZIP_DOS_TIME）"
        j = js.find(end_marker, i)
        assert j >= 0, f"app.js 里找不到 zip writer 段结尾标记 {end_marker!r}"
        return re.sub(r"\s+", " ", _strip_comments(js[i:j])).strip()

    w = section(_files()["app.js"], "/* ================= 7.8 MMD")
    d = section(_pages_files()["app.js"], "/* [ZIP-END] */")
    assert w == d, "两侧 zip writer 段不一致（同源移植漂移）"
    # 关键口径抽查：store 不压缩 + 1980 固定时间戳 + CRC 表驱动
    for frag in ("0x04034b50", "0x02014b50", "0x06054b50", "ZIP_DOS_DATE", "crc32"):
        assert frag in w, f"zip writer 段缺关键片段 {frag}"


def test_webui_canvas_to_blob_rejects():
    """canvasToBlob 编码失败必须 reject（v1.12.0 审计实修：旧实现 resolve(null)）。

    toBlob 回调收 **null**（编码失败）时旧实现照样 resolve——下游
    ``new File([null])`` 把字符串 "null" 存成 4 字节的 .png 交给用户
    （静默坏产物，用户下到的"图"打不开）。docs 版一直是 reject 口径，
    这里钉死两侧一致 + OffscreenCanvas 的 convertToBlob 分支在位。
    """
    for label, js in (("webui", _files()["app.js"]), ("docs", _pages_files()["app.js"])):
        body = _strip_comments(_fn_body(js, "canvasToBlob"))
        assert "reject(new Error(t('err.canvasEncode')))" in body, \
            f"{label}/canvasToBlob 编码失败应 reject(err.canvasEncode)（resolve(null) 会静默产出坏文件）"
        assert "convertToBlob" in body, f"{label}/canvasToBlob 应支持 OffscreenCanvas"
        assert "err.canvasNoEncode" in body, f"{label}/canvasToBlob 缺「画布不支持编码」分支"


def test_webui_batch_single_body_read():
    """/api/batch 错误路径只能读一次响应体（v1.12.0 审计实修：先 blob 再 json）。

    响应体只能读一次：旧实现先 ``res.blob()`` 再 ``res.json()``，错误路径的
    json 必然抛 "body stream already read"，被 .catch 吞成 null → 用户只看到
    「出错：HTTP 400」，服务端的人话（超 64MB / 没立绘 / dataURI 坏）整个丢。
    钉死 res.ok 判断在 res.blob() 之前。
    """
    body = _strip_comments(_fn_body(_files()["app.js"], "doBatch"))
    assert body.find("res.ok") >= 0 and body.find("await res.json()") >= 0
    assert body.find("res.ok") < body.find("res.blob()"), \
        "doBatch 必须先判 res.ok 再读 blob（顺序反了错误路径的 json 读不出来）"


def test_css_state_rules_parity():
    """3D 面板状态规则两版都要有（v1.12.0 审计实修：webui 缺三条）。

    refresh3dRows 给选中按钮打 ``className='on'``、d3dNote 切 ``error`` 类、
    model3dLabel/pose3dLabel 是 ``class="sub"``——三者在 webui/style.css 里
    原先都没有对应规则：选中的模型/姿势看不出来、3D 报错不是红色、行标题掉回
    正文 13px（docs 版一直有，SITE-SPEC §3 要求两版控件一致）。
    """
    for sel in (r"\.hint\.error\s*\{", r"\.fold\s+button\.on\s*\{", r"\.fold\s+\.sub\s*\{"):
        for label, folder in (("webui", WEBUI), ("docs", DOCS)):
            css = (folder / "style.css").read_text(encoding="utf-8")
            assert re.search(sel, css), f"{label}/style.css 缺规则 {sel}（3D 面板状态不可见）"


def test_webui_export3d_layer_target():
    """3D 模式下「下载立绘层」也必须客户端合成（v1.12.0 审计实修：layer 漏 divert）。

    旧代码只 divert png/bundle：layer 带着 base_only 落到服务端，出的是一张
    和预览无关的 2D 层（纯 3D 用户没上传过 2D 立绘，只会拿到 400）。
    docs 版走 charDraw 钩子本来就是 3D 层——两侧口径拉齐。
    """
    js = _strip_comments(_files()["app.js"])
    exp = _fn_body(js, "doExport")
    assert re.search(r"target === 'png' \|\| target === 'layer' \|\| target === 'bundle'", exp), \
        "doExport 应把 png/layer/bundle 三个 target 都 divert 到 3D 客户端合成"
    d3 = _fn_body(js, "doExport3D")
    assert re.search(r"target === 'layer'", d3), "doExport3D 应处理 layer target"
    assert "ARCAM_MMD.canvas()" in d3, "3D 立绘层应直接存 MMD 层画布"


def test_apply_lang_guard_and_title():
    """applyLang 的两条不变量（v1.12.0 补）：缺键守卫 + data-t-title。

    ① 缺键守卫：arcamT 对缺 key 返回**裸 key 字符串**，无脑覆写会把标签换成
       "param.x"——比不翻更糟。判据 ``v !== el.dataset.t``（docs 原有，webui 补）。
    ② data-t-title：原版 index.html 的 tooltip 翻译机制（#local-chip 的 title
       是中文说明），没有这段循环切英文后 tooltip 停在中文。
    """
    for label, js in (("webui", _files()["app.js"]), ("docs", _pages_files()["app.js"])):
        body = _strip_comments(_fn_body(js, "applyLang"))
        # ⚠ 断言要带 `) el.textContent` 收尾："v !== el.dataset.t" 是
        #    "v !== el.dataset.tTitle" 的子串——只查前半截会被 data-t-title
        #    循环里的同名判据骗过（变异实测复现：删掉标签守卫门禁照样绿）。
        assert "v !== el.dataset.t) el.textContent" in body, \
            f"{label}/applyLang 缺「键不存在则保留原文」守卫（裸键会覆盖中文标签）"
        assert "data-t-title" in body and "dataset.tTitle" in body, \
            f"{label}/applyLang 不处理 data-t-title（tooltip 切不了语言）"


def test_mmd_js_error_i18n_fallback():
    """mmd.js 的用户可见文案必须走 T(key, fallback) 兜底（v1.12.0 双语无漏）。

    3D 导入的坏 zip / 无 pmx / 坏压缩 / 引擎载入失败等提示原先全是硬编码
    中文——EN 用户看到中文。T() 助手在键未落地时回落中文原文（不现裸键），
    编排者按 DONE.md 落地 mmd.* 键后自动变英文。两侧同源，一起钉。
    """
    for label, folder in (("webui", WEBUI), ("docs", DOCS)):
        src = (folder / "mmd.js").read_text(encoding="utf-8")
        assert re.search(r"function T\(key, fallback\)", src), \
            f"{label}/mmd.js 缺 T(key, fallback) 兜底助手"
        # 兜底语义：键不存在（v === key）时用 fallback，不把裸键显示给用户
        assert "v !== key) return v;" in src, "T() 必须在键缺失时回落 fallback 原文"
        # 高频用户可见错误点必须已接线（sniff 的 6 条 + 包内 4 条 + 装配/解析 2 条）
        for key in ("mmd.rar", "mmd.unknown", "mmd.noPmxZip", "mmd.assembleFail",
                    "mmd.parseFail", "mmd.engineFail"):
            assert f"'{key}'" in src, f"{label}/mmd.js 未接线 {key}"


def test_network_error_humanized():
    """fetch 失败的英文 TypeError 必须换成人话（v1.12.0：断网 / 服务被杀）。

    本地版最常见事故：用户关掉「启动AR取景器」终端后继续点渲染——浏览器抛
    "Failed to fetch"（英文 TypeError），旧实现原样显示在状态条，用户不知道
    下一步干什么。errMsg 识别网络类错误换 T('err.network', …)（键落地前回落
    中文原文，不现裸键）。两侧都要有；webui 全部 API 错误过 showApiError 单漏斗。
    """
    for label, js in (("webui", _files()["app.js"]), ("docs", _pages_files()["app.js"])):
        body = _strip_comments(_fn_body(js, "errMsg"))
        # 判据是 e.name === 'TypeError'（fetch 失败的规范形态），不是消息文本正则
        # ——后者会把后端 error/hint 里带 "fetch" 字样的人话误吞成通用网络错误
        # （v1.12.0 对抗复核 P3-1）。
        assert re.search(r"e\s*&&\s*e\.name\s*===\s*['\"]TypeError['\"]", body), \
            f"{label}/errMsg 不识别网络类错误（浏览器英文 TypeError 会原样上状态条）"
        assert "T('err.network'" in body, f"{label}/errMsg 未走 T() 兜底取 err.network"
    webui = _strip_comments(_files()["app.js"])
    assert "t('note.error', errMsg(e))" in webui, \
        "webui showApiError 应经过 errMsg（全部 API 错误的单漏斗）"
    # docs 的五个 API catch 点（render/matte/三个导出）都要过人话化
    docs = _strip_comments(_pages_files()["app.js"])
    assert docs.count("errMsg(e)") >= 5, \
        f"docs 只有 {docs.count('errMsg(e)')} 处 errMsg(e)——render/matte/三个导出的 catch 都要过"
