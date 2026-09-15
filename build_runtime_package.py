#!/usr/bin/env python3
"""为 faster-whisper 组装单个自包含「运行时」包（A1：PBS + uv，非 PyInstaller）。

把过去的两层——可重定位的 python-build-standalone(PBS) 基座 + 可重定位引擎包
——合并为**一个**自包含目录：

    OUT/
      bin/python3 (unix) | python.exe (win)   # PBS 解释器（可重定位）
      lib/...                                   # PBS 标准库
      site-packages/<deps>                      # uv 解析的官方平台 wheel
      main.py, _version.py, engines/            # sidecar 源码

SmartSub 以 PYTHONHOME=OUT、PYTHONPATH=OUT/site-packages 启动 OUT 的解释器跑
OUT/main.py——与今天的运行时模型完全一致，只是解释器随包内置（不再有可单独下载的基座）。

为何不用 PyInstaller：faster-whisper 拖出原生 wheel（ctranslate2 / av / tokenizers /
onnxruntime）；uv 安装正确的逐平台 wheel，其原生依赖由上游 delocate/auditwheel/delvewheel
预捆绑，跨平台远比 PyInstaller 的 hook 收集稳健。`--only-binary=:all:` 保证 runner 上
不编译任何 sdist（故 runner CPU 的 -march 绝不会烘进产物）。

用法（需 PATH 上有 uv，在目标平台的原生 runner 上运行，host == target）：
  uv run --python 3.12.10 -- python build_runtime_package.py <OUT_DIR>            # CPU（默认）
  uv run --python 3.12.10 -- python build_runtime_package.py <OUT_DIR> --variant cuda  # GPU(CUDA12 Full)

变体（--variant）：
  cpu （默认）：仅 faster-whisper，产物即原有 CPU 运行时（包名不变）。
  cuda        ：额外捆绑 NVIDIA cuBLAS/cuDNN(cu12)+CUDA runtime，体积 ~1.4GB，N 卡开箱即用。
                仅 windows-x64 / linux-x64 有官方 nvidia wheel；macOS 无 GPU，请勿用本变体。
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON_VERSION = "3.12.10"
ENGINE_ID = "faster-whisper"
ASSERT_PKG = "faster_whisper"

# TRIM 与 App 侧 fetch-python-base.mjs 保持一致（裁剪同一套 PBS stdlib）。
# 同时覆盖 unix(lib/python3.12/...) 与 windows(Lib/...) 布局。
TRIM = [
    "lib/python3.12/test",
    "lib/python3.12/idlelib",
    "lib/python3.12/tkinter",
    "lib/python3.12/lib2to3",
    "lib/python3.12/ensurepip",
    "lib/python3.12/turtledemo",
    "lib/python3.12/pydoc_data",
    "Lib/test",
    "Lib/idlelib",
    "Lib/tkinter",
    "Lib/lib2to3",
    "Lib/ensurepip",
    "Lib/turtledemo",
    "Lib/pydoc_data",
    "include",  # C headers, not needed at runtime
]


def run(*cmd, **kw):
    print("+", " ".join(str(c) for c in cmd))
    return subprocess.run(list(cmd), check=True, **kw)


def uv_base_dir() -> Path:
    """安装并定位 uv 管理的 CPython，返回其可重定位根目录。"""
    run("uv", "python", "install", PYTHON_VERSION)
    exec_path = subprocess.run(
        ["uv", "python", "find", PYTHON_VERSION],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not exec_path:
        sys.exit(f"uv could not locate Python {PYTHON_VERSION}")
    real = Path(exec_path).resolve()
    # PBS 布局：win = <base>\python.exe ; unix = <base>/bin/python3.x
    if sys.platform == "win32":
        return real.parent
    return real.parent.parent


def copy_tree(src: Path, dest: Path) -> None:
    if sys.platform == "win32":
        shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
    else:
        # `cp -R src/.` 保留符号链接(bin/python3 -> python3.12) 与权限。
        run("cp", "-R", f"{src}/.", str(dest))


def out_python(out: Path) -> Path:
    return out / ("python.exe" if sys.platform == "win32" else "bin/python3")


def adhoc_sign(out: Path) -> None:
    """对 mach-o 文件 ad-hoc 重签（仅 macOS，无证书兜底），使下载/迁移后的
    解释器与原生库可在本机加载（arm64 未签名 dylib 会被内核拒绝 dlopen）。"""
    if sys.platform != "darwin":
        return
    count = 0
    for p in out.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        if p.suffix in (".so", ".dylib") or p.name in ("python3", "python3.12"):
            subprocess.run(
                ["codesign", "--force", "--sign", "-", str(p)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            count += 1
    print(f"ad-hoc signed {count} mach-o files")


def get_platform_suffix() -> str:
    if sys.platform == "darwin":
        import platform
        return "macos-arm64" if platform.machine() == "arm64" else "macos-x64"
    elif sys.platform == "win32":
        return "windows-x64"
    elif sys.platform.startswith("linux"):
        return "linux-x64"
    return sys.platform


def get_git_sha() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def write_manifest(out: Path, variant: str, platform_suffix: str) -> None:
    import json
    from datetime import datetime, timezone

    sys.path.insert(0, str(ROOT))
    import _version

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "version": "latest",
        "engineVersion": getattr(_version, "ENGINE_VERSION", "0.4.0"),
        "protocolVersion": getattr(_version, "PROTOCOL_VERSION", 1),
        "pythonAbi": "cp312",
        "platform": platform_suffix,
        "variant": variant,
        "engineId": ENGINE_ID,
        "builtAt": built_at,
        "gitSha": get_git_sha(),
    }
    manifest_path = out / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"wrote package manifest to {manifest_path}: variant={variant}, platform={platform_suffix}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build self-contained faster-whisper runtime")
    parser.add_argument("out_dir", help="output runtime directory")
    parser.add_argument(
        "--variant",
        choices=("cpu", "cuda"),
        default="cpu",
        help="cpu (default) or cuda (Full GPU: bundles NVIDIA cuBLAS/cuDNN)",
    )
    parser.add_argument(
        "--platform-suffix",
        default=None,
        help="Target platform suffix (e.g. windows-x64, macos-arm64). Defaults to auto-detect.",
    )
    args = parser.parse_args()
    out = Path(args.out_dir)
    variant = args.variant
    platform_suffix = args.platform_suffix or get_platform_suffix()

    # 1) PBS 基座拷入 OUT（copy + trim + drop config-*）
    base = uv_base_dir()
    print(f"PBS base source: {base}")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    copy_tree(base, out)
    for rel in TRIM:
        shutil.rmtree(out / rel, ignore_errors=True)
    stdlib = out / "lib" / "python3.12"
    if stdlib.exists():
        for entry in stdlib.iterdir():
            if entry.is_dir() and entry.name.startswith("config-"):
                shutil.rmtree(entry, ignore_errors=True)

    # 2) 依赖装进 OUT/site-packages（binary-only；--python 锁定到 OUT 的解释器，
    #    确保 wheel tag 与该 PBS ABI 完全一致；禁止 sdist 在 runner 源码编译）。
    site = out / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    req = ROOT / (
        f"requirements-{ENGINE_ID}-cuda.txt"
        if variant == "cuda"
        else f"requirements-{ENGINE_ID}.txt"
    )
    if not req.is_file():
        sys.exit(f"missing {req}")
    print(f"variant={variant} requirements={req.name}")
    run(
        "uv", "pip", "install",
        "--only-binary=:all:",
        "--python", str(out_python(out)),
        "--target", str(site),
        "-r", str(req),
    )

    # 3) sidecar 源码（所有引擎共用同一份 main.py / engines）
    shutil.copy2(ROOT / "main.py", out / "main.py")
    shutil.copy2(ROOT / "_version.py", out / "_version.py")
    shutil.copytree(ROOT / "engines", out / "engines")

    # 4) 清 __pycache__，避免跨机 .pyc 失配/无谓体积
    for p in out.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)

    # 5) 生成自描述 manifest.json，使解压产物自带版本与变体元数据
    write_manifest(out, variant, platform_suffix)

    # 6) macOS 无证书兜底：对内嵌解释器 + wheel 原生库 ad-hoc 重签
    adhoc_sign(out)

    # 7) 断言 + 包模式 smoke（OUT 解释器 + PYTHONPATH=OUT/site-packages 跑 OUT/main.py）
    assert (out / "main.py").is_file(), "main.py missing in runtime"
    assert (out / "manifest.json").is_file(), "manifest.json missing in runtime"
    assert (site / ASSERT_PKG).is_dir(), f"{ASSERT_PKG} missing in site-packages"
    if variant == "cuda":
        assert (site / "nvidia").is_dir(), (
            "cuda variant: bundled NVIDIA CUDA libs (site-packages/nvidia) missing"
        )
    py = out_python(out)
    run(str(py), str(ROOT / "smoke_test.py"), "--package", str(out), str(py))
    print(f"runtime [{ENGINE_ID}] variant={variant} assembled at {out} ({sys.platform})")


if __name__ == "__main__":
    main()
