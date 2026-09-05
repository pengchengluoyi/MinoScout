# Install Mino Scout next to config.json and register a startup task.
#
# 分层安装：zip 里带哪几层就装哪几层，没带的层原地不动。与 install.sh 一一对应。
#
#   layers.txt + runtime\         → 替换 bin\mino-scout.exe 与 bin\_internal
#   layers.txt + app\             → 替换 bin\app
#   layers.txt + browser\         → 替换 bin\ms-playwright
#   mino_scout\ + pyproject.toml  → 源码包（dev）：venv + pip，需要 Python 3.12+
#
# **刻意不再整体删掉 bin**：浏览器层实测 781 MB、占安装体积 78%，而它几乎从不变。
# 层与目录的映射见 scripts/layers.py 的 PAYLOAD_DIRS —— 改那里要同步改这里和 install.sh。
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($env:MINO_SCOUT_HOME) {
  $Prefix = $env:MINO_SCOUT_HOME
} else {
  $Prefix = Join-Path $env:APPDATA "MinoScout"
}

$Logs = Join-Path $Prefix "logs"
$Dest = Join-Path $Prefix "bin"
$Bin = $null
$LayerNames = @("runtime", "app", "browser")

Write-Host "Mino Scout → $Prefix"
New-Item -ItemType Directory -Force -Path $Prefix, $Logs | Out-Null

# ---------------- 层清单读取 ----------------

# `<层> <指纹> [k=v ...]`，# 开头是注释。刻意不解 JSON：与 install.sh 保持同一份格式。
function Read-LayerRows($path) {
  if (-not (Test-Path -LiteralPath $path)) { return @() }
  $rows = @()
  foreach ($line in Get-Content -LiteralPath $path) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith("#")) { continue }
    $rows += , ($t -split '\s+')
  }
  return $rows
}

function Get-ZipLayerKey($layer) {
  foreach ($row in Read-LayerRows (Join-Path $Root "layers.txt")) {
    if ($row[0] -eq $layer) { return $row[1] }
  }
  return $null
}

function Get-ZipLayerField($layer, $name) {
  foreach ($row in Read-LayerRows (Join-Path $Root "layers.txt")) {
    if ($row[0] -ne $layer) { continue }
    for ($i = 2; $i -lt $row.Count; $i++) {
      $kv = $row[$i] -split '=', 2
      if ($kv.Count -eq 2 -and $kv[0] -eq $name) { return $kv[1] }
    }
  }
  return $null
}

function Get-InstalledLayerKey($layer) {
  foreach ($row in Read-LayerRows (Join-Path $Dest "layers.txt")) {
    if ($row[0] -eq $layer) { return $row[1] }
  }
  return $null
}

function Set-InstalledLayer($layer, $key) {
  $f = Join-Path $Dest "layers.txt"
  $kept = @()
  foreach ($row in Read-LayerRows $f) {
    if ($row[0] -ne $layer) { $kept += "$($row[0]) $($row[1])" }
  }
  $kept += "$layer $key"
  Set-Content -LiteralPath $f -Value ($kept | Sort-Object) -Encoding ASCII
}

function Get-LayerTarget($layer) {
  switch ($layer) {
    "runtime" { return "" }
    "app"     { return "app" }
    "browser" { return "ms-playwright" }
    default   { throw "unknown layer: $layer" }
  }
}

# ---------------- 落地 ----------------

# 先拷到 .new 再改名，把"目标已删、新的还没到位"的窗口压到一次改名。
function Replace-Path($src, $dst) {
  foreach ($suffix in @(".new", ".old")) {
    $p = "$dst$suffix"
    if (Test-Path -LiteralPath $p) { Remove-Item -Recurse -Force -LiteralPath $p }
  }
  Copy-Item -Recurse -LiteralPath $src -Destination "$dst.new"
  if (Test-Path -LiteralPath $dst) { Move-Item -LiteralPath $dst -Destination "$dst.old" }
  Move-Item -LiteralPath "$dst.new" -Destination $dst
  if (Test-Path -LiteralPath "$dst.old") { Remove-Item -Recurse -Force -LiteralPath "$dst.old" }
}

function Install-Layer($layer) {
  $src = Join-Path $Root $layer
  $rel = Get-LayerTarget $layer
  if (-not $rel) {
    # runtime：逐个顶层条目替换，将来多出别的顶层文件也不用改脚本。
    foreach ($entry in Get-ChildItem -LiteralPath $src) {
      Replace-Path $entry.FullName (Join-Path $Dest $entry.Name)
    }
  } else {
    Replace-Path $src (Join-Path $Dest $rel)
  }
  Write-Host "  层 $layer → $(if ($rel) { $rel } else { 'bin\' })  ($(Get-ZipLayerKey $layer))"
}

