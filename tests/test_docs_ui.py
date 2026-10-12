"""docs 静态站（GitHub Pages 版）UI 接线静态检查（v1.9.0，W3 分区交付）。

与 tests/test_webui_static.py **完全独立**（不 import 它的任何东西）：那个文件
守 webui 本地版与 docs 双侧的共用门禁（DOM id 解析、参数 schema、i18n 两侧
比对等），本文件只钉 W3 本轮新加/改动的三件事：

- 颗粒强度滑块三段齐全：index.html 控件 + app.js bindSlider 绑定 +
  syncControlsFromParams 记忆写回，外加 composeParams 的 grainScale 透传
  （「控件存在于 UI 但参数到不了执行点」是本项目最高发 bug 类，逐段钉死）；
- saveBlob 兜底链：share → showSaveFilePicker → <a download>，
  AbortError 静默取消，5 个调用点全部 await 且都在 async 函数内；
- 三处 i18n（src/arcam/i18n.py 的 TXT、src/arcam/webui/i18n.js、
  docs/i18n.js）的 hint.fxWeb 逐字一致，且不再声称网页版不烘光。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SRC = ROOT / "src"
if str(SRC) not in sys.path:  # 与根 conftest.py 同口径（单独跑本文件时也认 src/）
    sys.path.insert(0, str(SRC))

# 三处必须逐字一致的 hint.fxWeb 文案（v1.9.0：bakeLit 移植后网页版也烘光/颗粒）
FXWEB_ZH = "网页版同样烘焙环境光与颗粒，颗粒强度可调"
FXWEB_EN = "The web build also bakes ambient light and grain; grain strength is adjustable"


def _strip_comments(src, html=False):
    """只剥注释、**保留字符串字面量**，供「接线是否真在代码里」的子串断言用。

    裸子串匹配会被整段注释骗过：把 bindSlider/addEventListener 那几行注释掉
    （括号仍配平），断言照样绿，控件变成死控件 —— 正是「控件存在但到不了
    执行点」这个最高发 bug 类的检测漏洞（v1.9.0 对抗复核变异实测复现，
    test_webui_static.py 有同款 helper，两边各自独立不互相 import）。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        if html and src.startswith("<!--", i):
            j = src.find("-->", i)
            i = n if j < 0 else j + 3
            continue
        c = src[i]
        if c in "'\"`":
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


