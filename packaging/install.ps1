# Install Mino Scout next to config.json and register a startup task.
# Frozen zip: copy onedir, no Python required.
# Source zip (dev): venv + pip, needs Python 3.12+.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($env:MINO_SCOUT_HOME) {
  $Prefix = $env:MINO_SCOUT_HOME
} else {
  $Prefix = Join-Path $env:APPDATA "MinoScout"
}

$Logs = Join-Path $Prefix "logs"
$Bin = $null

Write-Host "Mino Scout → $Prefix"
New-Item -ItemType Directory -Force -Path $Prefix, $Logs | Out-Null

$FrozenDir = Join-Path $Root "mino-scout"
$FrozenExe = Join-Path $FrozenDir "mino-scout.exe"
$FrozenBare = Join-Path $Root "mino-scout.exe"

if (Test-Path $FrozenExe) {
  $Dest = Join-Path $Prefix "bin"
  if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
  New-Item -ItemType Directory -Force -Path $Dest | Out-Null
  Copy-Item -Recurse (Join-Path $FrozenDir "*") $Dest
  $Bin = Join-Path $Dest "mino-scout.exe"
} elseif (Test-Path $FrozenBare) {
  $Dest = Join-Path $Prefix "bin"
  New-Item -ItemType Directory -Force -Path $Dest | Out-Null
  Copy-Item $FrozenBare (Join-Path $Dest "mino-scout.exe")
  $Bin = Join-Path $Dest "mino-scout.exe"
} else {
  $App = Join-Path $Prefix "app"
  $Venv = Join-Path $Prefix "venv"
  $Py = $null
  foreach ($candidate in @("py", "python", "python3")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $Py = $cmd.Source; break }
  }
  if (-not $Py) {
    throw "This zip is a source payload and needs Python 3.12+. Prefer the frozen GitHub Release."
  }
  $pyArgs = @()
  if ((Split-Path $Py -Leaf) -eq "py.exe" -or (Split-Path $Py -Leaf) -eq "py") {
    $pyArgs = @("-3")
  }
  & $Py @pyArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
  if ($LASTEXITCODE -ne 0) {
    throw "This zip is a source payload and needs Python 3.12+"
  }
  New-Item -ItemType Directory -Force -Path $App | Out-Null
  $srcPkg = Join-Path $Root "mino_scout"
  $dstPkg = Join-Path $App "mino_scout"
  if (Test-Path $dstPkg) { Remove-Item -Recurse -Force $dstPkg }
  Copy-Item -Recurse $srcPkg $dstPkg
  Copy-Item (Join-Path $Root "pyproject.toml") (Join-Path $App "pyproject.toml")
  & $Py @pyArgs -m venv $Venv
  $VenvPy = Join-Path $Venv "Scripts\python.exe"
  & $VenvPy -m pip install -U pip
  & $VenvPy -m pip install $App
  $Bin = Join-Path $Venv "Scripts\mino-scout.exe"
  if (-not (Test-Path $Bin)) {
    throw "install failed: $Bin not created"
  }
}

$taskName = "Mino Scout"
$registered = $false
$isAdmin = $false
try {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { $isAdmin = $false }

try {
  $action = New-ScheduledTaskAction -Execute $Bin -WorkingDirectory $Prefix
  if ($isAdmin) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
  } else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
  }
  Start-ScheduledTask -TaskName $taskName
  $registered = $true
  Write-Host "Registered scheduled task: $taskName"
} catch {
  Write-Host "Scheduled task failed ($($_.Exception.Message)); using Startup folder."
}

if (-not $registered) {
  $startup = [Environment]::GetFolderPath("Startup")
  $cmdPath = Join-Path $startup "MinoScout.cmd"
  Set-Content -LiteralPath $cmdPath -Value "@echo off`r`nstart `"`" `"$Bin`"" -Encoding ASCII
  Start-Process -FilePath $Bin -WorkingDirectory $Prefix
  Write-Host "Wrote $cmdPath"
}

Write-Host "Config file (written by Mino Studio): $(Join-Path $Prefix 'config.json')"
Write-Host "Binary: $Bin"
Write-Host "Control: $Bin status | $Bin stop"
