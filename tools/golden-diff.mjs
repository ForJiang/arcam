#!/usr/bin/env node
/**
 * golden 差分夹具：用 Node 跑 docs/tests/golden.json 对 docs/arcam-core.js
 * （Pages 纯 JS 移植）与 docs/arcam-lighting.js（光照烘焙移植）的逐向量数值比对。
 * tools/ 常驻，替代早期临时页面。
 *
 * 背景：浏览器内嵌 webview 的事件循环会被挂起（IAB 坑），await 类页面脚本
 * 跑不动；而 arcam-core.js / arcam-lighting.js 的数值函数是纯 ES module、
 * 无 DOM 依赖，Node 可直接 import —— 同一份 fixture、同一条判据，不必开浏览器。
 *
 * 用法（仓库根目录）：
 *   node tools/golden-diff.mjs            # 需要 node ≥ 14（ESM + ??）
 * 通过判据：
 *   · focal/project/place/placement（几何）与 measureRegion/charStats/
 *     litScaleBucket/grainTile（光照）：|js - fixture| ≤ 1e-9·max(1,|fixture|)；
 *   · bakeLit：输出字节**全等（0 容差）**，另查「不修改入参」契约；
 * 任一 MISMATCH 打印向量明细，退出码 1。实测 worst ≈ 5.551e-17（双精度尾差）。
 *
 * fixture 本身由 Python 侧重算门禁 tests/test_golden_recompute.py 钉死：
 * 两边任一侧行为漂移都会在各自门禁里红，不会互相捍卫一个错误的基准。
 *
 * 调用约定（与两侧导出签名逐字对齐，改 JS 签名必须同步这里）：
 *   focalPx(W, H, feq) —— 三参，不吃 cam 六元组
 *   project(x, y, d, cam) / place(u, v, cam) —— cam 是 {W,H,h,feq,pitch,roll}
 *   currentPlacement(charW, charH, subj, cam, opts) —— opts.rect 要
 *     {x,y,w,h} 对象（数组会被 rect.x 读成 undefined）
 * 光照组（arcam-lighting.js，契约见 golden.json 的 fixtures/各 group）：
 *   measureRegion(rgba, w, h, region, levels) —— rgba 是 RGBA 字节
 *     ArrayLike(0..255)、长 w*h*4；region: null | {x,y,w,h}（浮点）
 *     → { levels: number[]（长 levels）, mean: [r,g,b]（0..1） }
 *   bakeLit(rgba, w, h, sample, canvasW, grain, grainScale) —— sample: null |
 *     measureRegion 的产物；canvasW 是立绘目标宽（浮点）→ 新
 *     Uint8ClampedArray（**不修改入参**）
 *   charStats(rgba, w, h) → { lum, sat, r, b }
 *   litScaleBucket(scale) → number；grainTile() → Float64Array(65536)
 */
import { readFileSync, writeFileSync, mkdtempSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const g = JSON.parse(readFileSync(join(root, 'docs/tests/golden.json'), 'utf8'));
const TOL = 1e-9;

// docs/ 无 package.json type:module，Node 会把 .js 当 CommonJS 拒掉 named
// export —— 复制成临时 .mjs 再 import（纯数值函数、无 DOM 依赖，语义不变）。
const coreSrc = join(root, 'docs/arcam-core.js');
const coreTmp = join(mkdtempSync(join(tmpdir(), 'arcam-golden-')), 'core.mjs');
writeFileSync(coreTmp, readFileSync(coreSrc, 'utf8'));
const { focalPx, project, place, currentPlacement } = await import(coreTmp);

// 光照烘焙组同一技巧。W2 未交付 arcam-lighting.js 时明确报缺失并退出 1
// （不空跑假过：Python 侧重算门禁已独立覆盖该组，Node 侧待合并后复跑）。
const litSrc = join(root, 'docs/arcam-lighting.js');
if (!existsSync(litSrc)) {
  console.log(`MISSING ${litSrc}：JS 光照移植未就位，Node 侧实测跳过（退出 1）。` +
              `Python 侧重算门禁：.venv/bin/python -m pytest tests/test_golden_recompute.py -q`);
  process.exit(1);
}
// arcam-lighting.js 还 import 了 './arcam-core.js'（lightRegionRect 复用
// currentPlacement），所以连同 core 一起拷进**同一个**临时目录 —— 只拷
// lighting 会让相对 import 落空，真实根目录跑直接 ERR_MODULE_NOT_FOUND。
const litTmp = join(mkdtempSync(join(tmpdir(), 'arcam-golden-lit-')), 'lighting.mjs');
writeFileSync(join(dirname(litTmp), 'arcam-core.mjs'), readFileSync(coreSrc, 'utf8'));
// 临时目录里把 lighting 的 './arcam-core.js' 指向刚拷过去的 core（import 是
// 静态解析，改不了入参文件，就在副本里改这一行说明符）。
writeFileSync(litTmp, readFileSync(litSrc, 'utf8')
  .replace("from './arcam-core.js'", "from './arcam-core.mjs'"));
const { measureRegion, bakeLit, charStats, litScaleBucket, grainTile } = await import(litTmp);

const camOf = s => ({ W: +s[0], H: +s[1], h: +s[2], feq: +s[3], pitch: +s[4], roll: +s[5] });
const rectOf = r => (r ? { x: r[0], y: r[1], w: r[2], h: r[3] } : null);
const out5 = p => (p === null ? null : [p.u, p.v, p.w, p.h, p.rot]);

let total = 0, bad = 0, worst = 0;
const close = (a, b) => {
  if (a === null || a === undefined || b === null || b === undefined) return (a ?? null) === (b ?? null);
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((x, i) => close(x, b[i]));
  }
  if (typeof a === 'number' && typeof b === 'number') {
    const d = Math.abs(a - b);
    if (d > worst) worst = d;
    return d <= TOL * Math.max(1, Math.abs(b));
  }
  return a === b;
};
const fail = (group, v, got) => {
  bad++;
  console.log(`MISMATCH ${group} ${JSON.stringify(v)} got ${JSON.stringify(got)}`);
};

