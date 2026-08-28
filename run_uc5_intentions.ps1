# run_uc5_intentions.ps1 - UC5 cle en main : huit intentions variees.
#
# Envoie les huit phrases avec l'espacement correct, capture le contrat SLO
# produit par chacune, et ecrit un rapport lisible. Vous n'avez rien a
# relever dans les terminaux.
#
# PREREQUIS : les DEUX orchestrateurs tournent en federe
#             ($env:MULTI_PROVIDER_ENABLED="true"), et le PiCar roule.
#             Laisser 5 minutes d'autonome AVANT de lancer ce script.
#
# Usage :
#   .\run_uc5_intentions.ps1                 # espacement normal, 2 min
#   .\run_uc5_intentions.ps1 -SpacingSec 60  # plus rapide si le temps manque
#   .\run_uc5_intentions.ps1 -DryRun         # verifie tout sans rien envoyer
#
# Fichier en ASCII pur : PowerShell 5.1 lit les .ps1 en ANSI, un accent en
# UTF-8 casse le parsing des chaines.

param(
    [int]    $SpacingSec = 120,
    [int]    $SettleSec  = 25,
    [string] $HubUrl     = "http://localhost:8002",
    [string] $StatusUrl  = "http://localhost:8000/status",
    [switch] $DryRun
)

$ErrorActionPreference = "Continue"
$ROOT = $PSScriptRoot

# --- Les huit phrases -----------------------------------------------------
#
# PROVENANCE : selectionnees dans "intents-samples.docx" fourni par
# l'encadrant (20 intentions, scenario streaming video en vehicule).
# Le numero "doc" renvoie a la numerotation d'origine du document ; il
# doit etre cite tel quel dans le papier, la provenance externe des
# phrases etant precisement ce qui leur donne leur valeur (elles n'ont
# pas ete ecrites pour le systeme qu'elles testent).
#
# xQoS n'observe que latency / cpu_usage / ram_usage. Les intentions 7 et 8
# sont HORS-DOMAINE de facon deliberee : les deux issues possibles sont
# rapportables, et le choix est fige AVANT le run.
$INTENTIONS = @(
    @{ n=1; doc="#03"; texte="Reduce latency as much as possible, I'm watching a live event"
       teste="Latence reformulee"
       attendu="contrat latence dominant, meme placement que l'ancienne phrase latence" },
    @{ n=2; doc="#17"; texte="Always encrypt my stream end-to-end, even if it increases latency"
       teste="ARBITRAGE INVERSE - calcul prioritaire, latence deprioritee"
       attendu="cpu_usage dominant ET latence en poids faible ou absente" },
    @{ n=3; doc="#05"; texte="Pre-load the next episode before it starts so there is no gap between episodes"
       teste="Memoire IMPLICITE - le mot memoire n'apparait pas"
       attendu="ram_usage dominant ; aucune regle par mots-cles ne l'attrape" },
    @{ n=4; doc="#02"; texte="I always want at least 1080p. If the network cannot support it, pause and wait"
       teste="Plancher de qualite, tolere l'attente"
       attendu="latence NON prioritaire malgre le contexte reseau" },
    @{ n=5; doc="#01"; texte="I want the video to never buffer, even if that means reducing quality"
       teste="AMBIGUE - aucun terme technique"
       attendu="contrat coherent malgre l'absence d'indice explicite" },
    @{ n=6; doc="#06"; texte="Optimize my connection at every step of my commute from Toulouse to Bordeaux"
       teste="TRES VAGUE + mobilite - proche du scenario PiCar"
       attendu="contrat quelconque mais stable ; teste la tolerance au flou" },
    @{ n=7; doc="#12"; texte="Minimize the carbon footprint of my stream. Use green data centres when possible"
       teste="HORS-DOMAINE deguise en placement"
       attendu="rejet OU contrat invente - les DEUX se rapportent (decide avant le run)" },
    @{ n=8; doc="#16"; texte="Do not log or retain any metadata about my viewing sessions"
       teste="HORS-DOMAINE pur - aucune metrique observable"
       attendu="rejet OU contrat invente - les DEUX se rapportent (decide avant le run)" },
    # --- Ajoutees pour l'essai 2 -----------------------------------------
    # L'essai 1 (17 aout, 3 traductions sur 8) avait un denominateur trop
    # petit. Ces 4 phrases sont AJOUTEES ; AUCUNE des 8 precedentes n'est
    # retiree, y compris les 5 qui ont ete rejetees - les retirer apres
    # avoir vu le resultat serait une selection a posteriori.
    @{ n=9; doc="#18"; texte="Alert me if my connection is being intercepted or downgraded"
       teste="Analyse continue + alerte"
       attendu="cpu_usage (detection d'anomalie) ET latency (reactivite d'alerte)" },
    @{ n=10; doc="#07"; texte="When the car stops in traffic, use that opportunity to download and buffer more content"
       teste="Prefetch opportuniste - memoire implicite + mobilite"
       attendu="ram_usage dominant" },
    @{ n=11; doc="#04"; texte="Switch to audio-only mode when I'm in a tunnel or weak signal area"
       teste="Transcodage conditionnel - calcul implicite"
       attendu="cpu_usage (changement de codec) present" },
    @{ n=12; doc="#11"; texte="Alert me and downgrade quality before I exceed my monthly data plan"
       teste="Alerte + notion de budget hors-domaine"
       attendu="latency d'alerte, le budget de donnees n'etant pas observable" }
)

