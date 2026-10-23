"""Runtime control using the open-source Debugger MCP Server plugin."""
import os
import time
from pathlib import Path

import psutil

from mino_scout.ide_client import IDEError
from mino_scout.ide_runtime import inspect_running
from mino_scout import ide_run_control


def _accepts(client, name, fields):
    properties = (client.tools.get(name, {}).get("inputSchema") or {}).get("properties", {})
    return set(fields) <= properties.keys()


def has_state(client):
    return ide_run_control.available(client) or _accepts(client, "list_debug_sessions", {"project_path"})


def has_stop(client):
    return ide_run_control.available(client) or (has_state(client) and _accepts(client, "stop_debug_session", {"project_path", "session_id"}))


def has_restart(client):
    return ide_run_control.available(client) or (has_stop(client) and _accepts(client, "start_debug_session", {"project_path", "configuration_name"}))


def _project(binding):
    return str(Path(binding["project_path"]).expanduser().resolve(strict=True))


def read_state(client, binding):
    if ide_run_control.available(client):
        return ide_run_control.read_state(client, binding)
    raw = client.call("list_debug_sessions", {"projectPath": _project(binding)})
    if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), list):
        raise IDEError("Debugger MCP 未返回有效的调试会话列表")
    active = []
    seen = set()
    for row in raw["sessions"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in seen:
            raise IDEError("Debugger MCP 返回的会话标识无效或重复")
        seen.add(row["id"])
        if row.get("state") not in {"stopped", "running", "paused"}:
            raise IDEError("Debugger MCP 返回了无法识别的会话状态")
        if row["state"] != "stopped":
            active.append(row)
    selected = [row for row in active if row.get("runConfigurationName") == binding["run_configuration"]]
    if len(selected) > 1:
        return {"state": "ambiguous", "scope": "configuration", "configuration_verified": True,
                "configuration_name": binding["run_configuration"], "instances": [], "observed_at": time.time(),
                "reason": "所选配置有多个调试会话，未停止或重复启动"}
    instances = []
    for row in selected:
        pid = row.get("processId") or 0
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
            raise IDEError("Debugger MCP 返回的进程标识无效")
        created = 0
        if pid:
            try:
                created = psutil.Process(pid).create_time()
            except psutil.Error:
                pass
        instances.append({"session_id": row["id"], "pid": pid, "created_at": created,
                          "name": binding["run_configuration"], "debug_state": row["state"]})
    if not selected:
        project_runtime = inspect_running(binding, client.url)
        tracked_pids = {row.get("processId") for row in active if row.get("processId")}
        untracked = [row for row in project_runtime["instances"] if row["pid"] not in tracked_pids]
        if project_runtime["state"] not in {"running", "stopped"} or untracked:
            return {**project_runtime, "reason": "当前配置已有普通 Run 实例；Debugger MCP 不提供普通 Run 停止工具。接入 MCP Run Control 后可直接控制现有实例，无需先改为 Debug。"}
    return {"state": "running" if instances else "stopped", "scope": "configuration", "configuration_verified": True,
            "configuration_name": binding["run_configuration"], "instances": instances, "observed_at": time.time(),
            "reason": "已连接所选配置的调试会话" if instances else "所选配置没有活动调试会话"}


def _ids(runtime):
    return {row["session_id"] for row in runtime["instances"] if row.get("session_id")}


def _gone(instances):
    for row in instances:
        if not row.get("pid") or not row.get("created_at"):
            continue
        try:
            proc = psutil.Process(row["pid"])
            if proc.create_time() == row["created_at"] and proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                return False
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            return False
    return True


def _require_managed(runtime):
    if runtime.get("configuration_verified") is not True or runtime["state"] not in {"running", "stopped"}:
        raise IDEError(runtime["reason"], reason_code="ide_session_unmanaged",
                       remediation="在 PyCharm 接入社区 MCP Run Control；Mino 会复用本机已保存的 PyCharm 原生 MCP 连接，按当前配置接管普通 Run 实例。")


def control(client, binding, operation_id, on_stage, *, action):
    if ide_run_control.available(client):
        return ide_run_control.control(client, binding, operation_id, on_stage, action=action)
    before = read_state(client, binding)
    _require_managed(before)
    # 2026-10-23：与 ide_run_control 同步取消自我拒绝。停止按会话/实例定位，
    # pid 只是观测值；Scout 独立进程与目标配置同目录时会被误认成同一实例。
    context = {"provider": "debugger_mcp", "action": action, "operation_id": operation_id,
               "binding": binding, "previous_instances": before["instances"]}
    client.deadline = time.monotonic() + 115
    if action in {"stop", "restart"} and before["instances"]:
        on_stage("stopping", control={**context, "phase": "stopping"})
        for row in before["instances"]:
            response = client.call("stop_debug_session", {"projectPath": _project(binding), "session_id": row["session_id"]})
            if not isinstance(response, dict) or response.get("sessionId") != row["session_id"] or response.get("status") != "stopped":
                raise IDEError("停止会话回执不匹配，未启动新实例", effect_unknown=True)
        deadline = time.monotonic() + 25
        while True:
            runtime = read_state(client, binding)
            if not (_ids(runtime) & _ids(before)) and _gone(before["instances"]):
                break
            if time.monotonic() >= deadline:
                return {"state": "unknown", "reason": "旧调试会话或进程尚未退出，未启动第二个实例", "control": {**context, "phase": "stopping"}, "runtime": runtime}
            time.sleep(.5)
    if action == "stop":
        runtime = read_state(client, binding)
        if runtime["state"] != "stopped" or runtime.get("configuration_verified") is not True:
            raise IDEError("停止后发现其他运行实例，未将当前配置标记为已停止", effect_unknown=True)
        return {"state": "ready", "stopped": True, "process_running": False, "runtime": runtime,
                "reason": "当前配置的调试会话已停止"}
    runtime = read_state(client, binding)
    _require_managed(runtime)
    if runtime["state"] != "stopped":
        raise IDEError("启动前发现新的调试会话，未重复启动", effect_unknown=True)
    on_stage("starting", control={**context, "phase": "starting"})
    response = client.call("start_debug_session", {"projectPath": _project(binding), "configurationName": binding["run_configuration"]})
    if not isinstance(response, dict) or response.get("status") not in {"started", "starting"}:
        raise IDEError("Debugger MCP 未返回启动回执；不会重复启动", effect_unknown=True)
    deadline = time.monotonic() + 30
    while True:
        runtime = read_state(client, binding)
        if (runtime["state"] == "running" and runtime.get("configuration_verified") is True
                and _ids(runtime) and not (_ids(runtime) & _ids(before)) and _gone(before["instances"])):
            return {"state": "ready", "runtime": runtime, "process_running": True, "restarted": action == "restart",
                    "reused_existing": False, "control": {**context, "phase": "started"},
                    "reason": "当前配置已通过 Debugger MCP 重启" if action == "restart" else "当前配置已通过 Debugger MCP 启动"}
        if time.monotonic() >= deadline:
            return {"state": "unknown", "reason": "调试会话启动结果待确认；不会重复执行", "control": {**context, "phase": "starting"}, "runtime": runtime}
        time.sleep(.5)


def recover(client, context):
    if context.get("provider") == "run_control_mcp":
        if not ide_run_control.available(client):
            raise IDEError("原配置级运行控制工具不可用；不会换用调试工具重复执行")
        return ide_run_control.recover(client, context)
    binding = context["binding"]
    runtime = read_state(client, binding)
    previous = context["previous_instances"]
    old_ids = {row["session_id"] for row in previous}
    if runtime.get("configuration_verified") is True and _gone(previous):
        if context["action"] == "stop" and runtime["state"] == "stopped":
            return {"state": "ready", "stopped": True, "process_running": False, "runtime": runtime, "reason": "原停止操作已确认完成"}
        if context["phase"] in {"starting", "started"} and runtime["state"] == "running" and _ids(runtime) and not (_ids(runtime) & old_ids):
            from mino_scout.ide_prepare import probe
            return {**probe(binding, client.url, runtime=runtime), "restarted": context["action"] == "restart", "reused_existing": False}
    return {"state": "unknown", "runtime": runtime, "reason": "原操作尚未确认完成；只读取状态，不重复停止或启动"}
