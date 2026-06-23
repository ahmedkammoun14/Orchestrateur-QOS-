# start_all.ps1 - Lance tous les services QoS Orchestrator

$ROOT   = $PSScriptRoot
$PYTHON = "$ROOT\venv\Scripts\python.exe"
$WAIT_S = 4

$services = @(
    @{ Title="[8006] database";              Module="services.database.app";              Port=8006 },
    @{ Title="[8001] latency_manager";       Module="services.latency_manager.app";       Port=8001 },
    @{ Title="[8002] intent_manager";        Module="services.intent_manager.app";        Port=8002 },
    @{ Title="[8007] history_loader";        Module="services.history_loader.app";        Port=8007 },
    @{ Title="[8003] ml_predictor";          Module="services.ml_predictor.app";          Port=8003 },
    @{ Title="[8005] collector";             Module="services.collector.app";             Port=8005 },
    @{ Title="[8004] metrics_manager";       Module="services.metrics_manager.app";       Port=8004 },
    @{ Title="[8008] decision_intelligence"; Module="services.decision_intelligence.app"; Port=8008 },
    @{ Title="[8009] observability";         Module="services.observability.app";         Port=8009 }
)

Write-Host ""
Write-Host "  QoS Orchestrator -- Demarrage des services"
Write-Host "  ==========================================="
Write-Host ""

foreach ($svc in $services) {
    $cmd = "cd '$ROOT'; & '$PYTHON' -m $($svc.Module)"
    Write-Host "  > $($svc.Title)"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd -WindowStyle Normal
    Start-Sleep -Seconds $WAIT_S
}

Write-Host "  > [8000] hub (orchestrator_core)"
$hubCmd = "cd '$ROOT'; & '$PYTHON' -m hub.orchestrator_core"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $hubCmd -WindowStyle Normal

Write-Host ""
Write-Host "  Attente 12s pour que tous les services demarrent..."
Start-Sleep -Seconds 12

Write-Host ""
Write-Host "  -- Health checks --"

$all_ok = $true
$checks = @(
    @{ Name="database";              Port=8006 },
    @{ Name="latency_manager";       Port=8001 },
    @{ Name="intent_manager";        Port=8002 },
    @{ Name="history_loader";        Port=8007 },
    @{ Name="ml_predictor";          Port=8003 },
    @{ Name="collector";             Port=8005 },
    @{ Name="metrics_manager";       Port=8004 },
    @{ Name="decision_intelligence"; Port=8008 },
    @{ Name="observability";         Port=8009 },
    @{ Name="hub";                   Port=8000 }
)

foreach ($c in $checks) {
    try {
        $null = Invoke-WebRequest -Uri "http://localhost:$($c.Port)/health" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        Write-Host "  OK   $($c.Name.PadRight(24)) :$($c.Port)"
    } catch {
        Write-Host "  FAIL $($c.Name.PadRight(24)) :$($c.Port)"
        $all_ok = $false
    }
}

Write-Host ""
if ($all_ok) {
    Write-Host "  Tous les services sont prets -- lance le PiCar !"
    Write-Host "  Dashboard : http://localhost:8009"
} else {
    Write-Host "  Certains services ne repondent pas -- verifie les fenetres."
}
Write-Host ""
