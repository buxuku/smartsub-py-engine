// 用法: node scripts/pack_sherpa_libs.mjs <platformKey> <sherpaVersion> <outDir>
// platformKey ∈ darwin-arm64 | darwin-x64 | win-x64 | win-ia32 | linux-x64 | linux-arm64
//
// 从 npm 取 sherpa-onnx-node 的平台子包（仅 tarball，无需安装/编译器），
// 收集运行所需的原生文件（sherpa-onnx.node + *.dylib/.so/.dll），打成
// smartsub-sherpa-onnx-<platformKey>-<version>.tar.gz + .sha256，供主仓按需下载。
import { execSync } from 'node:child_process';
import {
  mkdirSync,
  readdirSync,
  copyFileSync,
  writeFileSync,
  createReadStream,
} from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';

const [platformKey, version, outDir] = process.argv.slice(2);
if (!platformKey || !version || !outDir) {
  throw new Error('usage: pack_sherpa_libs.mjs <platformKey> <version> <outDir>');
}

const pkg = `sherpa-onnx-${platformKey}@${version}`;
const work = path.join(outDir, '.work', platformKey);
mkdirSync(work, { recursive: true });

// 1) 用 npm pack 取平台包 tarball（跨平台可用，npm pack 只下载不执行安装脚本）
execSync(`npm pack ${pkg}`, { cwd: work, stdio: 'inherit' });
const tgz = readdirSync(work).find((f) => f.endsWith('.tgz'));
if (!tgz) throw new Error(`npm pack produced no tarball for ${pkg}`);
execSync(`tar -xzf ${tgz}`, { cwd: work, stdio: 'inherit' });
const pkgDir = path.join(work, 'package');

// 2) 收集运行所需文件：sherpa-onnx.node + 所有原生库
const wanted = readdirSync(pkgDir).filter(
  (f) => f === 'sherpa-onnx.node' || /\.(dylib|so(\.\d+)*|dll)$/.test(f),
);
if (!wanted.includes('sherpa-onnx.node')) {
  throw new Error(`sherpa-onnx.node not found in ${pkgDir}`);
}

const stage = path.join(outDir, `smartsub-sherpa-onnx-${platformKey}`);
mkdirSync(stage, { recursive: true });
for (const f of wanted) copyFileSync(path.join(pkgDir, f), path.join(stage, f));
writeFileSync(
  path.join(stage, 'manifest.json'),
  JSON.stringify(
    {
      platform: platformKey,
      sherpaVersion: version,
      files: wanted,
      builtAt: new Date().toISOString(),
    },
    null,
    2,
  ),
);

// 3) 打 tar.gz + sha256
const asset = path.join(
  outDir,
  `smartsub-sherpa-onnx-${platformKey}-${version}.tar.gz`,
);
execSync(`tar -czf ${asset} -C ${stage} .`, { stdio: 'inherit' });
const hash = createHash('sha256');
await new Promise((res, rej) =>
  createReadStream(asset)
    .on('data', (d) => hash.update(d))
    .on('end', res)
    .on('error', rej),
);
writeFileSync(`${asset}.sha256`, `${hash.digest('hex')}  ${path.basename(asset)}\n`);
console.log('packed', asset, '\nfiles:', wanted.join(', '));
