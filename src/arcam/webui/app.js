/* arcam web UI — 原生 JS，零依赖、零框架、零 CDN。
 * 与后端 src/arcam/webapp.py 的接口契约见 docs/WEB-SPEC.md §1/§2。
 *
 * 分组：1 常量状态 → 2 工具 → 3 fetch 封装 → 4 上传 → 5 渲染
 *      6 动作按钮 → 7 参数面板 → 8 初始化
 */
'use strict';

/* ================= 1. 常量与状态 ================= */

// WEB-SPEC §2：默认值 = 原版 S 默认（index.html:921–937，cli.py _add_placement 同款）。
// 注意 x/d/h/pitch/roll 在 CLI 里默认 None 是为了区分「显式给过」与「没给」，
// 所以前端显示用这些 S 默认，但只有用户动过的键才进请求体（见 buildParams）。
// t() 薄封装：i18n.js 把表挂在 window.arcamT 上（与 docs 版同一张表）。
// ⚠ 没有这层就直接调 t() 会在 init 里抛 ReferenceError，把后面的接线全 abort。
function t(key) {
  return window.arcamT ? window.arcamT.apply(null, arguments) : key;
}

var S_DEFAULTS = {
  x: 0, d: 4, h: 1.6, hp: 1.65, feq: 23, pitch: 0, roll: 0,
  shot: '全身', char_scale: 1.0, frame: '满幅', rot_basis: 'world',
  mode: 'ground', size_pct: 60, edge_bar: 0, tone: false,
  // 光影三开关（v1.3.0）：三项都直接改变出图。v1.9.0 起网页版同样真烘
  // 环境光/颗粒（docs/arcam-lighting.js 移植 bakeLit，本地版走 webapp →
  // compose 的同一条门），不再有「只叠阴影」的版本差。
  light: true, grain: true, shadow: true,
  shadow_az: 0, shadow_opacity: 1.0, grain_scale: 1.0,
  base_only: false          // 内部态：3D 就绪时由 buildParams 自动带上，不进用户面板
};

// 数字+滑块双控件的七个量；量程是自定的（原版 index.html 不在本仓库），
// 取投影合理区间：x 对 place 的 ±6 夹取（SPEC §3.1），其余按常见拍摄参数。
var NUMERIC = ['x', 'd', 'h', 'hp', 'feq', 'pitch', 'roll'];
var RANGES = {
  x: [-6, 6, 0.05], d: [0.5, 12, 0.05], h: [0.3, 3, 0.05], hp: [0.4, 2.5, 0.01],
  feq: [10, 120, 0.5], pitch: [-60, 60, 0.5], roll: [-90, 90, 0.5]
};

var DEBOUNCE_MS = 300;        // WEB-SPEC §3：任何参数改动 300ms 防抖自动重渲
var PREVIEW_LONG_EDGE = 1280; // 后端预览承诺长边 ≤1280 保证 <1s（export 仍全尺寸）

// 拖拽区标签（状态条拼接用）：走语言表 upload.* —— HTML 侧 dz.* 那几条已是
// data-t，这里不再各写一份硬编码中文
function kindLabel(kind) {
  return t('upload.' + kind);
}
var EXPORT_FILES = {
  png: 'arcam-render.png',
  layer: 'arcam-char-layer.png',
  bundle: 'arcam-bundle.zip'
};

var state = {
  params: Object.assign({}, S_DEFAULTS),
  touched: new Set(),      // 用户动过的参数键：显式 > 测出 > 标称 的「显式」这一档
  depthUploaded: false,    // LiDAR 诊断成功后置位，render 自动带 depth_uploaded=true
  lidarEnv: null,          // 最近一次 inspect 的完整 env（含 coverage/inliers/rms）；
                           // render 响应只带 3 个字段，合并进来而不是整体覆盖
  photo: null, char: null, depthFile: null,
  photoW: 0, photoH: 0,
  renderSeq: 0,            // 序号哨兵：丢弃过期的异步渲染响应，防旧图盖新图
  renderTimer: 0
};

/* ================= 2. 小工具 ================= */

function $(id) { return document.getElementById(id); }

function setStatus(msg, ms, isError) {
  var el = $('status-msg');
  el.textContent = msg;
  el.className = isError ? 'error' : 'ok';
  $('status-time').textContent = typeof ms === 'number' ? t('note.elapsed', Math.round(ms)) : '';
}

// 错误统一进状态条：显示 error+hint；不清空用户已调参数（state 不与失败路径耦合）。
function showApiError(e) {
  setStatus(t('note.error', e.message), null, true);
}

/* ================= 3. fetch 封装 ================= */

// 后端所有异常都收敛为 JSON {ok:false,error,hint?}（WEB-SPEC §1 硬规则），
// 所以非 2xx 也先按 JSON 解，解不出再退化成 HTTP 码人话。
class ApiError extends Error {
  constructor(error, hint) {
    super(hint ? error + '（' + hint + '）' : String(error));
    this.error = error;
    this.hint = hint || null;
  }
}

async function readJson(res) {
  var data = null;
  try { data = await res.json(); } catch (err) { /* 非 JSON 响应，走下面的兜底 */ }
  if (!res.ok || !data || data.ok !== true) {
    var msg = (data && data.error) || ('HTTP ' + res.status);
    throw new ApiError(msg, data && data.hint);
  }
  return data;
}

function apiForm(url, fd) {
  return fetch(url, { method: 'POST', body: fd }).then(readJson);
}

function apiJson(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(readJson);
}

/* ================= 4. 上传（drag&drop + 点击选择） ================= */

function setName(id, text) {
  var el = $(id);
  el.textContent = text;
  el.classList.toggle('empty', text === t('name.empty'));
}

async function uploadFile(kind, file) {
  var fd = new FormData();
  fd.append(kind, file); // 字段名 photo/char/depth 与 WEB-SPEC §1 对齐
  setStatus(t('note.uploading', kindLabel(kind)));
  try {
    var r = await apiForm('/api/upload', fd);
    var dims = (r.width && r.height) ? ' · ' + r.width + '×' + r.height : '';
    if (kind === 'photo') {
      state.photo = file;
      state.photoW = r.width || 0;
      state.photoH = r.height || 0;
      setName('photo-name', file.name + dims);
      $('preview-empty').hidden = true;
      if (state.char) renderNow();
      else setStatus(t('note.photoOk'), r.ms);
    } else if (kind === 'char') {
      state.char = file;
      // charName/charW/charH 是修图三钮、HUD 读数与 autoFixX 宽高比的唯一来源
      // （漏记 = 三钮整个点不动 + HUD 永远报「还没选立绘」）
      state.charName = file.name;
      state.charW = r.width || 0;
      state.charH = r.height || 0;
      setName('char-name', file.name + dims);
      // 后端用 fg 告诉这块立绘有没有 alpha：普通图提示可点抠图（不自动烧 CPU）
      if (r.fg === false) {
        $('input-hint').textContent = t('note.plainChar');
      } else {
        $('input-hint').textContent = '';
      }
      if (state.photo) renderNow();
      else setStatus(t('note.charOk'), r.ms);
      // 导入后问一句全身/半身（原版 loadChar：只定默认走哪条路，不改任何状态）
      showKind();
    } else if (kind === 'depth') {
      state.depthFile = file;
      setName('depth-name', file.name + dims);
      $('input-hint').textContent = t('hint.depthNext');
      setStatus(t('note.depthUploaded'), r.ms);
    }
  } catch (e) {
    showApiError(e); // 失败保留所有已选文件与已调参数
  }
}

// 只把「上传成功」的文件写进 state（photo/char/depth 一视同仁）：失败时留旧值，
// 界面上的「未选择」才与内部状态一致；深度帧的落账在 uploadFile 的 depth 分支。
function handleFile(kind, file) {
  return uploadFile(kind, file);
}

