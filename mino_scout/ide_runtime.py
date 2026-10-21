"""Read-only discovery of project processes owned by the connected PyCharm."""
from pathlib import Path
import time
from urllib.parse import urlsplit

import psutil

from mino_scout.ide_client import local_url
from mino_scout.ide_stop import listeners, same_user


def _auxiliary_process(proc, ide_home):
    if proc.name().lower() in {"sh", "bash", "zsh", "fish", "cmd.exe", "powershell.exe", "pwsh"}:
        return True
    script = next((Path(argument) for argument in proc.cmdline()[1:] if argument.endswith(".py")), None)
    return bool(script and script.name == "pydevconsole.py" and script.is_relative_to(ide_home))


def inspect_running(binding, endpoint):
    base = {"scope": "project", "configuration_verified": False, "observed_at": time.time(), "instances": []}
    try:
        project = Path(binding["project_path"]).expanduser().resolve(strict=True)
        source = Path(binding["source_dir"]).expanduser().resolve(strict=True)
        ide_port = urlsplit(local_url(endpoint)).port
        owners = [psutil.Process(pid) for pid in listeners(ide_port)]
        owners = [proc for proc in owners if same_user(proc) and "pycharm" in (proc.name() + " " + proc.exe()).lower()]
        if len(owners) != 1:
            return {**base, "state": "unknown", "reason": "无法确认当前 MCP 端口所属的 PyCharm，未判断为可安全启动"}
        ide_home = Path(owners[0].exe()).resolve().parent.parent
        candidates = {}
        incomplete = False
        for proc in owners[0].children(recursive=True):
            try:
                if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE or not same_user(proc):
                    continue
                if _auxiliary_process(proc, ide_home):
                    continue
                cwd = Path(proc.cwd()).resolve()
                if cwd != project and not cwd.is_relative_to(source):
                    continue
                candidates[proc.pid] = {"process": proc, "parent": proc.ppid(), "name": proc.name(), "created_at": proc.create_time()}
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError):
                incomplete = True
        roots = {}
        for pid, candidate in candidates.items():
            root = pid
            visited = set()
            while candidates[root]["parent"] in candidates and root not in visited:
                visited.add(root)
                root = candidates[root]["parent"]
            roots.setdefault(root, set()).add(pid)
        if binding.get("target_url"):
            target_port = urlsplit(local_url(binding["target_url"])).port
            target_pids = listeners(target_port)
            if target_pids:
                matched = {pid: members for pid, members in roots.items() if members & target_pids}
                if not matched:
                    return {**base, "state": "conflict", "reason": "健康检查端口已被其他项目或非当前 PyCharm 管理的进程占用"}
                roots = matched
        instances = [{"pid": pid, "name": candidates[pid]["name"], "created_at": candidates[pid]["created_at"]} for pid in sorted(roots)]
        if len(instances) > 1:
            return {**base, "state": "ambiguous", "instances": instances, "reason": "项目中有多个 PyCharm 运行实例，无法确定当前配置对应哪一个；未重复启动"}
        if instances:
            return {**base, "state": "running", "instances": instances, "reason": "已发现当前项目由 PyCharm 管理的运行实例；未证明其配置名与当前选择相同"}
        if incomplete:
            return {**base, "state": "unknown", "reason": "部分 PyCharm 子进程不可读取，无法排除已有实例；未重复启动"}
        return {**base, "state": "stopped", "reason": "未发现当前项目由 PyCharm 管理的运行实例"}
    except (psutil.Error, OSError, ValueError, RuntimeError) as error:
        return {**base, "state": "unknown", "reason": f"无法读取项目运行状态（{type(error).__name__}）；未重复启动"}
