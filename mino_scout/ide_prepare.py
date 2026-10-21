"""PyCharm development operations. Nexus owns business state; receipts prevent replay."""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from mino_scout.config import config_path, load_config, save_config
from mino_scout.ide_client import Client, IDEError, configuration_names, connection_spec, local_url
from mino_scout.ide_runtime import inspect_running
from mino_scout.ide_run_control import available as has_run_control, connected as runtime_connection
from mino_scout.ide_control import has_state, has_restart, read_state, control as control_configuration, recover as recover_control

_lock = threading.RLock()
_progress = contextvars.ContextVar("ide_progress", default=None)


def stage(name, **details):
    callback = _progress.get()
    if callback:
        callback(name, **details)


def redact_output(text):
    from mino_scout.secret_redaction import register_inputs, sanitize
    register_inputs(load_config())
    text = sanitize(text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    pattern = r"(?i)([\"']?(?:token|password|secret|api[_-]?key)[\"']?\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    return re.sub(pattern, r"\1[REDACTED]", text)


def public_connection(row):
    return {k: v for k, v in row.items() if k != "header_env"} | {
        "authentication": "environment" if row.get("header_env") else "none",
        "auth_env_names": list(row.get("header_env", {}).values())}



@contextmanager
def machine_lock(endpoint):
    # Same user / same IDE across formal and test Scouts. No network listener.
    if os.name == "nt":
        import msvcrt
        origin = urlsplit(endpoint)
        key = hashlib.sha256(f"{origin.hostname}:{origin.port}".encode()).hexdigest()[:24]
        path = Path(tempfile.gettempdir()) / f"mino-ide-{key}.lock"
        if path.is_symlink():
            raise IDEError("IDE 锁不能为符号链接")
        with path.open("a+b") as stream:
            if os.fstat(stream.fileno()).st_nlink != 1:
                raise IDEError("IDE 锁不能为硬链接")
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise IDEError("另一 Scout 正在准备同一 IDE")
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    if os.name != "posix":
        raise IDEError("此平台不支持开发互斥")
    import fcntl
    origin = urlsplit(endpoint)
    key = hashlib.sha256(f"{origin.hostname}:{origin.port}".encode()).hexdigest()[:24]
    path = Path(tempfile.gettempdir()) / f"mino-ide-{os.getuid()}-{key}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_nlink != 1:
            raise IDEError("IDE 锁文件身份不符")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IDEError("另一 Scout 正在准备同一 IDE，请等待其完成")
        yield
    finally:
        os.close(fd)


def _save_receipt(path, data):
    from mino_scout.secret_redaction import sanitize
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as stream:
        json.dump(sanitize(data), stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _connection(cid, revision=None):
    config = load_config()
    row = next((r for r in config.get("ide_connections", []) if r["connection_id"] == cid), None)
    if not row:
        raise IDEError("此 Scout 尚未保存该 IDE 连接", reason_code="ide_connection_missing",
                       remediation="通过“更换 IDE 连接”选择这台开发电脑已有的连接，或导入当前 PyCharm 的 HTTP 流配置，然后保存并检查。", error_field="connection_id")
    if revision is not None and revision != row["revision"]:
        raise IDEError("已保存的 IDE 连接版本发生变化", reason_code="ide_connection_changed",
                       remediation="打开“更换 IDE 连接”，刷新并重新选择当前连接，保存后再次检查。", error_field="connection_id")
    return row


def probe(binding, endpoint=None, *, runtime=None):
    if endpoint:
        runtime = runtime if runtime is not None else inspect_running(binding, endpoint)
    observation = {"runtime": runtime} if runtime is not None else {}
    if runtime is not None and runtime["state"] in {"running", "stopped"}:
        observation["process_running"] = runtime["state"] == "running"
    if runtime is not None and runtime["state"] == "running":
        observation["reused_existing"] = True
    if binding.get("target_url"):
        result = _probe_health(binding)
        return {**result, **observation}
    if runtime is None:
        return {"state": "awaiting_developer", "reason": "尚未读取运行实例；可检查项目运行状态或配置健康检查地址"}
    running = runtime["state"] == "running"
    return {"state": "ready" if running else "awaiting_developer", **observation,
            "reason": "已使用现有实例，进程运行正常" if running else runtime["reason"],
            "build_verified": False}


def _probe_health(binding):
    url = binding.get("target_url")
    if not url:
        return {"state": "awaiting_developer", "reason": "未配置服务就绪地址，请读取 IDE 输出并人工核对", "target_ready": False}
    url = local_url(url)
    with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as http:
        end = time.monotonic() + 15
        last = "目标尚未就绪"
        while time.monotonic() < end:
            try:
                with http.stream("GET", url) as response:
                    if response.status_code == 200:
                        expected = binding.get("expected_build", "")
                        expected_instance = binding.get("expected_instance_id", "")
                        if not expected and not expected_instance:
                            return {"state": "ready", "target_ready": True, "build_verified": False,
                                    "reason": "服务可访问；未证明本轮代码已生效，也不代表测试通过"}
                        content = b""
                        for part in response.iter_bytes():
                            content += part
                            if len(content) > 65536:
                                raise IDEError("版本探测响应过大")
                        document = json.loads(content)
                        identity = document.get("data", document) if isinstance(document, dict) else {}
                        actual_instance = identity.get("instance_id", "") if isinstance(identity, dict) else ""
                        evidence = {"expected_instance_id": expected_instance, "actual_instance_id": str(actual_instance)[:100],
                                    "instance_verified": bool(expected_instance and actual_instance == expected_instance)}
                        if expected_instance and actual_instance != expected_instance:
                            return {"state": "failed", "target_ready": True, "build_verified": False, **evidence,
                                    "reason_code": "instance_mismatch", "reason": "目标服务实例与预期不符"}
                        if isinstance(identity, dict) and identity.get("source_unchanged") is False:
                            return {"state": "failed", "target_ready": True, "build_verified": False, **evidence,
                                    "reason_code": "source_changed", "reason": "目标进程启动后源码已变化，请重启后重新核对"}
                        if not expected:
                            return {"state": "ready", "target_ready": True, "build_verified": False, **evidence,
                                    "reason": "实例已核对；尚未指定预期 build，也未运行用例"}
                        value = document
                        for key in binding.get("build_field", "build_id").split("."):
                            value = value.get(key) if isinstance(value, dict) else None
                        if value is None and binding.get("build_field", "build_id") == "build_id" and isinstance(identity, dict):
                            value = identity.get("build_id")
                        if value not in (None, "", "source-unpackaged") and str(value) == expected:
                            return {"state": "ready", "target_ready": True, "build_verified": True, **evidence, "build": expected, "actual_build": str(value), "reason": "服务就绪，实际 build 已与预期核对；尚未运行用例"}
                        return {"state": "failed", "target_ready": True, "build_verified": False, **evidence, "expected_build": expected, "actual_build": str(value)[:200], "reason_code": "build_mismatch", "reason": "服务可访问，但实际 build 与预期不匹配"}
                    if response.status_code in {401, 403}:
                        return {"state": "failed", "target_ready": False, "build_verified": False,
                                "reason_code": "target_auth_failed", "reason": "目标服务拒绝访问，请在测试 Workbench 使用测试账号登录"}
                    last = f"目标 HTTP {response.status_code}"
            except (httpx.HTTPError, ValueError):
                last = "目标暂不可访问或版本响应格式无效"
            time.sleep(.5)
        return {"state": "awaiting_developer", "target_ready": False, "reason": last}


def read_receipt(params):
    target = str(params.get("target_operation_id", ""))
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", target):
        raise IDEError("原操作 ID 无效")
    path = config_path().parent / "ide_receipts" / (target + ".json")
    if not path.is_file():
        return {"state": "unknown", "reason": "节点没有原操作回执；不会重新执行"}
    record = json.loads(path.read_text())
    if record.get("digest") != params.get("target_digest"):
        raise IDEError("原操作参数摘要不匹配")
    result = record.get("result") or {"state": "unknown", "reason": "原操作没有最终回执；不会重新执行"}
    control = result.get("control")
    if isinstance(control, dict) and control.get("provider") in {"debugger_mcp", "run_control_mcp"} and result.get("state") in {"running", "unknown"}:
        binding = control["binding"]
        try:
            with Client(_connection(binding["connection_id"], binding["connection_revision"])) as primary, runtime_connection(primary, expected=control.get("control_connection")) as client:
                recovered = recover_control(client, control)
            result = {**result, **recovered}
            _save_receipt(path, {"digest": record["digest"], "result": result})
        except (IDEError, httpx.HTTPError):
            pass
    return {**result, "receipt_verified": True, "receipt_operation_id": target}


def sample_project(path, *, field="project_path"):
    label = "IDE 项目路径" if field == "project_path" else "应用源码目录"
    try:
        root = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise IDEError(f"{label}不存在或无法访问：{path}", reason_code="ide_directory_invalid",
                       remediation=f"将“{label}”改为所选开发电脑上实际存在、当前 Scout 账号可访问的目录绝对路径。", error_field=field) from exc
    if not root.is_dir():
        raise IDEError(f"{label}指向文件而不是目录：{path}", reason_code="ide_directory_invalid",
                       remediation=f"将“{label}”改为目录路径，不要填写文件路径。", error_field=field)
    return root


def validate_project_paths(binding):
    project = sample_project(binding["project_path"])
    source = sample_project(binding["source_dir"], field="source_dir")
    if not source.is_relative_to(project):
        raise IDEError("应用源码目录不在 IDE 项目目录内", reason_code="ide_source_outside_project",
                       remediation="将“应用源码目录”设为 IDE 项目根目录或其子目录；如果应用属于另一个项目，请同时修改“IDE 项目路径”。", error_field="source_dir")


def perform(params):
    action = params["action"]
    if action == "receipt":
        return read_receipt(params)
    cid = str(params.get("connection_id", ""))
    if action == "save_connection":
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", cid):
            raise IDEError("connection_id 无效")
        spec = connection_spec(params["connection"])
        with _lock:
            path = config_path()
            if path.exists():
                # Do not replace an unreadable/corrupt config with an empty one.
                cfg = json.loads(path.read_text(encoding="utf-8-sig"))
            else:
                cfg = {}
            rows = cfg.get("ide_connections", [])
            old = next((r for r in rows if r["connection_id"] == cid), {})
            if params.get("connection_revision", 0) != old.get("revision", 0):
                raise IDEError("连接已被修改，请刷新后保存")
            row = {**spec, "connection_id": cid, "revision": old.get("revision", 0) + 1}
            cfg["ide_connections"] = [r for r in rows if r["connection_id"] != cid] + [row]
            save_config(cfg)
            return {"state": "ready", "connection": public_connection(row), "reason": "已保存到节点，尚未检查连接"}
    if action == "list_connections":
        return {"state": "ready", "connections": [public_connection(r) for r in load_config().get("ide_connections", [])]}
    binding = params["binding"]
    validate_project_paths(binding)
    connection = _connection(binding["connection_id"], binding["connection_revision"])
    if action == "probe":
        stage("probing")
        if binding.get("run_configuration"):
            try:
                with Client(connection) as primary, runtime_connection(primary) as client:
                    if has_state(client):
                        return probe(binding, connection["url"], runtime=read_state(client, binding))
            except (httpx.TransportError, httpx.TimeoutException):
                pass
        return probe(binding, connection["url"])
    if action == "logs":
        stage("reading_logs")
        source = Path(binding["source_dir"]).resolve(strict=True)
        path = Path(binding.get("service_log_path", "")).resolve(strict=True)
        if not path.is_relative_to(source) or not path.is_file():
            raise IDEError("只允许读取本应用源码目录内已登记的服务日志")
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 32768))
            text = stream.read(32768).decode("utf-8", errors="replace")
        text = redact_output(text)
        return {"state": "ready", "receipt": {"source": "configured_service_log", "output": text,
                "truncated": size > 32768, "byte_offset": max(0, size - 32768)}}
    if action == "install":
        if not binding.get("execution_allowed") or not binding.get("test_parameters_confirmed"):
            raise IDEError("尚未授权应用安装或确认测试参数")
        from mino_scout.ide_apk import install
        stage("validating_apk")
        return install(binding, on_stage=stage)
    stage("connecting")
    from contextlib import ExitStack
    with ExitStack() as stack:
        primary = stack.enter_context(Client(connection))
        client = stack.enter_context(runtime_connection(primary))
        if action != "check":
            stack.enter_context(machine_lock(client.url))
        if action == "execute":
            stage("inspecting_runtime")
            runtime = read_state(client, binding) if has_state(client) and binding.get("run_configuration") else inspect_running(binding, connection["url"])
            if runtime["state"] == "running":
                return probe(binding, connection["url"], runtime=runtime)
            if runtime["state"] != "stopped":
                return {"state": "awaiting_developer", "runtime": runtime, "reason_code": "ide_runtime_unconfirmed", "reason": runtime["reason"]}
        stage("matching_project")
        result = client.call("get_run_configurations", {"projectPath": binding["project_path"]})
        names = configuration_names(result)
        configuration_details = result.get("configurations", []) if isinstance(result, dict) else []
        selected_configuration = next((row for row in configuration_details if isinstance(row, dict) and row.get("name") == binding.get("run_configuration")), {})
        debuggable = selected_configuration.get("canDebug") is not False
        native_control = has_run_control(client)
        runtime = read_state(client, binding) if has_state(client) and binding.get("run_configuration") in names else inspect_running(binding, client.url)
        managed = runtime.get("configuration_verified") is True
        restart_supported = has_restart(client) and (native_control or (debuggable and managed))
        check = {"connected": True, "project_matched": True, "configurations": names,
                 "server": client.server, "tools": sorted(client.tools), "brave": "unknown",
                 "checked_at": time.time(), "connection_revision": connection["revision"],
                 "support": {"read": True, "execute": "execute_run_configuration" in client.tools,
                             "restart": restart_supported, "stop": restart_supported, "restart_mode": "run_control_mcp" if native_control else "debugger_mcp" if restart_supported else "run_control_required" if has_state(client) and not managed else "unsupported_configuration" if has_state(client) and not debuggable else "run_control_required", "inspect_running": True,
                             "restart_reason": "通过 IDE 配置级工具停止当前 Run/Debug 实例、启动同一配置并确认新实例" if native_control else "通过 Debugger MCP 控制已确认的调试会话" if restart_supported else "当前连接只有调试会话控制；普通 Run 和不支持 Debug 的配置需要社区 MCP Run Control，不能仅凭插件已连接就认为可重启",
                             "logs": bool(binding.get("service_log_path"))},
                 "control_connection": getattr(client, "control_connection", None), "details_verified": False}
        if action == "check":
            check["configuration_details"] = result.get("configurations", []) if isinstance(result, dict) else []
            return {"state": "ready", **check, "runtime": runtime}
        if action not in {"execute", "stop", "restart", "build", "install"}:
            raise IDEError("不支持的开发准备动作")
        name = binding.get("build_configuration") if action == "build" else binding.get("run_configuration")
        if not name or names.count(name) != 1:
            raise IDEError("所选运行配置不在当前项目中，请重新检查")
        if not binding.get("execution_allowed") or not binding.get("test_parameters_confirmed"):
            raise IDEError("尚未授权应用执行或确认测试参数")
        if action in {"stop", "restart"} or (action == "execute" and restart_supported):
            if not restart_supported:
                return {"state": "awaiting_developer", "reason_code": "ide_run_control_required",
                        "reason": check["support"]["restart_reason"], "remediation": "在 PyCharm 安装社区 MCP Run Control，启用原生 MCP。已保存的同一 PyCharm 原生连接会自动复用，无需改变已有 Run 配置或重复粘贴连接。"}
            details = result.get("configurations", []) if isinstance(result, dict) else []
            selected = next((row for row in details if isinstance(row, dict) and row.get("name") == name), {})
            if not native_control and action != "stop" and selected.get("canDebug") is False:
                raise IDEError("所选 IDE 配置不支持调试会话，Debugger MCP 无法为它提供停止和重启", reason_code="ide_debug_unsupported")
            result = control_configuration(client, binding, params["operation_id"], stage, action=action)
            if result["state"] != "ready" or action == "stop":
                return result
            stage("probing")
            verified = probe(binding, connection["url"], runtime=result["runtime"])
            return {**result, **verified, "reused_existing": False,
                    "reason": result["reason"] if verified["state"] == "ready" else verified["reason"]}
        if action == "execute":
            stage("inspecting_runtime")
            runtime = read_state(client, binding) if has_state(client) else inspect_running(binding, connection["url"])
            if runtime["state"] == "running":
                return {**probe(binding, connection["url"], runtime=runtime), "reused_existing": True}
            if runtime["state"] != "stopped":
                return {"state": "awaiting_developer", "runtime": runtime, "reason_code": "ide_runtime_unconfirmed",
                        "reason": runtime["reason"], "remediation": "先核对已运行实例；多个配置共用项目时可补充健康检查地址以定位目标实例，再检查运行状态。"}
        stage("building" if action == "build" else "starting")
        result = client.call("execute_run_configuration", {"configurationName": name,
            "projectPath": binding["project_path"], "mode": "run", "timeout": 5000, "maxLinesCount": 200, "truncateMode": "END"})
        if not isinstance(result, dict):
            raise IDEError("IDE 执行回执格式不支持")
        output = str(result.get("output", ""))
        output = redact_output(output)
        receipt = {"source": "pycharm.execute_run_configuration", "output": output[-20000:],
                   "truncated": len(output) > 20000 or len(output.splitlines()) >= 200,
                   "timedOut": result.get("timedOut"), "exitCode": result.get("exitCode")}
        if result.get("exitCode") not in (None, 0):
            return {"state": "failed", "reason": "运行配置非零退出；请检查输出和端口占用", "receipt": receipt}
        if action == "build":
            return {"state": "ready" if result.get("exitCode") == 0 else "unknown",
                    "reason": "构建回执；尚未安装或执行测试", "receipt": receipt}
        stage("probing")
        return {**probe(binding, connection["url"]), "receipt": receipt}


