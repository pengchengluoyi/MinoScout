"""Configuration-level control through the community MCP Run Control toolset."""
from contextlib import contextmanager
from pathlib import Path
import os
import time
from urllib.parse import urlsplit

import psutil
import httpx

from mino_scout.ide_client import Client, IDEError
from mino_scout.ide_runtime import inspect_running


def available(client):
    tools = client.tools
    stop = (tools.get('stop_run_configuration', {}).get('inputSchema') or {}).get('properties', {})
    execute = (tools.get('execute_run_configuration', {}).get('inputSchema') or {}).get('properties', {})
    return ('list_running_run_configurations' in tools and {'sessionId', 'configurationName', 'stopAllMatches'} <= stop.keys()
            and 'configurationName' in execute and 'projectPath' in execute)


def _ide_identity(endpoint):
    from mino_scout.ide_stop import listeners, same_user
    try:
        owners = [psutil.Process(pid) for pid in listeners(urlsplit(endpoint).port)]
        owners = [p for p in owners if same_user(p) and 'pycharm' in (p.name() + ' ' + p.exe()).lower()]
        return (owners[0].pid, owners[0].create_time()) if len(owners) == 1 else None
    except (psutil.Error, OSError, ValueError):
        return None


@contextmanager
def connected(primary, *, expected=None):
    """Reuse a saved native connection to the same IDE; never probe guessed ports."""
    if available(primary):
        if expected and expected.get('url') != primary.url:
            raise IDEError('原运行控制连接已变化，只读取原回执，不重复执行')
        primary.control_connection = {'url': primary.url}
        yield primary
        return
    from mino_scout.config import load_config
    identity = _ide_identity(primary.url)
    if identity:
        candidates = [r for r in load_config().get('ide_connections', [])
                      if r.get('url') != primary.url and _ide_identity(r.get('url', '')) == identity]
        for row in candidates:
            metadata = {k: row[k] for k in ('connection_id', 'revision', 'url')}
            if expected and metadata != expected:
                continue
            candidate = Client(row)
            try:
                candidate.__enter__()
            except (IDEError, OSError, httpx.HTTPError):
                candidate.__exit__(None, None, None)
                continue
            try:
                if available(candidate):
                    candidate.control_connection = metadata
                    yield candidate
                    return
            finally:
                candidate.__exit__(None, None, None)
    if expected:
        raise IDEError('原运行控制连接暂不可用；不会换连接重复执行')
    yield primary


def _canonical(value):
    return str(Path(value).expanduser().resolve()) if isinstance(value, str) and value else ''


def read_state(client, binding):
    project = _canonical(binding['project_path'])
    name = binding['run_configuration']
    raw = client.call('list_running_run_configurations', {'projectPath': project})
    # The upstream tool returns a JSON string through the native MCP wrapper.
    if isinstance(raw, dict) and isinstance(raw.get('output'), str):
        import json
        try:
            raw = json.loads(raw['output'])
        except ValueError as exc:
            raise IDEError('IDE 运行会话列表格式无效') from exc
    if not isinstance(raw, list):
        raise IDEError('IDE 未返回有效的运行会话列表')
    project_sessions = []
    seen = set()
    for row in raw:
        if not isinstance(row, dict) or not isinstance(row.get('sessionId'), str) or not row['sessionId'] or row['sessionId'] in seen:
            raise IDEError('IDE 返回的运行会话标识无效或重复')
        seen.add(row['sessionId'])
        if _canonical(row.get('projectPath')) == project:
            project_sessions.append(row)
    selected = [r for r in project_sessions if (r.get('configurationName') or r.get('displayName')) == name]
    instances = [{'session_id': r['sessionId'], 'pid': 0, 'created_at': 0, 'name': name,
                  'execution_mode': 'debug' if 'Debug' in r.get('executorIds', []) else 'run'} for r in selected]
    # The native session is always the control target. A unique observed source process
    # is used solely to verify actual exit. Other configurations may share the IDE project
    # while running in different source directories.
    observed = inspect_running(binding, client.url)
    snapshot = observed.get('instances', [])
    if len(instances) == 1 and len(snapshot) == 1 and observed['state'] == 'running':
        instances[0].update({k: snapshot[0][k] for k in ('pid', 'created_at')})
    return {'state': 'running' if len(instances) == 1 else 'ambiguous' if instances else 'stopped',
            'scope': 'configuration', 'configuration_verified': True, 'configuration_name': name,
            'instances': instances, 'project_instances': snapshot, 'project_process_state': observed['state'],
            'project_session_ids': [r['sessionId'] for r in project_sessions], 'observed_at': time.time(),
            'reason': 'IDE 已确认当前配置的运行实例' if len(instances) == 1 else '当前配置有多个运行实例' if instances else '当前配置未运行'}


def _ids(runtime):
    return {r['session_id'] for r in runtime['instances']}


def _alive(row):
    if not row.get('pid') or not row.get('created_at'):
        return None
    try:
        proc = psutil.Process(row['pid'])
        return proc.create_time() == row['created_at'] and proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return None


def terminated(before, after):
    if _ids(before) & _ids(after):
        return False
    if not before['instances']:
        return True
    known = [_alive(r) for r in before['instances']]
    if all(alive is False for alive in known):
        return True
    # Upstream hides terminating descriptors. Do not treat a missing session as process exit.
    # If every project session had one observed process root, verify precisely one root exited
    # and all the other original sessions and process roots remain unchanged.
    old = before.get('project_instances', [])
    other_ids = set(before.get('project_session_ids', [])) - _ids(before)
    if (len(before['instances']) <= len(old) <= len(before.get('project_session_ids', [])) and old
            and before.get('project_process_state') in {'running', 'ambiguous'}
            and after.get('project_process_state') in {'stopped', 'running', 'ambiguous'}
            and other_ids == set(after.get('project_session_ids', []))):
        statuses = [_alive(r) for r in old]
        if any(status is None for status in statuses):
            return False
        remaining = [r for r, alive in zip(old, statuses) if alive]
        current = {(r['pid'], r['created_at']) for r in after.get('project_instances', [])}
        return (len(remaining) == len(old) - len(before['instances'])
                and len(remaining) == len(current)
                and {(r['pid'], r['created_at']) for r in remaining} == current)
    return False


