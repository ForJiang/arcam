/* arcam web UI（GitHub Pages 版） — 原生 ES2022 module，零依赖、零框架、零 CDN。
 * 契约：docs/SITE-SPEC.md §2/§3（数值口径与 Python 逐字一致，由 arcam-core.js /
 * arcam-render.js 两个模块承载；本文件只做页面接线：上传 → composeShot → 预览/下载）。
 * 与本地 webui/ 的 DOM id 逐一对应（去掉 LiDAR 深度帧 / env 相关部分）。
 *
 * 分组：1 模块加载与降级 → 2 常量状态 → 3 工具 → 4 图片装载
 *      5 上传 → 6 渲染 → 7 动作按钮 → 8 zip 最小实现 → 9 参数面板 → 10 初始化
 */

/* ================= 1. 模块加载（动态 import；未就绪时页面降级提示） ================= */

// SITE-SPEC §0/§3：index.html 只引用本文件，两个管线模块在这里动态 import——
// 这样任一模块未发布/加载失败时 import 只是 reject，页面其余部分照常绑定，
// 只在状态条与预览区给出降级提示。
const _missing = [];
const core = await import('./arcam-core.js').catch(() => { _missing.push('arcam-core.js'); return null; });
const renderMod = await import('./arcam-render.js').catch(() => { _missing.push('arcam-render.js'); return null; });
const pipelineReady = core !== null && renderMod !== null;

/* ================= 2. 常量与状态 ================= */

// WEB-SPEC §2：默认值 = 原版 S 默认（与 CLI _add_placement 逐字一致）。
const S_DEFAULTS = {
  x: 0, d: 4, h: 1.6, hp: 1.65, feq: 23, pitch: 0, roll: 0,
  shot: '全身', char_scale: 1.0, frame: '满幅', rot_basis: 'world',
  mode: 'ground', size_pct: 60, edge_bar: 0, tone: false,
  // 光影三开关（v1.3.0）：本地版三项都真生效（webapp → compose 的门）；
  // 网页版（docs）只有 shadow 真生效——JS 侧没有 bakeLit（v1 起环境光烘焙
  // 就是 no-op），light/grain 在那边只影响状态显示，UI 提示里写明。
  light: true, grain: true, shadow: true,
  shadow_az: 0, shadow_opacity: 1.0,
  base_only: false          // 内部态：3D 就绪时由 buildParams 自动带上，不进用户面板
};

// 数字+滑块双控件的七个量；量程同本地 webui（x ±6 对应 place 的夹取范围，SPEC §3.1）。
const NUMERIC = ['x', 'd', 'h', 'hp', 'feq', 'pitch', 'roll'];
const RANGES = {
  x: [-6, 6, 0.05], d: [0.5, 12, 0.05], h: [0.3, 3, 0.05], hp: [0.4, 2.5, 0.01],
  feq: [10, 120, 0.5], pitch: [-60, 60, 0.5], roll: [-90, 90, 0.5]
};

const DEBOUNCE_MS = 300;         // 任何参数改动 300ms 防抖自动重渲（WEB-SPEC §3）
const PREVIEW_LONG_EDGE = 1280;  // 预览降采样长边上限（export 永远全尺寸，同本地后端 scale 语义）

const state = {
  params: Object.assign({}, S_DEFAULTS),
  photoImg: null, photoW: 0, photoH: 0,
  photoCanvasFull: null, photoCanvasPreview: null,
  charImg: null, charName: '', charCanvas: null, charOrig: null, charHasAlpha: false,
  renderSeq: 0,   // 序号哨兵：过期异步渲染直接丢弃，防旧图盖新图
  renderTimer: 0
};

/* ================= 3. 小工具 ================= */

function $(id) { return document.getElementById(id); }

function setStatus(msg, ms, isError) {
  const el = $('status-msg');
  el.textContent = msg;
  el.className = isError ? 'error' : 'ok';
  $('status-time').textContent = typeof ms === 'number' ? '耗时 ' + Math.round(ms) + ' ms' : '';
}

function setInputHint(text) { $('input-hint').textContent = text || ''; }

function setName(id, text) {
  const el = $(id);
  el.textContent = text;
  el.classList.toggle('empty', text === '未选择');
}

function pad2(n) { return String(n).padStart(2, '0'); }

// 下载文件名时间戳（本地时间 YYYYMMDD-HHMMSS）
function stamp() {
  const d = new Date();
  return d.getFullYear() + pad2(d.getMonth() + 1) + pad2(d.getDate()) + '-' +
    pad2(d.getHours()) + pad2(d.getMinutes()) + pad2(d.getSeconds());
}

// J4 的抠图返回 canvas（或图片/位图），统一成可 drawImage + toBlob 的 canvas。
// 容错：OffscreenCanvas 不是 HTMLCanvasElement 实例，但可以 drawImage 进来
//（SITE-SPEC §2 的契约是 HTMLCanvasElement，这里只是不让相邻情况硬失败）。
function toCanvas(x) {
  if (x instanceof HTMLCanvasElement) return x;
  if (x && (x.width || x.naturalWidth)) {
    return drawToCanvas(x, x.naturalWidth || x.width, x.naturalHeight || x.height);
  }
  throw new Error('抠图结果不是图片');
}

// 全画布扫 alpha：立绘带不带透明通道决定 hint 文案（本地版由后端 fg 告知，这里自己算）
function hasAlpha(canvas) {
  const d = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
  for (let i = 3; i < d.length; i += 4) { if (d[i] !== 255) return true; }
  return false;
}

function canvasToBlob(canvas, type, quality) {
  return new Promise((resolve, reject) => {
    if (typeof canvas.toBlob === 'function') {
      canvas.toBlob((b) => { b ? resolve(b) : reject(new Error('画布编码失败')); }, type, quality);
    } else if (typeof canvas.convertToBlob === 'function') { // OffscreenCanvas
      canvas.convertToBlob({ type: type, quality: quality })
        .then(resolve, (e) => reject(e || new Error('画布编码失败')));
    } else {
      reject(new Error('画布不支持编码导出'));
    }
  });
}

function saveBlob(blob, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // 延迟回收：个别浏览器同步 revoke 会把刚触发的下载一起取消（同本地 webui）
  setTimeout(() => { URL.revokeObjectURL(a.href); }, 10000);
}

/* ================= 4. 图片装载（file → ImageBitmap/Image → canvas） ================= */

function loadImageEl(file) {
  // createImageBitmap 优先（不解码 EXIF 方向由浏览器统一处理）；老浏览器退化为 <img> + objectURL
  if (typeof createImageBitmap === 'function') {
    return createImageBitmap(file).then((bm) => {
      return { img: bm, w: bm.width, h: bm.height, revoke: null };
    });
  }
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => resolve({ img: img, w: img.naturalWidth, h: img.naturalHeight, revoke: url });
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('图片解码失败：' + file.name)); };
    img.src = url;
  });
}

// 把上面两种来源统一画进 canvas（ImageBitmap 与 <img> 都是合法 drawImage 源）
function drawToCanvas(src, w, h) {
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(w));
  c.height = Math.max(1, Math.round(h));
  const ctx = c.getContext('2d');
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(src, 0, 0, c.width, c.height);
  return c;
}

