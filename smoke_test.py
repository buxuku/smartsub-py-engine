#!/usr/bin/env python3
"""ping 冒烟。

三种模式:
  python smoke_test.py                      # dev: 当前解释器跑 ./main.py
  python smoke_test.py <CMD> [ARGS...]      # 跑指定命令(如冻结产物)
  python smoke_test.py --package <DIR> <PY> # 包模式: 基座 PY + PYTHONPATH=DIR/site-packages 跑 DIR/main.py
"""
import json
import os
import subprocess
import sys


def _handshake(command, args, env=None):
    proc = subprocess.Popen(
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    line = ""
    try:
        proc.stdin.write(json.dumps({"id": "1", "method": "ping", "params": {}}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        proc.stdin.write(json.dumps({"method": "shutdown", "params": {}}) + "\n")
        proc.stdin.flush()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    if not line:
        raise SystemExit("no response from engine; stderr:\n" + proc.stderr.read())
    data = json.loads(line)
    assert "result" in data, data
    assert "engines" in data["result"], data
    print("smoke ok:", data["result"])


def _smoke_package(pkg_dir, py):
    pkg_dir = os.path.abspath(pkg_dir)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(pkg_dir, "site-packages")
    _handshake(py, [os.path.join(pkg_dir, "main.py")], env=env)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--package":
        _smoke_package(argv[1], argv[2])
    elif argv:
        _handshake(argv[0], argv[1:])
    else:
        _handshake(sys.executable, ["main.py"])
