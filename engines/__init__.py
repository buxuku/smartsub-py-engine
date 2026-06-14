"""引擎注册表。"""

import importlib.util
import logging

log = logging.getLogger(__name__)


class EngineError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.engine_error_code = code


def get_engine(name):
    if name == "faster_whisper":
        from engines import faster_whisper_engine

        return faster_whisper_engine
    raise EngineError("engine_not_found", "unknown engine: %s" % name)


def list_engines():
    """只探测依赖是否可导入，不真正导入（避免重依赖拖慢 ping）。

    find_spec 经 importlib 的 finder 查找（PyInstaller 冻结环境下走
    PyiFrozenImporter）只定位、不执行模块，毫秒级返回；重依赖
    （ctranslate2/av/tokenizers/onnxruntime 等）推迟到首个 transcribe 惰性加载。
    """
    return {
        "faster_whisper": importlib.util.find_spec("faster_whisper") is not None,
    }
