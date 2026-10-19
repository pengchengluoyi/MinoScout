"""PyCharm development operations. Nexus owns business state; receipts prevent replay."""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psutil

from mino_scout.config import config_path, load_config, save_config
from mino_scout.ide_client import Client, IDEError, configuration_names, connection_spec, local_url

_lock = threading.RLock()
_progress = contextvars.ContextVar("ide_progress", default=None)


def stage(name):
    callback = _progress.get()
    if callback:
        callback(name)


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
        raise IDEError("此 Scout 尚未保存该 IDE 连接")
    if revision is not None and revision != row["revision"]:
        raise IDEError("连接配置已变更，请重新检查")
    return row


def _listeners(port):
    from mino_scout.ide_stop import listeners
    return listeners(port)


def _old_process(binding, endpoint):
    if sys.platform not in {"darwin", "linux", "win32"}:
        raise IDEError("此平台尚不支持自动停止；请在 IDE 中 Rerun")
    target = urlsplit(local_url(binding["target_url"]))
    from mino_scout.config import development, resolve_runtime
    from mino_scout.ide_stop import protected_ports
    control = urlsplit(resolve_runtime()[0])
    control_port = control.port or (443 if control.scheme == "wss" else 80)
    if target.port in protected_ports(urlsplit(endpoint).port, control_port, source_runtime=development()):
        raise IDEError("测试地址与正式/控制端口冲突，拒绝停止")
    pids = _listeners(target.port)
    if not pids:
        return None
    if len(pids) != 1:
        raise IDEError("测试端口有多个监听进程，请在 IDE 人工处理")
    proc = psutil.Process(pids.pop())
    if proc.pid == os.getpid() or proc.pid in {p.pid for p in psutil.Process().parents()}:
        raise IDEError("禁止停止 Scout 自身或其父进程")
    from mino_scout.ide_stop import same_user
    if not same_user(proc) or Path(proc.cwd()).resolve() != Path(binding["source_dir"]).resolve():
        raise IDEError("端口占用进程的用户或源码目录不匹配")
    ide_pids = _listeners(urlsplit(endpoint).port)
    parents = proc.parents()
    if not any(p.pid in ide_pids and "pycharm" in (p.name() + " " + p.exe()).lower() for p in parents):
        raise IDEError("无法证明应用由当前已连接的 PyCharm 启动")
    return proc, proc.create_time(), target.port


def _stop(client, binding, old):
    if old is None:
        return
    proc, created, port = old
    from mino_scout.config import resolve_runtime
    control = urlsplit(resolve_runtime()[0])
    control_port = control.port or (443 if control.scheme == "wss" else 80)
    # Fixed generated command: no user-provided command, PID+creation time rechecked in IDE.
    args = ["--pid", str(proc.pid), "--created", repr(created), "--port", str(port),
            "--ide-port", str(urlsplit(client.url).port), "--control-port", str(control_port),
            "--source", binding["source_dir"]]
    command = [sys.executable, "internal-ide-stop"] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).with_name("ide_stop.py"))]
    if "execute_terminal_command" in client.tools and os.name != "nt":
        result = client.call("execute_terminal_command", {"projectPath": binding["project_path"],
            "command": shlex.join(command + args),
            "timeout": 10000, "maxLinesCount": 30, "truncateMode": "END", "executeInShell": True})
        if not isinstance(result, dict) or result.get("command_exit_code", result.get("exitCode")) != 0:
            raise IDEError("IDE 停止命令未确认成功；请人工检查，不重复发送", effect_unknown=True)
    else:
        from mino_scout.ide_stop import main as stop_main
        stop_main(args)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE) and not _listeners(port):
            return
        time.sleep(.2)
    raise IDEError("旧进程或端口未释放；未使用 SIGKILL，也未启动新进程", effect_unknown=True)


def probe(binding):
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
    return {**result, "receipt_verified": True, "receipt_operation_id": target}


def sample_project(path):
    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise IDEError("样例项目必须是已有目录")
    scout_root = Path(__file__).resolve().parent.parent
    if root == scout_root or root in scout_root.parents or root.is_relative_to(scout_root):
        raise IDEError("开发 Scout 不允许把自身代码目录作为样例项目")
    if any((root / marker).exists() for marker in ("mino_nexus", "mino_scout", "packaging/workbench_server.py")):
        raise IDEError("MINO 控制链源码目录不能登记为测试 Scout 的样例项目")
    return root