# --- Controles avant de commencer ---------------------------------------
Write-Host ""
Write-Host ("  UC5 - {0} intentions issues de intents-samples.docx" -f `
    $INTENTIONS.Count) -ForegroundColor Cyan
Write-Host "  ---------------------------------------------------" -ForegroundColor DarkGray

$statusAvant = $null
try {
    $statusAvant = Invoke-RestMethod -Uri $StatusUrl -TimeoutSec 5
    Write-Host ("    hub provider-1  : OK  | cycle {0} | mode {1} | role {2}" -f `
        $statusAvant.cycle, $statusAvant.mode, $statusAvant.role) -ForegroundColor Green
} catch {
    Write-Host "    hub provider-1  : INJOIGNABLE - lancer les orchestrateurs d'abord" -ForegroundColor Red
    exit 1
}

if ($statusAvant.cycle -lt 25) {
    Write-Host ("    ATTENTION : seulement {0} cycles ecoules." -f $statusAvant.cycle) -ForegroundColor Yellow
    Write-Host "    L'historique doit se remplir (>= 25 cycles, ~3 min) avant" -ForegroundColor Yellow
    Write-Host "    que le MI et le GRU aient de quoi travailler." -ForegroundColor Yellow
    $rep = Read-Host "    Continuer quand meme ? (o/N)"
    if ($rep -ne "o") { Write-Host "    Annule." -ForegroundColor DarkGray; exit 0 }
}

try {
    $null = Invoke-RestMethod -Uri "http://localhost:8100/status" -TimeoutSec 5
    Write-Host "    hub provider-2  : OK" -ForegroundColor Green
} catch {
    Write-Host "    hub provider-2  : INJOIGNABLE" -ForegroundColor Red
    Write-Host "    UC5 doit tourner en FEDERE, avec les deux orchestrateurs." -ForegroundColor Red
    exit 1
}

$dureeMin = [math]::Round((($INTENTIONS.Count - 1) * $SpacingSec + $INTENTIONS.Count * $SettleSec) / 60, 1)
Write-Host ("    duree estimee   : {0} min" -f $dureeMin) -ForegroundColor DarkGray
Write-Host ""

