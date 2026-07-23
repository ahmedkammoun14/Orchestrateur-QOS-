# start_all_multi.ps1 - Lance tous les services + provider_relay, avec capture des logs.
#
# Usage :
#   .\start_all_multi.ps1                      # multi-provider OFF (comportement actuel)
#   .\start_all_multi.ps1 -MultiProvider       # multi-provider ON
#
# Chaque service ecrit dans logs\run_<horodatage>\<service>.log (stdout+stderr).
# Les fenetres restent ouvertes pour l'observation en direct.
#
# NOTE : le seuil de latence n'est PAS pilotable ici - il est code en dur dans
# shared/config.py (METRICS_REGISTRY["latency"]["default_threshold"], ligne 208).
# Pour le changer : editer cette ligne, ou envoyer une intention en mode enhanced.
#
# Ce fichier est volontairement en ASCII pur : Windows PowerShell 5.1 lit les .ps1
# en ANSI par defaut, et un caractere accentue en UTF-8 casse le parsing des chaines.

param(
    [switch] $MultiProvider
)

$ROOT   = $PSScriptRoot
$PYTHON = "$ROOT\venv\Scripts\python.exe"
$WAIT_S = 4

if (-not (Test-Path $PYTHON)) {
    Write-Host "  ERREUR : python introuvable dans $PYTHON" -ForegroundColor Red
    Write-Host "  Active ton venv ou corrige le chemin dans ce script." -ForegroundColor Red
    exit 1
}

# --- Dossier de logs horodate ------------------------------------------
$stamp  = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$LOGDIR = Join-Path $ROOT "logs\run_$stamp"
New-Item -ItemType Directory -Force -Path $LOGDIR | Out-Null

# --- Variables d'environnement propagees aux services ------------------
$envPrefix = ""
if ($MultiProvider) {
    $envPrefix = '$env:MULTI_PROVIDER_ENABLED = "true"; '
}

# --- Services, dans l'ordre de demarrage attendu par le hub ------------
$services = @(
    @{ Title="[8006] database";              Module="services.database.app";              Log="database" },
    @{ Title="[8007] history_loader";        Module="services.history_loader.app";        Log="history_loader" },
    @{ Title="[8005] collector";             Module="services.collector.app";             Log="collector" },
    @{ Title="[8004] metrics_manager";       Module="services.metrics_manager.app";       Log="metrics_manager" },
    @{ Title="[8003] ml_predictor";          Module="services.ml_predictor.app";          Log="ml_predictor" },
    @{ Title="[8008] decision_intelligence"; Module="services.decision_intelligence.app"; Log="decision_intelligence" },
    @{ Title="[8002] intent_manager";        Module="services.intent_manager.app";        Log="intent_manager" },
    @{ Title="[8001] latency_manager";       Module="services.latency_manager.app";       Log="latency_manager" },
    @{ Title="[8009] observability";         Module="services.observability.app";         Log="observability" },
    @{ Title="[8010] provider_relay";        Module="services.provider_relay.app";        Log="provider_relay" }
)

if ($MultiProvider) { $mode = "ON" } else { $mode = "OFF" }

Write-Host ""
Write-Host "  QoS Orchestrator - demarrage" -ForegroundColor Cyan
Write-Host "  ============================"
Write-Host "  Multi-provider : $mode"
Write-Host "  Logs           : $LOGDIR"
Write-Host ""

foreach ($svc in $services) {
    $logFile = Join-Path $LOGDIR "$($svc.Log).log"
    # Tee-Object : affichage dans la fenetre ET ecriture dans le fichier.
    $cmd = $envPrefix + "cd '$ROOT'; & '$PYTHON' -m $($svc.Module) 2>&1 | Tee-Object -FilePath '$logFile'"
    Write-Host "  > $($svc.Title)"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd -WindowStyle Normal
    Start-Sleep -Seconds $WAIT_S
}

# --- Hub en dernier : il verifie la sante des spokes au demarrage ------
$hubLog = Join-Path $LOGDIR "hub.log"
$hubCmd = $envPrefix + "cd '$ROOT'; & '$PYTHON' -m hub.orchestrator_core 2>&1 | Tee-Object -FilePath '$hubLog'"
Write-Host "  > [8000] hub (orchestrator_core)"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $hubCmd -WindowStyle Normal

Write-Host ""
Write-Host "  Dashboard : http://localhost:8009" -ForegroundColor Green
Write-Host "  Relais    : http://localhost:8010/health" -ForegroundColor Green
Write-Host "  Logs      : $LOGDIR" -ForegroundColor Green
Write-Host ""