function bindDrop(zoneId, inputId, kind) {
  var zone = $(zoneId);
  var input = $(inputId);
  zone.addEventListener('click', function () { input.click(); });
  input.addEventListener('change', function () {
    if (input.files && input.files[0]) handleFile(kind, input.files[0]);
    input.value = ''; // 清空后同一个文件再次选择才会触发 change
  });
  zone.addEventListener('dragenter', function (e) { e.preventDefault(); zone.classList.add('drag'); });
  zone.addEventListener('dragover', function (e) { e.preventDefault(); zone.classList.add('drag'); });
  zone.addEventListener('dragleave', function () { zone.classList.remove('drag'); });
  zone.addEventListener('drop', function (e) {
    e.preventDefault();
    zone.classList.remove('drag');
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) handleFile(kind, f);
  });
}

/* ================= 5. 渲染（300ms 防抖） ================= */

function buildParams() {
  // 显式参数口径：只有用户动过的控件才进 render 请求体（缺键即未动，
  // 后端按 CLI 同款默认/深度测出值补齐 → 显式 > 测出 > 标称）。
  var out = {};
  state.touched.forEach(function (k) { out[k] = state.params[k]; });
  if (state.depthUploaded) out.depth_uploaded = true; // 由诊断状态自动带
  if (t3dReady()) out.base_only = true;   // 3D 层客户端合成：后端别画 2D 立绘
  return out;
}

function previewScale() {
  // 预览降采样到长边 1280 保证 <1s；export 永远全尺寸（WEB-SPEC §1）。
  var m = Math.max(state.photoW, state.photoH);
  if (!m) return 1;
  return Math.min(1, PREVIEW_LONG_EDGE / m);
}

function drawDataUri(uri) {
  var img = new Image();
  img.onload = function () {
    var c = $('preview-canvas');
    c.width = img.naturalWidth;
    c.height = img.naturalHeight;
    c.getContext('2d').drawImage(img, 0, 0);
  };
  img.onerror = function () { setStatus(t('note.imgDecodeFail'), null, true); };
  img.src = uri;
}

function showScene(scene) {
  $('scene-json').textContent = JSON.stringify(scene, null, 2);
}

function showEnv(env, frame) {
  var box = $('env-box');
  box.hidden = false;
  // coverage 约定是比例 0–1；后端若直接给百分数（>1）也不换算错
  var parts = [
    t('env.camH', Number(env.height_m).toFixed(2)),
    t('env.pitch', Number(env.pitch_deg).toFixed(1)),
    t('env.roll', Number(env.roll_deg).toFixed(1))
  ];
  // 诊断字段缺省就不显示（render 的 env 只有三个字段，见 mergeEnv）
  if (env.coverage != null) {
    var cov = env.coverage <= 1
      ? (env.coverage * 100).toFixed(1) + '%'
      : Number(env.coverage).toFixed(1) + '%';
    parts.push(t('env.coverage', cov));
  }
  if (env.inliers != null) parts.push(t('env.inliers', env.inliers));
  if (env.rms != null) parts.push('RMS ' + Number(env.rms).toFixed(3));   // 符号，不翻
  if (frame) parts.push(t('env.frame', frame.width, frame.height));
  box.textContent = t('env.box') + parts.join(' · ');
}

/** render 响应里只有三字段的 env 合并进完整 env，防止覆盖率/内点/RMS 被冲掉。 */
function mergeEnv(env3, frame) {
  if (env3 && env3.height_m != null) {
    state.lidarEnv = Object.assign({}, state.lidarEnv, env3);
  }
  if (state.lidarEnv) showEnv(state.lidarEnv, frame || null);
}

function scheduleRender() {
  clearTimeout(state.renderTimer);
  state.renderTimer = setTimeout(renderNow, DEBOUNCE_MS);
}

async function renderNow() {
  clearTimeout(state.renderTimer);
  if (!state.photo || !state.char) {
    setStatus(t('note.needBoth'), null, true);
    return;
  }
  var seq = ++state.renderSeq;
  var t0 = performance.now();
  try {
    var r = await apiJson('/api/render', { params: buildParams(), scale: previewScale() });
    if (seq !== state.renderSeq) return; // 过期响应直接丢弃
    drawDataUri(r.image);
    if (t3dReady()) {
      var cv = $('preview-canvas');
      if (!drawMmdLayer(cv, cv.width, cv.height)) {
        d3dNote(t('note.3dDrawFail', t('err.renderZero')), true);
      } else {
        d3dNote('');
      }
    }
    $('preview-empty').hidden = true;
    showScene(attachMmdKey(r.scene, $('preview-canvas').width, $('preview-canvas').height));
    if (r.env) mergeEnv(r.env, null); // render 的 env 只有三字段，合并进完整 env
    setStatus(t('note.renderDone'), typeof r.ms === 'number' ? r.ms : performance.now() - t0);
    drawHudOverlay();                   // HUD 浮层跟着预览尺寸/参数重画
  } catch (e) {
    showApiError(e);
  }
}

/* ================= 6. 动作按钮 ================= */

async function doMatte() {
  if (!state.char) { setStatus(t('note.needChar'), null, true); return; }
  try {
    setStatus(t('note.matteRunning'));
    var r = await apiJson('/api/matte', { source: 'char' });
    drawDataUri(r.image); // 先把抠图结果画出来：照片没上传也能看到效果
    $('preview-empty').hidden = true;
    await apiJson('/api/matte-result-as-char', {}); // 结果设为当前立绘
    $('input-hint').textContent = t('note.matteDone');
    setStatus(t('note.matteOk'), r.ms);
    if (state.photo) scheduleRender();
  } catch (e) {
    showApiError(e); // 模型缺失等情况：error 带下载指引，不 500
  }
}

async function doLidar() {
  if (!state.depthFile) {
    setStatus(t('note.needDepth'), null, true);
    return;
  }
  try {
    setStatus(t('note.lidarRunning'));
    var fd = new FormData();
    fd.append('depth', state.depthFile);
    var r = await apiForm('/api/lidar/inspect', fd);
    state.depthUploaded = true; // 之后 render 带 depth_uploaded=true 走深度驱动
    state.lidarEnv = r.env;     // 完整 env（含 coverage/inliers/rms）
    showEnv(r.env, r.frame);
    setStatus(t('note.lidarDone'));
    scheduleRender();
  } catch (e) {
    showApiError(e);
  }
}

//: 3D 模式的导出（v1.7.0）：服务端只会出 2D，这里客户端合成。
//  shot = 照片底（base_only 全尺寸）+ MMD 层；character.png = MMD 层本身
//  （alpha 画布，盖回去严丝合缝——与 2D 立绘层的契约一致）。
async function doExport3D(target) {
  try {
    setStatus(t('note.export3d'));
    var p = state.params;
    var r = await apiJson('/api/render', {
      params: buildParams(), scale: 1        // base_only 由 buildParams 自动带
    });
    var base = await loadImage(r.image);
    var W = base.width, H = base.height;
    var cv = document.createElement('canvas');
    cv.width = W; cv.height = H;
    cv.getContext('2d').drawImage(base, 0, 0);
    if (!drawMmdLayer(cv, W, H)) {
      throw new ApiError(t('err.3dDraw'), t('err.3dDrawHint'));
    }
    var scene = attachMmdKey(r.scene, W, H);
    if (target === 'png') {
      var blob = await canvasToBlob(cv, 'image/png');
      var how = await saveBlob(blob, 'arcam-3d-' + stamp() + '.png');
      setStatus(how === 'cancel' ? t('note.saveCancel') : t('note.saveStart', 'arcam-3d-*.png'));
      return;
    }
    // bundle：shot.jpg + scene.json + character.png（MMD 层）
    var shotBlob = await canvasToBlob(cv, 'image/jpeg', 0.95);
    var layerBlob = await canvasToBlob(window.ARCAM_MMD.canvas(), 'image/png');
    var parts = [
      { name: 'shot.jpg', data: new Uint8Array(await shotBlob.arrayBuffer()) },
      { name: 'scene.json', data: new TextEncoder().encode(JSON.stringify(scene, null, 2)) },
      { name: 'character.png', data: new Uint8Array(await layerBlob.arrayBuffer()) }
    ];
    var how = await saveBlob(buildBundleZip(parts), 'arcam-bundle-' + stamp() + '.zip');
    setStatus(how === 'cancel'
      ? t('note.saveCancel')
      : t('note.bundleStart', 'arcam-bundle-*.zip'));
  } catch (e) { showApiError(e); }
}

