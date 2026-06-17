"""引擎版本与协议版本的单一来源（main.py 与 CI 都从这里读）。

- ENGINE_VERSION：引擎产物版本，发布新构建时手动 bump。
- PROTOCOL_VERSION：stdio JSON-lines 协议大版本。改动协议（字段/语义）才 +1，
  并同步 SmartSub 的 SUPPORTED_PROTOCOL_MAX。
"""

ENGINE_VERSION = "0.3.4"
PROTOCOL_VERSION = 1
