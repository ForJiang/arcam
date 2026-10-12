#!/usr/bin/env node
/**
 * zip writer 对拍夹具：把两侧 app.js 里**手写的 store 式最小 zip**
 * （webui/app.js 与 docs/app.js 各一份同源移植）抠出来，在 Node 里真跑，
 * 与 Python zipfile 的产物逐字节比对。tools/ 常驻，替代「浏览器点一遍导出」。
 *
 * 背景（为什么值得对拍）：素材包（shot.jpg + scene.json + character.png）和
 * 批量 zip 都走这段纯 JS zip writer——它写的是**二进制文件头**（本地头 /
 * 中央目录 / EOCD，CRC32 表驱动），一个字段写错用户端就是「压缩包损坏」，
 * 而单测只能查代码在不在，查不出字节错。Python 侧同一语义由 zipfile 生成
 * （webapp._zip_bytes / CLI cmd_bundle，ZIP_STORED + 1980 固定时间戳 +
 * 0o644 权限位），所以拿 Python zipfile 当基准逐字节比。
 *
 * 用法（仓库根目录，需要 node ≥ 18：ESM + Blob + zlib.crc32）：
 *   node tools/zip-writer-diff.mjs                 # 自检：两侧同源 + crc32 + 落盘 JS zip
 *   node tools/zip-writer-diff.mjs /tmp/js.zip     # 指定输出路径（默认 /tmp/arcam-zip-js.zip）
 * 通过判据（本脚本负责前两条，第三条由下面的 Python 命令完成）：
 *   ① 两侧 zip writer 段剥注释后逐字节一致（同源移植没漂移）；
 *   ② crc32 与 Node zlib.crc32 逐值一致（表驱动实现没写错）；
 *   ③ 产出的 zip 与 Python zipfile 参考实现逐字节相同：
 *        .venv/bin/python - <<'PY'
 *        import hashlib, zipfile, io
 *        rng = 12345
 *        def det(n):
 *            global rng
 *            out = bytearray()
 *            for _ in range(n):
 *                rng = (rng * 48271) % 2147483647   # MINSTD：中间积 < 2^53，JS 双精度也精确
 *                out.append((rng >> 16) & 0xff)
 *            return bytes(out)
 *        files = [("shot.jpg", det(4096)),
 *                 ("scene.json", '{\n  "version": 1\n}\n'.encode()),
 *                 ("character.png", det(2048))]
 *        buf = io.BytesIO()
 *        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
 *            for name, data in files:
 *                zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
 *                zi.compress_type = zipfile.ZIP_STORED
 *                zi.external_attr = 0o644 << 16
 *                z.writestr(zi, data)
 *        print("PY ", hashlib.sha256(buf.getvalue()).hexdigest(), len(buf.getvalue()))
 *        PY
 *      两行 sha256/长度相同即逐字节一致（平台口径：create_system=3，即
 *      macOS/Linux 的 Python zipfile；JS 侧硬编码 (3<<8)|20 就是照它写的）。
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { crc32 as zlibCrc32 } from 'node:zlib';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const outPath = process.argv[2] || join(tmpdir(), 'arcam-zip-js.zip');

function extractZipSection(src, endMarker, label) {
  const i = src.indexOf('const ZIP_DOS_TIME');
  if (i < 0) throw new Error(`${label}: 找不到 zip writer 段（const ZIP_DOS_TIME）`);
  const j = src.indexOf(endMarker, i);
  if (j < 0) throw new Error(`${label}: 找不到段尾标记 ${endMarker}`);
  return src.slice(i, j);
}

// 剥注释（保留字符串）——docs 版比 webui 版多一行 JSDoc 注释，无行为差异
function stripComments(src) {
  let out = '', i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === "'" || c === '"' || c === '`') {
      let j = i + 1;
      while (j < src.length) {
        if (src[j] === '\\') { j += 2; continue; }
        if (src[j] === c) { j++; break; }
        j++;
      }
      out += src.slice(i, j); i = j; continue;
    }
    if (src.startsWith('//', i)) { const j = src.indexOf('\n', i); i = j < 0 ? src.length : j; continue; }
    if (src.startsWith('/*', i)) { const j = src.indexOf('*/', i); i = j < 0 ? src.length : j + 2; continue; }
    out += c; i++;
  }
  return out;
}

const webuiSrc = readFileSync(join(root, 'src/arcam/webui/app.js'), 'utf8');
const docsSrc = readFileSync(join(root, 'docs/app.js'), 'utf8');
const wSec = extractZipSection(webuiSrc, '/* ================= 7.8 MMD', 'webui/app.js');
const dSec = extractZipSection(docsSrc, '/* [ZIP-END] */', 'docs/app.js');

// ① 两侧同源（剥注释 + 折叠空白后一致）
const norm = (s) => stripComments(s).replace(/\s+/g, ' ').trim();
if (norm(wSec) !== norm(dSec)) throw new Error('两侧 zip writer 段不一致（同源移植漂移）');
console.log('PARITY ok: 两侧 zip writer 段剥注释后一致');

// 抠出来的段是 module 级纯代码（无 DOM 依赖），复制成 .mjs 直接 import
const tmp = join(mkdtempSync(join(tmpdir(), 'arcam-zip-')), 'writer.mjs');
writeFileSync(tmp, wSec + '\nexport { buildBundleZip, crc32 };\n');
const { buildBundleZip, crc32 } = await import(tmp);

// ② crc32 与 Node zlib 对拍（空串 / ASCII / 二进制 / 长缓冲）
const cases = [
  new Uint8Array(0),
  new TextEncoder().encode('arcam'),
  new Uint8Array([0, 1, 2, 255, 128, 64, 32, 16, 8, 4, 2, 1]),
  new Uint8Array(65536).map((_, i) => (i * 7 + (i >> 8)) & 0xff),
];
for (const [i, buf] of cases.entries()) {
  const mine = crc32(buf), ref = zlibCrc32(buf);
  if (mine !== ref) throw new Error(`crc32 用例 ${i} 不一致：js=${mine} zlib=${ref}`);
}
console.log(`CRC32 ok: ${cases.length} 个用例与 zlib.crc32 逐值一致`);

// ③ 确定性三件套（与 Python 参考实现同内容同顺序）。
//   ⚠ LCG 必须选**中间积不超 2^53** 的参数：JS 的 Number 是双精度，
//   `s * 1103515245`（≈2^61）会静默丢精度、和 Python 大整数结果不同——
//   对拍数据本身就会假红。MINSTD（乘子 48271、模 2^31-1）中间积 ≈2^46，安全。
let rng = 12345;
const det = (n) => {
  const out = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    rng = (rng * 48271) % 2147483647;
    out[i] = (rng >>> 16) & 0xff;
  }
  return out;
};
const parts = [
  { name: 'shot.jpg', data: det(4096) },
  { name: 'scene.json', data: new TextEncoder().encode('{\n  "version": 1\n}\n') },
  { name: 'character.png', data: det(2048) },
];
const blob = buildBundleZip(parts);
const bytes = new Uint8Array(await blob.arrayBuffer());
writeFileSync(outPath, bytes);
const sha = createHash('sha256').update(bytes).digest('hex');
console.log(`ZIP  ok: ${blob.type} ${bytes.length} B sha256=${sha}`);
console.log(`      → ${outPath}（用脚本头里的 Python 命令生成参考 zip 比对 sha256/长度）`);
