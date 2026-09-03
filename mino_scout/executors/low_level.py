# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""通用 low_level 执行契约：让 capability 只靠 YAML 声明就能跑，不必写 Python 分支。

背景
----
原先 AdbExecutor.execute() 是硬编码 if-chain，capability yaml 里的 `low_level`
从未被执行 —— 所以"加个 yaml 就多一个能力"是假的。本模块把 `low_level` 变成
真正的执行契约，executor 只需在 if-chain 末尾兜底调用 run_low_level()。

支持的 kind
-----------
shell        单条命令
shell_batch  一次取多个状态（取证用），各条独立，失败不互相影响
shell_seq    有序多步，任一步 rc != 0 即整体失败

安全边界（第三方 pack 会走这条路，必须严格）
--------------------------------------------
1. 模板参数值只允许 [A-Za-z0-9_.:/@%,+=-] 与空格，命中即拒绝；
2. 命令首词白名单，未在白名单内直接拒绝；
3. 危险片段黑名单（重定向 / 管道到 sh / rm 等）直接拒绝；
4. 拒绝时返回 FAIL 并把原因写进 error，不静默降级。
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Callable, Optional

from mino_scout.log import SLog

TAG = "LowLevel"

# 允许作为命令首词的可执行名（Android shell 常用只读/输入类）
_CMD_WHITELIST = frozenset({
    "input", "dumpsys", "am", "pm", "settings", "getprop", "svc", "cmd",
    "locksettings", "appops", "logcat", "pidof", "wm", "content", "monkey",
    "echo", "cat", "grep", "sleep",
})

# 危险片段：出现即拒绝（在参数渲染后的整串上匹配）
_DANGER_PATTERNS = (
    re.compile(r"(^|\s)rm(\s|$)"),
    re.compile(r"(^|\s)dd(\s|$)"),
    re.compile(r"(^|\s)reboot(\s|$)"),
    re.compile(r"(^|\s)mount(\s|$)"),
    re.compile(r"(^|\s)su(\s|$)"),
    re.compile(r"[>;`$]"),          # 重定向 / 命令分隔 / 反引号 / 变量展开
    re.compile(r"\|\s*sh\b"),
    re.compile(r"\|\s*sh$"),
    re.compile(r"&&|\|\|"),
)

# 参数值允许的字符（渲染进命令前逐个校验）
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/@%,+=\- ]*$")

# {name} 占位符
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class LowLevelError(Exception):
    """低层执行契约被拒绝（不安全 / 声明不完整）。"""


def render_template(template: str, params: dict[str, Any]) -> str:
    """把 {name} 占位符替换成 params 里的值，逐个校验字符白名单。

    缺失的占位符视为声明错误（而不是渲染成空串），避免拼出半截命令。
    """
    missing: list[str] = []
    bad: list[str] = []

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in params or params[key] is None:
            missing.append(key)
            return ""
        val = str(params[key])
        if not _SAFE_VALUE_RE.match(val):
            bad.append(f"{key}={val!r}")
            return ""
        return val

    out = _PLACEHOLDER_RE.sub(_sub, template or "")
    if missing:
        raise LowLevelError(f"low_level 模板缺少参数: {sorted(set(missing))}")
    if bad:
        raise LowLevelError(f"low_level 参数含非法字符: {bad}")
    return out.strip()


def assert_command_allowed(cmd: str) -> None:
    """命令白名单 + 危险片段黑名单校验。不通过抛 LowLevelError。"""
    text = (cmd or "").strip()
    if not text:
        raise LowLevelError("low_level 命令为空")
    for pat in _DANGER_PATTERNS:
        if pat.search(text):
            raise LowLevelError(f"low_level 命令含危险片段（{pat.pattern}）: {text}")
    try:
        head = shlex.split(text)[0]
    except ValueError as exc:
        raise LowLevelError(f"low_level 命令无法解析: {exc}") from exc
    if head not in _CMD_WHITELIST:
        raise LowLevelError(
            f"low_level 命令首词 {head!r} 不在白名单内；"
            f"如确需支持请在 low_level._CMD_WHITELIST 显式添加"
        )