async function setPhoto(file) {
  let loaded;
  try {
    loaded = await loadImageEl(file);
  } catch (e) {
    setStatus(e.message, null, true);
    return;
  }
  if (loaded.revoke) URL.revokeObjectURL(loaded.revoke); // 解码完成后 objectURL 即可回收
  state.photoImg = loaded.img;
  state.photoW = loaded.w;
  state.photoH = loaded.h;
  state.photoCanvasFull = drawToCanvas(loaded.img, loaded.w, loaded.h);
  // 预览降采样（长边 ≤1280）：与本地后端 previewScale() 同语义，export 仍走全尺寸
  const s = Math.min(1, PREVIEW_LONG_EDGE / Math.max(loaded.w, loaded.h));
  state.photoCanvasPreview = s < 1
    ? drawToCanvas(loaded.img, loaded.w * s, loaded.h * s)
    : state.photoCanvasFull;
  setName('photo-name', file.name + ' · ' + loaded.w + '×' + loaded.h);
  $('preview-empty').hidden = true;
  if (state.charCanvas) renderNow();
  else setStatus('照片已上传，请继续上传立绘');
}

async function setChar(file) {
  let loaded;
  try {
    loaded = await loadImageEl(file);
  } catch (e) {
    setStatus(e.message, null, true);
    return;
  }
  state.charImg = loaded.img;
  state.charName = file.name;
  state.charCanvas = drawToCanvas(loaded.img, loaded.w, loaded.h);
  state.charOrig = drawToCanvas(loaded.img, loaded.w, loaded.h);   // 还原基线（/api/reset 同语义）
  state.charHasAlpha = hasAlpha(state.charCanvas);
  setName('char-name', file.name + ' · ' + loaded.w + '×' + loaded.h);
  $('preview-empty').hidden = true;
  if (state.charHasAlpha) {
    setInputHint('');
  } else {
    // 普通图立绘：有抠图模块就引导点按钮，没有就指引去本地版（matte.js 懒加载一次）
    await ensureMatte();
    setInputHint(typeof window.arcamMatte === 'function'
      ? '这是普通图立绘，可点「抠图」去除背景'
      : '这是普通图立绘（无 alpha）：本页抠图不可用，请用本地版「抠图」后导出带 alpha 的 PNG');
  }
  if (state.photoCanvasFull) renderNow();
  else setStatus('立绘已上传，请上传照片');
  // 导入后问一句全身/半身（只定默认走哪条路，不改任何状态）
  $('kind-modal').hidden = false;
}

/* ================= 5. 上传（drag&drop + 点击选择） ================= */

function handleFile(kind, file) {
  if (!file) return;
  if (!file.type || file.type.indexOf('image/') !== 0) {
    setStatus('只支持图片文件（' + (file.name || '未命名') + '）', null, true);
    return;
  }
  if (kind === 'photo') return setPhoto(file);
  return setChar(file);
}

function bindDrop(zoneId, inputId, kind) {
  const zone = $(zoneId);
  const input = $(inputId);
  zone.addEventListener('click', () => { input.click(); });
  input.addEventListener('change', () => {
    if (input.files && input.files[0]) handleFile(kind, input.files[0]);
    input.value = ''; // 清空后同一个文件再次选择才会触发 change（同本地 webui）
  });
  zone.addEventListener('dragenter', (e) => { e.preventDefault(); zone.classList.add('drag'); });
  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('drag'); });
  zone.addEventListener('dragleave', () => { zone.classList.remove('drag'); });
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('drag');
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) handleFile(kind, f);
  });
}

/* ================= 6. 渲染（300ms 防抖 → composeShot → drawImage） ================= */

function camOf(p, W, H) { return { W: W, H: H, h: p.h, feq: p.feq, pitch: p.pitch, roll: p.roll }; }
function subjOf(p) { return { hp: p.hp, d: p.d, x: p.x }; }

// composeShot/composeCharLayer 的 params。键名取 arcam-render.js normalizeParams
// 实际读的那一套（SITE-SPEC §1 的 opts 就是 camelCase：charScale/rotBasis/sizePct/
// edgeBar；frame 另收 frameKey 别名）。渲染侧只读 camelCase，不放 snake_case 副本。
function composeParams(p) {
  const kw = {
    frame: p.frame,
    mode: p.mode,
    shot: p.shot,
    sizePct: p.size_pct,
    charScale: p.char_scale,
    rotBasis: p.rot_basis,
    edgeBar: p.edge_bar,
    tone: !!p.tone,
    shadow: p.shadow !== false,
    shadowAz: p.shadow_az, shadowOpacity: p.shadow_opacity,
    shadow_az: p.shadow_az, shadow_opacity: p.shadow_opacity
  };
  if (mmdReady()) {
    kw.charDraw = draw3DInto;      // 3D 层挂钩点：替换 arcam-render.js 里的 drawInto
  }
  return kw;
}

function scheduleRender() {
  clearTimeout(state.renderTimer);
  state.renderTimer = setTimeout(renderNow, DEBOUNCE_MS);
}

async function renderNow() {
  clearTimeout(state.renderTimer);
  if (!pipelineReady) {
    setStatus('运算模块未就绪：' + _missing.join('、') + ' 未发布或加载失败', null, true);
    return;
  }
  if (!state.photoCanvasPreview || !state.charCanvas) {
    setStatus('请先上传照片与立绘', null, true);
    return;
  }
  const seq = ++state.renderSeq;
  const t0 = performance.now();
  try {
    const p = state.params;
    // 轻量校验：值域口径与后端 choices 一致（在core的 SHOTS/FRAMES 里查）
    if (!core.SHOTS[p.shot] || !(p.frame in core.FRAMES)) {
      throw new Error('参数越界：shot=' + p.shot + ' / frame=' + p.frame);
    }
    const r = await renderMod.composeShot(
      state.photoCanvasPreview, state.charCanvas, subjOf(p),
      camOf(p, state.photoCanvasPreview.width, state.photoCanvasPreview.height),
      composeParams(p));
    if (seq !== state.renderSeq) return; // 过期渲染直接丢弃
    drawToPreview(r.shot);
    drawHudOverlay();
    showScene(attachMmdKey(r.scene, state.photoCanvasPreview.width, state.photoCanvasPreview.height));
    setStatus('渲染完成', performance.now() - t0);
  } catch (e) {
    setStatus('渲染出错：' + (e && e.message ? e.message : e), null, true);
  }
}

function drawToPreview(src) {
  if (!src) throw new Error('合成结果为空');
  const c = $('preview-canvas');
  c.width = src.width;
  c.height = src.height;
  const ctx = c.getContext('2d');
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(src, 0, 0);
  $('preview-empty').hidden = true;
}

function showScene(scene) {
  $('scene-json').textContent = scene ? JSON.stringify(scene, null, 2) : '';
}

/* ================= 7. 动作按钮 ================= */

/* ---- 抠图（J4 挂钩点：仅当 window.arcamMatte 存在才显示按钮） ---- */

let matteTried = false;

function refreshMatteBtn() {
  $('btn-matte').hidden = typeof window.arcamMatte !== 'function';
}

// matte.js 懒加载：仅在「可能要抠图」时试一次；文件不存在时 import 静默失败，按钮保持隐藏
async function ensureMatte() {
  refreshMatteBtn();
  if (typeof window.arcamMatte === 'function' || matteTried) return;
  matteTried = true;
  try { await import('./matte.js'); } catch (e) { /* 未部署/加载失败：忽略 */ }
  refreshMatteBtn();
}

