"""FunASR / SenseVoice 引擎（基于 sherpa-onnx 跑 ONNX 推理）。

对外引擎标识保持 ``funasr``（与 App 侧 engineId / sidecar 名一致），底层实现
用 sherpa-onnx 加载 SenseVoice-Small（int8 ONNX）+ silero-vad，torch-free、
体积小、自带 VAD 分段，适合长音频离线转写。

约定输入音频为 SmartSub 预处理后的 16kHz / 单声道 / s16le WAV；用标准库 ``wave``
读取，避免引入 soundfile / librosa 等重依赖（仅依赖 numpy）。
"""

import logging
import threading
import wave

from engines import EngineError

log = logging.getLogger(__name__)

_recognizer_cache = {}
_recognizer_lock = threading.Lock()

TARGET_SAMPLE_RATE = 16000
# SenseVoice 支持的语言；其余一律回落自动检测（空串）。
_SUPPORTED_LANGS = {"zh", "en", "ja", "ko", "yue"}


def _load_sherpa():
    try:
        import sherpa_onnx  # noqa: PLC0415 - 惰性加载原生依赖

        return sherpa_onnx
    except ImportError as exc:
        raise EngineError(
            "engine_not_installed",
            "sherpa-onnx is not installed: %s" % exc,
        )


def _normalize_language(language):
    if language in (None, "", "auto"):
        return ""
    return language if language in _SUPPORTED_LANGS else ""


def _build_recognizer(sherpa_onnx, params):
    asr_model = params.get("asr_model")
    tokens = params.get("tokens")
    if not asr_model or not tokens:
        raise EngineError("invalid_params", "asr_model and tokens are required")

    model_type = params.get("model_type") or "sense_voice"
    num_threads = int(params.get("num_threads", 2))
    provider = params.get("provider") or "cpu"

    if model_type == "paraformer":
        return sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=asr_model,
            tokens=tokens,
            num_threads=num_threads,
            sample_rate=TARGET_SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            provider=provider,
            debug=False,
        )

    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=asr_model,
        tokens=tokens,
        num_threads=num_threads,
        sample_rate=TARGET_SAMPLE_RATE,
        use_itn=bool(params.get("use_itn", True)),
        language=_normalize_language(params.get("language")),
        provider=provider,
        debug=False,
    )


def _get_recognizer(sherpa_onnx, params):
    key = (
        params.get("model_type") or "sense_voice",
        params.get("asr_model"),
        params.get("tokens"),
        int(params.get("num_threads", 2)),
        bool(params.get("use_itn", True)),
        _normalize_language(params.get("language")),
        params.get("provider") or "cpu",
    )
    with _recognizer_lock:
        if key not in _recognizer_cache:
            log.info("loading SenseVoice recognizer provider=%s", key[-1])
            _recognizer_cache[key] = _build_recognizer(sherpa_onnx, params)
        return _recognizer_cache[key]


def _read_wav_mono16k(path):
    """读取 WAV → float32 [-1,1] mono 16k。要求 s16le；非 16k 时线性重采样兜底。"""
    import numpy as np  # noqa: PLC0415

    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if sample_width != 2:
        raise EngineError(
            "invalid_audio",
            "expected 16-bit PCM wav, got sample_width=%d bytes" % sample_width,
        )

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)

    if sample_rate != TARGET_SAMPLE_RATE and samples.size > 0:
        duration = samples.size / float(sample_rate)
        tgt_len = int(round(duration * TARGET_SAMPLE_RATE))
        if tgt_len > 0:
            src_idx = np.linspace(0, samples.size - 1, num=tgt_len)
            samples = np.interp(src_idx, np.arange(samples.size), samples).astype(
                np.float32
            )

    return np.ascontiguousarray(samples, dtype=np.float32)


def _build_vad(sherpa_onnx, params):
    vad_model = params.get("vad_model")
    if not vad_model:
        raise EngineError("invalid_params", "vad_model (silero_vad.onnx) is required")

    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = vad_model
    config.silero_vad.threshold = float(params.get("vad_threshold", 0.5))
    config.silero_vad.min_silence_duration = (
        float(params.get("vad_min_silence_duration_ms", 100)) / 1000.0
    )
    config.silero_vad.min_speech_duration = (
        float(params.get("vad_min_speech_duration_ms", 250)) / 1000.0
    )
    max_speech = float(params.get("vad_max_speech_duration_s") or 0)
    if max_speech > 0:
        config.silero_vad.max_speech_duration = max_speech
    config.sample_rate = TARGET_SAMPLE_RATE
    config.num_threads = int(params.get("num_threads", 2))
    config.validate()
    return config


def preload(params):
    """仅加载 ASR/VAD 模型，不执行转写。"""
    sherpa_onnx = _load_sherpa()
    _get_recognizer(sherpa_onnx, params)
    if params.get("vad_model"):
        # 触发一次 VAD 构建，确保 silero 模型可加载（构建失败尽早暴露）。
        sherpa_onnx.VoiceActivityDetector(_build_vad(sherpa_onnx, params), 30)
    return {"engine": "funasr", "preloaded": True}


def _decode_segment(recognizer, samples):
    stream = recognizer.create_stream()
    stream.accept_waveform(TARGET_SAMPLE_RATE, samples)
    recognizer.decode_stream(stream)
    return stream.result.text.strip()


def transcribe(params, emit_event, is_cancelled):
    import numpy as np  # noqa: PLC0415

    audio_file = params.get("audio_file")
    if not audio_file:
        raise EngineError("invalid_params", "audio_file is required")

    sherpa_onnx = _load_sherpa()
    recognizer = _get_recognizer(sherpa_onnx, params)
    vad_config = _build_vad(sherpa_onnx, params)

    emit_event("progress", {"percent": 0})
    samples = _read_wav_mono16k(audio_file)
    total_samples = int(samples.size)
    total_duration = total_samples / float(TARGET_SAMPLE_RATE) if total_samples else 0.0
    if total_samples == 0:
        return {"engine": "funasr", "language": _normalize_language(params.get("language")) or "auto", "duration": 0.0, "segments": []}

    window = int(vad_config.silero_vad.window_size)
    vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=100)

    segments = []

    def _drain(flush_remaining):
        while not vad.empty():
            if is_cancelled():
                return False
            seg = vad.front
            text = _decode_segment(recognizer, seg.samples)
            start = seg.start / float(TARGET_SAMPLE_RATE)
            end = (seg.start + len(seg.samples)) / float(TARGET_SAMPLE_RATE)
            vad.pop()
            if text:
                segment = {"start": start, "end": end, "text": text}
                segments.append(segment)
                emit_event("segment", segment)
        return True

    offset = 0
    while offset < total_samples:
        if is_cancelled():
            return None
        chunk = samples[offset : offset + window]
        if chunk.size < window:
            chunk = np.pad(chunk, (0, window - chunk.size))
        vad.accept_waveform(chunk)
        offset += window
        if not _drain(False):
            return None
        if total_duration:
            percent = min(offset / float(total_samples) * 99.0, 99.0)
            emit_event("progress", {"percent": round(percent, 2)})

    vad.flush()
    if not _drain(True):
        return None

    emit_event("progress", {"percent": 99.0})
    return {
        "engine": "funasr",
        "language": _normalize_language(params.get("language")) or "auto",
        "duration": total_duration,
        "segments": segments,
    }
