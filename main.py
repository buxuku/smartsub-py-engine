#!/usr/bin/env python3
"""SmartSub Python 推理引擎 sidecar。

协议:stdin/stdout 上的 JSON-lines(每行一条消息),JSON-RPC 风格:

  请求(Electron -> Python):   {"id": "req-1", "method": "transcribe", "params": {...}}
  通知(Electron -> Python):   {"method": "cancel", "params": {"id": "req-1"}}        # 无 id,不期待应答
  成功响应(Python -> Electron): {"id": "req-1", "result": {...}}
  错误响应(Python -> Electron): {"id": "req-1", "error": {"code": "...", "message": "..."}}
  事件推送(Python -> Electron): {"method": "progress", "params": {"id": "req-1", "percent": 42.5}}

约束:
  - stdout 专用于协议消息,任何日志必须走 stderr(logging 已配置)。
  - transcribe 在 worker 线程中执行,主循环始终保持响应(用于 cancel/ping)。

手动调试:
  echo '{"id":"1","method":"ping","params":{}}' | python3 main.py
"""

import json
import logging
import os
import sys
import threading
import traceback

from engines import get_engine, list_engines
from _version import ENGINE_VERSION, PROTOCOL_VERSION

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[py-engine] %(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def _make_protocol_stream():
    """保护协议 stdout 不被原生库污染。

    把真正的 stdout(fd 1) dup 出来专供 JSON 协议消息，随后把 fd 1 重定向到
    stderr(fd 2)。这样任何原生库（典型如 onnxruntime / sherpa-onnx 首次初始化）
    写到 fd 1 的诊断信息都会落到 stderr 当作日志排掉，绝不污染协议流。

    背景:Windows 上 sherpa-onnx 首个 transcribe 初始化会向 stdout 打印信息,
    污染首条结果行导致 Electron 端 JSON 解析失败、任务永久卡死(取消重试因二次
    初始化不再有该输出而恢复正常)。把协议流与 fd 1 解耦后该问题根除。
    """
    try:
        protocol_fd = os.dup(1)
        os.dup2(2, 1)
        return os.fdopen(protocol_fd, "w", encoding="utf-8", newline="\n")
    except (OSError, ValueError):  # 极少数平台 dup 失败时退回原 stdout，至少保持可用
        return sys.stdout


# 必须在任何原生库加载（首个 transcribe/preload 的 worker 线程内）之前完成。
_protocol_out = _make_protocol_stream()
log.info("[DIAG] protocol stream isolated via dup(1)+dup2(2,1); logging on stderr")

_stdout_lock = threading.Lock()
# 进行中请求的取消标记: request_id -> threading.Event
_cancel_events = {}
_cancel_lock = threading.Lock()
_shutdown = threading.Event()
# [DIAG/FIX] 已在主线程完成重依赖导入/模型构造的引擎集合。首个 transcribe 时在主线程
# 预热，规避 worker 线程首次原生 import 在 Windows 上的 loader-lock 死锁。
_warmed_engines = set()


def _warmup_engine_on_main_thread(engine_name, params):
    """在主线程完成重依赖导入(+模型构造)，并逐模块打日志定位卡点。仅首次执行。"""
    if engine_name in _warmed_engines:
        return
    log.info("[DIAG] main-thread warmup begin for %s (thread=%s)",
             engine_name, threading.current_thread().name)
    try:
        engine = get_engine(engine_name)
        warm = getattr(engine, "warmup_imports", None)
        if warm:
            warm()
        # import 通过后，再在主线程完成模型构造(若构造也涉及 loader-lock/OpenMP 一并规避)。
        preload = getattr(engine, "preload", None)
        if preload:
            log.info("[DIAG] main-thread preload (import+construct) begin")
            preload(params)
            log.info("[DIAG] main-thread preload done")
        _warmed_engines.add(engine_name)
        log.info("[DIAG] main-thread warmup end OK for %s", engine_name)
    except Exception as exc:  # noqa: BLE001 - 预热失败不致命，worker 会复现并把错误回传客户端
        log.warning("[DIAG] main-thread warmup error (non-fatal): %r", exc)


def emit(message):
    """线程安全地向受保护的协议流写出一条消息（绕开被原生库污染的 fd 1）。"""
    line = json.dumps(message, ensure_ascii=False)
    with _stdout_lock:
        _protocol_out.write(line + "\n")
        _protocol_out.flush()


def emit_event(method, params):
    emit({"method": method, "params": params})


def emit_result(req_id, result):
    emit({"id": req_id, "result": result})


def emit_error(req_id, code, message):
    emit({"id": req_id, "error": {"code": code, "message": message}})


def handle_ping(req_id, params):
    emit_result(
        req_id,
        {
            "version": ENGINE_VERSION,
            "engineVersion": ENGINE_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "python": sys.version.split()[0],
            "frozen": bool(getattr(sys, "frozen", False)),
            "engines": list_engines(),
        },
    )


def handle_preload(req_id, params):
    """在 worker 线程中预加载模型（仅 _get_model，不转写）。"""

    def worker():
        engine_name = params.get("engine", "faster_whisper")
        try:
            engine = get_engine(engine_name)
            result = engine.preload(params)
            emit_result(req_id, result)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "engine_error_code", "internal_error")
            log.error("preload failed: %s\n%s", exc, traceback.format_exc())
            emit_error(req_id, code, str(exc))

    threading.Thread(target=worker, name="preload-%s" % req_id, daemon=True).start()