async function doMatte() {
  if (!state.charImg) { setStatus('请先上传立绘', null, true); return; }
  await ensureMatte();
  const fn = window.arcamMatte;
  if (typeof fn !== 'function') {
    setStatus('抠图模块未加载（vendor/ 缺文件；本地版「arcam web」同样可抠图）', null, true);
    return;
  }
  try {
    setStatus('抠图模型推理中…');
    const t0 = performance.now();
    const onProgress = (frac) => {
      if (typeof frac === 'number' && isFinite(frac)) {
        setStatus('抠图模型推理中… ' + Math.round(frac * 100) + '%');
      }
    };
    const out = await fn(state.charImg, onProgress); // 契约：async (imgEl, onProgress) => canvas
    state.charCanvas = toCanvas(out);
    // 重新扫一次 alpha：抠图结果理应有透明像素；没有就把 hint 留着提醒换图
    state.charHasAlpha = hasAlpha(state.charCanvas);
    setName('char-name', state.charName + ' · 已抠图');
    setInputHint(state.charHasAlpha ? '' : '抠图结果没有透明像素，请换一张立绘');
    setStatus('抠图完成', performance.now() - t0);
    scheduleRender();
  } catch (e) {
    setStatus('抠图失败：' + (e && e.message ? e.message : e), null, true);
  }
}

/* ---- 下载：PNG / 立绘层 / zip 三件套（全尺寸重合成，不用预览那份） ---- */

async function composeFull() {
  const p = state.params;
  return renderMod.composeShot(
    state.photoCanvasFull, state.charCanvas, subjOf(p),
    camOf(p, state.photoCanvasFull.width, state.photoCanvasFull.height),
    composeParams(p));
}

async function doExportPng() {
  try {
    setStatus('导出中…');
    if (!state.photoCanvasFull) throw new Error('还没有上传照片，出不了图');
    const r = await composeFull();
    const blob = await canvasToBlob(r.shot, 'image/png');
    const name = 'arcam-render-' + stamp() + '.png';
    saveBlob(blob, name);
    setStatus('已开始下载 ' + name);
  } catch (e) {
    setStatus('导出失败：' + (e && e.message ? e.message : e), null, true);
  }
}

async function doExportLayer() {
  try {
    setStatus('导出中…');
    const p = state.params;
    if (!state.charCanvas) throw new Error('还没有上传立绘，出不了立绘层');
    // 层与照片同尺寸：没有照片时 camOf 会拿 null.width 抛裸 TypeError，和人话校验一致先挡
    if (!state.photoCanvasFull) throw new Error('还没有上传照片，出不了立绘层');
    // 立绘层：与照片同尺寸的透明层，直接盖在出图上严丝合缝（Python cmd_char_layer 同语义）
    const r = await renderMod.composeCharLayer(
      state.charCanvas, subjOf(p),
      camOf(p, state.photoCanvasFull.width, state.photoCanvasFull.height),
      composeParams(p));
    const layer = r && (r.layer || r.charLayer || r.char_layer);
    if (!layer) throw new Error('立绘层为空（立绘缺失或落位失败）');
    const blob = await canvasToBlob(layer, 'image/png');
    const name = 'arcam-char-layer-' + stamp() + '.png';
    saveBlob(blob, name);
    setStatus('已开始下载 ' + name);
  } catch (e) {
    setStatus('导出失败：' + (e && e.message ? e.message : e), null, true);
  }
}

async function doExportBundle() {
  try {
    setStatus('导出中…');
    const p = state.params;
    if (!state.charCanvas) throw new Error('素材包需要立绘');
    // 没有照片时 composeFull 里的 camOf 会拿 null.width 抛裸 TypeError；先人话挡掉，
    // 也省掉一遍白跑的合成（doExportPng 同款校验）
    if (!state.photoCanvasFull) throw new Error('还没有上传照片，出不了素材包');
    const r = await composeFull(); // { shot, rect, scene, light }
    // 素材包的 character.png 必须是「与 shot.jpg 同一个裁切框」的那份立绘层：
    // Python cmd_bundle 明确不另调 compose_char_layer（它内部固定 '满幅'，
    // 换画幅会与裁切后的 shot.jpg 错位），而是复用 composeShot 内部带出来的层。
    // JS 侧 composeShot 的返回值里没有层（那是服务端 _Render 的内部产物），
    // 所以用同一套参数走 composeCharLayer 拿同框层：frameRect 与落位都由同一批
    // 参数决定，裁出来必然与 shot.jpg 严丝合缝。
    const layerRes = await renderMod.composeCharLayer(
      state.charCanvas, subjOf(p),
      camOf(p, state.photoCanvasFull.width, state.photoCanvasFull.height),
      composeParams(p));
    const layer = layerRes && layerRes.layer;
    if (!layer) throw new Error('这一遍没有产出立绘层（立绘缺失或落位失败）');
    const scene = r && r.scene;
    if (!scene) throw new Error('scene.json 生成失败');
    // 3D 开着：scene.json 的 mmd 段记这一枪是 3D 渲的（原版 index.html:3776-3787）
    attachMmdKey(scene, state.photoCanvasFull.width, state.photoCanvasFull.height);
    const shotBlob = await canvasToBlob(r.shot, 'image/jpeg', 0.95); // JPEG q0.95（同 CLI encode_jpeg 95）
    const layerBlob = await canvasToBlob(layer, 'image/png');
    const sceneBytes = new TextEncoder().encode(JSON.stringify(scene, null, 2));
    const zip = buildBundleZip([
      { name: 'shot.jpg', data: new Uint8Array(await shotBlob.arrayBuffer()) },
      { name: 'scene.json', data: sceneBytes },
      { name: 'character.png', data: new Uint8Array(await layerBlob.arrayBuffer()) }
    ]);
    const name = 'arcam-bundle-' + stamp() + '.zip';
    saveBlob(zip, name);
    setStatus('已开始下载 ' + name + '（shot.jpg + scene.json + character.png）');
  } catch (e) {
    setStatus('导出失败：' + (e && e.message ? e.message : e), null, true);
  }
}

async function doCopyScene() {
  const text = $('scene-json').textContent;
  if (!text) { setStatus('scene.json 还是空的，先渲染一次', null, true); return; }
  try {
    await navigator.clipboard.writeText(text);
    setStatus('scene.json 已复制到剪贴板');
  } catch (err) {
    // 部分浏览器/非安全上下文拒绝写剪贴板：展开面板让人手动复制（同本地 webui）
    $('scene-details').open = true;
    setStatus('浏览器拒绝写剪贴板，已展开 scene.json 请手动复制', null, true);
  }
}

/* ================= 8. zip 最小实现（store 方式，不压缩） =================
 * pure-JS ZIP writer：手写本地文件头 + 中央目录 + EOCD，stored（method 0）。
 * 与 CLI bundle 语义一致的三件套 shot.jpg/scene.json/character.png；
 * 时间戳固定 1980-01-01（DOS date 0x0021），与 Python cmd_bundle 的
 * ZIP_DATE_TIME 一致，确定性复现。CRC32 用标准表驱动实现（多项式 0xEDB88320）。
 * [ZIP-BEGIN] */

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

// files: [{ name, data: Uint8Array }] → application/zip Blob
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
/* [ZIP-END] */

/* ================= 9. 参数面板 ================= */

