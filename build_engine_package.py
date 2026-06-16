#!/usr/bin/env python3
"""为当前平台组装可重定位的 faster_whisper 引擎包。

产物布局（默认 dist/package/）：
  main.py, _version.py, engines/, site-packages/<deps...>

运行（需 PATH 上有 uv，且当前解释器即目标 3.12）：
  uv run --python 3.12.13 -- python build_engine_package.py [OUT_DIR]
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "package"
SITE = OUT / "site-packages"


def run(*args):
    print("+", " ".join(str(a) for a in args))
    subprocess.check_call(list(args))


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    SITE.mkdir(parents=True)

    # 依赖装进 relocatable 顶层目录，可直接进 PYTHONPATH。--python 锁定 wheel tag 到当前 3.12。
    run(
        "uv", "pip", "install",
        "--python", sys.executable,
        "--target", str(SITE),
        "-r", str(ROOT / "requirements.txt"),
    )

    # sidecar 源码与依赖分离（便于 P1 共享同一份 main.py）
    shutil.copy2(ROOT / "main.py", OUT / "main.py")
    shutil.copy2(ROOT / "_version.py", OUT / "_version.py")
    shutil.copytree(ROOT / "engines", OUT / "engines")

    # 清 __pycache__，避免跨机 .pyc 失配/无谓体积
    for p in OUT.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)

    assert (OUT / "main.py").is_file(), "main.py missing in package"
    assert (SITE / "faster_whisper").is_dir(), "faster_whisper missing in site-packages"
    print("package assembled at", OUT)


if __name__ == "__main__":
    main()