def _fn_body(js, name):
    """取 JS 函数体（`function name(...) { … }`，按花括号配平切）。"""
    i = js.find("function " + name + "(")
    assert i >= 0, f"app.js 里找不到 function {name}"
    j = js.find("{", i)
    assert j >= 0, f"function {name} 没有函数体"
    depth = 0
    for k in range(j, len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[j:k + 1]
    raise AssertionError(f"function {name} 花括号不配平")


def _js_txt_entry(path, key):
    """从 i18n.js 里取一条 ["zh", "en"] 条目（键名带点，正则转义）。"""
    src = path.read_text(encoding="utf-8")
    m = re.search(r'"' + re.escape(key) + r'":\s*\["([^"]*)",\s*"([^"]*)"\]', src)
    assert m, f"{path.name} 里找不到 {key} 条目"
    return (m.group(1), m.group(2))


def test_grain_scale_slider_in_docs_html():
    """颗粒强度滑块在 docs/index.html 里，且结构与阴影滑块同构（同行排布）。"""
    html = _strip_comments((DOCS / "index.html").read_text(encoding="utf-8"), html=True)
    assert 'id="p-grain-scale-val"' in html, "docs/index.html 缺颗粒强度数值 span"
    assert 'id="p-grain-scale-rng" min="0" max="3" step="0.05" value="1"' in html, \
        "docs/index.html 缺颗粒强度滑块（量程 0–3、步进 0.05、默认 1）"
    # 与本地 webui 侧同一块逐字一致（SITE-SPEC §3：两版控件结构保持同步）
    assert 'id="p-grain-scale-rng" min="0" max="3" step="0.05" value="1"' in \
        (SRC / "arcam" / "webui" / "index.html").read_text(encoding="utf-8"), \
        "webui/index.html 与 docs/index.html 的颗粒滑块不一致"
    # 排在阴影浓度滑块之后（效果开关折叠块内的顺序）
    assert html.find("p-shadow-op-rng") < html.find("p-grain-scale-rng"), \
        "颗粒强度滑块应排在阴影浓度滑块后面"
    # label 硬编码中文（该行不用 data-t，与阴影滑块同构）
    assert re.search(r'<label for="p-grain-scale-rng">颗粒强度 ', html), \
        "颗粒强度滑块缺中文 label"


def test_grain_scale_bound_and_synced_in_docs_app_js():
    """滑块绑定 + 记忆写回 + 渲染透传三段齐全（缺一段 = 假控件/静默失效）。"""
    js = _strip_comments((DOCS / "app.js").read_text(encoding="utf-8"))
    assert "bindSlider('grain_scale', 'p-grain-scale-rng', 'p-grain-scale-val'" in js, \
        "docs/app.js 未把颗粒强度滑块绑到 grain_scale 参数键"
    # 参数记忆恢复后必须写回控件，否则界面与 state.params 不一致
    body = _fn_body(js, "syncControlsFromParams")
    assert re.search(r"\$\('p-grain-scale-rng'\)\.value\s*=\s*state\.params\.grain_scale",
                     body), "syncControlsFromParams 未写回颗粒强度滑块"
    assert re.search(r"\$\('p-grain-scale-val'\)\.textContent\s*=\s*"
                     r"state\.params\.grain_scale\.toFixed\(2\)", body), \
        "syncControlsFromParams 未写回颗粒强度数值 span"
    # 参数键默认值在 S_DEFAULTS 里（没有它 restore/save 会漏掉这一项）
    assert re.search(r"grain_scale:\s*1\.0", js), "S_DEFAULTS 缺 grain_scale: 1.0"
    # 最后一环：composeParams 必须把 snake_case 透传成渲染侧的 grainScale
    # （v1.9.0 前已落；这里钉死，防回归成「UI 有、渲染侧收不到」）
    assert re.search(r"grainScale:\s*p\.grain_scale", js), \
        "composeParams 未透传 grainScale（滑块将到不了渲染侧）"


def test_save_blob_fallback_chain():
    """saveBlob 兜底链：share → picker → download，AbortError 静默取消。"""
    js = _strip_comments((DOCS / "app.js").read_text(encoding="utf-8"))
    assert "async function saveBlob(" in js, "saveBlob 应为 async（兜底链里有 await）"
    body = _fn_body(js, "saveBlob")
    # 安卓 WebView 桥明确不移植（本项目没有安卓壳）
    assert "ArcLog" not in body, "saveBlob 不应出现安卓桥 ArcLog"
    # File 包一层再给 canShare/share（blob.type 为空时兜底 octet-stream）
    assert "new File([blob], name, { type: blob.type || 'application/octet-stream' })" in body, \
        "saveBlob 未用 File 包装 blob"
    for frag in ("navigator.canShare({ files: [file] })", "navigator.share({ files: [file] })",
                 "window.showSaveFilePicker({ suggestedName: name })",
                 "createWritable()", "'AbortError'"):
        assert frag in body, f"saveBlob 兜底链缺：{frag}"
    # 顺序：canShare → share → picker → <a download>，逐级下落
    order = [body.find(s) for s in ("navigator.canShare", "navigator.share",
                                    "showSaveFilePicker", "a.click()")]
    assert all(i >= 0 for i in order) and order == sorted(order), \
        "saveBlob 兜底链顺序应为 canShare → share → showSaveFilePicker → download"
    # 四态返回值
    for ret in ("'share'", "'picker'", "'download'", "'cancel'"):
        assert ret in body, f"saveBlob 缺返回值 {ret}"
    # 调用点数 = await 数 + 1（定义行那处）：漏一个 await 就是静默丢下载。
    # 用不变式而不是写死个数 —— 死函数 doBatch 删掉后少一个调用点，写死数会误红。
    calls = len(re.findall(r"\bsaveBlob\(", js))
    awaited = len(re.findall(r"await\s+saveBlob\(", js))
    assert calls == awaited + 1, \
        f"saveBlob 共 {calls} 处引用、仅 {awaited} 处带 await"
    assert not re.findall(r"(?m)^\s*saveBlob\(", js), \
        "存在未 await 的 saveBlob 调用（async 函数漏 await 会静默丢结果）"
    for fn in ("doExportPng", "doExportLayer", "doExportBundle"):
        assert re.search(r"async function " + fn + r"\(", js), f"{fn} 不是 async 函数"
    # 批量出图的真正实现是 bindBatch 里内联的 async change 处理器（doBatch 死函数
    # 已于 v1.9.0 删除：唯一调用点随 init 双绑定一起没了，留着就是两份批量实现）
    assert "async function doBatch" not in js, "doBatch 是死代码（应已删）"
    assert "addEventListener('change', async (e)" in js, \
        "批量出图的 change 处理器应为 async"


def test_i18n_fxweb_three_places_identical():
    """hint.fxWeb 三处（Python TXT / webui i18n.js / docs i18n.js）逐字一致。"""
    from arcam.i18n import TXT
    py_entry = TXT["hint.fxWeb"]
    webui_entry = _js_txt_entry(SRC / "arcam" / "webui" / "i18n.js", "hint.fxWeb")
    docs_entry = _js_txt_entry(DOCS / "i18n.js", "hint.fxWeb")
    assert py_entry == (FXWEB_ZH, FXWEB_EN), f"src/arcam/i18n.py 的 hint.fxWeb 文案不对：{py_entry}"
    assert webui_entry == (FXWEB_ZH, FXWEB_EN), \
        f"src/arcam/webui/i18n.js 的 hint.fxWeb 与 Python 不一致：{webui_entry}"
    assert docs_entry == (FXWEB_ZH, FXWEB_EN), \
        f"docs/i18n.js 的 hint.fxWeb 与 Python 不一致：{docs_entry}"
    # 新版如实描述：烘焙在网页版也有，且颗粒强度可调
    assert "同样烘焙" in py_entry[0] and "颗粒强度" in py_entry[0], \
        "hint.fxWeb 中文案应说明网页版同样烘光/颗粒、强度可调"
    assert "also bakes" in py_entry[1], "hint.fxWeb 英文案应说明网页版同样烘光/颗粒"
    # 旧文案（"网页版不烘光/本地版专属"）三处都不许残留
    for label, src in (("i18n.py", (SRC / "arcam" / "i18n.py")),
                       ("webui/i18n.js", SRC / "arcam" / "webui" / "i18n.js"),
                       ("docs/i18n.js", DOCS / "i18n.js")):
        text = src.read_text(encoding="utf-8")
        for stale in ("网页版只叠阴影开关", "本地版专属（JS 侧无 bakeLit）", "local-only"):
            assert stale not in text, f"{label} 仍残留过期文案：{stale}"


def test_fx_light_grain_switches_in_docs():
    """docs 侧光影三开关齐（v1.9.0 合并修复）：bakeLit 移植后 light/grain 必须可关。

    arcam-render.js 的 normalizeParams 按 p.light/p.grain 门控采光与颗粒，
    但 docs UI 原先只有 fx-shadow —— 两个开关不存在 = 烘光永远开着关不掉
    （又一个「参数到得了执行点、UI 到不了参数」的静默失效）。三件套钉死：
    HTML 控件 + bindFx 绑定 + syncControlsFromParams 写回，外加 composeParams
    透传（渲染侧白名单只认 camelCase，漏一个键整段门失效）。
    """
    html = _strip_comments((DOCS / "index.html").read_text(encoding="utf-8"), html=True)
    js = _strip_comments((DOCS / "app.js").read_text(encoding="utf-8"))
    # 控件在效果开关折叠块内，且排在阴影开关之前（与本地 webui 同序）
    for cid in ("fx-light", "fx-grain", "fx-shadow"):
        assert f'id="{cid}"' in html, f"docs/index.html 缺 {cid} 开关"
    assert html.find("fx-light") < html.find("fx-shadow") < html.find("fx-hud-grid"), \
        "docs 效果开关顺序应为 light/grain/shadow → HUD 三件（同本地 webui）"
    # 绑定与写回
    body = _fn_body(js, "bindFx")
    for cid, key in (("fx-light", "light"), ("fx-grain", "grain")):
        assert f"$('{cid}').addEventListener('change'" in body, f"bindFx 未绑 {cid}"
        assert f"state.params.{key} = e.target.checked" in body, f"{cid} 未写 params.{key}"
    sync = _fn_body(js, "syncControlsFromParams")
    for cid, key in (("fx-light", "light"), ("fx-grain", "grain")):
        assert re.search(r"\$\('%s'\)\.checked\s*=\s*state\.params\.%s\s*!==\s*false"
                         % (cid, key), sync), f"syncControlsFromParams 未写回 {cid}"
    # composeParams 透传（渲染侧读的是 light/grain 布尔，不是 snake_case 影子键）
    kw = _fn_body(js, "composeParams")
    assert re.search(r"light:\s*p\.light\s*!==\s*false", kw), \
        "composeParams 未透传 light（normalizeParams 的采光门永远吃默认值）"
    assert re.search(r"grain:\s*p\.grain\s*!==\s*false", kw), \
        "composeParams 未透传 grain（颗粒开关到不了 bakeLit）"
    # 旧死键：渲染侧只读 camelCase，snake_case 影子键是误导性死代码
    assert "shadow_az: p.shadow_az" not in kw, \
        "composeParams 不应再放 snake_case 影子键（normalizeParams 只读 camelCase）"


def test_docs_batch_bound_exactly_once():
    """批量出图的接线只能各绑一次（v1.9.0 双 change + v1.12.0 双 click 事故回归）。

    v1.9.0：init 内联防 change 调 doBatch，bindBatch() 又各自绑了一份 change
    —— 选一次文件发两次 /api/batch、下两个 zip。
    v1.12.0：W5 审计发现 click 也双着——init 与 bindBatch 各绑一条
    `$('btn-batch').addEventListener('click', …)`，点一次批量钮开两回文件
    选择框（旧门禁只断言 init 里有一条，没数全文件总条数，正好放错方向）。
    本地 webui 侧同款坑已修，两边一起钉死：change 与 click 都只许一处。
    """
    js = _strip_comments((DOCS / "app.js").read_text(encoding="utf-8"))
    assert js.count("$('file-photos').addEventListener('change'") == 1, \
        "file-photos 的 change 被重复绑定（一次选择会跑两遍批量）"
    assert js.count("$('btn-batch').addEventListener('click'") == 1, \
        "批量钮的 click 被重复绑定（点一次开两回文件选择框）"
    body = _fn_body(js, "bindBatch")
    assert "$('btn-batch').addEventListener('click'" not in body, \
        "bindBatch 不应再绑 click（init 里已绑；双绑 = 开两回选择框）"
    assert "e.target.value = ''" in body, \
        "bindBatch 未清空 file input（同一批文件再选不触发 change）"
    # init 里不再出现 file-photos 的 change 绑定（按钮 click 打开选择框仍保留）
    init = js[:js.find("function bindBatch(")]
    assert "file-photos').addEventListener('change'" not in init, \
        "init 里又绑了一条 file-photos change（与 bindBatch 双跑）"
    assert "$('btn-batch').addEventListener('click'" in init, \
        "批量钮的 click（打开文件选择框）应保留在 init"


def test_docs_exports_cancel_aware():
    """三个导出 + 批量的「保存取消」必须反映在状态条（v1.12.0 审计实修）。

    saveBlob 返回 'cancel'（用户在保存框/分享面板里关掉）时，旧实现无视返回值
    照显示「已开始下载 {name}」——状态条撒谎。本地 webui 的 doExport 一直判
    ``how === 'cancel'``，docs 版拉齐；复用既有键 note.saveCancel / note.batchCancel。
    """
    js = _strip_comments((DOCS / "app.js").read_text(encoding="utf-8"))
    for fn in ("doExportPng", "doExportLayer", "doExportBundle"):
        body = _fn_body(js, fn)
        assert re.search(r"const how = await saveBlob\(", body), \
            f"{fn} 未接收 saveBlob 的返回值（拿不到 cancel/share/picker/download）"
        assert re.search(r"how === 'cancel' \? t\('note\.saveCancel'\)", body), \
            f"{fn} 取消时应报 note.saveCancel（旧实现取消也显示下载中）"
    batch = _fn_body(js, "bindBatch")
    assert re.search(r"how === 'cancel'", batch), \
        "bindBatch 取消时应把 note.batchCancel 缀在结果后面"
    # 长任务秒表：批量逐张全尺寸合成可能几十秒，状态条要能报「已用 N 秒」
    assert re.search(r"setInterval\(", batch) and "note.aiElapsed" in batch, \
        "bindBatch 缺长任务秒表（复用 note.aiElapsed；进行中无任何反馈）"