# app 层单独更新时，必须确认本机 runtime 与它是同一套依赖 —— 否则新代码引用了
# 本机 runtime 里没有的依赖，换完就起不来，症状只是启动时一句 ImportError。
function Assert-AppGate {
  $need = Get-ZipLayerField "app" "requires_runtime"
  if (-not $need) { return }
  $have = Get-InstalledLayerKey "runtime"
  if (-not $have) {
    throw "app layer needs runtime $need but nothing is installed here. Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip) first."
  }
  if ($have -ne $need) {
    throw "app layer needs runtime $need but $have is installed. Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip) instead."
  }
}

function Install-Layers {
  New-Item -ItemType Directory -Force -Path $Dest | Out-Null

  # app-only 增量：先过闸门，再动任何文件。
  if ((Test-Path -LiteralPath (Join-Path $Root "app")) -and
      -not (Test-Path -LiteralPath (Join-Path $Root "runtime"))) {
    Assert-AppGate
  }

  $present = 0
  foreach ($layer in $LayerNames) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $layer))) { continue }
    Install-Layer $layer
    Set-InstalledLayer $layer (Get-ZipLayerKey $layer)
    $present++
  }
  if ($present -eq 0) {
    throw "layers.txt found but no payload directory (runtime\ app\ browser\)"
  }

  $exe = Join-Path $Dest "mino-scout.exe"
  if (-not (Test-Path -LiteralPath $exe)) {
    throw "runtime layer missing: $exe not present after install. Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip)."
  }
  $cli = Join-Path $Dest "app\mino_scout\cli.py"
  if (-not (Test-Path -LiteralPath $cli)) {
    throw "app layer missing: $cli not present after install"
  }
  $script:Bin = $exe
}

function Install-Source {
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
  $dstPkg = Join-Path $App "mino_scout"
  if (Test-Path -LiteralPath $dstPkg) { Remove-Item -Recurse -Force -LiteralPath $dstPkg }
  Copy-Item -Recurse (Join-Path $Root "mino_scout") $dstPkg
  Copy-Item (Join-Path $Root "pyproject.toml") (Join-Path $App "pyproject.toml")
  & $Py @pyArgs -m venv $Venv
  $VenvPy = Join-Path $Venv "Scripts\python.exe"
  & $VenvPy -m pip install -U pip
  & $VenvPy -m pip install $App
  $script:Bin = Join-Path $Venv "Scripts\mino-scout.exe"
  if (-not (Test-Path -LiteralPath $script:Bin)) {
    throw "install failed: $($script:Bin) not created"
  }
}

# ---------------- 停掉在跑的实例 ----------------

# 刻意在动载荷**之前**停：替换正在运行的 exe 与 _internal 会让当前进程崩在半路，
# Windows 上更直接 —— 文件被占用时改名会失败。
try {
  Stop-ScheduledTask -TaskName "Mino Scout" -ErrorAction SilentlyContinue | Out-Null
} catch { }
try {
  Get-Process -Name "mino-scout" -ErrorAction SilentlyContinue | Stop-Process -Force
} catch { }

if (Test-Path -LiteralPath (Join-Path $Root "layers.txt")) {
  Install-Layers
} elseif ((Test-Path -LiteralPath (Join-Path $Root "mino_scout")) -and
         (Test-Path -LiteralPath (Join-Path $Root "pyproject.toml"))) {
  Install-Source
} else {
  throw "installer payload not found (need layers.txt with runtime\ app\ browser\, or mino_scout\)"
}

# CI 与本地自测只想验证"层有没有正确落地"，不该在跑测试的机器上留一个常驻任务。
if ($env:MINO_SCOUT_SKIP_SERVICE) {
  Write-Host "MINO_SCOUT_SKIP_SERVICE set - not registering a scheduled task."
} else {
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
}

$installed = Join-Path $Dest "layers.txt"
if (Test-Path -LiteralPath $installed) {
  Write-Host "Installed layers:"
  foreach ($line in Get-Content -LiteralPath $installed) { Write-Host "  $line" }
}
Write-Host "Config file (written by Mino Studio): $(Join-Path $Prefix 'config.json')"
Write-Host "Binary: $Bin"
Write-Host "Control: $Bin status | $Bin stop"