function loadImage(uri) {
  return new Promise(function (resolve, reject) {
    var img = new Image();
    img.onload = function () { resolve(img); };
    img.onerror = function () { reject(new Error(t('err.imgDecode'))); };
    img.src = uri;
  });
}
function canvasToBlob(canvas, type, quality) {
  return new Promise(function (resolve) {
    canvas.toBlob(function (b) { resolve(b); }, type, quality);
  });
}
//: 保存兜底链（原版 index.html:3407-3453 移植；安卓 WebView 桥不移植——本项目没有安卓壳）。
//  顺序：navigator.share（能分享文件时优先，移动端体验最好）→ showSaveFilePicker
//  （桌面 Chromium 能让用户选路径）→ <a download>（老兜底）。前一段失败自然落到下一段；
//  AbortError 是用户主动取消，静默返回 'cancel' 不往上抛。
async function saveBlob(blob, name) {
  var file = null;
  try { file = new File([blob], name, { type: blob.type || 'application/octet-stream' }); } catch (e) {}
  if (file && navigator.canShare && navigator.canShare({ files: [file] })) {
    try {
      await navigator.share({ files: [file] });
      return 'share';
    } catch (e) {
      if (e && e.name === 'AbortError') return 'cancel';
    }
  }
  if (window.showSaveFilePicker) {
    try {
      var handle = await window.showSaveFilePicker({ suggestedName: name });
      var w = await handle.createWritable();
      await w.write(blob);
      await w.close();
      return 'picker';
    } catch (e) {
      if (e && e.name === 'AbortError') return 'cancel';
    }
  }
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  // 延迟回收：个别浏览器同步 revoke 会把刚触发的下载一起取消
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 10000);
  return 'download';
}
function stamp() {
  var d = new Date();
  var z = function (n) { return String(n).padStart(2, '0'); };
  return '' + d.getFullYear() + z(d.getMonth() + 1) + z(d.getDate()) + '-' +
         z(d.getHours()) + z(d.getMinutes()) + z(d.getSeconds());
}

async function doExport(target) {
  if (t3dReady() && (target === 'png' || target === 'bundle')) {
    return doExport3D(target);      // 3D 模式：客户端合成（服务端只会出 2D）
  }
  try {
    setStatus(t('note.exporting'));
    var res = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ params: buildParams(), target: target })
    });
    var ct = res.headers.get('Content-Type') || '';
    if (!res.ok || ct.indexOf('application/json') !== -1) {
      // 错误也按 JSON 收敛（WEB-SPEC §1），别把 JSON 当文件下载
      var data = await res.json().catch(function () { return null; });
      throw new ApiError((data && data.error) || ('HTTP ' + res.status), data && data.hint);
    }
    var blob = await res.blob();
    var name = EXPORT_FILES[target] || 'arcam-export';
    // 兜底链（share → picker → download）统一走这里；cancel = 用户主动取消
    var how = await saveBlob(blob, name);
    setStatus(how === 'cancel' ? t('note.saveCancel') : t('note.saveStart', name));
  } catch (e) {
    showApiError(e);
  }
}

async function doCopyScene() {
  var text = $('scene-json').textContent;
  if (!text) { setStatus(t('note.sceneEmpty'), null, true); return; }
  try {
    await navigator.clipboard.writeText(text);
    setStatus(t('note.sceneCopied'));
  } catch (err) {
    // 部分浏览器/非安全上下文拒绝写剪贴板：展开面板让人手动复制
    $('scene-details').open = true;
    setStatus(t('note.clipDenied'), null, true);
  }
}

/* ================= 7. 参数面板 ================= */

function markTouched(key) {
  // 只有真正动过的键会进 buildParams（前端「显式」口径）
  state.touched.add(key);
}

function bindNumeric(key) {
  const num = $('p-' + key + '-num');
  const rng = $('p-' + key + '-rng');
  const range = RANGES[key];
  function commit(v) {
    // 数字框手输可能超量程：夹到滑块区间，保证请求合法 + 双控件镜像一致
    v = Math.min(range[1], Math.max(range[0], v));
    state.params[key] = v;
    markTouched(key);
    // 数字与滑块互为镜像；正在编辑的那个不回写，避免打断输入
    if (document.activeElement !== num) num.value = v;
    if (document.activeElement !== rng) rng.value = v;
    scheduleRender();
  }
  num.addEventListener('input', function () {
    var v = parseFloat(num.value);
    if (isFinite(v)) commit(v); // 输入中途（"1."/"−"）不提交
  });
  rng.addEventListener('input', function () { commit(parseFloat(rng.value)); });
  // 失焦时把夹取后的值写回两个控件：手输超量程时输入框还留着 99，
  // 与真正发出去的 params（已夹到量程）不一致，会让人以为没夹成功
  num.addEventListener('change', function () {
    num.value = state.params[key];
    rng.value = state.params[key];
  });
}

function bindSelect(key, id, parse) {
  var el = $(id);
  el.addEventListener('change', function () {
    state.params[key] = parse ? parse(el.value) : el.value;
    markTouched(key);
    if (key === 'mode') syncModeVisibility();
    // 画幅改了立刻刷取景框读数——不能等 renderNow：没上传照片时它早退，
    // 读数会停在旧画幅上（原版在 hud() 里每帧刷，没这个坑）。
    if (key === 'frame') {
      var cv0 = $('preview-canvas');
      if (cv0 && cv0.width >= 8) {
        updateFrameRead(frameRectJs(state.params.frame, cv0.width, cv0.height));
      }
    }
    scheduleRender();
  });
}

function bindSlider(key, id, valId, fmt) {
  var el = $(id);
  var val = $(valId);
  el.addEventListener('input', function () {
    var v = parseFloat(el.value);
    state.params[key] = v;
    markTouched(key);
    val.textContent = fmt(v);
    scheduleRender();
  });
}

function bindTone() {
  $('p-tone').addEventListener('change', function (e) {
    state.params.tone = e.target.checked;
    markTouched('tone');
    scheduleRender();
  });
}

// edge-bar 仅自由模式有意义（WEB-SPEC §3：仅 free 模式显示）
function syncModeVisibility() {
  $('row-edge-bar').hidden = state.params.mode !== 'free';
}

/* ================= 7.5 语言（FEATURE-SPEC §3） ================= */

//: localStorage 键与原版一致（index.html:900）；?lang= 只影响当次加载。
var LANG_KEY = 'arcam.lang';

function applyLang() {
  const lang = window.ARCAM_LANG === 'en' ? 'en' : 'zh';
  document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
  // [data-t] → textContent；HUD/常驻条下一帧自然跟上（hud.js 每帧拼）
  document.querySelectorAll('[data-t]').forEach(function (el) {
    var v = window.arcamT ? window.arcamT(el.dataset.t) : null;
    if (v !== null) el.textContent = v;
  });
  var btn = $('btn-lang');
  if (btn) {
    btn.textContent = lang === 'en' ? '中' : 'EN';   // 显示的是「切过去会变成什么」
    btn.title = window.arcamT ? window.arcamT('lang.switch') : '';
  }
  if (window.arcamLive && window.arcamLive.active) {
    $('btn-live').textContent = window.arcamT('btn.liveStop');
  } else {
    $('btn-live').textContent = window.arcamT('btn.live');
  }
  // 取景框读数不是 data-t 元素（内容是 t('read.frame', W, H) 拼的），切语言要手动刷
  drawHudOverlay();
  // 拖拽区文件名：空态（setName 打的 empty 类）跟着语言走，已上传的文件名不动。
  // ⚠ 不给这些 span 挂 data-t——上面的无脑覆写会把文件名冲成「未选择」；
  //    frame-read 同款「非 data-t 手动刷」口径。
  Array.prototype.forEach.call(
    document.querySelectorAll('.dz-name.empty'), function (el) {
      el.textContent = window.arcamT('name.empty');
    });
}