for (const v of g.focal) {
  total++;
  if (!close(focalPx(v.cam[0], v.cam[1], v.cam[3]), v.F)) fail('focal', v, focalPx(v.cam[0], v.cam[1], v.cam[3]));
}
for (const v of g.project) {
  total++;
  const got = project(v.p[0], v.p[1], v.p[2], camOf(v.cam));
  if (!close(got === null ? null : [got[0], got[1]], v.uv)) fail('project', v, got);
}
for (const v of g.place) {
  total++;
  const got = place(v.uv[0], v.uv[1], camOf(v.cam));
  if (!close(got === null ? null : [got[0], got[1]], v.xd)) fail('place', v, got);
}
for (const v of g.placement) {
  total++;
  const c = camOf(v.cam);
  const subj = { hp: v.subj[0], d: v.subj[1], x: v.subj[2] };
  const opts = { mode: v.kind };
  if (v.kind === 'ground') {
    opts.shot = v.shot;
    opts.charScale = v.char_scale;
    opts.rotBasis = v.rot_basis ?? 'world';
  } else {
    opts.sizePct = v.size_pct;
    opts.rotBasis = v.rot_basis ?? 'world';
    opts.rotDeg = v.rot_deg;
    opts.edgeBar = v.edge_bar ?? 0;
    opts.rect = rectOf(v.rect);
  }
  const p = currentPlacement(v.char[0], v.char[1], subj, c, opts);
  if (!close(out5(p), v.out)) fail('placement', v, out5(p));
  if ('out_nosnap' in v) {
    const p2 = currentPlacement(v.char[0], v.char[1], subj, c, { ...opts, rect: null });
    if (!close(out5(p2), v.out_nosnap)) fail('placement-nosnap', v, out5(p2));
  }
}

// ── 光照烘焙组（arcam-lighting.js）────────────────────────────────────
const fixture = name => {
  const f = g.fixtures[name];
  return { bytes: Uint8Array.from(Buffer.from(f.rgba, 'base64')), w: +f.w, h: +f.h };
};
const regionOf = r => (r ? { x: r[0], y: r[1], w: r[2], h: r[3] } : null);
const sampleOf = s => (s ? { levels: s.levels, mean: s.mean } : null);

for (const v of g.measureRegion) {
  total++;
  const { bytes, w, h } = fixture(v.img);
  const got = measureRegion(bytes, w, h, regionOf(v.region), v.levels);
  if (!close(got.mean, v.out.mean) || !close(got.levels, v.out.levels)) {
    fail('measureRegion', v, got);
  }
}
for (const v of g.bakeLit) {
  total++;
  const { bytes, w, h } = fixture(v.char);
  const before = Uint8Array.from(bytes);
  const got = bakeLit(bytes, w, h, sampleOf(v.sample), v.canvasW, v.grain, v.grainScale);
  if (!before.every((x, i) => x === bytes[i])) {
    bad++;
    console.log(`MISMATCH bakeLit-input-mutated ${v.char}（契约：不修改入参）`);
  }
  const want = Buffer.from(v.out, 'base64');   // 字节全等，0 容差
  let same = got.length === want.length;
  for (let i = 0; same && i < want.length; i++) same = got[i] === want[i];
  if (!same) {
    bad++;
    const diffs = [];
    for (let i = 0; i < Math.max(got.length, want.length) && diffs.length < 6; i++) {
      if (got[i] !== want[i]) diffs.push(`${i}:${want[i]}->${got[i]}`);
    }
    console.log(`MISMATCH bakeLit ${v.char} canvasW=${v.canvasW} grain=${v.grain} ` +
                `grainScale=${v.grainScale} 字节不等: [${diffs.join(' ')}]`);
  }
}
for (const v of g.charStats) {
  total++;
  const { bytes, w, h } = fixture(v.img);
  const got = charStats(bytes, w, h);
  if (!close([got.lum, got.sat, got.r, got.b],
             [v.out.lum, v.out.sat, v.out.r, v.out.b])) fail('charStats', v, got);
}
for (const v of g.litScaleBucket) {
  total++;
  const got = litScaleBucket(v.s);
  if (!close(got, v.out)) fail('litScaleBucket', v, got);
}
const grain = grainTile();
if (grain.length !== 65536) {
  bad++;
  console.log(`MISMATCH grainTile 长度 ${grain.length} != 65536`);
}
for (const v of g.grainTile) {
  total++;
  const got = v.idx.map(i => grain[i]);
  if (!close(got, v.out)) fail('grainTile', v, got);
}

console.log(`DIFF total=${total} bad=${bad} worst=${worst}`);
process.exit(bad ? 1 : 0);