def handle_transcribe(req_id, params):
    """在 worker 线程中执行转写,逐段上报 progress/segment 事件。"""
    cancel_event = threading.Event()
    with _cancel_lock:
        _cancel_events[req_id] = cancel_event

    # [DIAG/FIX] 首个 transcribe 在主线程预热(import + 构造)，规避 Windows worker
    # 线程首次原生 import 的 loader-lock 死锁；逐模块日志定位卡点。后续命中缓存即时返回。
    _warmup_engine_on_main_thread(params.get("engine", "faster_whisper"), params)

    def worker():
        log.info("[DIAG] transcribe worker thread started id=%s", req_id)
        engine_name = params.get("engine", "faster_whisper")
        try:
            engine = get_engine(engine_name)
            log.info("[DIAG] engine resolved (%s); calling engine.transcribe()", engine_name)
            result = engine.transcribe(
                params,
                emit_event=lambda method, p: emit_event(method, dict(p, id=req_id)),
                is_cancelled=cancel_event.is_set,
            )
            if cancel_event.is_set():
                emit_error(req_id, "cancelled", "transcription cancelled by client")
            else:
                emit_result(req_id, result)
        except Exception as exc:  # noqa: BLE001 - 协议边界,任何异常都要转成 error 响应
            code = getattr(exc, "engine_error_code", "internal_error")
            log.error("transcribe failed: %s\n%s", exc, traceback.format_exc())
            emit_error(req_id, code, str(exc))
        finally:
            with _cancel_lock:
                _cancel_events.pop(req_id, None)

    threading.Thread(target=worker, name="transcribe-%s" % req_id, daemon=True).start()


def handle_cancel(params):
    target_id = params.get("id")
    with _cancel_lock:
        event = _cancel_events.get(target_id)
    if event:
        event.set()
        log.info("cancel requested for %s", target_id)


HANDLERS = {
    "ping": handle_ping,
    "preload": handle_preload,
    "transcribe": handle_transcribe,
}


def dispatch(message):
    method = message.get("method")
    params = message.get("params") or {}
    req_id = message.get("id")
    log.info("[DIAG] dispatch method=%s id=%s", method, req_id)

    if method == "shutdown":
        _shutdown.set()
        return
    if method == "cancel":
        handle_cancel(params)
        return

    if req_id is None:
        log.warning("notification with unknown method ignored: %s", method)
        return

    handler = HANDLERS.get(method)
    if handler is None:
        emit_error(req_id, "method_not_found", "unknown method: %s" % method)
        return
    handler(req_id, params)


def main():
    log.info("engine started (version=%s, python=%s)", ENGINE_VERSION, sys.version.split()[0])
    for line in sys.stdin:
        if _shutdown.is_set():
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            log.error("invalid json line: %s (%s)", line[:200], exc)
            continue
        try:
            dispatch(message)
        except Exception:  # noqa: BLE001 - 主循环绝不能死
            log.error("dispatch crashed:\n%s", traceback.format_exc())
        if _shutdown.is_set():
            break
    log.info("engine exiting")


if __name__ == "__main__":
    main()