function initLang() {
  // 优先级：URL 参数 > localStorage > 默认中文（index.html:903-914）
  var q = new URLSearchParams(location.search).get('lang');
  if (q === 'zh' || q === 'en') {
    window.ARCAM_LANG = q;
  } else {
    try {
      var s = localStorage.getItem(LANG_KEY);
      if (s === 'zh' || s === 'en') window.ARCAM_LANG = s;
    } catch (e) {}
  }
  applyLang();
  $('btn-lang').addEventListener('click', function () {
    window.ARCAM_LANG = window.ARCAM_LANG === 'en' ? 'zh' : 'en';
    try { localStorage.setItem(LANG_KEY, window.ARCAM_LANG); } catch (e) {}
    applyLang();
  });
}

/* ================= 7.6 修图线（纯色底/裁边/还原 + kind 弹窗 + tip） ================= */

// showTip(key, ...args)：多余参数原样透传给 t()（note.chLoaded/note.trimmed
// 这类带 {0} 占位符的文案，不传参就会在提示条上显示裸 {0}）
function showTip(key) {
  var el = $('tip');
  if (!el) return;
  el.textContent = window.arcamT ? window.arcamT.apply(null, arguments) : key;
  el.hidden = false;
  clearTimeout(showTip._t);
  showTip._t = setTimeout(function () { el.hidden = true; }, 3600);   // 原版 TIP_MS
}

// dataURI → 尺寸（修图后立绘换了尺寸，本地记录的 charW/charH 要跟着更新）
function imageDims(uri) {
  return new Promise(function (resolve) {
    var img = new Image();
    img.onload = function () { resolve({ w: img.naturalWidth, h: img.naturalHeight }); };
    img.onerror = function () { resolve(null); };
    img.src = uri;
  });
}

async function doCutSolid() {
  if (!state.char) { showTip('note.chNone'); return; }
  setStatus(t('note.prepRunning'));
  try {
    var r = await apiJson('/api/prep', { steps: ['solid', 'trim', 'soften', 'blur'] });
    if (r.ok) {
      // 后端已把处理后的立绘替换进会话；刷新本地尺寸（提示条与 autoFixX 宽高比都用它）
      var dims = await imageDims(r.image);
      if (dims && dims.w && dims.h) { state.charW = dims.w; state.charH = dims.h; }
      showEditNote(r);
      showTip('note.chLoaded', state.charName, state.charW, state.charH);
      scheduleRender();
    }
  } catch (e) { showApiError(e); }
}

async function doTrim() {
  if (!state.char) { showTip('note.chNone'); return; }
  setStatus(t('note.trimRunning'));
  var w0 = state.charW, h0 = state.charH;
  try {
    var r = await apiJson('/api/trim', {});
    if (r.ok) {
      // rect = [x, y, w, h]（/api/trim 的返回口径）
      if (r.rect && r.rect.length >= 4) { state.charW = r.rect[2]; state.charH = r.rect[3]; }
      showEditNote({ actions: ['note.autoTrim'] });
      showTip('note.trimmed', w0, h0, state.charW, state.charH);
      scheduleRender();
    }
  } catch (e) { showApiError(e); }
}

async function doReset() {
  if (!state.char) { showTip('note.chNone'); return; }
  try {
    var r = await apiJson('/api/reset', {});
    if (r.ok) {
      // /api/reset 直接带还原图的尺寸（原图的宽高比可能和修过的那张不同）
      if (r.width && r.height) { state.charW = r.width; state.charH = r.height; }
      showEditNote({ actions: [] });
      showTip('note.chReset');
      scheduleRender();
    }
  } catch (e) { showApiError(e); }
}

function showEditNote(r) {
  var el = $('edit-note');
  if (!el) return;
  var parts = [];
  if (r.cutRatio !== null && r.cutRatio !== undefined) {
    parts.push(window.arcamT('note.autoCut', (r.cutRatio * 100).toFixed(0)));
  }
  (r.actions || []).forEach(function (a) {
    if (a !== 'note.autoCut') parts.push(window.arcamT(a));
  });
  el.textContent = parts.join('　');
}

//: 全身/半身弹窗：只定**默认走哪条路**（原版"只是问，不改任何状态"）
function showKind() {
  var el = $('kind-modal');
  if (el) el.hidden = false;
}
function hideKind() {
  var el = $('kind-modal');
  if (el) el.hidden = true;
}

// ⚠ 这里直接改 state.params，必须逐个 markTouched：buildParams 只发「动过的键」，
//   漏标记 = 弹窗选完界面变了、请求体还是旧值（后端按默认 ground/全身渲）——
//   静默失效。docs 版没有 touched 机制，那边不受影响。
function applyCharKind(half) {
  if (half) {
    state.params.mode = 'free';
    state.params.size_pct = 60;
    state.params.edge_bar = 2;                 // 切口贴下边（原版 kind.doneHalf）
    markTouched('mode'); markTouched('size_pct'); markTouched('edge_bar');
    $('p-mode').value = 'free';
    $('p-size-pct-rng').value = 60;
    $('p-size-pct-val').textContent = '60%';
    $('p-edge-bar').value = '2';
  } else {
    state.params.mode = 'ground';
    state.params.shot = '全身';
    markTouched('mode'); markTouched('shot');
    $('p-mode').value = 'ground';
    $('p-shot').value = '全身';
  }
  syncModeVisibility();
  hideKind();
  scheduleRender();
}

function bindEditLine() {
  $('btn-cut-solid').addEventListener('click', doCutSolid);
  $('btn-trim').addEventListener('click', doTrim);
  $('btn-reset').addEventListener('click', doReset);
  $('kind-full').addEventListener('click', function () { applyCharKind(false); });
  $('kind-half').addEventListener('click', function () { applyCharKind(true); });
  $('kind-close').addEventListener('click', hideKind);
}

/* ================= 7.7 实时相机（FEATURE-SPEC §4） ================= */

function bindLive() {
  $('btn-live').addEventListener('click', function () {
    if (window.arcamLive.active) window.arcamLive.stop();
    else window.arcamLive.start();
  });
  $('btn-shot').addEventListener('click', function () {
    var cv = window.arcamLive.shoot();
    if (!cv) { showTip('note.shotFail'); return; }
    cv.toBlob(function (blob) {
      if (!blob) { showTip('note.shotFail'); return; }
      handleFile('photo', new File([blob], 'camera.png', { type: 'image/png' }));
      showTip('note.chLoaded', 'camera.png', cv.width, cv.height);
    }, 'image/png');
  });
  // 传感器 → 摆放参数（pitch/roll 实时跟上，300ms 防抖渲染不受影响：
  // HUD 每帧画，render 仍按 scheduleRender 的节奏）
  window.arcamLive.onOrient = function (pitch, roll) {
    state.params.pitch = Math.round(pitch * 2) / 2;
    state.params.roll = Math.round(roll * 2) / 2;
    $('p-pitch-num').value = state.params.pitch;
    $('p-pitch-rng').value = state.params.pitch;
    $('p-roll-num').value = state.params.roll;
    $('p-roll-rng').value = state.params.roll;
    markTouched('pitch'); markTouched('roll');
    scheduleRender();
  };
  // 相机开启后 fit() 会把预览/HUD 画布改成视频尺寸：HUD 浮层要按新尺寸重画
  // （不包这一层，桌面端无传感器时 HUD 会停在 16×9 的初始尺寸上，docs 侧同款）
  var origFit = window.arcamLive.fit;
  window.arcamLive.fit = function () {
    origFit();
    drawHudOverlay();
  };
}

