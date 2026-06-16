#!/usr/bin/env python3
"""Build a relocatable, trimmed PBS base for SmartSub Layer 1 (downloadable base).

Mirrors the app's ``scripts/fetch-python-base.mjs``: it uses uv-managed CPython
(python-build-standalone ``install_only`` distribution, relocatable) so the
*downloaded* base is a drop-in replacement for the *bundled* base — same
``3.12.10`` / ``cp312`` ABI and the same on-disk layout / trim set.

Why uv (instead of a hardcoded PBS release tag): uv resolves and caches the
correct PBS release for a pinned CPython version, so we never hardcode an
easily-stale ``python-build-standalone`` release date (those assets are removed
/ renamed across releases). The engine repo already standardizes on uv
(``astral-sh/setup-uv`` + ``uv python install``) for the engine packages, so the
base shares the exact same CPython source.

Usage:
  python build_base_package.py <OUT_DIR>

Runs on each target's *native* runner (host == target), exactly like
electron-builder / fetch-python-base.mjs. Requires ``uv`` on PATH.
"""
import shutil
import subprocess
import sys
from pathlib import Path

PYTHON_VERSION = "3.12.10"

# Keep this list in sync with app repo scripts/fetch-python-base.mjs (TRIM).
# Covers both unix (lib/python3.12/...) and windows (Lib/...) layouts.
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


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def uv_base_dir() -> Path:
    """Install + locate the uv-managed CPython, return its relocatable root."""
    run(["uv", "python", "install", PYTHON_VERSION])
    exec_path = subprocess.run(
        ["uv", "python", "find", PYTHON_VERSION],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not exec_path:
        sys.exit(f"uv could not locate Python {PYTHON_VERSION}")
    real = Path(exec_path).resolve()
    # PBS layout: win = <base>\python.exe ; unix = <base>/bin/python3.x
    if sys.platform == "win32":
        return real.parent
    return real.parent.parent


def copy_tree(src: Path, dest: Path) -> None:
    if sys.platform == "win32":
        shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
    else:
        # `cp -R src/.` preserves symlinks (bin/python3 -> python3.12) + perms.
        run(["cp", "-R", f"{src}/.", str(dest)])


def adhoc_sign(out: Path) -> None:
    """Ad-hoc codesign mach-o files so the downloaded base runs without a
    developer certificate (macOS Gatekeeper for unsigned downloaded dylibs)."""
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


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: build_base_package.py <OUT_DIR>")
    out = Path(sys.argv[1])

    base = uv_base_dir()
    print(f"Base source: {base}")

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    copy_tree(base, out)

    for rel in TRIM:
        shutil.rmtree(out / rel, ignore_errors=True)

    # Drop config-* dirs (build-time Makefile/static-lib refs; unix only).
    stdlib = out / "lib" / "python3.12"
    if stdlib.exists():
        for entry in stdlib.iterdir():
            if entry.is_dir() and entry.name.startswith("config-"):
                shutil.rmtree(entry, ignore_errors=True)

    for pc in out.rglob("__pycache__"):
        shutil.rmtree(pc, ignore_errors=True)

    adhoc_sign(out)

    py = out / ("python.exe" if sys.platform == "win32" else "bin/python3")
    probe = subprocess.run(
        [
            str(py),
            "-c",
            "import ssl, ctypes, sqlite3, lzma, hashlib, zlib, bz2; "
            'print("base ok", __import__("sys").version.split()[0])',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    print(probe.stdout.strip())
    print(f"base ready at {out} ({sys.platform})")


if __name__ == "__main__":
    main()