# ---------- 输出解析器 ----------


def _parse_raw(text: str) -> Any:
    return text


def _parse_lines(text: str) -> Any:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _parse_keyvalue_lines(text: str) -> Any:
    """从 dumpsys 风格输出里抽 key=value / key: value 对。

    一行里可能有多对（dumpsys 常见 `mShowingDream=false mDreamingLockscreen=false`），
    所以逐行 finditer 全取，先出现的优先（同名不覆盖）。
    """
    out: dict[str, str] = {}
    for ln in _parse_lines(text):
        for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_.]*)\s*[=:]\s*([^\s,]+)", ln):
            out.setdefault(m.group(1), m.group(2))
    return out


def _parse_first_token(text: str) -> Any:
    lines = _parse_lines(text)
    if not lines:
        return ""
    parts = lines[0].split()
    return parts[0] if parts else ""


_PARSERS: dict[str, Callable[[str], Any]] = {
    "": _parse_raw,
    "raw": _parse_raw,
    "lines": _parse_lines,
    "keyvalue_lines": _parse_keyvalue_lines,
    "first_token": _parse_first_token,
}


def parse_output(text: str, parser: str) -> Any:
    fn = _PARSERS.get((parser or "").strip().lower())
    if fn is None:
        raise LowLevelError(
            f"未知 parse={parser!r}；可用：{sorted(k for k in _PARSERS if k)}"
        )
    return fn(text)


# ---------- 执行 ----------


class LowLevelOutcome:
    """一次 low_level 执行的结果（executor 再包装成 EventResult）。"""

    def __init__(self) -> None:
        self.ok: bool = True
        self.summary: str = ""
        self.error: str = ""
        self.data: dict[str, Any] = {}     # 进 raw_response
        self.commands: list[dict[str, Any]] = []   # 审计用：每条命令与 rc

    def as_raw_response(self) -> dict[str, Any]:
        return {"low_level": self.data, "cmds": self.commands}


ShellRunner = Callable[[str], tuple[int, str, str]]
"""执行一条 shell 的回调：接收完整命令串，返回 (rc, stdout, stderr)。"""


def run_low_level(
    low: dict[str, Any],
    params: dict[str, Any],
    shell: ShellRunner,
    *,
    log_prefix: str = "",
) -> LowLevelOutcome:
    """按 low_level 声明执行，返回 LowLevelOutcome。

    参数
    ----
    low     capability implementation 里的 low_level 字典
    params  事件参数（用于渲染 {placeholder}）
    shell   executor 提供的单条 shell 执行回调
    """
    out = LowLevelOutcome()
    kind = str((low or {}).get("kind") or "").strip().lower()
    if not kind:
        # 兼容只写了 shell 字段、没写 kind 的老声明
        kind = "shell" if low.get("shell") else ""
    try:
        if kind == "shell":
            _run_single(low, params, shell, out)
        elif kind == "shell_batch":
            _run_batch(low, params, shell, out)
        elif kind == "shell_seq":
            _run_seq(low, params, shell, out)
        else:
            raise LowLevelError(
                f"不支持的 low_level.kind={kind!r}；可用：shell / shell_batch / shell_seq"
            )
    except LowLevelError as exc:
        out.ok = False
        out.error = str(exc)
        out.summary = "low_level 声明被拒绝"
        SLog.w(TAG, f"{log_prefix}rejected: {exc}")
    return out


def _exec_one(cmd: str, shell: ShellRunner, out: LowLevelOutcome) -> tuple[int, str, str]:
    assert_command_allowed(cmd)
    rc, stdout, stderr = shell(cmd)
    out.commands.append({"cmd": cmd, "rc": rc})
    return rc, stdout, stderr