/* ================= 7.8 HUD 浮层（FEATURE-SPEC §2） ================= */

//: HUD 开关状态（原版 S.grid/body/horizon 三根；默认只开网格，别一上来满屏线）
var HUD_ON = { grid: true, body: false, horizon: false };

function drawHudOverlay() {
  var hud = $('hud-canvas');
  var cv = $('preview-canvas');
  if (!hud || !cv) return;
  if (!window.arcamHud) return;
  var W = cv.width, H = cv.height;
  if (W < 8 || H < 8) return;
  hud.width = W; hud.height = H;
  var ctx = hud.getContext('2d');
  ctx.clearRect(0, 0, W, H);
  var p = state.params;
  var cam = { W: W, H: H, h: p.h, feq: p.feq, pitch: p.pitch, roll: p.roll };
  var subj = { hp: p.hp, d: p.d, x: p.x };
  // 取景框（frame_rect 的 JS 对译：与 Python frames.py 同公式）
  var R = frameRectJs(p.frame, W, H);
  if (R) window.arcamHud.drawFrame(ctx, R, { x: 0, y: 0, w: W, h: H }, W, H);
  if (HUD_ON.grid) window.arcamHud.drawGrid(ctx, cam);
  if (HUD_ON.horizon) window.arcamHud.drawHorizon(ctx, cam, window.arcamT('horizon'));
  if (HUD_ON.body) window.arcamHud.drawFigure(ctx, cam, subj);
  window.arcamHud.drawHud(ctx, cam, subj, !!state.charName, null, p.shot, p.mode);
  updateFrameRead(R);
}

//: 取景框读数（原版 frameRead，index.html:1971：hud() 里每帧刷「出图 W×H」）。
//  选完画幅立刻知道裁到多大，不用等渲染完看尺寸。语言跟着 applyLang 走。
function updateFrameRead(R) {
  var el = $('frame-read');
  if (!el || !R) return;
  el.textContent = window.arcamT('read.frame', Math.round(R.w), Math.round(R.h));
}

//: frameRect 的 JS 对译（frames.py 同公式；aspect 表与 Python FRAMES 逐字一致）
var FRAMES_JS = { '自由': 0.0, '1:1': 1.0, '4:5': 0.8, '16:9': 16 / 9, '9:16': 9 / 16, '满幅': -1.0 };

function frameRectJs(key, W, H) {
  var a = FRAMES_JS[key];
  if (a === undefined || a === -1.0) return { x: 0, y: 0, w: W, h: H };   // 满幅
  if (a === 0.0) {                                                       // 自由 = viewRect
    return { x: 0, y: 0, w: W, h: H };
  }
  var rw, rh;
  if (W / H > a) { rw = H * a; rh = H; } else { rw = W; rh = W / a; }
  var x = (W - rw) / 2, y = (H - rh) / 2;
  return { x: x, y: y, w: rw, h: rh };
}



/* ================= 6.5 批量出图（v1.6.0） ================= */

//: 多选照片 → dataURI 数组 → /api/batch → zip 下载。
//  与 CLI arcam batch 同语义：同一立绘同一组参数，逐张出图；单张失败不拖垮
//  整批（服务端跳过的原因写进 zip 里的 _errors.txt）。
async function doBatch(files) {
  if (!files || !files.length) return;
  if (!state.char) { setStatus(t('note.batchNeedChar'), null, true); return; }
  if (files.length > 12) { setStatus(t('note.batchTooMany'), null, true); return; }
  setStatus(t('note.batchRunning', files.length));
  var readers = Array.prototype.map.call(files, function (f) {
    return new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onload = function () { resolve(r.result); };
      r.onerror = function () { reject(new Error(t('err.readFail', f.name))); };
      r.readAsDataURL(f);
    });
  });
  try {
    var uris = await Promise.all(readers);
    var res = await fetch('/api/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ photos: uris, params: buildParams() })
    });
    var blob = await res.blob();
    if (!res.ok) {
      var d = await res.json().catch(function () { return null; });
      throw new ApiError((d && d.error) || ('HTTP ' + res.status), d && d.hint);
    }
    var note = res.headers.get('X-Arcam-Batch') || '';
    // 走 saveBlob 兜底链（v1.9.0）：和单张导出/3D 导出同一条路，
    // navigator.share / showSaveFilePicker 优先、<a download> 兜底
    var how = await saveBlob(blob, 'arcam-batch.zip');
    setStatus(t('note.batchDoneWeb', note + (how === 'cancel' ? t('note.batchCancel') : '')));
  } catch (e) { showApiError(e); }
}

/* ================= 9. 效果开关 + 拖拽摆位 + 自动摆正 ================= */

//: 光影三开关 → params（直接改变出图）；HUD 三开关 → HUD_ON（只画预览）
function bindFx() {
  // 显式逐个绑（不用 map 循环）：DOM id 与参数键的对应一眼可见，
  // 静态门禁的「死 id」扫描也认得出这些引用。
  $('fx-light').addEventListener('change', function (e) {
    state.params.light = e.target.checked; markTouched('light'); scheduleRender();
  });
  $('fx-grain').addEventListener('change', function (e) {
    state.params.grain = e.target.checked; markTouched('grain'); scheduleRender();
  });
  $('fx-shadow').addEventListener('change', function (e) {
    state.params.shadow = e.target.checked; markTouched('shadow'); scheduleRender();
  });
  $('fx-hud-grid').addEventListener('change', function (e) {
    HUD_ON.grid = e.target.checked; drawHudOverlay();
  });
  $('fx-hud-horizon').addEventListener('change', function (e) {
    HUD_ON.horizon = e.target.checked; drawHudOverlay();
  });
  $('fx-hud-body').addEventListener('change', function (e) {
    HUD_ON.body = e.target.checked; drawHudOverlay();
  });
  // 阴影方位/浓度（v1.6.0）：az=0、opacity=1 时与原版逐位一致
  bindSlider('shadow_az', 'p-shadow-az-rng', 'p-shadow-az-val',
             function (v) { return v.toFixed(0) + '°'; });
  bindSlider('shadow_opacity', 'p-shadow-op-rng', 'p-shadow-op-val',
             function (v) { return v.toFixed(2); });
  // 颗粒强度（v1.9.0）：乘在颗粒幅度上，不动曲线/白平衡/饱和；1.0 = 原版逐位一致
  bindSlider('grain_scale', 'p-grain-scale-rng', 'p-grain-scale-val',
             function (v) { return v.toFixed(2); });
  $('btn-autofix').addEventListener('click', doAutoFix);
}

//: 自动摆正（原版「自动摆正」钮；ground 用数值导数解、free 直接改 freeP）
function doAutoFix() {
  if (!window.arcamHud) return;
  var cv = $('preview-canvas');
  var p = state.params;
  var cam = { W: cv.width, H: cv.height, h: p.h, feq: p.feq, pitch: p.pitch, roll: p.roll };
  var subj = { hp: p.hp, d: p.d, x: p.x };
  var aspect = state.charW && state.charH ? state.charW / state.charH : 0.6;
  var r = window.arcamHud.autoFixX(cam, subj, p.mode, p.size_pct,
                                   { shot: p.shot, charScale: p.char_scale, charAspect: aspect });
  if (!r) { setStatus(t('note.autofixFail')); return; }
  if (r.freeU !== undefined) {
    // free 模式：freeP.u 由后端参数 x 承载不了——webui 的 freeP 在后端默认 (0.5,0.6)，
    // 这里把吸附结果换算成等效提示（后端不接收 freeP，保持简单：只报吸附到了哪）
    setStatus(t('note.snapFree', Math.round(r.snapped * 100)));
    return;
  }
  state.params.x = Math.round(r.x * 100) / 100;
  $('p-x-num').value = state.params.x;
  $('p-x-rng').value = state.params.x;
  markTouched('x');
  setStatus(t('note.snapGround', Math.round(r.snapped * 100), state.params.x));
  scheduleRender();
}

