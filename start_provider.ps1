# start_provider.ps1 - Lance une stack orchestrateur complete pour un provider,
# dans sa propre fenetre PowerShell.
# Usage : .\start_provider.ps1 -Provider provider1
#         .\start_provider.ps1 -Provider provider2
#
# Ce script ne fait QUE lancer launch_provider.py dans une fenetre separee
# avec capture des logs. Il ne redefinit PAS la liste des services ni
# MULTI_PROVIDER_ENABLED : avant le 26/08/2026, ce fichier dupliquait ces
# informations a la main, et avait diverge en silence -- il forcait
# MULTI_PROVIDER_ENABLED='false' (federation desactivee) et avait oublie
# placement_arbiter. Resultat : des runs entiers sans une seule negociation,
# indetectables sans comparer les deux fichiers ligne a ligne. launch_provider.py
# est desormais la SEULE source de verite pour la liste des services et le
# flag de federation -- ne pas la recopier ici.
#
# Fichier en ASCII pur : PowerShell 5.1 lit les .ps1 en ANSI, un accent en
# UTF-8 casse le parsing des chaines.

param(
    [Parameter(Mandatory=$true)][ValidateSet("provider1","provider2")]
    [string] $Provider
)

$ROOT   = $PSScriptRoot
$PYTHON = "$ROOT\venv\Scripts\python.exe"

if (-not (Test-Path $PYTHON)) {
    Write-Host "  ERREUR : python introuvable dans $PYTHON" -ForegroundColor Red
    exit 1
}

$stamp   = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$LOGDIR  = Join-Path $ROOT ("logs\" + $Provider + "_" + $stamp)
New-Item -ItemType Directory -Force -Path $LOGDIR | Out-Null
$logFile = Join-Path $LOGDIR "stack.log"

Write-Host ""
Write-Host "  Orchestrateur $Provider (via launch_provider.py)" -ForegroundColor Cyan
Write-Host "  Logs : $logFile"
Write-Host ""

# PYTHONIOENCODING='utf-8' : launch_provider.py reconfigure deja son propre
# stdout en UTF-8 (corrige le 26/08/2026 -- sans ca, rediriger sa sortie
# faisait planter les threads de relais de logs sur le premier accent recu,
# ce qui bloquait silencieusement les services dont le pipe stdout n'etait
# plus draine). Le mettre aussi ici est une securite redondante, pas une
# dependance.
$cmd = "`$env:PYTHONIOENCODING='utf-8'; cd '$ROOT'; " +
       "& '$PYTHON' launch_provider.py --provider $Provider 2>&1 | Tee-Object -FilePath '$logFile'"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd -WindowStyle Normal

$offset = if ($Provider -eq "provider1") { 0 } else { 100 }
Write-Host "  Hub       : http://localhost:$(8000+$offset)" -ForegroundColor Green
Write-Host "  Dashboard : http://localhost:$(8009+$offset)" -ForegroundColor Green
Write-Host "  Arbitre   : http://localhost:$(8011+$offset)" -ForegroundColor Green
Write-Host ""
Write-Host "  Ctrl+C DANS LA FENETRE lancee arrete la stack proprement." -ForegroundColor DarkGray
Write-Host ""
