# start_relay.ps1 - Relais partage entre les 2 orchestrateurs (un seul).
# Lance UNE fois, avant les 2 stacks provider.
$ROOT   = $PSScriptRoot
$PYTHON = "$ROOT\venv\Scripts\python.exe"
$stamp  = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$LOGDIR = Join-Path $ROOT "logs\relay_$stamp"
New-Item -ItemType Directory -Force -Path $LOGDIR | Out-Null

$logFile = Join-Path $LOGDIR "provider_relay.log"
$cmd = "cd '$ROOT'; " +
       "`$env:ORCHESTRATOR_URL_PROVIDER_1='http://localhost:8000'; " +
       "`$env:ORCHESTRATOR_URL_PROVIDER_2='http://localhost:8100'; " +
       "& '$PYTHON' -m services.provider_relay.app 2>&1 | Tee-Object -FilePath '$logFile'"
Write-Host "  > provider_relay (partage) : http://localhost:8010" -ForegroundColor Green
Start-Process powershell -ArgumentList "-NoExit","-Command",$cmd -WindowStyle Normal