//: 拖拽摆位（原版核心交互：ground 拖画面把人摆到那块地面；free 直接拖立绘）
function bindDrag() {
  var cv = $('preview-canvas');
  if (!cv) return;
  var dragging = false;

  function toCanvasPixel(ev) {
    var r = cv.getBoundingClientRect();
    return {
      u: (ev.clientX - r.left) * (cv.width / r.width),
      v: (ev.clientY - r.top) * (cv.height / r.height)
    };
  }

  cv.addEventListener('pointerdown', function (ev) {
    if (!state.photo || !state.char) return;
    dragging = true;
    try { cv.setPointerCapture(ev.pointerId); } catch (e) {}
    ev.preventDefault();
  });
  cv.addEventListener('pointermove', function (ev) {
    if (!dragging) return;
    var px = toCanvasPixel(ev);
    var p = state.params;
    if (p.mode === 'free') {
      // free：拖动 = 立绘图心跟手（归一化 freeP；后端不收 freeP，
      // 用 size_pct 高度 + 画面占比反推不出自由位置——故 free 拖动只改提示）
      setStatus(t('note.dragFree', Math.round(px.u / cv.width * 100),
                  Math.round(px.v / cv.height * 100)));
      return;
    }
    if (!window.arcamHud) return;
    var cam = { W: cv.width, H: cv.height, h: p.h, feq: p.feq, pitch: p.pitch, roll: p.roll };
    var hit = window.arcamHud.placeAt(px.u, px.v, cam);
    if (!hit) return;
    state.params.x = Math.round(hit.x * 100) / 100;
    state.params.d = Math.round(hit.d * 100) / 100;
    $('p-x-num').value = state.params.x;
    $('p-x-rng').value = state.params.x;
    $('p-d-num').value = state.params.d;
    $('p-d-rng').value = state.params.d;
    markTouched('x'); markTouched('d');
    scheduleRender();
  });
  var end = function () { dragging = false; };
  cv.addEventListener('pointerup', end);
  cv.addEventListener('pointercancel', end);
}


/* ================= 9.5 全屏取景 + 参数记忆（v1.4.0） ================= */

//: 全屏取景：把 #stage 推入 fullscreen（原版手机端就是整屏取景）。
//  HUD 浮层与拖拽在全屏下照旧（canvas 尺寸不变，CSS 放大而已）。
function bindFullscreen() {
  $('btn-fullscreen').addEventListener('click', function () {
    var stage = $('stage');
    if (!stage) return;
    if (document.fullscreenElement) {
      document.exitFullscreen();
      return;
    }
    var p = stage.requestFullscreen && stage.requestFullscreen();
    if (p && p.catch) p.catch(function () {
      setStatus(t('note.fsDenied'), null, true);
    });
  });
  document.addEventListener('fullscreenchange', function () {
    var on = !!document.fullscreenElement;
    $('btn-fullscreen').textContent = on ? t('btn.exitFullscreen') : t('btn.fullscreen');
    if (on) setStatus(t('note.fsOn'));
  });
}

//: 参数记忆：params 存 localStorage，下次打开自动恢复（原版只存语言，这里多存参数——
//  调好一套站位后关掉页面不再丢。reset 键清空。
var PARAM_KEY = 'arcam.params';

function saveParams() {
  try {
    localStorage.setItem(PARAM_KEY, JSON.stringify({
      params: state.params, touched: Array.from(state.touched)
    }));
  } catch (e) {}
}

function restoreParams() {
  try {
    var raw = localStorage.getItem(PARAM_KEY);
    if (!raw) return false;
    var data = JSON.parse(raw);
    if (!data || !data.params) return false;
    // 只恢复 schema 里有的键（旧版本存过的怪键不进来）
    Object.keys(S_DEFAULTS).forEach(function (k) {
      if (data.params[k] !== undefined) state.params[k] = data.params[k];
    });
    if (data.touched) data.touched.forEach(function (k) { state.touched.add(k); });
    return true;
  } catch (e) { return false; }
}

// 把恢复后的参数写回控件（不写回的话界面和状态不一致）
function syncControlsFromParams() {
  NUMERIC.forEach(function (key) {
    var v = state.params[key];
    var num = $('p-' + key + '-num'), rng = $('p-' + key + '-rng');
    if (num) num.value = v;
    if (rng) rng.value = v;
  });
  var sel = { shot: 'p-shot', frame: 'p-frame', mode: 'p-mode',
              rot_basis: 'p-rot', edge_bar: 'p-edge-bar' };
  Object.keys(sel).forEach(function (k) {
    var el = $(sel[k]);
    if (el) el.value = state.params[k];
  });
  $('p-char-scale-rng').value = state.params.char_scale;
  $('p-char-scale-val').textContent = state.params.char_scale.toFixed(2);
  $('p-size-pct-rng').value = state.params.size_pct;
  $('p-size-pct-val').textContent = state.params.size_pct.toFixed(0) + '%';
  $('p-tone').checked = !!state.params.tone;
  $('p-shadow-az-rng').value = state.params.shadow_az;
  $('p-shadow-az-val').textContent = state.params.shadow_az.toFixed(0) + '°';
  $('p-shadow-op-rng').value = state.params.shadow_opacity;
  $('p-shadow-op-val').textContent = state.params.shadow_opacity.toFixed(2);
  $('p-grain-scale-rng').value = state.params.grain_scale;
  $('p-grain-scale-val').textContent = state.params.grain_scale.toFixed(2);
  $('fx-light').checked = !!state.params.light;
  $('fx-grain').checked = !!state.params.grain;
  $('fx-shadow').checked = !!state.params.shadow;
  syncModeVisibility();
}

/* ============ zip 最小实现（store 方式，不压缩；v1.7.0 从 docs/app.js 移植） ============
 * pure-JS ZIP writer：手写本地文件头 + 中央目录 + EOCD，stored（method 0）。
 * 与 CLI bundle 语义一致的三件套 shot.jpg/scene.json/character.png；
 * 时间戳固定 1980-01-01（DOS date 0x0021），与 Python cmd_bundle 的
 * ZIP_DATE_TIME 一致，确定性复现。CRC32 用标准表驱动实现（多项式 0xEDB88320）。
 */

const ZIP_DOS_TIME = 0;     // 00:00:00
const ZIP_DOS_DATE = 0x21;  // 1980-01-01 = (1980-1980)<<9 | 1<<5 | 1

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) { c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1); }
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(bytes) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i++) {
    c = CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
  }
  return (c ^ 0xFFFFFFFF) >>> 0;
}