if ($DryRun) {
    Write-Host "  MODE DRY-RUN : rien ne sera envoye." -ForegroundColor Yellow
    foreach ($i in $INTENTIONS) {
        Write-Host ("    {0}. doc {1} [{2}]" -f $i.n, $i.doc, $i.teste) -ForegroundColor DarkGray
        Write-Host ("       `"{0}`"" -f $i.texte) -ForegroundColor DarkGray
    }
    Write-Host ""
    exit 0
}

# --- Envoi ---------------------------------------------------------------
$stamp   = Get-Date -Format "yyyyMMdd_HHmm"
$rapport = Join-Path $ROOT "logs\UC5_rapport_$stamp.md"
New-Item -ItemType Directory -Force -Path (Join-Path $ROOT "logs") | Out-Null

$lignes = @()
$lignes += "# UC5 - $($INTENTIONS.Count) intentions variees"
$lignes += ""
$lignes += "Phrases selectionnees dans ``intents-samples.docx`` (fourni par"
$lignes += "l'encadrant, 20 intentions, scenario streaming video en vehicule)."
$lignes += "Les numeros ``#NN`` renvoient a la numerotation du document."
$lignes += ""
$lignes += "Date : $(Get-Date -Format 'yyyy-MM-dd HH:mm') (heure locale)"
$lignes += "Espacement : $SpacingSec s | stabilisation avant releve : $SettleSec s"
$lignes += ""

foreach ($i in $INTENTIONS) {
    $tEnvoi = Get-Date
    Write-Host ("  [{0}/{1}] doc {2} - {3}" -f `
        $i.n, $INTENTIONS.Count, $i.doc, $i.teste) -ForegroundColor Cyan
    Write-Host ("        `"{0}`"" -f $i.texte) -ForegroundColor DarkGray

    $reponse = $null
    $erreur  = $null
    try {
        # Corps envoye en OCTETS UTF-8 explicites. PowerShell 5.1 encode une
        # chaine avec le charset du Content-Type ; sans charset il retombe sur
        # un encodage qui fait echouer le decodage JSON cote serveur (422).
        # Passer des octets contourne la difference entre 5.1 et 7.
        $json    = @{ intention = $i.texte } | ConvertTo-Json -Compress
        $body    = [System.Text.Encoding]::UTF8.GetBytes($json)
        $reponse = Invoke-RestMethod -Uri "$HubUrl/intent" -Method Post `
                     -ContentType "application/json; charset=utf-8" `
                     -Body $body -TimeoutSec 120
        Write-Host ("        ACCEPTEE - {0} SLO(s)" -f `
            $reponse.slos_count) -ForegroundColor Green
    } catch {
        # ErrorDetails.Message porte le corps JSON du serveur ; sans lui on ne
        # distingue pas un rejet metier (422) d'une panne reseau.
        $code   = $_.Exception.Response.StatusCode.value__
        $detail = ($_.ErrorDetails.Message -replace "\s+", " ").Trim()
        if (-not $detail) { $detail = $_.Exception.Message }
        $erreur = "HTTP $code - $detail"
        Write-Host ("        REJETEE : {0}" -f $erreur) -ForegroundColor Red
    }

    # Laisser le contrat se stabiliser avant de le relever
    Start-Sleep -Seconds $SettleSec

    $contrat = $null
    if (-not $erreur) {
        try {
            $st = Invoke-RestMethod -Uri $StatusUrl -TimeoutSec 5
            $contrat = $st.active_slos
        } catch { }
    } else {
        Write-Host "        REJETEE - contrat inchange, rien relevE" -ForegroundColor Yellow
    }

    $lignes += "## Intention $($i.n) (document $($i.doc)) - $($i.teste)"
    $lignes += ""
    $lignes += "**Phrase :** ``$($i.texte)``"
    $lignes += ""
    $lignes += "- Source : intents-samples.docx (encadrant), entree $($i.doc)"
    $lignes += "- Envoyee a : $($tEnvoi.ToString('HH:mm:ss')) (locale)"
    $lignes += "- Attendu : $($i.attendu)"
    if ($erreur) { $lignes += "- **ERREUR :** $erreur" }
    if ($reponse) { $lignes += "- Temps LLM : $($reponse.llm_ms) ms" }
    $lignes += ""

    if ($erreur) {
        # Le contrat lu dans /status est celui de l'intention PRECEDENTE :
        # un rejet ne modifie rien. L'afficher comme resultat serait faux.
        $lignes += "**REJETEE** - aucun contrat produit. Le systeme conserve le"
        $lignes += "contrat anterieur. Cause a lire dans le log du intent_manager :"
        $lignes += "tableau vide renvoye par le LLM (garde-fou) ou echec des deux"
        $lignes += "niveaux LLM (panne). Le code HTTP ne les distingue pas."
        $lignes += ""
    } elseif ($contrat) {
        $lignes += "| Metrique | Op | Seuil | Unite | Poids | Primaire |"
        $lignes += "|---|---|---|---|---|---|"
        foreach ($s in $contrat) {
            $lignes += "| $($s.metric) | $($s.operator) | $($s.threshold) | $($s.unit) | $($s.weight) | $($s.is_primary) |"
            Write-Host ("        -> {0} {1} {2} {3} (poids {4}, primaire {5})" -f `
                $s.metric, $s.operator, $s.threshold, $s.unit, $s.weight, $s.is_primary) -ForegroundColor White
        }
    } else {
        $lignes += "_Contrat non releve (hub injoignable au moment du releve)._"
        Write-Host "        contrat non releve" -ForegroundColor Yellow
    }
    $lignes += ""

    # Signal d'alarme : signature du bug de course corrige le 14 aout
    if ($contrat) {
        # La signature du bug est le DEFAUT autonome promu en primaire, donc
        # cpu_usage EXACTEMENT a 1.0 (AUTONOMOUS_CPU_FLOOR_CORES) accompagne
        # de ram_usage a 1.0. Un LLM qui repond 0.5 ou 0.3 core en primaire
        # est un contrat legitime : tester "<= 1" declenchait a tort sur
        # toutes les intentions a faible besoin de calcul (constate le
        # 17/08 sur 4 des 12 phrases).
        $cpuDef = $contrat | Where-Object { $_.metric -eq "cpu_usage" -and $_.threshold -eq 1.0 -and $_.is_primary }
        $ramDef = $contrat | Where-Object { $_.metric -eq "ram_usage" -and $_.threshold -eq 1.0 -and $_.is_primary }
        $suspect = if ($cpuDef -and $ramDef) { $cpuDef } else { $null }
        if ($suspect) {
            Write-Host "        ALARME : cpu_usage >= 1 en PRIMAIRE - signature du bug de course." -ForegroundColor Red
            Write-Host "        Noter l'heure et signaler. Ne pas arreter le run." -ForegroundColor Red
            $lignes += "> **ALARME** : ``cpu_usage >= 1`` en primaire - signature du bug de course."
            $lignes += ""
        }
    }

    if ($i.n -lt $INTENTIONS.Count) {
        $reste = $SpacingSec - $SettleSec
        if ($reste -gt 0) {
            Write-Host ("        attente {0} s avant la suivante..." -f $reste) -ForegroundColor DarkGray
            Start-Sleep -Seconds $reste
        }
    }
    Write-Host ""
}

$lignes += "---"
$lignes += ""
$lignes += "## Apres le run"
$lignes += ""
$lignes += '```powershell'
$lignes += 'mv data data_UC5_intentions ; mkdir data'
$lignes += 'Copy-Item "logs\UC5_*" "data_UC5_intentions\"'
$lignes += '```'
$lignes += ""
$lignes += "Puis scp de latences.csv depuis le Pi, et sur le Pi :"
$lignes += "``mv latences.csv latences_UC5.csv``"

$lignes | Set-Content -Path $rapport -Encoding UTF8

Write-Host "  ---------------------------------------------------" -ForegroundColor DarkGray
Write-Host ("  Termine. Rapport : {0}" -f $rapport) -ForegroundColor Green
Write-Host ""
Write-Host "  Etape suivante :" -ForegroundColor Yellow
Write-Host "    mv data data_UC5_intentions ; mkdir data" -ForegroundColor White
Write-Host "    Copy-Item `"logs\UC5_*`" `"data_UC5_intentions\`"" -ForegroundColor White
Write-Host ""
