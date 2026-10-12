$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$logDir = Join-Path $root 'build_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'build_details.log'
function Run-Step([string]$title,[string]$exe,[string[]]$arguments) {
  Write-Host "== $title ==" -ForegroundColor Cyan
  ("`r`n== $title ==") | Out-File -FilePath $log -Append -Encoding UTF8
  # Native tools write ordinary warnings to stderr. With Stop enabled,
  # PowerShell 5.1 can turn those warnings into terminating errors.
  $previousPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    & $exe @arguments 2>&1 | ForEach-Object {
      $line = $_.ToString()
      $line | Out-File -FilePath $log -Append -Encoding UTF8
      Write-Host $line
    }
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousPreference
  }
  if ($exitCode -ne 0) {
    throw "$title failed (exit code $exitCode). See $log"
  }
}

try {
  "Build started $(Get-Date -Format o)" | Out-File $log -Encoding UTF8
  # Prefer a self-contained NuGet Python distribution. This avoids the python.org
  # installer silently reusing an existing installation and ignoring TargetDir.
  $target = Join-Path $env:LOCALAPPDATA 'CallCenterBuildPython312Nuget'
  $python = Join-Path $target 'tools\python.exe'
  if (!(Test-Path -LiteralPath $python)) {
    $package = Join-Path $env:TEMP 'callcenter-python-3.12.10.nupkg'
    $url = 'https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg'
    Write-Host "Downloading portable Python package: $url"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    if (!(Test-Path -LiteralPath $package) -or (Get-Item -LiteralPath $package).Length -lt 1000000) {
      Invoke-WebRequest -Uri $url -OutFile $package -UseBasicParsing
    }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($package,$target)
  }
  if (!(Test-Path -LiteralPath $python)) {
    throw "Portable Python missing: $python. Verify NuGet download/extraction."
  }
  Write-Host "Portable Python found: $python"
  Run-Step 'Check pip' $python @('-m','pip','--version')
  if ($false) {
    Write-Host 'pip unavailable; attempting ensurepip...'
    Run-Step 'Bootstrap pip' $python @('-m','ensurepip','--upgrade')
  }
  Run-Step 'Python version' $python @('--version')
  Run-Step 'Install dependencies' $python @('-m','pip','install','--disable-pip-version-check','-r',(Join-Path $root 'shift_planner_app\requirements.txt'),'pyinstaller>=6,<7')
  Run-Step 'Compile check' $python @('-m','compileall','-q','unified.py','shift_planner_app','break_engine')
  $args = @('-m','PyInstaller','--noconfirm','--clean','--onedir','--name','CallCenterUnified',
    '--paths',(Join-Path $root 'shift_planner_app'),
    '--paths',(Join-Path $root 'break_engine'),
    '--add-data',('shift_planner_app/templates'+[IO.Path]::PathSeparator+'shift_planner_app/templates'),
    '--add-data',('break_engine/static'+[IO.Path]::PathSeparator+'break_engine/static'),
    '--hidden-import','realtime_sync',
    '--hidden-import','app',
    '--hidden-import','flexible_optimizer',
    '--hidden-import','bulk_shift_delete',
    '--hidden-import','operator_shift_export',
    '--hidden-import','operator_shift_visibility',
    '--hidden-import','monthly_workdays',
    '--hidden-import','attendance_sync',
    '--hidden-import','legacy_operator_migration',
    '--hidden-import','server',
    '--collect-submodules','openpyxl',
    '--log-level','INFO',
    'unified.py')
  # --noconfirm may replace the complete dist\CallCenterUnified folder. Persist
  # its SQLite state before running PyInstaller, and restore it afterwards.
  # STOP the EXE first: an active SQLite DB can have uncommitted WAL changes.
  $restoreOldData = $false
  $oldData = Join-Path $root 'dist\CallCenterUnified\data'
  $safeData = Join-Path $root 'build_logs\preserved_data'
  if (Test-Path -LiteralPath (Join-Path $oldData 'unified.sqlite3')) {
    $restoreOldData = $true
    New-Item -ItemType Directory -Force -Path $safeData | Out-Null
    foreach ($ext in @('unified.sqlite3-wal','unified.sqlite3-shm')) {
      $stale = Join-Path $safeData $ext
      if (Test-Path -LiteralPath $stale) { Remove-Item -LiteralPath $stale -Force }
    }
    foreach ($ext in @('unified.sqlite3','unified.sqlite3-wal','unified.sqlite3-shm')) {
      $src = Join-Path $oldData $ext
      if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $safeData $ext) -Force -ErrorAction Stop
      }
    }
    Write-Host 'Existing SQLite DB backed up to build_logs\preserved_data before rebuild.'
  }
  Run-Step 'PyInstaller build' $python $args
  $newData = Join-Path $root 'dist\CallCenterUnified\data'
  if ($restoreOldData -and (Test-Path -LiteralPath (Join-Path $safeData 'unified.sqlite3'))) {
    New-Item -ItemType Directory -Force -Path $newData | Out-Null
    foreach ($ext in @('unified.sqlite3','unified.sqlite3-wal','unified.sqlite3-shm')) {
      $src = Join-Path $safeData $ext
      if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $newData $ext) -Force -ErrorAction Stop
      }
    }
    Write-Host 'Previous SQLite DB restored to dist\CallCenterUnified\data.'
  }

  $exe = Join-Path $root 'dist\CallCenterUnified\CallCenterUnified.exe'
  if (!(Test-Path $exe)) { throw "Build reported success but EXE not found: $exe" }
  Write-Host "EXE generated: $exe" -ForegroundColor Green
  Write-Host 'Windows execution is not automatically verified by this build.'
} catch {
  Write-Host "`nBUILD FAILED: $($_.Exception.Message)" -ForegroundColor Red
  Write-Host "Please send build_logs\build_latest.log and build_details.log (redact personal paths)." -ForegroundColor Yellow
  exit 1
}
