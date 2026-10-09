#!/usr/bin/env node
/**
 * golden 差分夹具：用 Node 跑 docs/tests/golden.json 对 docs/arcam-core.js
 * （Pages 纯 JS 移植）的逐向量数值比对。tools/ 常驻，替代早期临时页面。
 *
 * 背景：浏览器内嵌 webview 的事件循环会被挂起（IAB 坑），await 类页面脚本
 * 跑不动；而 arcam-core.js 的数值函数是纯 ES module、无 DOM 依赖，Node 可直接
 * import —— 同一份 fixture、同一条判据，不必开浏览器。
 *
 * 用法（仓库根目录）：
 *   node tools/golden-diff.mjs            # 需要 node ≥ 14（ESM + ??）
 * 通过判据：total 向量全部 |js - fixture| ≤ 1e-9·max(1,|fixture|)，退出码 0；
 * 任一 MISMATCH 打印向量明细，退出码 1。实测 worst ≈ 5.551e-17（双精度尾差）。
 *
 * fixture 本身由 Python 侧重算门禁 tests/test_golden_recompute.py 钉死：
 * 两边任一侧行为漂移都会在各自门禁里红，不会互相捍卫一个错误的基准。
 *
 * 调用约定（与 arcam-core.js 的导出签名逐字对齐，改 JS 签名必须同步这里）：
 *   focalPx(W, H, feq) —— 三参，不吃 cam 六元组
 *   project(x, y, d, cam) / place(u, v, cam) —— cam 是 {W,H,h,feq,pitch,roll}
 *   currentPlacement(charW, charH, subj, cam, opts) —— opts.rect 要
 *     {x,y,w,h} 对象（数组会被 rect.x 读成 undefined）
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
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

console.log(`DIFF total=${total} bad=${bad} worst=${worst}`);
process.exit(bad ? 1 : 0);
