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
import time


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


def _funasr_smoke(py, pkg_dir):
    """env 门控的 funasr/SenseVoice 转写冒烟（需真实模型，CI 默认跳过）。

    设 SMARTSUB_FUNASR_ASR_MODEL / _TOKENS / _VAD_MODEL / _WAV 后启用，
    走完整 transcribe 协议（progress/segment 事件 + 最终 result），断言有 segments。
    """
    asr = os.environ.get("SMARTSUB_FUNASR_ASR_MODEL")
    tokens = os.environ.get("SMARTSUB_FUNASR_TOKENS")
    vad = os.environ.get("SMARTSUB_FUNASR_VAD_MODEL")
    wav = os.environ.get("SMARTSUB_FUNASR_WAV")
    if not (asr and tokens and vad and wav):
        print("[smoke] funasr transcription skipped (set SMARTSUB_FUNASR_* to enable)")
        return

    pkg_dir = os.path.abspath(pkg_dir)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(pkg_dir, "site-packages")
    req = {
        "id": "s1",
        "method": "transcribe",
        "params": {
            "engine": "funasr",
            "audio_file": wav,
            "asr_model": asr,
            "tokens": tokens,
            "vad_model": vad,
            "language": "",
        },
    }
    proc = subprocess.Popen(
        [py, os.path.join(pkg_dir, "main.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    result = None
    try:
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        # 读到 s1 的最终 result/error 为止（其间是 progress/segment 事件）。
        # 保持 stdin 打开，避免 EOF 提前关闭主循环、误杀 worker 线程。
        deadline = time.time() + 600
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if msg.get("id") == "s1":
                result = msg
                break
        proc.stdin.write(json.dumps({"method": "shutdown", "params": {}}) + "\n")
        proc.stdin.flush()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    if not result or "result" not in result:
        raise SystemExit(
            "funasr smoke failed: %s\nstderr:\n%s" % (result, proc.stderr.read())
        )
    segs = result["result"].get("segments")
    assert segs, "funasr smoke produced no segments: %s" % result
    print("[smoke] funasr transcription OK (%d segments)" % len(segs))


def _smoke_package(pkg_dir, py):
    pkg_dir = os.path.abspath(pkg_dir)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(pkg_dir, "site-packages")
    _handshake(py, [os.path.join(pkg_dir, "main.py")], env=env)
    # funasr 包（含 sherpa_onnx）才尝试转写冒烟（env 未配置时内部跳过）。
    if os.path.isdir(os.path.join(pkg_dir, "site-packages", "sherpa_onnx")):
        _funasr_smoke(py, pkg_dir)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--package":
        _smoke_package(argv[1], argv[2])
    elif argv:
        _handshake(argv[0], argv[1:])
    else:
        _handshake(sys.executable, ["main.py"])
