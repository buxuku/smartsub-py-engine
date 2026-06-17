#!/usr/bin/env python3
"""为指定引擎组装可重定位的依赖包（site-packages）。

用法（需 PATH 上有 uv，且当前解释器即目标 3.12）：
  uv run --python 3.12.10 -- python build_engine_package.py <OUT_DIR> <ENGINE_ID>

ENGINE_ID ∈ {faster-whisper}；读取 requirements-<ENGINE_ID>.txt。
产物布局（OUT_DIR）：
  main.py, _version.py, engines/, site-packages/<deps...>

main.py / _version.py / engines 对所有引擎相同（同一份 sidecar 源码 + 不同依赖包）。
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 每个引擎产物必须存在的顶层 site-packages 包（构建期断言，防止 requirements 写错）
# funasr 已迁移到 App 侧 sherpa-onnx-node 原生库（见 sherpa-libs.yml），不再在此构建。
ENGINE_ASSERT_PKG = {
    "faster-whisper": "faster_whisper",
}


def run(*args):
    print("+", " ".join(str(a) for a in args))
    subprocess.check_call(list(args))


def adhoc_resign_macos(site: Path):
    """ad-hoc 重签 site-packages 内的 Mach-O 原生库（仅 macOS，无证书兜底）。

    无开发者证书时的兜底：PyPI 的 arm64 wheel 通常构建期已 ad-hoc 签名，但部分
    wheel 的 .dylib 可能未签或在打包/传输后失效，arm64 上未签名的库会被内核拒绝
    dlopen。用 `codesign -s -` 重新 ad-hoc 签名即可在本机加载。

    不改写 install name：wheel 多由 delocate 预处理过，内部以 @loader_path 相对
    引用，只要保持 site-packages 内部目录结构，整体迁移后仍可解析。
    """
    if sys.platform != "darwin":
        return
    count = 0
    for path in site.rglob("*"):
        if path.is_file() and path.suffix in (".so", ".dylib"):
            subprocess.run(
                ["codesign", "--force", "--sign", "-", str(path)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            count += 1
    print(f"ad-hoc resigned {count} mach-o libs")


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: build_engine_package.py <OUT_DIR> <ENGINE_ID>")
    out = Path(sys.argv[1])
    engine_id = sys.argv[2]
    if engine_id not in ENGINE_ASSERT_PKG:
        sys.exit(
            f"unknown engine id: {engine_id} (expected {list(ENGINE_ASSERT_PKG)})"
        )

    req = ROOT / f"requirements-{engine_id}.txt"
    if not req.is_file():
        sys.exit(f"missing {req}")

    site = out / "site-packages"
    if out.exists():
        shutil.rmtree(out)
    site.mkdir(parents=True)

    # 依赖装进 relocatable 顶层目录，可直接进 PYTHONPATH。--python 锁定 wheel tag 到当前 3.12。
    run(
        "uv", "pip", "install",
        "--python", sys.executable,
        "--target", str(site),
        "-r", str(req),
    )

    # sidecar 源码与依赖分离（所有引擎共用同一份 main.py / engines）
    shutil.copy2(ROOT / "main.py", out / "main.py")
    shutil.copy2(ROOT / "_version.py", out / "_version.py")
    shutil.copytree(ROOT / "engines", out / "engines")

    # 清 __pycache__，避免跨机 .pyc 失配/无谓体积
    for p in out.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)

    # macOS 无证书兜底：ad-hoc 重签原生库
    adhoc_resign_macos(site)

    assert (out / "main.py").is_file(), "main.py missing in package"
    pkg = ENGINE_ASSERT_PKG[engine_id]
    assert (site / pkg).is_dir(), f"{pkg} missing in site-packages for {engine_id}"
    print(f"package [{engine_id}] assembled at {out}")


if __name__ == "__main__":
    main()