function bindNumeric(key) {
  const num = $('p-' + key + '-num');
  const rng = $('p-' + key + '-rng');
  const range = RANGES[key];
  function commit(v) {
    // 数字框手输可能超量程：夹到滑块区间，保证合成入参合法 + 双控件镜像一致（同本地 webui）
    v = Math.min(range[1], Math.max(range[0], v));
    state.params[key] = v;
    if (document.activeElement !== num) num.value = v;
    if (document.activeElement !== rng) rng.value = v;
    scheduleRender();
  }
  num.addEventListener('input', () => {
    const v = parseFloat(num.value);
    if (isFinite(v)) commit(v); // 输入中途（"1."/"−"）不提交
  });
  rng.addEventListener('input', () => { commit(parseFloat(rng.value)); });
  // 失焦时把夹取后的值写回两个控件：手输超量程时输入框还留着 99，
  // 与真正参与合成的 params（已夹到量程）不一致，会让人以为没夹成功（同本地 webui）
  num.addEventListener('change', () => {
    num.value = state.params[key];
    rng.value = state.params[key];
  });
}

function bindSelect(key, id, parse) {
  const el = $(id);
  el.addEventListener('change', () => {
    state.params[key] = parse ? parse(el.value) : el.value;
    if (key === 'mode') syncModeVisibility();
    scheduleRender();
  });
}

function bindSlider(key, id, valId, fmt) {
  const el = $(id);
  const val = $(valId);
  el.addEventListener('input', () => {
    const v = parseFloat(el.value);
    state.params[key] = v;
    val.textContent = fmt(v);
    scheduleRender();
  });
}

function bindTone() {
  $('p-tone').addEventListener('change', (e) => {
    state.params.tone = e.target.checked;
    scheduleRender();
  });
}

// edge-bar 仅自由模式有意义（同本地 webui）
function syncModeVisibility() {
  $('row-edge-bar').hidden = state.params.mode !== 'free';
}

/* ================= 10. 初始化 ================= */

function init() {
  bindDrop('drop-photo', 'file-photo', 'photo');
  bindDrop('drop-char', 'file-char', 'char');

  NUMERIC.forEach(bindNumeric);
  bindSelect('shot', 'p-shot');
  bindSelect('frame', 'p-frame');
  bindSelect('mode', 'p-mode');
  bindSelect('rot_basis', 'p-rot');
  bindSelect('edge_bar', 'p-edge-bar', (v) => parseInt(v, 10));
  bindSlider('char_scale', 'p-char-scale-rng', 'p-char-scale-val', (v) => v.toFixed(2));
  bindSlider('size_pct', 'p-size-pct-rng', 'p-size-pct-val', (v) => v.toFixed(0) + '%');
  bindTone();

  $('btn-matte').addEventListener('click', doMatte);
  $('btn-rerender').addEventListener('click', renderNow);
  $('btn-batch').addEventListener('click', () => $('file-photos').click());
  $('file-photos').addEventListener('change', (e) => {
    doBatch(e.target.files);
    e.target.value = '';
  });
  $('btn-png').addEventListener('click', doExportPng);
  $('btn-layer').addEventListener('click', doExportLayer);
  $('btn-bundle').addEventListener('click', doExportBundle);
  $('btn-copy-scene').addEventListener('click', doCopyScene);

  syncModeVisibility();
  setStatus('就绪');
  $('status-msg').className = '';

  // J4 挂钩点：抠图钮只在 window.arcamMatte 就绪后出现。三种上线方式都覆盖——
  // ① module 早于本文件设好（init 时已在）；② load 后才设好；③ 设好后派发事件。
  // 直接调一次而不只等 load：webview 里 module 可能晚于 load 事件执行，
  // 只挂 load 监听会永远错过（matte.js 本身只是模块壳，44MB 模型在首次
  // 抠图调用时才下载， init 时加载它没有流量成本）。
  refreshMatteBtn();
  ensureMatte();
  window.addEventListener('load', ensureMatte);
  window.addEventListener('arcam:matte-ready', refreshMatteBtn);

  initLang();
  bindEditLine();
  bindLive();
  bindFx();
  bindDrag();
  bindBatch();
  bindFullscreen();
  if (restoreParams()) {
    syncControlsFromParams();
    setStatus(t('note.paramsRestored'));
  }
  const _origSchedule = scheduleRender;
  scheduleRender = function () {
    _origSchedule();
    clearTimeout(scheduleRender._t);
    scheduleRender._t = setTimeout(saveParams, 500);
  };
  window.addEventListener('beforeunload', saveParams);
  bind3D();
  $('hud-canvas').hidden = false;
  drawHudOverlay();

  if (!pipelineReady) {
    const msg = '运算模块未就绪：' + _missing.join('、') +
      ' 未发布或加载失败，本页功能暂不可用';
    setStatus(msg, null, true);
    $('preview-empty').textContent = msg;
    $('preview-empty').hidden = false;
  }
}

/* ================= 10. 语言（FEATURE-SPEC §3） ================= */

// t() 薄封装：i18n.js 挂在 window.arcamT 上（与 webui 侧同一张表）
function t(key) {
  return window.arcamT ? window.arcamT.apply(null, arguments) : key;
}

function initLang() {
  const q = new URLSearchParams(location.search).get('lang');
  if (q === 'zh' || q === 'en') window.ARCAM_LANG = q;
  else {
    try {
      const s = localStorage.getItem('arcam.lang');
      if (s === 'zh' || s === 'en') window.ARCAM_LANG = s;
    } catch (e) {}
  }
  applyLang();
  $('btn-lang').addEventListener('click', () => {
    window.ARCAM_LANG = window.ARCAM_LANG === 'en' ? 'zh' : 'en';
    try { localStorage.setItem('arcam.lang', window.ARCAM_LANG); } catch (e) {}
    applyLang();
    renderNow();
  });
}

function applyLang() {
  const lang = window.ARCAM_LANG === 'en' ? 'en' : 'zh';
  document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
  // ⚠ 不覆写 document.title：原版的 title 就是 app.title（"手机端"），但本 port
  //    有自己的标题（index.html 里写死"arcam（GitHub Pages 版）"）——语言切换
  //    只换正文，不动牌子。
  document.querySelectorAll('[data-t]').forEach((el) => {
    const v = t(el.dataset.t);
    if (v !== el.dataset.t) el.textContent = v;
  });
  $('btn-lang').textContent = lang === 'en' ? '中' : 'EN';
  $('btn-lang').title = t('lang.switch');
  if (window.arcamLive && window.arcamLive.active) $('btn-live').textContent = t('btn.liveStop');
  else $('btn-live').textContent = t('btn.live');
}

/* ================= 11. 修图线（纯色底/裁边/还原 + kind 弹窗） ================= */

function showTip(key, ...args) {
  const el = $('tip');
  el.textContent = args.length ? t(key, ...args) : t(key);
  el.hidden = false;
  clearTimeout(showTip._t);
  showTip._t = setTimeout(() => { el.hidden = true; }, 3600);
}

function bindEditLine() {
  $('btn-cut-solid').addEventListener('click', doPrep);
  $('btn-trim').addEventListener('click', doTrim);
  $('btn-reset').addEventListener('click', doReset);
  $('kind-full').addEventListener('click', () => applyCharKind(false));
  $('kind-half').addEventListener('click', () => applyCharKind(true));
  $('kind-close').addEventListener('click', () => { $('kind-modal').hidden = true; });
}

