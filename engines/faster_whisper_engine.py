"""faster-whisper 引擎。"""

import logging
import threading

from engines import EngineError

log = logging.getLogger(__name__)
_model_cache = {}
_model_lock = threading.Lock()

# 抗幻觉/抗重复参数透传白名单：仅当 SmartSub 显式下发时才覆盖，缺省键回落
# faster-whisper 自身默认值，老客户端（不发这些键）行为完全不变。
_ADVANCED_KEYS = (
    "condition_on_previous_text",
    "repetition_penalty",
    "no_repeat_ngram_size",
    "compression_ratio_threshold",
    "log_prob_threshold",
    "no_speech_threshold",
    "hallucination_silence_threshold",
    "temperature",
)


def _load_faster_whisper():
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415 - 惰性加载重依赖

        return WhisperModel
    except ImportError as exc:
        raise EngineError(
            "engine_not_installed",
            "faster-whisper is not installed: %s" % exc,
        )


def _get_model(model, device, compute_type, download_root=None):
    key = (model, device, compute_type, download_root)
    with _model_lock:
        if key not in _model_cache:
            WhisperModel = _load_faster_whisper()
            kwargs = {"device": device, "compute_type": compute_type}
            if download_root:
                kwargs["download_root"] = download_root
            log.info("loading model %s device=%s compute_type=%s", model, device, compute_type)
            _model_cache[key] = WhisperModel(model, **kwargs)
        return _model_cache[key]


def preload(params):
    """仅下载/加载模型，不执行转写。"""
    model = params.get("model", "base")
    _get_model(
        model,
        params.get("device", "auto"),
        params.get("compute_type", "auto"),
        params.get("download_root"),
    )
    return {"engine": "faster_whisper", "model": model, "preloaded": True}


def transcribe(params, emit_event, is_cancelled):
    audio_file = params.get("audio_file")
    if not audio_file:
        raise EngineError("invalid_params", "audio_file is required")

    model = _get_model(
        params.get("model", "base"),
        params.get("device", "auto"),
        params.get("compute_type", "auto"),
        params.get("download_root"),
    )

    language = params.get("language")
    if language in (None, "", "auto"):
        language = None

    # max_speech_duration_s：SmartSub 传 0 表示「不限制」，映射为 faster-whisper 的 inf
    # （JSON 无法承载 inf，故在此本地转换）。samples_overlap 是 whisper.cpp 专有项，
    # faster-whisper 的 VadOptions 不支持，故不接收。
    max_speech = float(params.get("vad_max_speech_duration_s") or 0)
    # 仅透传 SmartSub 显式给出的抗幻觉/抗重复参数，其余回落 faster-whisper 默认。
    extra = {k: params[k] for k in _ADVANCED_KEYS if params.get(k) is not None}

    emit_event("progress", {"percent": 0})
    segments_iter, info = model.transcribe(
        audio_file,
        language=language,
        initial_prompt=params.get("initial_prompt") or None,
        word_timestamps=bool(params.get("word_timestamps", False)),
        vad_filter=bool(params.get("vad", True)),
        vad_parameters={
            "threshold": float(params.get("vad_threshold", 0.5)),
            "min_speech_duration_ms": int(params.get("vad_min_speech_duration_ms", 250)),
            "max_speech_duration_s": max_speech if max_speech > 0 else float("inf"),
            "min_silence_duration_ms": int(params.get("vad_min_silence_duration_ms", 100)),
            "speech_pad_ms": int(params.get("vad_speech_pad_ms", 30)),
        },
        **extra,
    )

    total = float(info.duration or 0) or None
    segments = []
    for seg in segments_iter:
        if is_cancelled():
            return None
        segment = {"start": seg.start, "end": seg.end, "text": seg.text}
        if params.get("word_timestamps") and seg.words:
            segment["words"] = [
                {"start": w.start, "end": w.end, "word": w.word} for w in seg.words
            ]
        segments.append(segment)
        emit_event("segment", segment)
        if total:
            emit_event("progress", {"percent": round(min(seg.end / total * 100, 99.0), 2)})

    return {
        "engine": "faster_whisper",
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
        "segments": segments,
    }
