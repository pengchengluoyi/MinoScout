"""日志。

port(scout): 源自 MiniOrangeServer `script/log.py`。

与上游的差异（有意为之）：
  - **去掉了 DB 回调**（上游 `SLog._db_write_callback` 把日志写进 WorkflowLog 表）。
    Scout 不碰数据库（CLAUDE.md §1 约束 1），trace 归 Nexus。本机日志只服务排障。
  - 保留 `current_run_id` / `current_flow_id` 等 contextvar，并把 run_id 打进行首，
    这样 Scout 的日志能和 Nexus 的 trace 对齐（CONVENTIONS.md §1）。
  - 对外 API（`SLog.d/i/w/e`）与上游逐字一致，搬过来的代码不需要改调用点。
"""
from __future__ import annotations

import contextvars
import logging
import os
import threading
import sys
from datetime import datetime

current_run_id = contextvars.ContextVar("run_id", default=None)
current_step_idx = contextvars.ContextVar("step_idx", default=None)
# 上游有 flow_id / node_id（workflow 时代的概念）。保留 flow_id 以兼容搬来的代码；
# node_id 在本仓改指「Scout 节点 id」，由 transport 在启动时 set 一次。
current_flow_id = contextvars.ContextVar("flow_id", default=None)
current_node_id = contextvars.ContextVar("node_id", default=None)

RESET = "\033[0m"
RED = "\033[91m"
YELLOW = "\033[93m"
WHITE = "\033[37m"
LIGHT_WHITE = "\033[97m"

_LEVEL_COLOR = {"D": WHITE, "I": LIGHT_WHITE, "W": YELLOW, "E": RED}


def _configure_utf8_stdio() -> None:
    """Frozen Windows 默认 cp1252：日志/probe 里的中文会把进程打挂，stdout 还是空的。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not reconfigure:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configure_utf8_stdio()


class LogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        now = datetime.now()
        date_str = now.strftime("%m-%d %H:%M:%S.") + now.strftime("%f")[:3]
        level = record.levelname[0]
        color = _LEVEL_COLOR.get(level, RESET)
        tag = getattr(record, "tag", "default_tag")

        # run_id/step 前缀：便于把 Scout 的行和 Nexus 的 trace 对齐
        run_id = current_run_id.get()
        step = current_step_idx.get()
        ctx = ""
        if run_id:
            ctx = f"[{str(run_id)[:8]}"
            ctx += f"#{step}] " if step is not None else "] "

        return (
            f"{color}{date_str}  {os.getpid()}  {threading.get_ident()} "
            f"{level} {tag}: {ctx}{record.getMessage()}{RESET}"
        )


logger = logging.getLogger("MinoScout")
logger.setLevel(logging.DEBUG)
logger.propagate = False  # 避免 uvicorn/root 再打印一遍
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(LogFormatter())
    logger.addHandler(_handler)

# 压低第三方库的重试/代理告警
for _noisy in ("urllib3", "httpx", "websockets", "filelock", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


class SLog:
    """API 与上游 `script.log.SLog` 逐字一致，便于搬来的代码零改动。"""

    _LEVELS = {"debug": logger.debug, "info": logger.info,
               "warning": logger.warning, "error": logger.error}

    @classmethod
    def _log(cls, level: str, tag: str, message: str) -> None:
        fn = cls._LEVELS.get(level)
        if fn is not None:
            fn(message, extra={"tag": tag})

    @staticmethod
    def d(tag: str, message: str) -> None:
        SLog._log("debug", tag, message)

    @staticmethod
    def i(tag: str, message: str) -> None:
        SLog._log("info", tag, message)

    @staticmethod
    def w(tag: str, message: str) -> None:
        SLog._log("warning", tag, message)

    @staticmethod
    def e(tag: str, message: str) -> None:
        SLog._log("error", tag, message)


# ---------------- 凭据脱敏 ----------------
# CONVENTIONS.md §1：EXECUTE.device_hint 里的 password / token 一律不入日志。
_SECRET_KEYS = frozenset({"password", "token", "auth_token", "session_token", "secret", "otp"})


def redact(data: dict) -> dict:
    """浅层脱敏。日志里要打 device_hint / params 时先过一遍这个。"""
    out = {}
    for k, v in (data or {}).items():
        if k.lower() in _SECRET_KEYS and v not in (None, "", 0):
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = redact(v)
        else:
            out[k] = v
    return out