function doPrep() {
  if (!state.charCanvas) { showTip('note.chNone'); return; }
  const MT = window.arcamMatteTools;
  if (!MT) { setStatus('修图模块未加载', null, true); return; }
  const before = MT.copyOf(state.charCanvas);
  const r = MT.autoPrepareChar(state.charCanvas);
  if (r.canvas !== before && (r.cut !== null || r.trimmed)) {
    state.charCanvas = r.canvas;
  }
  const parts = [];
  if (r.cut !== null) parts.push(t('note.autoCut', (r.cut * 100).toFixed(0)));
  (r.actions || []).forEach((a) => { if (a !== 'note.autoCut') parts.push(t(a)); });
  $('edit-note').textContent = parts.join('　');
  showTip('note.chLoaded', state.charName, state.charCanvas.width, state.charCanvas.height);
  renderNow();
}

function doTrim() {
  if (!state.charCanvas) { showTip('note.chNone'); return; }
  const MT = window.arcamMatteTools;
  if (!MT) { setStatus('修图模块未加载', null, true); return; }
  const w0 = state.charCanvas.width, h0 = state.charCanvas.height;
  const r = MT.autoTrim(state.charCanvas);
  if (!r) { $('edit-note').textContent = t('note.noMargin'); return; }
  state.charCanvas = r.canvas;
  $('edit-note').textContent = t('note.trimmed', w0, h0, r.rect.w, r.rect.h);
  renderNow();
}

function doReset() {
  const MT = window.arcamMatteTools;
  if (!MT || !state.charOrig) { showTip('note.chNone'); return; }
  state.charCanvas = MT.copyOf(state.charOrig);
  $('edit-note').textContent = t('note.chReset');
  renderNow();
}

function applyCharKind(half) {
  const p = state.params;
  if (half) {
    p.mode = 'free'; p.size_pct = 60; p.edge_bar = 2;
    $('p-mode').value = 'free';
    $('p-size-pct-rng').value = 60;
    $('p-size-pct-val').textContent = '60%';
    $('p-edge-bar').value = '2';
  } else {
    p.mode = 'ground'; p.shot = '全身';
    $('p-mode').value = 'ground';
    $('p-shot').value = '全身';
  }
  syncModeVisibility();
  $('kind-modal').hidden = true;
  renderNow();
}


/* ================= 12.5 MMD 3D 层（FEATURE-SPEC §5） =================
 * 它**不改 2D 那条路的任何东西**：mmd.js（经典 script，挂 window.ARCAM_MMD）把模型
 * 按 arcam 的投影公式渲到一块离屏画布上，这里通过 composeParams 的 charDraw 挂钩
 * 把它画进 composeShot/composeCharLayer 的 full 画布（帧坐标 0,0），之后的裁切 /
 * 落位 / scene.json / 导出全部原样走 —— "预览 = 出图"照旧成立。
 * 引擎未就绪（文件没发布 / file:// 被 CORS 拦 / 没导入模型）时全部路径给人话提示，
 * 不静默失败；charDraw 内部渲染失败则只记落位（层不回退 2D —— 3D 开着却画 2D 会
 * 双重叠图，原版 draw3DLayer 返 false 时由 draw() 决定回退，本管线在钩子外层）。
 */

//: 3D 开关与姿势下标（原版 use3D / pose3D，index.html:1511；-1 = 静止）
state.use3D = false;
state.pose3D = -1;

//: composeShot 的 charDraw 钩子实现：把 WebGL 层画进 full 画布。
//  原版 draw3DLayer 的对译（index.html:1692-1710）——几何/裁切/落位与 2D 路径
//  同一套投影（mmd.js 的相机矩阵按 arcam 投影公式拼，见其文件头注释）。
function draw3DInto(full, rect) {
  const M3D = window.ARCAM_MMD;
  const p = state.params;
  const W = full.width, H = full.height;
  const cam = camOf(p, W, H);
  let lay = null;
  try {
    const ms = M3D.render({
      W: W, H: H, f: core.focalPx(W, H, p.feq),
      camH: p.h, pitch: p.pitch, roll: p.roll,
      x: p.x, d: p.d, hp: p.hp, scale: p.char_scale,
      pose: state.pose3D, env: null, envFrame: mmdEnvFrame(),
    });
    if (ms) lay = M3D.canvas();
  } catch (e) {
    console.warn('[arcam] 3D 层渲染抛异常：', e);
  }
  if (!lay) {
    // 渲失败：不叠层（drawInto 已被 charDraw 顶掉），只记落位 + 把报错摆出来
    d3dNote(t('note.3dDrawFail', 'render 返回 0'), true);
    return placementForScene(cam, rect);
  }
  full.getContext('2d').drawImage(lay, 0, 0, W, H);
  return placementForScene(cam, rect);
}

//: scene.json 仍记 2D 落位（模型站在同一个地面点；配方与 2D 路径同构）
function placementForScene(cam, rect) {
  // ⚠ core 是本文件顶部 import 的 arcam-core.js；window.arcamCore 从来没挂过，别用
  //   （HUD 的取景框同理：走 window.arcamHud.frameRect —— hud.js 从 core 再导出了它）
  const p = state.params;
  const subj = { hp: p.hp, d: p.d, x: p.x };
  const ch = state.charCanvas || char3dPlaceholder();
  return core.currentPlacement(ch.width, ch.height, subj, cam, {
    mode: p.mode, sizePct: p.size_pct, charScale: p.char_scale,
    rotBasis: p.rot_basis, shot: p.shot, edgeBar: p.edge_bar, rect: rect,
  });
}

function d3dNote(msg, isError) {
  const el = $('d3dNote');
  if (!el) return;
  el.textContent = msg || '';
  el.hidden = !msg;
  el.classList.toggle('error', !!isError);
}

/** mmd.js 是否真的能出图（引擎就绪 + 有模型）。charDraw 只在此时挂 —— 没就绪时
 *  走原 drawInto（2D 立绘照旧画），这就是"3D 开着但模型没进来"的外层回退。 */
function mmdReady() {
  const M3D = window.ARCAM_MMD;
  return !!(state.use3D && M3D && M3D.ready && M3D.srcName);
}

async function d3dInitEngine() {
  const M3D = window.ARCAM_MMD;
  if (!M3D) { d3dNote(t('note.3dNoEngine'), true); return false; }
  if (M3D.ready) return true;
  d3dNote(t('note.3dLoading'));
  let ok = false;
  try { ok = await M3D.init(); }
  catch (e) { d3dNote(t('note.3dFail', (e && e.message) || e), true); return false; }
  if (!ok) { d3dNote(t('note.3dFail', M3D.error || '?'), true); return false; }
  return true;
}

/** 3D 就位那句话（换模型/换包都走这里，别在两处各写一遍；原版 d3dNoteOk） */
function d3dNoteOk() {
  if (!mmdReady()) return;
  const M3D = window.ARCAM_MMD;
  const st = M3D.stats;
  d3dNote(t('note.3dOk', M3D.srcName, st.tris, st.bones, st.poses) +
    (st.texMiss ? ' ' + t('note.3dTexMiss', st.texOk || 0, (st.texOk || 0) + st.texMiss)
                : (st.kind === 'pmx' && st.texOk ? ' ' + t('note.3dTexPool', st.texOk) : '')));
}

/** 给 3D 当环境贴图（IBL）的照片缩略图（64×36）。原版用相机当前帧
 *  （index.html:1679-1685）；Pages 没有实时帧 —— **照片就是这片环境**，拿它同源。
 *  缓存着，照片换了才重画（PMREM 不便宜，别每帧跑）。 */
