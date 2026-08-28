# start_local_vms.ps1 - Lance les 8 agents VM en LOCAL, plus le bridge PiCar.
#
# Reproduit le parc du LAAS sur une seule machine : memes identifiants, memes
# coordonnees, memes capacites, memes formules de latence. Seules les adresses
# changent - tout est sur 127.0.0.1, donc chaque agent prend un port distinct.
#
# Usage :
#   .\start_local_vms.ps1              # lance les 8 agents + le bridge
#   .\start_local_vms.ps1 -NoBridge    # agents seulement
#
# Ensuite, lancer les orchestrateurs avec ALL_VM_REGISTRY_JSON (le script
# affiche la ligne exacte a copier).
#
# NOTE TECHNIQUE : chaque agent/bridge est lance dans un powershell.exe
# enfant via -EncodedCommand (Base64), PAS via -Command avec une chaine
# construite a la main. Raison : le JSON du bridge contient des guillemets
# doubles imbriques dans une commande elle-meme imbriquee dans les
# arguments de Start-Process - Windows perd des guillemets en chemin et le
# JSON arrive tronque cote enfant (observe : JSONDecodeError sur VMS_JSON).
# -EncodedCommand elimine ce probleme par construction : aucune re-analyse
# de la chaine par le shell, le script est decode tel quel.
#
# Fichier en ASCII pur : PowerShell 5.1 lit les .ps1 en ANSI, un accent en
# UTF-8 casse le parsing des chaines.

param(
    [switch] $NoBridge
)

$ROOT     = $PSScriptRoot
$PYTHON   = "$ROOT\venv\Scripts\python.exe"
$AGENT    = "$ROOT\infrastructure\VMS\edge1\vm_agent_sim.py"
$BRIDGE   = "$ROOT\infrastructure\Picar\picar_bridge_QoS2.py"
$OSCLIENT = "$ROOT\infrastructure\VMS\master\openstack_client_local.py"

if (-not (Test-Path $PYTHON)) { Write-Host "ERREUR : $PYTHON introuvable" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $AGENT))  { Write-Host "ERREUR : $AGENT introuvable"  -ForegroundColor Red; exit 1 }

function Start-EncodedProcess {
    param([string] $ScriptText)
    $bytes  = [System.Text.Encoding]::Unicode.GetBytes($ScriptText)
    $b64    = [Convert]::ToBase64String($bytes)
    Start-Process powershell -ArgumentList "-NoExit", "-EncodedCommand", $b64 -WindowStyle Minimized
}

# --- Le parc, identique au LAAS -----------------------------------------
# ping_port en 51xx : 5001-5003 sont pris par les APIs ML.
# agent_port en 82xx : lu par l'orchestrateur via ALL_VM_REGISTRY_JSON.
$VMS = @(
    @{ id="edge1";  type="edge";  x=3;   y=-9;  cores=2;  ram=2;  ping=5101; agent=8200 },
    @{ id="edge1b"; type="edge";  x=34;  y=19;  cores=3;  ram=3;  ping=5102; agent=8201 },
    @{ id="edge1c"; type="edge";  x=-6;  y=51;  cores=4;  ram=4;  ping=5103; agent=8202 },
    @{ id="edge2";  type="edge";  x=31;  y=-8;  cores=2;  ram=2;  ping=5104; agent=8203 },
    @{ id="edge2b"; type="edge";  x=4;   y=23;  cores=3;  ram=3;  ping=5105; agent=8204 },
    @{ id="edge2c"; type="edge";  x=-23; y=30;  cores=4;  ram=4;  ping=5106; agent=8205 },
    @{ id="cloud1"; type="cloud"; x=-4;  y=34;  cores=16; ram=16; ping=5107; agent=8206 },
    @{ id="cloud2"; type="cloud"; x=18;  y=4;   cores=8;  ram=8;  ping=5108; agent=8207 }
)

$LOGDIR = Join-Path $ROOT ("logs\local_vms_" + (Get-Date -Format "yyyyMMdd_HHmm"))
New-Item -ItemType Directory -Force -Path $LOGDIR | Out-Null

Write-Host ""
Write-Host "  Lancement des 8 agents VM en local" -ForegroundColor Cyan
Write-Host "  Logs : $LOGDIR" -ForegroundColor DarkGray
Write-Host ""

