# Mino Scout 远程安装（Windows）。Studio 复制的一行 powershell 命令。
param(
  [Parameter(Mandatory = $true)][string]$Token,
  [string]$NexusUrl = "http://127.0.0.1:10104",
  [string]$StudioId = "",
  [string]$ReleaseVersion = "",
  [string]$ManifestUrl = ""
)

$ErrorActionPreference = "Stop"
$Repo = if ($env:MINO_SCOUT_GITHUB_REPO) { $env:MINO_SCOUT_GITHUB_REPO } else { "pengchengluoyi/MinoScout" }

if (-not $ManifestUrl) {
  if ($ReleaseVersion) {
    $ver = $ReleaseVersion.TrimStart('v')
    $ManifestUrl = "https://github.com/$Repo/releases/download/v$ver/manifest.json"
  } else {
    $ManifestUrl = "https://github.com/$Repo/releases/latest/download/manifest.json"
  }
}

$Prefix = if ($env:MINO_SCOUT_HOME) { $env:MINO_SCOUT_HOME } else { Join-Path $env:LOCALAPPDATA "MinoScout" }
$ScoutOs = "win32"
$ScoutArch = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x64" }

Write-Host "→ manifest $ManifestUrl"
$manifestJson = Invoke-RestMethod -Uri $ManifestUrl -UseBasicParsing
$row = $null
if ($manifestJson.items) {
  foreach ($it in $manifestJson.items) {
    if ($it.os -eq $ScoutOs -and ($it.arch -eq $ScoutArch -or $it.arch -eq "x64")) {
      $row = $it; break
    }
  }
} else {
  $row = $manifestJson
}
if (-not $row -or -not $row.url) { throw "manifest 里没有 $ScoutOs-$ScoutArch 安装包" }

$work = New-Item -ItemType Directory -Force -Path (Join-Path $env:TEMP ("mino-scout-bootstrap-" + [guid]::NewGuid()))
$zip = Join-Path $work.FullName "package.zip"
Invoke-WebRequest -Uri $row.url -OutFile $zip -UseBasicParsing
if ($row.sha256) {
  $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
  if ($hash -ne $row.sha256.ToLower()) { throw "sha256 mismatch" }
}
Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $work.FullName "extract") -Force
$installSh = Get-ChildItem -Path (Join-Path $work.FullName "extract") -Recurse -Filter "install.ps1" | Select-Object -First 1
if (-not $installSh) { throw "zip 内找不到 install.ps1" }
Push-Location $installSh.DirectoryName
& $installSh.FullName
Pop-Location

$configPath = Join-Path $Prefix "config.json"
New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
$appVer = if ($row.version) { [string]$row.version } elseif ($manifestJson.version) { [string]$manifestJson.version } else { "" }
$studioClean = if ($StudioId) { ($StudioId.ToLower() -replace '[^a-z0-9]', '') } else { "" }
$obj = [ordered]@{
  nexus_url = $NexusUrl.TrimEnd('/')
  token     = $Token
  version   = $appVer
}
if ($studioClean) { $obj.studio_id = $studioClean }
$obj | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
Write-Host "→ 配置已写入 $configPath"
Write-Host "Mino Scout 安装完成。Chromium 将在 Scout 首次连上 Nexus 后自动下载。"