let envFrameCv = null, envFrameSrc = null;
function mmdEnvFrame() {
  const src = state.photoCanvasFull;
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

/** 3D 模式要有个**占位立绘**：贴边吸附、构图提示、scene.json 全在用立绘宽高
 *  （原版 ensurePlaceholderChar，index.html:1615-1620）。真正画的是 MMD 层，
 *  这张全透明占位图只是让那些几何量有个合理的宽高比（≈0.58，接近全身立绘）。 */
let char3dPh = null;
function char3dPlaceholder() {
  if (!char3dPh) {
    char3dPh = document.createElement('canvas');
    char3dPh.width = 300; char3dPh.height = 520;
  }
  return char3dPh;
}
function ensurePlaceholderChar() {
  if (state.charCanvas) return;
  state.charCanvas = char3dPlaceholder();
  state.charOrig = char3dPlaceholder();
}

/** 3D 必须站地上：自由模式那套（贴边吸附 / 高度占比）对真透视模型没意义 */
function forceGroundForMmd() {
  if (state.params.mode !== 'free') return;
  state.params.mode = 'ground';
  $('p-mode').value = 'ground';
  syncModeVisibility();
}

/** scene.json 的 mmd 段（原版 index.html:3776-3787）。3D 开着才记，否则恒 null
 *  （arcam-render.js 的 buildScene 本来就填 null）。**新 key**，老字段一个没动。 */
function attachMmdKey(scene, W, H) {
  if (!scene) return scene;
  const M3D = window.ARCAM_MMD;
  if (!mmdReady()) { scene.mmd = null; return scene; }
  const p = state.params;
  scene.mmd = {
    model: M3D.srcName,
    pose: (M3D.poseNames()[state.pose3D]) || null,
    modelHeightM: +(M3D.modelHeight()).toFixed(4),
    heightM: +(p.hp * p.char_scale).toFixed(4),
    groundPoint: { lateralM: +p.x.toFixed(3), distanceM: +p.d.toFixed(3) },
    camera: {
      heightM: +p.h.toFixed(4), pitchDeg: +p.pitch.toFixed(3), rollDeg: +p.roll.toFixed(3),
      focalPx: +core.focalPx(W, H, p.feq).toFixed(2), canvas: [W, H],
    },
    stats: { tris: M3D.stats.tris, bones: M3D.stats.bones, morphs: M3D.stats.morphs },
  };
  return scene;
}

//: 模型列表 + 姿势行（一个包里多个 pmx 时列出来；姿势来自 mmd/poses.json）
function refresh3dRows() {
  const M3D = window.ARCAM_MMD;
  const on = mmdReady();
  $('d3dLight').hidden = !on;
  if (!on) { $('model3dRow').hidden = true; $('pose3dRow').hidden = true; return; }
  const names = M3D.modelNames();
  const mrow = $('model3dRow');
  mrow.replaceChildren();
  names.forEach((name, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = name.split(/[\\/]/).pop().replace(/\.pmx$/i, '');   // 目录/后缀是噪音
    b.className = (i === M3D.modelIndex()) ? 'on' : '';
    b.addEventListener('click', async () => {
      if (i === M3D.modelIndex()) return;
      b.dataset.busy = '1';
      // 换模型 = 用同一份输入重建，贴图自然还是这个包里的（不用再选一次）
      const ok = await M3D.switchModel(i);
      delete b.dataset.busy;
      if (!ok) { d3dNote(t('note.3dFail', M3D.error || '?'), true); return; }
      refresh3dRows(); d3dNoteOk(); renderNow();
    });
    mrow.appendChild(b);
  });
  // 「去掉这个」：包里常混着角色 + 武器 + 部件，把不要的删掉，列表就只剩要用的
  const del = document.createElement('button');
  del.type = 'button';
  del.style.marginLeft = '6px';
  del.textContent = t('btn.modelDrop');
  del.addEventListener('click', async () => {
    del.dataset.busy = '1';
    const ok = await M3D.dropModel(M3D.modelIndex());
    delete del.dataset.busy;
    if (!ok) { d3dNote(t('note.3dAllDropped')); return; }
    refresh3dRows(); d3dNoteOk(); renderNow();
  });
  mrow.appendChild(del);
  $('model3dLabel').hidden = names.length < 2;
  mrow.hidden = names.length < 2;
  const prow = $('pose3dRow');
  prow.textContent = '';
  const mk = (label, idx) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.className = (state.pose3D === idx) ? 'on' : '';
    b.addEventListener('click', () => { state.pose3D = idx; refresh3dRows(); renderNow(); });
    return b;
  };
  prow.appendChild(mk('静止', -1));
  M3D.poseNames().forEach((name, i) => prow.appendChild(mk(name, i)));
  const hasPose = M3D.poseNames().length > 0;
  $('pose3dLabel').hidden = !hasPose;
  prow.hidden = !hasPose;
  // 模型进来了才谈得上调主光方向 / 景深 —— 跟着显示，并回填当前值
  const prm = M3D.params;
  if (prm) {
    $('az3d').value = prm.keyAz;  $('oaz3d').textContent = prm.keyAz + '°';
    $('el3d').value = prm.keyEl;  $('oel3d').textContent = prm.keyEl + '°';
    $('dof3d').value = prm.dof;   $('odof3d').textContent = (+prm.dof).toFixed(2);
    // 对焦距离默认跟人物同距（=不虚）。用户把距离滑块拖走，他就该虚。
    $('focus3d').value = state.params.d;
    $('ofocus3d').textContent = state.params.d.toFixed(1) + 'm';
  }
}

//: 导入模型（zip/glb/文件夹/pmx）。load 成功返 true；统计信息在 M3D.stats。
async function do3DImport(src, pmxIdx) {
  if (!src) return;
  const M3D = window.ARCAM_MMD;
  if (!M3D) { d3dNote(t('note.3dNoEngine'), true); return; }
  d3dNote(t('note.3dLoading'));
  try {
    // onProgress 第一个参量是 0~1 的分数（mmd.js readBytes/buildFromPackage 的约定）
    const ok = await M3D.load(src, (frac) =>
      d3dNote(t('note.3dLoading') + ' ' + Math.round(frac * 100) + '%'), pmxIdx);
    if (!ok) { d3dNote(t('note.3dFail', M3D.error || '未知错误'), true); return; }
    state.use3D = true;
    $('t3d').classList.add('on');
    forceGroundForMmd();          // 3D 必须站地上：自由模式那套对真透视模型没意义
    ensurePlaceholderChar();
    refresh3dRows();
    d3dNoteOk();
    renderNow();
  } catch (e) {
    d3dNote(t('note.3dFail', (e && e.message) || e), true);
  }
}

function bind3D() {
  $('t3d').addEventListener('click', async () => {
    state.use3D = !state.use3D;
    $('t3d').classList.toggle('on', state.use3D);
    if (state.use3D) {
      forceGroundForMmd();
      ensurePlaceholderChar();
      const ok = await d3dInitEngine();
      if (ok && window.ARCAM_MMD && !window.ARCAM_MMD.srcName) d3dNote(t('note.3dNeedModel'));
    } else {
      d3dNote('');
    }
    refresh3dRows();
    renderNow();
  });
  $('f3d').addEventListener('click', () => $('f3dFile').click());
  $('f3dFile').addEventListener('change', (e) => {
    if (e.target.files && e.target.files[0]) do3DImport(e.target.files[0]);
    e.target.value = '';               // 同一个文件再选一次也要能触发
  });
  $('f3dDirBtn').addEventListener('click', () => $('f3dDir').click());
  $('f3dDir').addEventListener('change', (e) => {
    const files = e.target.files;
    if (!files || !files.length) return;
    // 文件夹导入：FileList 原样喂给 mmd.js（它自己挑 pmx 与贴图；安卓退化成多选）
    do3DImport(files);
    e.target.value = '';
  });
  // 3D 专属控件（原版 az/el/focus/dof 四滑块）。mmd.js 的光照参数是
  // params getter 返回的 L 对象引用——直接改字段即生效（无 setLight）。
  const bind3 = (id, key, fmt) => {
    const el = $(id), out = $('o' + id);
    el.addEventListener('input', () => {
      const v = parseFloat(el.value);
      if (window.ARCAM_MMD && window.ARCAM_MMD.params) window.ARCAM_MMD.params[key] = v;
      if (out) out.textContent = fmt(v);
      renderNow();
    });
  };
  bind3('az3d', 'keyAz', (v) => v.toFixed(0) + '°');
  bind3('el3d', 'keyEl', (v) => v.toFixed(0) + '°');
  bind3('focus3d', 'focus', (v) => v.toFixed(1) + 'm');
  bind3('dof3d', 'dof', (v) => v.toFixed(2));
}