# --- Substitut local d'openstack_client -----------------------------------
# Sans lui, la federation reste bloquee : /award n'est jamais envoye et
# aucun provider ne demissionne, car les deux dependent d'un kubectl reel
# qui n'existe pas ici. Voir l'entete de openstack_client_local.py.
$osLog = Join-Path $LOGDIR "openstack_client_local.log"
$osCmd = "`$env:PYTHONIOENCODING='utf-8'; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " +
         "& '$PYTHON' '$OSCLIENT' *>&1 | Tee-Object -FilePath '$osLog'"
Start-EncodedProcess -ScriptText $osCmd
Write-Host "    openstack_client_local lance   -> http://127.0.0.1:8024" -ForegroundColor Green
Write-Host ""

foreach ($vm in $VMS) {
    $log = Join-Path $LOGDIR "$($vm.id).log"
    $cmd = "`$env:PYTHONIOENCODING='utf-8'; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " +
           "`$env:VM_ID='$($vm.id)'; `$env:VM_NAME='$($vm.id)'; `$env:VM_TYPE='$($vm.type)'; " +
           "`$env:VM_IP='127.0.0.1'; `$env:VM_X='$($vm.x)'; `$env:VM_Y='$($vm.y)'; " +
           "`$env:TOTAL_CORES='$($vm.cores)'; `$env:TOTAL_RAM_GB='$($vm.ram)'; " +
           "`$env:PING_PORT='$($vm.ping)'; `$env:AGENT_PORT='$($vm.agent)'; " +
           "& '$PYTHON' '$AGENT' *>&1 | Tee-Object -FilePath '$log'"
    Start-EncodedProcess -ScriptText $cmd
    Write-Host ("    {0,-8} cores={1,-3} ram={2,-3} ping={3} agent={4}" -f $vm.id, $vm.cores, $vm.ram, $vm.ping, $vm.agent) -ForegroundColor Green
    Start-Sleep -Milliseconds 400
}

# --- Registre a passer aux orchestrateurs -------------------------------
$reg = @{}
foreach ($vm in $VMS) { $reg[$vm.id] = @{ ip = "127.0.0.1"; port = $vm.agent } }
$regJson = ($reg | ConvertTo-Json -Compress)

# --- Liste VMS a passer au bridge ---------------------------------------
$bridgeVms = @()
foreach ($vm in $VMS) {
    $prov = if ($vm.id -eq "cloud2" -or $vm.id -like "edge2*") { "provider2" } else { "provider1" }
    $bridgeVms += @{ name = $vm.id; id = $vm.id; type = $vm.type;
                     ip = "127.0.0.1"; ping_port = $vm.ping; provider = $prov }
}
$bridgeJson = ($bridgeVms | ConvertTo-Json -Compress)

if (-not $NoBridge) {
    Start-Sleep -Seconds 2
    $blog = Join-Path $LOGDIR "bridge.log"
    $bcmd = "`$env:PYTHONIOENCODING='utf-8'; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " +
            "`$env:VMS_JSON='$bridgeJson'; `$env:ORCH_HOST='127.0.0.1'; " +
            "& '$PYTHON' '$BRIDGE' *>&1 | Tee-Object -FilePath '$blog'"
    Start-EncodedProcess -ScriptText $bcmd
    Write-Host ""
    Write-Host "    bridge PiCar lance   -> http://localhost:8080" -ForegroundColor Green
}

Write-Host ""
Write-Host "  ------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "  A COPIER dans CHAQUE terminal d'orchestrateur, AVANT la commande :" -ForegroundColor Yellow
Write-Host ""
Write-Host "  `$env:ALL_VM_REGISTRY_JSON='$regJson'" -ForegroundColor White
Write-Host "  `$env:PICAR_BRIDGE_URL='http://localhost:8080'" -ForegroundColor White
Write-Host "  `$env:OPENSTACK_MASTER_IP='127.0.0.1'" -ForegroundColor White
Write-Host ""
Write-Host "  Sans la 3e ligne, les deux providers restent tous deux ACTIF" -ForegroundColor DarkYellow
Write-Host "  indefiniment (aucun kubectl reel pour demettre le perdant)." -ForegroundColor DarkYellow
Write-Host ""
Write-Host "  Verification (les 8 doivent repondre) :" -ForegroundColor Yellow
Write-Host "  8200..8207 | ForEach-Object { curl.exe -sS http://127.0.0.1:`$_/health }" -ForegroundColor White
Write-Host "  Bridge :" -ForegroundColor Yellow
Write-Host "  curl.exe -sS http://127.0.0.1:8080/health" -ForegroundColor White
Write-Host "  ------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""
