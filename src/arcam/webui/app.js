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
var S_DEFAULTS = {
  x: 0, d: 4, h: 1.6, hp: 1.65, feq: 23, pitch: 0, roll: 0,
  shot: '全身', char_scale: 1.0, frame: '满幅', rot_basis: 'world',
  mode: 'ground', size_pct: 60, edge_bar: 0, tone: false
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

var KIND_LABEL = { photo: '照片', char: '立绘', depth: '深度帧' };
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
  $('status-time').textContent = typeof ms === 'number' ? '耗时 ' + Math.round(ms) + ' ms' : '';
}

// 错误统一进状态条：显示 error+hint；不清空用户已调参数（state 不与失败路径耦合）。
function showApiError(e) {
  setStatus('出错：' + e.message, null, true);
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
  el.classList.toggle('empty', text === '未选择');
}

async function uploadFile(kind, file) {
  var fd = new FormData();
  fd.append(kind, file); // 字段名 photo/char/depth 与 WEB-SPEC §1 对齐
  setStatus('上传' + KIND_LABEL[kind] + '…');
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
      else setStatus('照片已上传，请继续上传立绘', r.ms);
    } else if (kind === 'char') {
      state.char = file;
      setName('char-name', file.name + dims);
      // 后端用 fg 告诉这块立绘有没有 alpha：普通图提示可点抠图（不自动烧 CPU）
      if (r.fg === false) {
        $('input-hint').textContent = '这是普通图立绘，可点「抠图」去除背景';
      } else {
        $('input-hint').textContent = '';
      }
      if (state.photo) renderNow();
      else setStatus('立绘已上传，请上传照片', r.ms);
      // 导入后问一句全身/半身（原版 loadChar：只定默认走哪条路，不改任何状态）
      showKind();
    } else if (kind === 'depth') {
      state.depthFile = file;
      setName('depth-name', file.name + dims);
      $('input-hint').textContent = '深度帧已上传；点「LiDAR 诊断」解析环境量';
      setStatus('深度帧已上传', r.ms);
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
  img.onerror = function () { setStatus('后端返回的图片解码失败', null, true); };
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
    '相机高 ' + Number(env.height_m).toFixed(2) + ' m',
    '俯仰 ' + Number(env.pitch_deg).toFixed(1) + '°',
    '横滚 ' + Number(env.roll_deg).toFixed(1) + '°'
  ];
  // 诊断字段缺省就不显示（render 的 env 只有三个字段，见 mergeEnv）
  if (env.coverage != null) {
    var cov = env.coverage <= 1
      ? (env.coverage * 100).toFixed(1) + '%'
      : Number(env.coverage).toFixed(1) + '%';
    parts.push('覆盖率 ' + cov);
  }
  if (env.inliers != null) parts.push('内点 ' + env.inliers);
  if (env.rms != null) parts.push('RMS ' + Number(env.rms).toFixed(3));
  if (frame) parts.push('帧 ' + frame.width + '×' + frame.height);
  box.textContent = 'LiDAR 环境量（深度测出，显式参数仍优先）：' + parts.join(' · ');
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
    setStatus('请先上传照片与立绘', null, true);
    return;
  }
  var seq = ++state.renderSeq;
  var t0 = performance.now();
  try {
    var r = await apiJson('/api/render', { params: buildParams(), scale: previewScale() });
    if (seq !== state.renderSeq) return; // 过期响应直接丢弃
    drawDataUri(r.image);
    $('preview-empty').hidden = true;
    showScene(r.scene);
    if (r.env) mergeEnv(r.env, null); // render 的 env 只有三字段，合并进完整 env
    setStatus('渲染完成', typeof r.ms === 'number' ? r.ms : performance.now() - t0);
    drawHudOverlay();                   // HUD 浮层跟着预览尺寸/参数重画
  } catch (e) {
    showApiError(e);
  }
}

/* ================= 6. 动作按钮 ================= */

async function doMatte() {
  if (!state.char) { setStatus('请先上传立绘', null, true); return; }
  try {
    setStatus('抠图模型推理中…');
    var r = await apiJson('/api/matte', { source: 'char' });
    drawDataUri(r.image); // 先把抠图结果画出来：照片没上传也能看到效果
    $('preview-empty').hidden = true;
    await apiJson('/api/matte-result-as-char', {}); // 结果设为当前立绘
    $('input-hint').textContent = '抠图完成，结果已设为当前立绘';
    setStatus('抠图完成', r.ms);
    if (state.photo) scheduleRender();
  } catch (e) {
    showApiError(e); // 模型缺失等情况：error 带下载指引，不 500
  }
}