/* ================= 12.6 效果开关 + 自动摆正（v1.3.0） ================= */

function bindFx() {
  $('fx-shadow').addEventListener('change', (e) => {
    state.params.shadow = e.target.checked;
    renderNow();
  });
  $('fx-hud-grid').addEventListener('change', (e) => {
    HUD_ON.grid = e.target.checked;
    drawHudOverlay();
  });
  $('fx-hud-horizon').addEventListener('change', (e) => {
    HUD_ON.horizon = e.target.checked;
    drawHudOverlay();
  });
  $('fx-hud-body').addEventListener('change', (e) => {
    HUD_ON.body = e.target.checked;
    drawHudOverlay();
  });
  bindSlider('shadow_az', 'p-shadow-az-rng', 'p-shadow-az-val', (v) => v.toFixed(0) + '°');
  bindSlider('shadow_opacity', 'p-shadow-op-rng', 'p-shadow-op-val', (v) => v.toFixed(2));
  $('btn-autofix').addEventListener('click', doAutoFix);
}

//: 自动摆正：hud.js 的 autoFixX（与 Python placement.py:240 同算法）。
//  ⚠ 不走 window.arcamCore——arcam-core 是 module、由 app.js 动态 import，
//     从不挂 window；hud.js 在 IIFE 里已把同算法的一份挂到 window.arcamHud。
function doAutoFix() {
  const H = window.arcamHud;
  if (!H || !H.autoFixX) return;
  const cv = $('preview-canvas');
  const W = cv.width, H2 = cv.height;
  if (W < 8 || H2 < 8) return;
  const p = state.params;
  const cam = { W: W, H: H2, h: p.h, feq: p.feq, pitch: p.pitch, roll: p.roll };
  const subj = { hp: p.hp, d: p.d, x: p.x };
  const aspect = state.charCanvas ? state.charCanvas.width / state.charCanvas.height : 0.6;
  const r = H.autoFixX(cam, subj, p.mode, p.size_pct,
                       { shot: p.shot, charScale: p.char_scale, charAspect: aspect });
  if (!r) { setStatus('摆不正：人物在相机后方或参数退化', null, true); return; }
  if (r.freeU !== undefined) {
    setStatus('已吸附到 ' + Math.round(r.snapped * 100) + '% 三分线（自由模式请用拖动微调）');
    return;
  }
  p.x = Math.round(r.x * 100) / 100;
  $('p-x-num').value = p.x;
  $('p-x-rng').value = p.x;
  setStatus('已吸附到 ' + Math.round(r.snapped * 100) + '% 三分线（x=' + p.x + 'm）');
  renderNow();
}


//: 拖拽摆位（原版核心交互；ground 拖画面把人摆到那块地面）
function bindDrag() {
  const cv = $('preview-canvas');
  if (!cv) return;
  let dragging = false;
  const toCanvasPixel = (ev) => {
    const r = cv.getBoundingClientRect();
    return {
      u: (ev.clientX - r.left) * (cv.width / r.width),
      v: (ev.clientY - r.top) * (cv.height / r.height)
    };
  };
  cv.addEventListener('pointerdown', (ev) => {
    if (!state.photoCanvasFull || !state.charCanvas) return;
    dragging = true;
    try { cv.setPointerCapture(ev.pointerId); } catch (e) {}
    ev.preventDefault();
  });
  cv.addEventListener('pointermove', (ev) => {
    if (!dragging) return;
    const px = toCanvasPixel(ev);
    const p = state.params;
    if (p.mode === 'free') {
      setStatus('自由模式拖动：' + Math.round(px.u / cv.width * 100) + '% , '
                + Math.round(px.v / cv.height * 100) + '%（精细位置请用左侧参数）');
      return;
    }
    const H = window.arcamHud;
    if (!H || !H.placeAt) return;
    const cam = { W: cv.width, H: cv.height, h: p.h, feq: p.feq, pitch: p.pitch, roll: p.roll };
    const hit = H.placeAt(px.u, px.v, cam);
    if (!hit) return;
    p.x = Math.round(hit.x * 100) / 100;
    p.d = Math.round(hit.d * 100) / 100;
    $('p-x-num').value = p.x;
    $('p-x-rng').value = p.x;
    $('p-d-num').value = p.d;
    $('p-d-rng').value = p.d;
    scheduleRender();
  });
  const end = () => { dragging = false; };
  cv.addEventListener('pointerup', end);
  cv.addEventListener('pointercancel', end);
}


/* ================= 12.7 全屏取景 + 参数记忆（v1.4.0） ================= */

function bindFullscreen() {
  $('btn-fullscreen').addEventListener('click', () => {
    const stage = $('stage');
    if (!stage) return;
    if (document.fullscreenElement) { document.exitFullscreen(); return; }
    const p = stage.requestFullscreen && stage.requestFullscreen();
    if (p && p.catch) p.catch(() => setStatus('浏览器不让全屏（需用户手势；iOS Safari 不支持元素全屏）', null, true));
  });
  document.addEventListener('fullscreenchange', () => {
    const on = !!document.fullscreenElement;
    $('btn-fullscreen').textContent = on ? t('btn.exitFullscreen') : t('btn.fullscreen');
    if (on) setStatus('全屏取景中——拖画面摆位，Esc 退出');
  });
}

const PARAM_KEY = 'arcam.params';

function saveParams() {
  try {
    localStorage.setItem(PARAM_KEY, JSON.stringify({ params: state.params }));
  } catch (e) {}
}

function restoreParams() {
  try {
    const raw = localStorage.getItem(PARAM_KEY);
    if (!raw) return false;
    const data = JSON.parse(raw);
    if (!data || !data.params) return false;
    Object.keys(S_DEFAULTS).forEach((k) => {
      if (data.params[k] !== undefined) state.params[k] = data.params[k];
    });
    return true;
  } catch (e) { return false; }
}