def _allowed_rcs(item: dict, default: tuple[int, ...] = (0,)) -> tuple[int, ...]:
    """哪些 rc 算成功。

    取证类命令的非 0 常常是**事实而不是错误**：`pidof` 找不到进程给 1、
    `grep -c` 计数为 0 也给 1。声明 allow_rc: [0, 1] 即把它当正常结果处理。
    """
    raw = item.get("allow_rc")
    if raw is None:
        return default
    if isinstance(raw, (int, str)):
        raw = [raw]
    try:
        return tuple(int(x) for x in raw)
    except (TypeError, ValueError) as exc:
        raise LowLevelError(f"allow_rc 必须是整数或整数列表，得到 {raw!r}") from exc


def _run_single(low: dict, params: dict, shell: ShellRunner, out: LowLevelOutcome) -> None:
    cmd = render_template(str(low.get("shell") or ""), params)
    rc, stdout, stderr = _exec_one(cmd, shell, out)
    parsed = parse_output(stdout, str(low.get("parse") or ""))
    out.data["result"] = parsed
    if rc in _allowed_rcs(low):
        out.summary = str(low.get("summary") or f"执行 {cmd[:60]}")
    else:
        out.ok = False
        out.error = (stderr or stdout or f"rc={rc}")[:240]
        out.summary = f"命令失败 rc={rc}"


def _run_batch(low: dict, params: dict, shell: ShellRunner, out: LowLevelOutcome) -> None:
    commands = low.get("commands") or []
    if not isinstance(commands, list) or not commands:
        raise LowLevelError("low_level.kind=shell_batch 需要非空 commands 列表")
    parser = str(low.get("parse") or "")
    batch_allow = low.get("allow_rc")
    okc = 0
    for item in commands:
        if not isinstance(item, dict):
            raise LowLevelError(f"commands 每项必须是 dict，得到 {type(item).__name__}")
        name = str(item.get("name") or "").strip()
        if not name:
            raise LowLevelError("commands 每项必须有 name")
        cmd = render_template(str(item.get("shell") or ""), params)
        try:
            rc, stdout, stderr = _exec_one(cmd, shell, out)
        except LowLevelError as exc:
            # 单条被拒不影响其它取证项，标错继续（取证要尽力而为）
            out.data[name] = {"error": str(exc)}
            continue
        if item.get("allow_rc") is not None:
            allowed = _allowed_rcs(item)
        elif batch_allow is not None:
            allowed = _allowed_rcs({"allow_rc": batch_allow})
        else:
            allowed = (0,)
        if rc in allowed:
            out.data[name] = parse_output(stdout, str(item.get("parse") or parser))
            okc += 1
        else:
            out.data[name] = {"error": (stderr or stdout or f"rc={rc}")[:200]}
    # 取证类只要有一条成功就算可用（拿到部分事实比整体失败更有价值）
    out.ok = okc > 0
    out.summary = f"采集 {okc}/{len(commands)} 项"
    if not out.ok:
        out.error = "全部采集项均失败"


def _run_seq(low: dict, params: dict, shell: ShellRunner, out: LowLevelOutcome) -> None:
    steps = low.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise LowLevelError("low_level.kind=shell_seq 需要非空 steps 列表")
    for idx, item in enumerate(steps, 1):
        if not isinstance(item, dict):
            raise LowLevelError(f"steps 每项必须是 dict，得到 {type(item).__name__}")
        cmd = render_template(str(item.get("shell") or ""), params)
        rc, stdout, stderr = _exec_one(cmd, shell, out)
        expect_rc = item.get("expect_rc", 0)
        if expect_rc is not None and rc != int(expect_rc):
            out.ok = False
            out.error = (stderr or stdout or f"rc={rc}")[:240]
            out.summary = f"第 {idx}/{len(steps)} 步失败：{cmd[:50]}"
            return
        if item.get("name"):
            out.data[str(item["name"])] = parse_output(stdout, str(item.get("parse") or ""))
    out.summary = str(low.get("summary") or f"顺序执行 {len(steps)} 步完成")
