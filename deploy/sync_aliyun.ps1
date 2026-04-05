param(
  [Parameter(Mandatory = $true)]
  [string]$KeyPath,

  [string]$RemoteHost = "112.124.59.54",
  [string]$RemoteUser = "root",
  [string]$RemoteDir = "/root/bamboo-ai",
  [int]$AppPort = 8000,
  [switch]$SkipPip,
  [switch]$SkipRestart
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$resolvedKeyPath = (Resolve-Path $KeyPath).Path
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$remote = "$RemoteUser@$RemoteHost"

$sourceFiles = @(
  "ai_service.py",
  "config.py",
  "database.py",
  "logging_setup.py",
  "main.py",
  "manage.py",
  "models.py",
  "requirements.txt",
  "security.py",
  "start.sh",
  "world_extraction_service.py",
  ".env.example",
  "README.md",
  "docs",
  "deploy",
  "static",
  "tests"
)

Write-Host "[1/5] Backing up remote source ..."
($backupScript = @'
mkdir -p /root/bamboo-ai-backups
cd /root
if [ -d '__REMOTE_DIR__' ]; then
  tar -czf /root/bamboo-ai-backups/bamboo-ai-src-__TIMESTAMP__.tar.gz -C '__REMOTE_DIR__' \
    ai_service.py config.py database.py logging_setup.py main.py manage.py models.py requirements.txt security.py start.sh world_extraction_service.py .env.example README.md docs deploy static tests 2>/dev/null || true
fi
'@).
  Replace("__REMOTE_DIR__", $RemoteDir).
  Replace("__TIMESTAMP__", $timestamp)
ssh -i $resolvedKeyPath -o StrictHostKeyChecking=no $remote $backupScript

Write-Host "[2/5] Uploading source files ..."
$expandedSources = $sourceFiles | ForEach-Object { Join-Path $projectRoot $_ }
scp -i $resolvedKeyPath -o StrictHostKeyChecking=no -r $expandedSources "$remote`:$RemoteDir/"

if (-not $SkipPip) {
  Write-Host "[3/5] Installing/updating Python dependencies ..."
  ssh -i $resolvedKeyPath -o StrictHostKeyChecking=no $remote "cd '$RemoteDir' && '$RemoteDir/.venv/bin/python' -m pip install -r requirements.txt"
} else {
  Write-Host "[3/5] Skipped pip install"
}

Write-Host "[4/5] Running remote syntax checks ..."
ssh -i $resolvedKeyPath -o StrictHostKeyChecking=no $remote "cd '$RemoteDir' && '$RemoteDir/.venv/bin/python' -m compileall main.py manage.py ai_service.py world_extraction_service.py"

if (-not $SkipRestart) {
  Write-Host "[5/5] Restarting remote app and checking health ..."
  $restartScript = @'
set -e
old_pids=$(pgrep -f '__REMOTE_DIR__/.venv/bin/python -m uvicorn main:app' || true)
if [ -n "$old_pids" ]; then
  echo "$old_pids" | xargs -r kill -TERM || true
  sleep 3
  remaining_pids=$(pgrep -f '__REMOTE_DIR__/.venv/bin/python -m uvicorn main:app' || true)
  if [ -n "$remaining_pids" ]; then
    echo "$remaining_pids" | xargs -r kill -KILL || true
    sleep 1
  fi
fi
cd '__REMOTE_DIR__'
nohup bash -lc 'cd __REMOTE_DIR__ && set -a && [ -f ./.env ] && . ./.env && set +a && exec __REMOTE_DIR__/.venv/bin/python -m uvicorn main:app --host "${HOST:-0.0.0.0}" --port "${APP_PORT:-__APP_PORT__}"' > '__REMOTE_DIR__/logs/uvicorn.out' 2>&1 < /dev/null &
sleep 5
health_port="${APP_PORT:-__APP_PORT__}"
curl -fsS "http://127.0.0.1:${health_port}/healthz"
'@
  $restartScript = $restartScript.
    Replace("__REMOTE_DIR__", $RemoteDir).
    Replace("__APP_PORT__", [string]$AppPort)
  $restartScript = $restartScript -replace "`r`n", "`n"
  $localRestartScript = Join-Path $env:TEMP "zhulin_restart_$timestamp.sh"
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($localRestartScript, $restartScript, $utf8NoBom)
  try {
    scp -i $resolvedKeyPath -o StrictHostKeyChecking=no $localRestartScript "$remote`:/tmp/zhulin_restart.sh"
    ssh -i $resolvedKeyPath -o StrictHostKeyChecking=no $remote "bash /tmp/zhulin_restart.sh && rm -f /tmp/zhulin_restart.sh"
  } finally {
    Remove-Item $localRestartScript -Force -ErrorAction SilentlyContinue
  }
} else {
  Write-Host "[5/5] Skipped restart"
}

Write-Host "Sync complete."