function syncControlsFromParams() {
  NUMERIC.forEach((key) => {
    const v = state.params[key];
    const num = $('p-' + key + '-num'), rng = $('p-' + key + '-rng');
    if (num) num.value = v;
    if (rng) rng.value = v;
  });
  const sel = { shot: 'p-shot', frame: 'p-frame', mode: 'p-mode', rot_basis: 'p-rot', edge_bar: 'p-edge-bar' };
  Object.keys(sel).forEach((k) => { const el = $(sel[k]); if (el) el.value = state.params[k]; });
  $('p-char-scale-rng').value = state.params.char_scale;
  $('p-char-scale-val').textContent = state.params.char_scale.toFixed(2);
  $('p-size-pct-rng').value = state.params.size_pct;
  $('p-size-pct-val').textContent = state.params.size_pct.toFixed(0) + '%';
  $('p-tone').checked = !!state.params.tone;
  $('p-shadow-az-rng').value = state.params.shadow_az;
  $('p-shadow-az-val').textContent = state.params.shadow_az.toFixed(0) + '°';
  $('p-shadow-op-rng').value = state.params.shadow_opacity;
  $('p-shadow-op-val').textContent = state.params.shadow_opacity.toFixed(2);
  $('fx-shadow').checked = state.params.shadow !== false;
  syncModeVisibility();
}


/* ================= 11.5 批量出图（v1.6.0，客户端） ================= */

//: 多选照片 → 逐张 composeShot → 一个 zip。与 CLI arcam batch 同语义；单张
//  失败不拖垮整批（跳过，明细进 zip 里的 _errors.txt——HTTP 头是 latin-1，
//  中文原因进头会 500，那是服务端 /api/batch 踩过的坑，客户端同理不犯）。
async function doBatch(files) {
  try {
    if (!files || !files.length) return;
    if (!state.charCanvas) throw new Error('批量出图需要立绘');
    if (files.length > 12) throw new Error('一次最多 12 张（分批处理）');
    setStatus('批量出图中（' + files.length + ' 张）…');
    const p = state.params;
    const entries = [];
    const errors = [];
    for (let i = 0; i < files.length; i++) {
      try {
        const loaded = await loadImageEl(files[i]);
        const canvas = drawToCanvas(loaded.img, loaded.w, loaded.h);
        if (loaded.revoke) URL.revokeObjectURL(loaded.revoke);
        const r = await renderMod.composeShot(
          canvas, state.charCanvas, subjOf(p),
          camOf(p, loaded.w, loaded.h), composeParams(p));
        const blob = await canvasToBlob(r.shot, 'image/png');
        entries.push({ name: `shot_${String(i + 1).padStart(3, '0')}.png`,
                       data: new Uint8Array(await blob.arrayBuffer()) });
      } catch (e) {
        errors.push(`  第 ${i + 1} 张：${(e && e.message) || e}`);
      }
    }
    if (!entries.length) throw new Error('一张都没出图：' + errors.slice(0, 3).join('；'));
    if (errors.length) {
      entries.push({ name: '_errors.txt',
                     data: new TextEncoder().encode(
                       'arcam batch —— 跳过的照片及原因：\n\n' + errors.join('\n')) });
    }
    const zip = buildBundleZip(entries);
    saveBlob(zip, 'arcam-batch.zip');
    setStatus(`批量完成：${entries.length - (errors.length ? 1 : 0)} 张出图` +
              (errors.length ? `，${errors.length} 张跳过（见 zip 内 _errors.txt）` : ''));
  } catch (e) {
    setStatus('批量失败：' + (e && e.message ? e.message : e), null, true);
  }
}

/* ================= 11.5 批量出图（v1.6.0；纯客户端逐张合成再打包） ================= */

function bindBatch() {
  $('btn-batch').addEventListener('click', () => $('file-photos').click());
  $('file-photos').addEventListener('change', async (e) => {
    const files = e.target.files;
    if (!files || !files.length) return;
    if (!state.charCanvas) { setStatus('批量出图需要先上传立绘', null, true); return; }
    if (files.length > 12) { setStatus(`一次最多 12 张（收到 ${files.length}）`, null, true); return; }
    setStatus(`批量出图中（${files.length} 张）…`);
    const parts = [];
    const failed = [];
    for (let i = 0; i < files.length; i++) {
      try {
        await setPhoto(files[i]);                     // 逐张换成当前照片
        const r = await composeFull();                // 全尺寸重合成（同 render 路径）
        const blob = await canvasToBlob(r.shot, 'image/png');
        parts.push({ name: `shot_${String(i + 1).padStart(3, '0')}.png`,
                     data: new Uint8Array(await blob.arrayBuffer()) });
      } catch (err) {
        failed.push(`第 ${i + 1} 张：${(err && err.message) || err}`);
      }
    }
    if (!parts.length) {
      setStatus('一张都没出图：' + failed.slice(0, 3).join('；'), null, true);
      return;
    }
    if (failed.length) {
      parts.push({ name: '_errors.txt', data: new TextEncoder().encode(failed.join('\n') + '\n') });
    }
    const zip = buildBundleZip(parts);
    saveBlob(zip, 'arcam-batch.zip');
    setStatus(`批量完成：${parts.length - (failed.length ? 1 : 0)} 张` +
              (failed.length ? `，${failed.length} 张跳过（见 zip 内 _errors.txt）` : ''));
  });
}

/* ================= 12. 实时相机 + HUD（FEATURE-SPEC §4/§2） ================= */

function bindLive() {
  $('btn-live').addEventListener('click', () => {
    if (window.arcamLive.active) window.arcamLive.stop();
    else window.arcamLive.start();
  });
  $('btn-shot').addEventListener('click', () => {
    const cv = window.arcamLive.shoot();
    if (!cv) { showTip('note.shotFail'); return; }
    cv.toBlob((blob) => {
      if (!blob) { showTip('note.shotFail'); return; }
      setPhoto(new File([blob], 'camera.png', { type: 'image/png' }));
      showTip('note.chLoaded', 'camera.png', cv.width, cv.height);
    }, 'image/png');
  });
  window.arcamLive.onOrient = (pitch, roll) => {
    const p = state.params;
    p.pitch = Math.round(pitch * 2) / 2;
    p.roll = Math.round(roll * 2) / 2;
    $('p-pitch-num').value = p.pitch;
    $('p-pitch-rng').value = p.pitch;
    $('p-roll-num').value = p.roll;
    $('p-roll-rng').value = p.roll;
    scheduleRender();
    drawHudOverlay();
  };
  // 相机开启后每帧重画 HUD（视频尺寸在 fit 里才知道）
  const origFit = window.arcamLive.fit;
  window.arcamLive.fit = function () {
    origFit();
    drawHudOverlay();
  };
}

//: HUD 开关（原版 S.grid/body/horizon；默认只开网格）
const HUD_ON = { grid: true, body: false, horizon: false };

function drawHudOverlay() {
  const hud = $('hud-canvas'), cv = $('preview-canvas');
  if (!hud || !cv || !window.arcamHud) return;
  const W = cv.width, H = cv.height;
  if (W < 8 || H < 8) return;
  hud.width = W; hud.height = H;
  const ctx = hud.getContext('2d');
  ctx.clearRect(0, 0, W, H);
  const p = state.params;
  const cam = { W: W, H: H, h: p.h, feq: p.feq, pitch: p.pitch, roll: p.roll };
  const subj = { hp: p.hp, d: p.d, x: p.x };
  const R = window.arcamHud.frameRect(p.frame, W, H);
  if (R) window.arcamHud.drawFrame(ctx, R, { x: 0, y: 0, w: W, h: H }, W, H);
  if (HUD_ON.grid) window.arcamHud.drawGrid(ctx, cam);
  if (HUD_ON.horizon) window.arcamHud.drawHorizon(ctx, cam, t('horizon'));
  if (HUD_ON.body) window.arcamHud.drawFigure(ctx, cam, subj);
  window.arcamHud.drawHud(ctx, cam, subj, !!state.charCanvas, null, p.shot, p.mode);
}

init();
