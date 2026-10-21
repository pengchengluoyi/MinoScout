"""Read-only facts for the download centre; no dependency preparation here."""
from __future__ import annotations

import json
import os
import subprocess
import shutil
import sys
from pathlib import Path

from mino_scout.config import config_dir
from mino_scout.plugins.state import installed, configured, plugin_dir


def snapshot(core):
    from mino_scout.app_version import report_scout_version
    from mino_scout.adb_ime import resolve_local_apk
    from mino_scout.install_plan import runtime_chromium_revision
    from mino_scout.plugins import langbot_host
    ready = bool(getattr(core, '_plugins_ready', False))
    items = [dict(tool_id='scout.core', label='Scout 核心', purpose='节点运行', state='installed', version=report_scout_version(),
                  detail='请使用添加节点提供的安装方式手动升级完整 Scout；不支持自动升级或回退。')]
    revision = runtime_chromium_revision(config_dir() / 'bin')
    from mino_scout.playwright_hub import browsers_dir
    root_override = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    browser_root = Path(root_override) if root_override else browsers_dir()
    if not root_override and not browser_root.is_dir():
        browser_root = Path.home() / 'Library/Caches/ms-playwright' if sys.platform == 'darwin' else Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'ms-playwright' if os.name == 'nt' else Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'ms-playwright'
    if not revision:
        try:
            import playwright
            path = Path(playwright.__file__).parent / 'driver/package/browsers.json'
            revision = next(str(r['revision']) for r in json.loads(path.read_text())['browsers'] if r['name'] == 'chromium')
        except (ImportError, OSError, ValueError, KeyError, StopIteration):
            pass
    matched = browser_root / f'chromium-{revision}'
    executable = any(p.is_file() for p in (matched / 'chrome-linux/chrome', matched / 'chrome-linux64/chrome', matched / 'chrome-win/chrome.exe', matched / 'chrome-win64/chrome.exe', matched / 'chrome-mac/Chromium.app/Contents/MacOS/Chromium', matched / 'chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium', matched / 'chrome-mac-x64/Chromium.app/Contents/MacOS/Chromium')) if revision else False
    items.append(dict(tool_id='chromium', label='Chromium', purpose='Web 自动化', state='matched' if executable else 'missing' if revision else 'unknown', version=revision or None,
                      detail='按当前 runtime 的 Chromium revision 检查；启动准备策略保持不变。'))
    items.append(dict(tool_id='adb.keyboard', label='ADB Keyboard', purpose='Android 文本输入', state='cached' if resolve_local_apk() else 'missing',
                      detail='电脑缓存与设备安装分别记录；设备状态来自最近一次自检，未自检时未知。' + ('当前未找到 ADB，请修复或手动升级 Scout。' if not shutil.which('adb') else ''), device_state='unknown', devices=[{'sn': sn, **dict(fact)} for sn, fact in dict(getattr(core, '_adb_ime_facts', {})).items()]))
    for plugin_id, label in (('feishu', '飞书 CLI'), ('meego', 'Meego CLI')):
        present = installed('cli', plugin_id) if ready else None
        config = configured(core.node_id, 'cli', plugin_id) if ready else None
        version = None
        if present:
            try:
                marker = json.loads((plugin_dir('cli', plugin_id) / '.installed').read_text())
                version = marker.get('version') if isinstance(marker, dict) else None
            except (OSError, ValueError):
                pass  # Legacy "ok" marker does not prove a version.
        items.append(dict(tool_id=f'cli.{plugin_id}', label=label, purpose='工作项接入', state='installed' if present else 'missing' if ready else 'unknown',
                          version=version, configured=config, installable=ready and not present, config_key=f'cli:{plugin_id}',
                          detail='安装不代表配置或连通；工作项 CLI 不负责 Webhook 和对话。'))
    present = langbot_host.is_installed() if ready else None
    python_ok = False
    if ready:
        try:
            python_ok = subprocess.run([langbot_host._base_python(), '-c', 'import sys; sys.exit(0 if (3,11)<=sys.version_info[:2]<(4,0) else 1)'], capture_output=True, timeout=3).returncode == 0
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
    items.append(dict(tool_id='langbot', label='LangBot', purpose='飞书、钉钉、企业微信共用对话宿主', state='installed' if present else 'unknown' if not ready else 'missing' if python_ok else 'missing_python',
                      installable=ready and python_ok and not present, config_key='im:langbot:lark',
                      detail='安装一个共享宿主，飞书、钉钉、企业微信渠道分别配置。使用独立环境，不升级系统 Python。' if python_ok or not ready else '缺少可用的系统 Python 3.11+；请手动准备 Python。'))
    from mino_scout.plugins.state import _item
    for tool in items:
        key = tool['tool_id']
        if key in ('cli.feishu', 'cli.meego', 'langbot'):
            kind, pid = ('im', 'langbot') if key == 'langbot' else ('cli', key.split('.')[1])
            tool.update(plugin_class=kind, plugin_id=pid, removable=ready and installed(kind, pid), configured=configured(core.node_id, kind, pid) if ready else None)
            tool['required_by_config'] = bool(_item(kind, pid).get('import_blocked'))
    return dict(version=1, items=items)


def handle(core, req, cmd, event, started):
    from mino_scout.executors.base import make_event_result
    from mino_scout.schemas import EventStatus
    from mino_scout.plugins import tool_tasks
    params = dict(req.params or {})
    try:
        if cmd == 'tools':
            body = {'tools': snapshot(core)}
        elif not getattr(core, '_plugins_ready', False):
            raise ValueError('plugin_owner_unverified')
        elif cmd == 'tool_task_get':
            body = {'task': tool_tasks.get(str(params.get('task_id') or ''))}
        else:
            body = {'task': tool_tasks.submit(core.node_id, str(params.get('task_id') or ''), str(params.get('tool_id') or ''), str(params.get('action') or ''))}
        return make_event_result(event, status=EventStatus.PASS, executor_used='core', started_at=started, elapsed_ms=0, summary='工具状态', raw_response=body)
    except (ValueError, OSError, RuntimeError) as exc:
        return make_event_result(event, status=EventStatus.FAIL, executor_used='core', started_at=started, elapsed_ms=0, summary=str(exc), error=str(exc))
