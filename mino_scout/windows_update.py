"""Stage verified Windows payloads and install only after Scout exits."""
from __future__ import annotations

import json
from contextlib import contextmanager
import os
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

from mino_scout.config import config_dir

_HELPER = r'''$ErrorActionPreference = 'Stop'
$job = Get-Content -LiteralPath $JobPath -Raw -Encoding UTF8 | ConvertFrom-Json
$env:MINO_SCOUT_HOME = $job.home
$statusPath = Join-Path $job.home 'scout_update.json'
function Save-Status($state, $errorText = '') {
  @{state=$state; version=$job.version; updated_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); error=$errorText; backup=$job.backup} |
    ConvertTo-Json | Set-Content -LiteralPath "$statusPath.tmp" -Encoding UTF8
  Move-Item -LiteralPath "$statusPath.tmp" -Destination $statusPath -Force
}
$before = @()
try {
  $process = Get-Process -Id $job.pid -ErrorAction SilentlyContinue
  if ($process) {
    $started = ([DateTimeOffset]$process.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0
    if ([Math]::Abs($started - $job.created_at) -lt 0.01 -and -not $process.WaitForExit(120000)) {
      throw 'Scout did not exit. No installation files changed.'
    }
  }
  Save-Status 'installing'
  New-Item -ItemType Directory -Path $job.backup -Force | Out-Null
  $dest = Join-Path $job.home 'bin'
  # Back up only payload entries being replaced, not the unchanged browser tree.
  $paths = @((Join-Path $dest 'layers.txt'))
  foreach ($installer in $job.installers) {
    $root = Split-Path -Parent $installer
    if (Test-Path -LiteralPath (Join-Path $root 'runtime')) {
      foreach ($entry in Get-ChildItem -LiteralPath (Join-Path $root 'runtime') -Force) { $paths += Join-Path $dest $entry.Name }
    }
    if (Test-Path -LiteralPath (Join-Path $root 'app')) { $paths += Join-Path $dest 'app' }
    if (Test-Path -LiteralPath (Join-Path $root 'browser')) { $paths += Join-Path $dest 'ms-playwright' }
  }
  foreach ($path in ($paths | Select-Object -Unique)) {
    $saved = Join-Path $job.backup ([string]$before.Count)
    $existed = Test-Path -LiteralPath $path
    if ($existed) { Copy-Item -LiteralPath $path -Destination $saved -Recurse }
    $before += @{path=$path; saved=$saved; existed=$existed}
  }
  $env:MINO_SCOUT_UPDATING = '1'
  $env:MINO_SCOUT_SKIP_SERVICE = '1'
  foreach ($installer in $job.installers) { & $installer }
  Remove-Item Env:MINO_SCOUT_UPDATING -ErrorAction SilentlyContinue
  Remove-Item Env:MINO_SCOUT_SKIP_SERVICE -ErrorAction SilentlyContinue
  $env:MINO_SCOUT_REGISTER_ONLY = '1'
  & ($job.installers[-1])
  Save-Status 'completed'
} catch {
  # Keep backups and the failed payloads for recovery; never alter config or credentials.
  for ($i = $before.Count - 1; $i -ge 0; $i--) {
    $entry = $before[$i]
    try {
      if (Test-Path -LiteralPath $entry.path) {
        Move-Item -LiteralPath $entry.path -Destination (Join-Path $job.backup ("failed-" + $i))
      }
      if ($entry.existed) { Copy-Item -LiteralPath $entry.saved -Destination $entry.path -Recurse }
    } catch { Write-Warning 'A rollback entry needs manual recovery from the retained backup.' }
  }
  Save-Status 'failed' 'Installation failed; previous payloads restored where possible. See update helper log.'
  $bin = Join-Path $job.home 'bin\mino-scout.exe'
  if (Test-Path -LiteralPath $bin) { Start-Process -FilePath $bin -ArgumentList 'run' -WorkingDirectory $job.home }
  Write-Error $_
  exit 1
}
'''


@contextmanager
def _update_lock(home: Path):
    # Kernel releases the lock even if the updater crashes; retain the harmless lock file.
    home.mkdir(parents=True, exist_ok=True)
    with (home / ".scout-update.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            lock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
        try:
            lock()
        except OSError as exc:
            raise RuntimeError("已有 Scout 更新正在准备，请勿重复提交") from exc
        try:
            yield
        finally:
            handle.seek(0)
            unlock()


def _write_status(home: Path, data: dict):
    path = home / "scout_update.json"
    tmp = home / (".scout_update." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def stage_update(steps: list[dict], *, version: str) -> dict:
    from mino_scout.runtime_sidecar import read_update_status
    home = config_dir()
    with _update_lock(home):
        if read_update_status().get("state") in ("awaiting_exit", "installing"):
            raise RuntimeError("已有 Windows 更新等待退出或正在安装，请先查看 status.update")
        if not steps:
            raise RuntimeError("没有需要安装的更新载荷")
        return _stage_update(steps, version=version)


def _stage_update(steps: list[dict], *, version: str) -> dict:
    from mino_scout.http_fetch import download_file
    from mino_scout.self_update import _verify_sha256
    from mino_scout.service import read_pid
    from mino_scout.process_control import request_local_stop
    import psutil
    pid = read_pid()
    process = psutil.Process(pid)
    home = config_dir()
    work = home / 'updates' / uuid.uuid4().hex
    work.mkdir(parents=True)
    installers = []
    for index, step in enumerate(steps):
        expected = str(step.get('sha256') or '')
        if len(expected) != 64:
            raise RuntimeError('Windows 延后更新需要发布包 SHA256 校验值')
        archive = work / f'payload-{index}.zip'
        download_file(str(step['url']), archive)
        _verify_sha256(archive, expected)
        extract = work / f'payload-{index}'
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                target = (extract / info.filename).resolve()
                if not target.is_relative_to(extract.resolve()) or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError('发布包包含不安全路径')
            zf.extractall(extract)
        matches = list(extract.rglob('install.ps1'))
        if len(matches) != 1:
            raise RuntimeError('发布包必须包含一个 Windows 安装脚本')
        installers.append(str(matches[0]))
    job = {'pid': pid, 'created_at': process.create_time(), 'home': str(home), 'version': version,
           'installers': installers, 'backup': str(work / 'backup')}
    job_path = work / 'job.json'
    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    helper = work / 'update.ps1'
    helper.write_text("param([string]$JobPath)\n" + _HELPER, encoding='utf-8-sig')
    log = (work / 'helper.log').open('ab')
    _write_status(home, {'state': 'awaiting_exit', 'version': version,
                         'updated_at': time.time(), 'backup': job['backup']})
    try:
        subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(helper), '-JobPath', str(job_path)],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, close_fds=True,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
    except OSError:
        _write_status(home, {'state': 'failed', 'version': version, 'updated_at': time.time(),
                            'backup': job['backup'], 'error': '无法启动 Windows 更新助手；安装文件未修改'})
        raise
    finally:
        log.close()
    if pid != os.getpid():
        request_local_stop(pid)
    return {'ok': True, 'mode': 'staged', 'deferred': True, 'version': version,
            'summary': '更新包已校验；Scout 退出后安装。status.update 显示最终结果。'}