function buildBundleZip(files) {
  const chunks = [];
  const central = [];
  let offset = 0;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    const nameBytes = new TextEncoder().encode(f.name);
    const crc = crc32(f.data);
    const size = f.data.length;

    // 本地文件头（30 字节固定 + 文件名）
    const lh = new Uint8Array(30 + nameBytes.length);
    const dv = new DataView(lh.buffer);
    dv.setUint32(0, 0x04034b50, true);  // local file header 签名
    dv.setUint16(4, 20, true);          // version needed to extract
    dv.setUint16(6, 0, true);           // general purpose flags（文件名全 ASCII，不置 UTF-8 位）
    dv.setUint16(8, 0, true);           // method 0 = stored（不压缩）
    dv.setUint16(10, ZIP_DOS_TIME, true);
    dv.setUint16(12, ZIP_DOS_DATE, true);
    dv.setUint32(14, crc, true);
    dv.setUint32(18, size, true);       // compressed = uncompressed（store）
    dv.setUint32(22, size, true);
    dv.setUint16(26, nameBytes.length, true);
    dv.setUint16(28, 0, true);          // extra field 长度
    lh.set(nameBytes, 30);
    chunks.push(lh, f.data);

    // 中央目录头（46 字节固定 + 文件名）
    const cd = new Uint8Array(46 + nameBytes.length);
    const cv = new DataView(cd.buffer);
    cv.setUint32(0, 0x02014b50, true);  // central directory header 签名
    cv.setUint16(4, (3 << 8) | 20, true); // version made by（unix，同 Python zipfile on darwin）
    cv.setUint16(6, 20, true);          // version needed
    cv.setUint16(8, 0, true);           // flags
    cv.setUint16(10, 0, true);          // method 0
    cv.setUint16(12, ZIP_DOS_TIME, true);
    cv.setUint16(14, ZIP_DOS_DATE, true);
    cv.setUint32(16, crc, true);
    cv.setUint32(20, size, true);
    cv.setUint32(24, size, true);
    cv.setUint16(28, nameBytes.length, true);
    cv.setUint16(30, 0, true);          // extra 长度
    cv.setUint16(32, 0, true);          // comment 长度
    cv.setUint16(34, 0, true);          // disk number start
    cv.setUint16(36, 0, true);          // internal attrs
    cv.setUint32(38, (0o644 << 16) >>> 0, true); // external attrs：rw-r--r--，同 CLI
    cv.setUint32(42, offset, true);     // 本地头相对偏移
    cd.set(nameBytes, 46);
    central.push(cd);

    offset += lh.length + size;
  }

  const cdSize = central.reduce((s, c) => s + c.length, 0);
  const end = new Uint8Array(22);
  const ev = new DataView(end.buffer);
  ev.setUint32(0, 0x06054b50, true);    // end of central directory 签名
  ev.setUint16(4, 0, true);             // 本磁盘编号
  ev.setUint16(6, 0, true);             // 中央目录所在磁盘
  ev.setUint16(8, files.length, true);  // 本磁盘条目数
  ev.setUint16(10, files.length, true); // 总条目数
  ev.setUint32(12, cdSize, true);
  ev.setUint32(16, offset, true);       // 中央目录偏移
  ev.setUint16(20, 0, true);            // comment 长度

  return new Blob(chunks.concat(central, [end]), { type: 'application/zip' });
}

/* ================= 7.8 MMD 3D 层（v1.7.0；与 Pages 版同源） =================
 * 分工：后端只出**照片底**（render 带 base_only=true，同裁切框），WebGL 层由
 * 本页 mmd.js 合成——Python 没有 WebGL，这是唯一方式。预览/导出/参数全部照旧，
 * 3D 只是把「画 2D 立绘」换成「画 MMD 层」。
 * 引擎未就绪/没导模型 → 全部路径给人话提示，不静默失败；渲染抛异常只记落位。
 */

state.use3D = false;
state.pose3D = -1;

function t3dReady() {
  var M3D = window.ARCAM_MMD;
  return !!(state.use3D && M3D && M3D.ready && M3D.srcName);
}

function d3dNote(msg, isError) {
  var el = $('d3dNote');
  if (!el) return;
  el.textContent = msg || '';
  el.hidden = !msg;
  el.classList.toggle('error', !!isError);
}

//: 3D 要有个占位立绘的宽高比（吸附/构图提示/scene 全在用立绘几何）。
//   真正画的是 MMD 层，这张全透明占位图只提供宽高比（≈0.58，接近全身立绘）。
var char3dPh = null;
function char3dPlaceholder() {
  if (!char3dPh) {
    char3dPh = document.createElement('canvas');
    char3dPh.width = 300; char3dPh.height = 520;
  }
  return char3dPh;
}
function ensurePlaceholderChar() {
  if (state.char) return;
  state.char = char3dPlaceholder();
  state.charW = 300; state.charH = 520;
}

/** 3D 必须站地上：自由模式那套（贴边吸附/高度占比）对真透视模型没意义 */
function forceGroundForMmd() {
  if (state.params.mode !== 'free') return;
  state.params.mode = 'ground';
  $('p-mode').value = 'ground';
  markTouched('mode');
  syncModeVisibility();
}

async function d3dInitEngine() {
  var M3D = window.ARCAM_MMD;
  if (!M3D) { d3dNote(t('note.3dNoEngine'), true); return false; }
  if (M3D.ready) return true;
  d3dNote(t('note.3dLoading'));
  var ok = false;
  try { ok = await M3D.init(); }
  catch (e) { d3dNote(t('note.3dFail', (e && e.message) || e), true); return false; }
  if (!ok) { d3dNote(t('note.3dFail', M3D.error || '?'), true); return false; }
  return true;
}

function d3dNoteOk() {
  if (!t3dReady()) return;
  var M3D = window.ARCAM_MMD, st = M3D.stats;
  d3dNote(t('note.3dOk', M3D.srcName, st.tris, st.bones, st.poses));
}

/** 给 3D 当环境贴图（IBL）的照片缩略图（64×36）。本地版有实拍照片——
 *  照片就是这片环境（与 Pages 版同源）。缓存着，照片换了才重画。 */
var envFrameCv = null, envFrameSrc = null;
function mmdEnvFrame() {
  var src = state.photo;
  if (!src) return null;
  if (!envFrameCv) {
    envFrameCv = document.createElement('canvas');
    envFrameCv.width = 64; envFrameCv.height = 36;
  }
  if (envFrameSrc !== src) {
    try { envFrameCv.getContext('2d').drawImage(src, 0, 0, 64, 36); }
    catch (e) { return null; }
    envFrameSrc = src;
  }
  return envFrameCv;
}

/** 把 MMD 层画到目标画布上（预览/导出共用）。返回是否真画了。 */
function drawMmdLayer(target, W, H) {
  var M3D = window.ARCAM_MMD;
  if (!t3dReady()) return false;
  var p = state.params;
  try {
    var ms = M3D.render({
      W: W, H: H, f: window.arcamHud.focalPx(W, H, p.feq),
      camH: p.h, pitch: p.pitch, roll: p.roll,
      x: p.x, d: p.d, hp: p.hp, scale: p.char_scale,
      pose: state.pose3D, env: null, envFrame: mmdEnvFrame()
    });
    if (!ms) return false;
    var lay = M3D.canvas();
    if (!lay) return false;
    target.getContext('2d').drawImage(lay, 0, 0, W, H);
    return true;
  } catch (e) {
    console.warn('[arcam] 3D 层渲染抛异常：', e);
    return false;
  }
}

/** scene.json 挂 mmd 段（与 Pages 版同字段；老字段一个不动）。 */
function attachMmdKey(scene, W, H) {
  if (!scene) return scene;
  var M3D = window.ARCAM_MMD;
  if (!t3dReady()) { scene.mmd = null; return scene; }
  var p = state.params;
  scene.mmd = {
    model: M3D.srcName,
    pose: (M3D.poseNames()[state.pose3D]) || null,
    modelHeightM: +(M3D.modelHeight()).toFixed(4),
    heightM: +(p.hp * p.char_scale).toFixed(4),
    groundPoint: { lateralM: +p.x.toFixed(3), distanceM: +p.d.toFixed(3) },
    camera: {
      heightM: +p.h.toFixed(4), pitchDeg: +p.pitch.toFixed(3), rollDeg: +p.roll.toFixed(3),
      focalPx: +window.arcamHud.focalPx(W, H, p.feq).toFixed(2), canvas: [W, H]
    },
    stats: { tris: M3D.stats.tris, bones: M3D.stats.bones, morphs: M3D.stats.morphs }
  };
  return scene;
}

