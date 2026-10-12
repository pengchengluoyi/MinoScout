#!/usr/bin/env python3
"""Windows release guard: real child lifecycle and parser, with an isolated home."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if os.name != 'nt':
        print('Windows lifecycle guard runs on the Windows release runner.')
        return 0
    with tempfile.TemporaryDirectory(prefix='scout-lifecycle-') as folder:
        os.environ['MINO_SCOUT_HOME'] = folder
        os.environ['MINO_SCOUT_NO_REEXEC'] = '1'
        from mino_scout.service import pid_alive, request_stop
        from mino_scout.windows_update import _HELPER
        from mino_scout.desktop_windows import windows_session
        helper = Path(folder) / 'update.ps1'
        helper.write_text('param([string]$JobPath)\n' + _HELPER, encoding='utf-8-sig')
        for path in (ROOT / 'packaging/install.ps1', helper):
            literal = "'" + str(path).replace("'", "''") + "'"
            command = ("$t=$null; $e=$null; [System.Management.Automation.Language.Parser]::ParseFile("
                       + literal + ", [ref]$t, [ref]$e) | Out-Null; if ($e.Count) {$e | Out-String | Write-Host; exit 1}")
            subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], check=True)
        worker = """import time
from mino_scout.service import write_pid
from mino_scout.process_control import local_stop_requested
write_pid()
print('ready', flush=True)
while not local_stop_requested(): time.sleep(0.05)
print('cooperative-stop', flush=True)
"""
        process = subprocess.Popen([sys.executable, '-c', worker], cwd=ROOT, env=os.environ.copy(), stdout=subprocess.PIPE, text=True)
        try:
            assert process.stdout.readline().strip() == 'ready'
            assert pid_alive(process.pid) and process.poll() is None
            outcome = request_stop(timeout_sec=5)
            assert outcome['ok'] and not outcome.get('killed'), outcome
            assert process.wait(timeout=5) == 0
            assert process.stdout.read().strip() == 'cooperative-stop'
            assert not pid_alive(process.pid)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        session = windows_session()
        assert session['state'] in ('ready', 'no_session', 'locked'), session
        print(json.dumps({'windows_child_stop': 'pass', 'pid_probe_non_destructive': 'pass',
                          'installer_and_update_helper_parser': 'pass', 'native_session_state': session['state']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