def control(client, binding, operation_id, on_stage, *, action):
    before = read_state(client, binding)
    if before['state'] == 'ambiguous':
        raise IDEError('当前配置在 IDE 中有多个活动会话，未选择其他配置或停止所有匹配实例')
    if action == 'execute' and before['state'] == 'running':
        return {'state': 'ready', 'runtime': before, 'process_running': True, 'reused_existing': True}
    if action == 'restart':
        try:
            ancestors = {os.getpid(), *(p.pid for p in psutil.Process().parents())}
        except (psutil.Error, OSError) as exc:
            raise IDEError('无法确认执行节点的进程身份，尚未发送重启') from exc
        if any(r.get('pid') in ancestors for r in before['instances']):
            raise IDEError('目标配置运行着当前执行节点；分步停止将中断本次操作', reason_code='ide_controller_restart')
    context = {'provider': 'run_control_mcp', 'action': action, 'operation_id': operation_id,
               'binding': binding, 'previous_instances': before['instances'], 'before_runtime': before,
               'control_connection': getattr(client, 'control_connection', {'url': client.url})}
    client.deadline = time.monotonic() + 115
    if action in {'stop', 'restart'} and before['instances']:
        on_stage('stopping', control={**context, 'phase': 'stopping'})
        row = before['instances'][0]
        result = client.call('stop_run_configuration', {'projectPath': binding['project_path'],
                            'sessionId': row['session_id'], 'configurationName': binding['run_configuration'], 'stopAllMatches': False})
        if isinstance(result, dict) and isinstance(result.get('output'), str):
            import json
            try:
                result = json.loads(result['output'])
            except ValueError as exc:
                raise IDEError('IDE 停止回执无法解析；不会重复发送', effect_unknown=True) from exc
        stopped = result.get('stopped') if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get('ok') is not True or not isinstance(stopped, list)
                or len(stopped) != 1 or stopped[0].get('sessionId') != row['session_id']):
            raise IDEError('IDE 停止回执与当前配置不一致；未启动新实例', effect_unknown=True)
        deadline = time.monotonic() + 25
        while True:
            runtime = read_state(client, binding)
            if terminated(before, runtime):
                break
            if time.monotonic() >= deadline:
                return {'state': 'unknown', 'runtime': runtime, 'control': {**context, 'phase': 'stopping'},
                        'reason': 'IDE 已收到停止请求，旧进程退出尚未确认；未启动第二个实例'}
            time.sleep(.5)
    runtime = read_state(client, binding)
    if action == 'stop':
        if runtime['state'] != 'stopped':
            raise IDEError('停止后出现新的当前配置会话，未标记为已停止', effect_unknown=True)
        return {'state': 'ready', 'runtime': runtime, 'stopped': True, 'process_running': False,
                'control': {**context, 'phase': 'stopped'}, 'reason': '当前配置已由 IDE 停止'}
    if runtime['state'] != 'stopped':
        raise IDEError('启动前当前配置已有活动会话，未重复启动', effect_unknown=True)
    on_stage('starting', control={**context, 'phase': 'starting'})
    result = client.call('execute_run_configuration', {'projectPath': binding['project_path'],
                         'configurationName': binding['run_configuration'], 'timeout': 1000,
                         'waitForExit': False, 'maxLinesCount': 20, 'truncateMode': 'END'})
    if not isinstance(result, dict) or result.get('exitCode') not in (None, 0):
        raise IDEError('IDE 未确认当前配置启动成功；不会重复启动', effect_unknown=True)
    deadline = time.monotonic() + 30
    while True:
        runtime = read_state(client, binding)
        if runtime['state'] == 'running' and _ids(runtime) and not (_ids(runtime) & _ids(before)):
            return {'state': 'ready', 'runtime': runtime, 'process_running': True, 'restarted': action == 'restart',
                    'reused_existing': False, 'control': {**context, 'phase': 'started'},
                    'reason': '当前配置已由 IDE 重启' if action == 'restart' else '当前配置已由 IDE 启动'}
        if time.monotonic() >= deadline:
            return {'state': 'unknown', 'runtime': runtime, 'control': {**context, 'phase': 'starting'},
                    'reason': 'IDE 启动结果待确认；不会重复启动'}
        time.sleep(.5)


def recover(client, context):
    runtime = read_state(client, context['binding'])
    before = context['before_runtime']
    if context['action'] == 'stop' and runtime['state'] == 'stopped' and terminated(before, runtime):
        return {'state': 'ready', 'stopped': True, 'process_running': False, 'runtime': runtime, 'reason': '原停止操作已确认完成'}
    if (context['phase'] in {'starting', 'started'} and runtime['state'] == 'running'
            and _ids(runtime) and not (_ids(runtime) & _ids(before))):
        from mino_scout.ide_prepare import probe
        return {**probe(context['binding'], client.url, runtime=runtime), 'restarted': context['action'] == 'restart', 'reused_existing': False}
    return {'state': 'unknown', 'runtime': runtime, 'reason': '原操作尚未确认完成；只读取状态，不重复停止或启动'}