def require_sample_project(binding):
    from mino_scout.config import development
    if not development():
        return
    project = sample_project(binding["project_path"])
    source = Path(binding["source_dir"]).expanduser().resolve(strict=True)
    allowed = load_config().get("development_ide_projects", [])
    if not isinstance(allowed, list) or project not in [sample_project(path) for path in allowed]:
        raise IDEError("开发 Scout 仅能操作专用样例项目；请先用 development-ide-projects --add 登记")
    if not source.is_relative_to(project) or not source.is_dir():
        raise IDEError("测试源码目录必须位于已登记的样例项目内")
    sample_project(source)


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
    require_sample_project(binding)
    connection = _connection(binding["connection_id"], binding["connection_revision"])
    if action == "probe":
        stage("probing")
        return probe(binding)
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
    lock = nullcontext() if action == "check" else machine_lock(connection["url"])
    stage("connecting")
    with lock, Client(connection) as client:
        result = client.call("get_run_configurations", {"projectPath": binding["project_path"]})
        names = configuration_names(result)
        check = {"connected": True, "project_matched": True, "configurations": names,
                 "server": client.server, "tools": sorted(client.tools), "brave": "unknown",
                 "checked_at": time.time(), "connection_revision": connection["revision"],
                 "support": {"read": True, "execute": "execute_run_configuration" in client.tools,
                             "restart": sys.platform in {"darwin", "linux"} and "execute_run_configuration" in client.tools,
                             "restart_mode": "window_close_only" if os.name == "nt" else "sigterm",
                             "logs": bool(binding.get("service_log_path"))},
                 "details_verified": False}
        if action == "check":
            return {"state": "ready", **check, "target_ready": False}
        if action not in {"execute", "restart", "build", "install"}:
            raise IDEError("不支持的开发准备动作")
        name = binding.get("build_configuration") if action == "build" else binding.get("run_configuration")
        if not name or name not in names:
            raise IDEError("所选运行配置不在当前项目中，请重新检查")
        if not binding.get("execution_allowed") or not binding.get("test_parameters_confirmed"):
            raise IDEError("尚未授权应用执行或确认测试参数")
        if action == "restart":
            stage("locating_process")
            old = _old_process(binding, connection["url"])
            stage("stopping" if old else "no_old_process")
            _stop(client, binding, old)
        stage("building" if action == "build" else "starting")
        result = client.call("execute_run_configuration", {"configurationName": name,
            "projectPath": binding["project_path"], "timeout": 5000, "maxLinesCount": 200, "truncateMode": "END"})
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
        return {**probe(binding), "receipt": receipt}


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
        mutation = params.get("action") in {"execute", "restart", "build", "install"}
        admitted = False
        stages = []
        def record_stage(name):
            stages.append({"name": name, "at": time.time()})
            _save_receipt(path, {"digest": digest, "result": {"state": "running", "stage": name, "stages": list(stages)}})
        progress_token = _progress.set(record_stage)
        try:
            if mutation:
                with core._lock:
                    if core._active_runs or getattr(core, "_development_io", 0) or getattr(core, "_development_busy", False):
                        raise IDEError("有活动测试或准备操作，请等待结束后发起新的准备操作")
                    core._development_busy = True
                    admitted = True
            data = perform(params)
        except (httpx.TimeoutException, httpx.TransportError):
            data = {"state": "unknown" if mutation else "failed", "reason": "IDE 无回执，执行结果尚未确认；不会自动重试"}
        except Exception as exc:
            effect_started = any(s["name"] in {"stopping", "starting", "building", "installing"} for s in stages)
            known_rejection = isinstance(exc, IDEError) and not exc.effect_unknown and not effect_started
            state = ("awaiting_developer" if known_rejection else "unknown") if admitted else "failed"
            data = {"state": state,
                    "reason": str(exc)[:2000] if isinstance(exc, IDEError) else type(exc).__name__}
        finally:
            _progress.reset(progress_token)
            if admitted:
                with core._lock:
                    core._development_busy = False
        data = sanitize({**data, "stages": stages} if stages else data)
        with _lock:
            _save_receipt(path, {"digest": digest, "result": data})
    return make_event_result(_event_from_execute(req), status=EventStatus.PASS, executor_used="ide",
        started_at=now_iso(), elapsed_ms=0, summary=data.get("reason", data["state"]), raw_response={"development": data})