async function doLidar() {
  if (!state.depthFile) {
    setStatus('请先在左栏选择深度帧文件', null, true);
    return;
  }
  try {
    setStatus('LiDAR 深度诊断中…');
    var fd = new FormData();
    fd.append('depth', state.depthFile);
    var r = await apiForm('/api/lidar/inspect', fd);
    state.depthUploaded = true; // 之后 render 带 depth_uploaded=true 走深度驱动
    state.lidarEnv = r.env;     // 完整 env（含 coverage/inliers/rms）
    showEnv(r.env, r.frame);
    setStatus('LiDAR 诊断完成，已启用深度驱动');
    scheduleRender();
  } catch (e) {
    showApiError(e);
  }
}

async function doExport(target) {
  try {
    setStatus('导出中…');
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
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = EXPORT_FILES[target] || 'arcam-export';
    document.body.appendChild(a);
    a.click();
    a.remove();
    // 延迟回收：个别浏览器同步 revoke 会把刚触发的下载一起取消
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 10000);
    setStatus('已开始下载 ' + a.download);
  } catch (e) {
    showApiError(e);
  }
}

async function doCopyScene() {
  var text = $('scene-json').textContent;
  if (!text) { setStatus('scene.json 还是空的，先渲染一次', null, true); return; }
  try {
    await navigator.clipboard.writeText(text);
    setStatus('scene.json 已复制到剪贴板');
  } catch (err) {
    // 部分浏览器/非安全上下文拒绝写剪贴板：展开面板让人手动复制
    $('scene-details').open = true;
    setStatus('浏览器拒绝写剪贴板，已展开 scene.json 请手动复制', null, true);
  }
}

/* ================= 7. 参数面板 ================= */

function markTouched(key) {
  // 只有真正动过的键会进 buildParams（前端「显式」口径）
  state.touched.add(key);
}

function bindNumeric(key) {
  var num = $('p-' + key + '-num');
  var rng = $('p-' + key + '-rng');
  var range = RANGES[key];
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

function currentLang() {
  return window.ARCAM_LANG === 'en' ? 'en' : 'zh';
}

function applyLang() {
  var lang = currentLang();
  window.ARCAM_LANG = lang;
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
    window.ARCAM_LANG = currentLang() === 'en' ? 'zh' : 'en';
    try { localStorage.setItem(LANG_KEY, window.ARCAM_LANG); } catch (e) {}
    applyLang();
  });
}

/* ================= 7.6 修图线（纯色底/裁边/还原 + kind 弹窗 + tip） ================= */

function showTip(key) {
  var el = $('tip');
  if (!el) return;
  el.textContent = window.arcamT ? window.arcamT(key) : key;
  el.hidden = false;
  clearTimeout(showTip._t);
  showTip._t = setTimeout(function () { el.hidden = true; }, 3600);   // 原版 TIP_MS
}

async function doCutSolid() {
  if (!state.charName) { showTip('note.chNone'); return; }
  setStatus('修图中…');
  try {
    var r = await apiJson('/api/prep', { steps: ['solid', 'trim', 'soften', 'blur'] });
    if (r.ok) {
      state.charName = state.charName;          // 名字不变（还是那张立绘）
      showEditNote(r);
      showTip('note.chLoaded');
      scheduleRender();
    }
  } catch (e) { showApiError(e); }
}

async function doTrim() {
  if (!state.charName) { showTip('note.chNone'); return; }
  setStatus('裁边中…');
  try {
    var r = await apiJson('/api/trim', {});
    if (r.ok) {
      showEditNote({ trimmed: true, rect: r.rect, actions: ['note.autoTrim'] });
      showTip('note.trimmed');
      scheduleRender();
    }
  } catch (e) { showApiError(e); }
}

async function doReset() {
  if (!state.charName) { showTip('note.chNone'); return; }
  try {
    var r = await apiJson('/api/reset', {});
    if (r.ok) {
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

function applyCharKind(half) {
  if (half) {
    state.params.mode = 'free';
    state.params.size_pct = 60;
    state.params.edge_bar = 2;                 // 切口贴下边（原版 kind.doneHalf）
    $('p-mode').value = 'free';
    $('p-size-pct-rng').value = 60;
    $('p-size-pct-val').textContent = '60%';
    $('p-edge-bar').value = '2';
  } else {
    state.params.mode = 'ground';
    state.params.shot = '全身';
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
      showTip('note.chLoaded');
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
  $('btn-rerender').addEventListener('click', renderNow);
  $('btn-png').addEventListener('click', function () { doExport('png'); });
  $('btn-layer').addEventListener('click', function () { doExport('layer'); });
  $('btn-bundle').addEventListener('click', function () { doExport('bundle'); });
  $('btn-copy-scene').addEventListener('click', doCopyScene);

  bindEditLine();
  bindLive();

  syncModeVisibility();
  setStatus('就绪');
  $('status-msg').className = '';
  // HUD 浮层常驻可见（画布上没有预览图时也能看到网格/读数）
  var hud = $('hud-canvas');
  if (hud) hud.hidden = false;
  drawHudOverlay();
}

init();