def dispatch(core, req):
    from mino_scout.core import _event_from_execute
    from mino_scout.executors.base import make_event_result, now_iso
    from mino_scout.schemas import EventStatus
    from mino_scout.secret_redaction import sanitize
    params = req.params
    opid = str(params.get("operation_id", ""))
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", opid):
        data = {"state": "failed", "reason": "缺少稳定 operation_id"}
    else:
        directory = config_path().parent / "ide_receipts"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / (opid + ".json")
        digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        with _lock:
            if path.exists():
                old = json.loads(path.read_text())
                data = old.get("result") or {"state": "unknown", "reason": "已有请求未确认，不重复执行"}
                if old["digest"] != digest:
                    data = {"state": "failed", "reason": "operation_id 参数冲突"}
                return make_event_result(_event_from_execute(req), status=EventStatus.PASS, executor_used="ide",
                    started_at=now_iso(), elapsed_ms=0, summary=data.get("reason", data["state"]), raw_response={"development": data})
            try:
                with path.open("x") as marker:
                    os.chmod(path, 0o600)
                    json.dump({"digest": digest}, marker)
                    marker.flush()
                    os.fsync(marker.fileno())
            except FileExistsError:
                data = {"state": "unknown", "reason": "同一操作已有跨进程请求，未重复执行"}
                return make_event_result(_event_from_execute(req), status=EventStatus.PASS,
                    executor_used="ide", started_at=now_iso(), elapsed_ms=0,
                    summary=data["reason"], raw_response={"development": data})
        mutation = params.get("action") in {"execute", "stop", "restart", "build", "install"}
        admitted = False
        stages = []
        checkpoint = {}
        def record_stage(name, **details):
            checkpoint.update(details)
            stages.append({"name": name, "at": time.time()})
            _save_receipt(path, {"digest": digest, "result": {"state": "running", "stage": name, "stages": list(stages), **checkpoint}})
        progress_token = _progress.set(record_stage)
        try:
            if mutation:
                with core._lock:
                    if core._active_runs or getattr(core, "_development_io", 0) or getattr(core, "_development_busy", False):
                        raise IDEError("有活动测试或准备操作，请等待结束后发起新的准备操作")
                    core._development_busy = True
                    admitted = True
            data = perform(params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            data = {"state": "unknown" if mutation else "failed",
                    "reason": "IDE 无回执，执行结果尚未确认；不会自动重试" if mutation else "PyCharm 响应超时" if isinstance(exc, httpx.TimeoutException) else "无法连接 PyCharm 的 MCP 服务",
                    "reason_code": "ide_result_unknown" if mutation else "ide_timeout" if isinstance(exc, httpx.TimeoutException) else "ide_unreachable",
                    "remediation": "先在 PyCharm 核对当前配置是否已执行，再读取本次操作的原回执；不要重复启动。" if mutation else "确认 PyCharm 已打开并启用 MCP 服务器，核对所选开发电脑和 HTTP 流配置的端口、路径；如 PyCharm 显示授权请求，请允许连接后重新检查。",
                    "error_field": "connection_id"}
        except Exception as exc:
            effect_started = any(s["name"] in {"stopping", "starting", "building", "installing"} for s in stages)
            known_rejection = isinstance(exc, IDEError) and not exc.effect_unknown and not effect_started
            state = ("awaiting_developer" if known_rejection else "unknown") if admitted else "failed"
            data = {"state": state,
                    "reason": str(exc)[:2000] if isinstance(exc, IDEError) else type(exc).__name__}
            if isinstance(exc, IDEError):
                data.update({key: getattr(exc, key) for key in ("reason_code", "remediation", "error_field") if getattr(exc, key)})
        finally:
            _progress.reset(progress_token)
            if admitted:
                with core._lock:
                    core._development_busy = False
        data = sanitize({**checkpoint, **data, "stages": stages} if stages else data)
        with _lock:
            _save_receipt(path, {"digest": digest, "result": data})
    return make_event_result(_event_from_execute(req), status=EventStatus.PASS, executor_used="ide",
        started_at=now_iso(), elapsed_ms=0, summary=data.get("reason", data["state"]), raw_response={"development": data})
