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
  mode: 'ground', size_pct: 60, edge_bar: 0, tone: false
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
  charImg: null, charName: '', charCanvas: null, charHasAlpha: false,
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
  return {
    frame: p.frame,
    mode: p.mode,
    shot: p.shot,
    sizePct: p.size_pct,
    charScale: p.char_scale,
    rotBasis: p.rot_basis,
    edgeBar: p.edge_bar,
    tone: !!p.tone
  };
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
    showScene(r.scene);
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

  if (!pipelineReady) {
    const msg = '运算模块未就绪：' + _missing.join('、') +
      ' 未发布或加载失败，本页功能暂不可用';
    setStatus(msg, null, true);
    $('preview-empty').textContent = msg;
    $('preview-empty').hidden = false;
  }
}

init();
