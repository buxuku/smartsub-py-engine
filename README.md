# smartsub-py-engine

SmartSub 的 Python 推理 sidecar（faster-whisper），独立仓库构建与发布。

> 注：funasr 已迁移到 App 侧 sherpa-onnx-node 原生运行库（见 `.github/workflows/sherpa-libs.yml`），不再是 Python 引擎。

主应用 [buxuku/SmartSub](https://github.com/buxuku/SmartSub) 通过 `latest` Release 按需下载，不内置源码。

引擎与底层库：

| engineId | 底层库 | 说明 |
| --- | --- | --- |
| `faster-whisper` | `faster-whisper`（ctranslate2） | 通用多语种 Whisper |

## Release

每次推送到 `main` 或手动触发 workflow 后，CI 以 `engine_id × 平台` 矩阵（1 引擎 × 4 平台 = 4 个包）发布到：

**https://github.com/buxuku/smartsub-py-engine/releases/tag/latest**

资产（产物名 `smartsub-<engineId>-<suffix>.tar.gz`）：

```
smartsub-faster-whisper-{macos-arm64,macos-x64,windows-x64,linux-x64}.tar.gz
checksums.sha256
manifest.json   # engines[] + 顶层 artifacts(=faster-whisper, 兼容) + enginePackages{<engineId>:{sidecar,artifacts}}
```

## 本地开发

依赖 [`uv`](https://docs.astral.sh/uv/)（锁定 Python，见 `.python-version`）。产物是
**可重定位引擎包**（`main.py` + `site-packages/`），由 SmartSub 内置的
python-build-standalone 基座经 `PYTHONPATH` 加载，不再使用 PyInstaller 冻结。

```bash
# 开发模式冒烟（用 uv 环境直接跑 ./main.py）
uv run --python "$(cat .python-version)" -- python smoke_test.py

# 构建可重定位引擎包到 dist/<engineId>/（main.py + engines/ + site-packages/）
# 第二个参数是 engineId：faster-whisper，读取 requirements-<engineId>.txt
uv run --python "$(cat .python-version)" -- python build_engine_package.py dist/faster-whisper faster-whisper

# 包模式冒烟：基座解释器 + PYTHONPATH=site-packages 跑 dist/<engineId>/main.py
PY="$(uv python find "$(cat .python-version)")"
"$PY" smoke_test.py --package dist/faster-whisper "$PY"
```

SmartSub 主仓库开发时，把 `dist/<engineId>/` 拷到 `userData/py-engines/<engineId>/`，
或从 Resource Hub 下载安装；App 用内置基座加载该包。

## 协议

stdio JSON-lines，与 SmartSub `PythonRuntimeManager` 对应。详见 `main.py` 头部注释。