/** 模型列表 + 姿势行（一个包里多个 pmx 时列出来；姿势来自 mmd/poses.json） */
function refresh3dRows() {
  var M3D = window.ARCAM_MMD;
  var on = t3dReady();
  $('d3dLight').hidden = !on;
  if (!on) { $('model3dRow').hidden = true; $('pose3dRow').hidden = true; return; }
  var names = M3D.modelNames();
  var mrow = $('model3dRow');
  mrow.textContent = '';
  names.forEach(function (name, i) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = name.split(/[\/]/).pop().replace(/\.pmx$/i, '');
    b.className = (i === M3D.modelIndex()) ? 'on' : '';
    b.addEventListener('click', async function () {
      if (i === M3D.modelIndex()) return;
      b.dataset.busy = '1';
      var ok = await M3D.switchModel(i);   // 同一份输入重建，贴图自然还是这个包里的
      delete b.dataset.busy;
      if (!ok) { d3dNote(t('note.3dFail', M3D.error || '?'), true); return; }
      refresh3dRows(); d3dNoteOk(); scheduleRender();
    });
    mrow.appendChild(b);
  });
  var del = document.createElement('button');
  del.type = 'button';
  del.style.marginLeft = '6px';
  del.textContent = t('btn.modelDrop');
  del.addEventListener('click', async function () {
    del.dataset.busy = '1';
    var ok = await M3D.dropModel(M3D.modelIndex());
    delete del.dataset.busy;
    if (!ok) { d3dNote(t('note.3dAllDropped')); return; }
    refresh3dRows(); d3dNoteOk(); scheduleRender();
  });
  mrow.appendChild(del);
  $('model3dLabel').hidden = names.length < 2;
  mrow.hidden = names.length < 2;
  var prow = $('pose3dRow');
  prow.textContent = '';
  var mk = function (label, idx) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.className = (state.pose3D === idx) ? 'on' : '';
    b.addEventListener('click', function () { state.pose3D = idx; refresh3dRows(); scheduleRender(); });
    return b;
  };
  prow.appendChild(mk(t('pose.still'), -1));
  M3D.poseNames().forEach(function (name, i) { prow.appendChild(mk(name, i)); });
  var hasPose = M3D.poseNames().length > 0;
  $('pose3dLabel').hidden = !hasPose;
  prow.hidden = !hasPose;
  var prm = M3D.params;
  if (prm) {
    $('az3d').value = prm.keyAz;  $('oaz3d').textContent = prm.keyAz + '°';
    $('el3d').value = prm.keyEl;  $('oel3d').textContent = prm.keyEl + '°';
    $('dof3d').value = prm.dof;   $('odof3d').textContent = (+prm.dof).toFixed(2);
    $('focus3d').value = state.params.d;
    $('ofocus3d').textContent = state.params.d.toFixed(1) + 'm';
  }
}

/** 导入模型（zip/glb/文件夹/pmx）。load 成功返 true；统计在 M3D.stats。 */
async function do3DImport(src, pmxIdx) {
  if (!src) return;
  var M3D = window.ARCAM_MMD;
  if (!M3D) { d3dNote(t('note.3dNoEngine'), true); return; }
  d3dNote(t('note.3dLoading'));
  try {
    var ok = await M3D.load(src, function (frac) {
      d3dNote(t('note.3dLoading') + ' ' + Math.round(frac * 100) + '%');
    }, pmxIdx);
    if (!ok) { d3dNote(t('note.3dFail', M3D.error || t('err.unknown')), true); return; }
    state.use3D = true;
    $('t3d').classList.add('on');
    forceGroundForMmd();
    ensurePlaceholderChar();
    refresh3dRows();
    d3dNoteOk();
    scheduleRender();
  } catch (e) {
    d3dNote(t('note.3dFail', (e && e.message) || e), true);
  }
}

function bind3D() {
  $('t3d').addEventListener('click', async function () {
    state.use3D = !state.use3D;
    $('t3d').classList.toggle('on', state.use3D);
    if (state.use3D) {
      forceGroundForMmd();
      ensurePlaceholderChar();
      var ok = await d3dInitEngine();
      if (ok && window.ARCAM_MMD && !window.ARCAM_MMD.srcName) d3dNote(t('note.3dNeedModel'));
    } else {
      d3dNote('');
    }
    refresh3dRows();
    scheduleRender();
  });
  $('f3d').addEventListener('click', function () { $('f3dFile').click(); });
  $('f3dFile').addEventListener('change', function (e) {
    if (e.target.files && e.target.files[0]) do3DImport(e.target.files[0]);
    e.target.value = '';               // 同一个文件再选一次也要能触发
  });
  $('f3dDirBtn').addEventListener('click', function () { $('f3dDir').click(); });
  $('f3dDir').addEventListener('change', function (e) {
    var files = e.target.files;
    if (!files || !files.length) return;
    do3DImport(files);                 // FileList 原样喂给 mmd.js（它自己挑 pmx 与贴图）
    e.target.value = '';
  });
  // 3D 专属控件（az/el/focus/dof）。mmd.js 的 params getter 返回 L 对象引用——
  // 直接改字段即生效（无 setLight）。
  var bind3 = function (id, key, fmt) {
    var el = $(id), out = $('o' + id);
    el.addEventListener('input', function () {
      var v = parseFloat(el.value);
      if (window.ARCAM_MMD && window.ARCAM_MMD.params) window.ARCAM_MMD.params[key] = v;
      if (out) out.textContent = fmt(v);
      scheduleRender();
    });
  };
  bind3('az3d', 'keyAz', function (v) { return v.toFixed(0) + '°'; });
  bind3('el3d', 'keyEl', function (v) { return v.toFixed(0) + '°'; });
  bind3('focus3d', 'focus', function (v) { return v.toFixed(1) + 'm'; });
  bind3('dof3d', 'dof', function (v) { return v.toFixed(2); });
}

/* ================= 8. 初始化 ================= */

function init() {
  initLang();
  bindDrop('drop-photo', 'file-photo', 'photo');
  bindDrop('drop-char', 'file-char', 'char');
  bindDrop('drop-depth', 'file-depth', 'depth');

  NUMERIC.forEach(bindNumeric);
  bindSelect('shot', 'p-shot');
  bindSelect('frame', 'p-frame');
  bindSelect('mode', 'p-mode');
  bindSelect('rot_basis', 'p-rot');
  bindSelect('edge_bar', 'p-edge-bar', function (v) { return parseInt(v, 10); });
  bindSlider('char_scale', 'p-char-scale-rng', 'p-char-scale-val', function (v) { return v.toFixed(2); });
  bindSlider('size_pct', 'p-size-pct-rng', 'p-size-pct-val', function (v) { return v.toFixed(0) + '%'; });
  bindTone();

  $('btn-matte').addEventListener('click', doMatte);
  $('btn-lidar').addEventListener('click', doLidar);
  $('btn-batch').addEventListener('click', function () { $('file-photos').click(); });
  $('file-photos').addEventListener('change', function (e) {
    doBatch(e.target.files);
    e.target.value = '';                    // 清掉，否则同一批文件再选不触发 change
  });
  $('btn-rerender').addEventListener('click', renderNow);
  $('btn-png').addEventListener('click', function () { doExport('png'); });
  $('btn-layer').addEventListener('click', function () { doExport('layer'); });
  $('btn-bundle').addEventListener('click', function () { doExport('bundle'); });
  $('btn-copy-scene').addEventListener('click', doCopyScene);

  bindEditLine();
  bindLive();
  bindFx();
  bindDrag();
  bind3D();
  bindFullscreen();
  syncModeVisibility();
  setStatus(t('note.ready'));
  $('status-msg').className = '';
  // HUD 浮层常驻可见（画布上没有预览图时也能看到网格/读数）
  var hud = $('hud-canvas');
  if (hud) hud.hidden = false;
  drawHudOverlay();

  // 参数记忆：恢复上次的摆放参数（原版只存语言；调好一套站位关掉页面不再丢）
  if (restoreParams()) {
    syncControlsFromParams();
    setStatus(t('note.paramsRestored'));
  }
  // 参数改动后防抖存盘（包一层 scheduleRender；渲染节奏不变）
  var _origSchedule = scheduleRender;
  scheduleRender = function () {
    _origSchedule();
    clearTimeout(scheduleRender._saveT);
    scheduleRender._saveT = setTimeout(saveParams, 500);
  };
  window.addEventListener('beforeunload', saveParams);
}

init();
