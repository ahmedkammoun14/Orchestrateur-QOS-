# start_provider.ps1 - Lance une stack orchestrateur complete pour un provider.
# Usage : .\start_provider.ps1 -Provider provider1
#         .\start_provider.ps1 -Provider provider2
param(
    [Parameter(Mandatory=$true)][ValidateSet("provider1","provider2")]
    [string] $Provider
)
$ROOT   = $PSScriptRoot
$PYTHON = "$ROOT\venv\Scripts\python.exe"
$WAIT_S = 4

if ($Provider -eq "provider1") { $PID_ENV="provider-1"; $OFFSET=0;   $RDB=0 }
else                           { $PID_ENV="provider-2"; $OFFSET=100; $RDB=1 }

$stamp  = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$LOGDIR = Join-Path $ROOT ("logs\" + $Provider + "_" + $stamp)
New-Item -ItemType Directory -Force -Path $LOGDIR | Out-Null

# Env commun a TOUS les services de cette stack (offset + provider + redis).
# ORCHESTRATOR_URL_* : hub de chaque provider (utilise par le relais LOCAL pour
#   joindre SON hub). RELAY_URL_* : relais de chaque provider (routage
#   relais -> relais). P1 relais 8010, P2 relais 8110.
$P = "`$env:PROVIDER_ID='$PID_ENV'; `$env:PORT_OFFSET='$OFFSET'; " +
     "`$env:REDIS_DB='$RDB'; `$env:MULTI_PROVIDER_ENABLED='false'; " +
     "`$env:ORCHESTRATOR_URL_PROVIDER_1='http://localhost:8000'; " +
     "`$env:ORCHESTRATOR_URL_PROVIDER_2='http://localhost:8100'; " +
     "`$env:RELAY_URL_PROVIDER_1='http://localhost:8010'; " +
     "`$env:RELAY_URL_PROVIDER_2='http://localhost:8110'; "

$services = @(
    "services.database.app","services.history_loader.app","services.collector.app",
    "services.metrics_manager.app","services.ml_predictor.app",
    "services.decision_intelligence.app","services.intent_manager.app",
    "services.latency_manager.app","services.observability.app",
    "services.provider_relay.app"
)

Write-Host ""
Write-Host "  Orchestrateur $Provider ($PID_ENV) | offset $OFFSET | Redis DB $RDB" -ForegroundColor Cyan
Write-Host "  Logs : $LOGDIR"

foreach ($mod in $services) {
    $name = $mod.Split(".")[1]
    $logFile = Join-Path $LOGDIR "$name.log"
    $cmd = $P + "cd '$ROOT'; & '$PYTHON' -m $mod 2>&1 | Tee-Object -FilePath '$logFile'"
    Write-Host "  > $name"
    Start-Process powershell -ArgumentList "-NoExit","-Command",$cmd -WindowStyle Normal
    Start-Sleep -Seconds $WAIT_S
}
# Hub en dernier (verifie la sante des spokes au demarrage)
$hubLog = Join-Path $LOGDIR "hub.log"
$hubCmd = $P + "cd '$ROOT'; & '$PYTHON' -m hub.orchestrator_core 2>&1 | Tee-Object -FilePath '$hubLog'"
Write-Host "  > hub"
Start-Process powershell -ArgumentList "-NoExit","-Command",$hubCmd -WindowStyle Normal

$hp = 8000 + $OFFSET; $op = 8009 + $OFFSET
Write-Host ""
Write-Host "  Hub $Provider       : http://localhost:$hp" -ForegroundColor Green
Write-Host "  Dashboard $Provider : http://localhost:$op" -ForegroundColor Green
Write-Host ""
