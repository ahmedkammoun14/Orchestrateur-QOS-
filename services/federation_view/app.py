"""
services/federation_view/app.py — Vue de fédération (lot 8a, couche données).

Service UNIQUE : lancé une seule fois pour l'ensemble des
providers (contrairement aux services de stack, un par provider — voir
launch_provider.py, qui ne le lance PAS, volontairement, §5 du lot 8a).
Agrège l'état live des deux orchestrateurs et permet de rejouer n'importe
quel cycle de décision passé via le TOPSIS RÉEL de production
(services/decision_intelligence/topsis.py, jamais réimplémenté — voir
services/federation_view/replay.py).

⚠️ ÉCRITURES STRICTEMENT LIMITÉES à deux actions explicitement déclenchées
par l'opérateur depuis la page (panneau de contrôle) : POST /api/intent
(relais vers l'intent_manager du provider CHOISI) et POST /api/reset (retour
au mode autonomous sur tous les hubs). Tout le reste — /status, /data,
/audit/log — reste en lecture seule. Ce service ne décide JAMAIS de rien
de lui-même : il n'émet aucune écriture sans action utilisateur.

Endpoints :
  GET /                                 → page HTML (lot 8b) — LIVE + rejeu historique
  GET /health                           → santé de CE service
  GET /api/state                        → état live agrégé des deux providers
  GET /api/cycles                       → journal fusionné des deux providers
  GET /api/cycle/{provider_id}/{cycle}  → rejeu complet d'un cycle passé
  POST /api/intent                      → relaie une intention vers le provider choisi
  POST /api/reset                       → repasse TOUS les hubs en mode autonomous
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx
import uvicorn
from fastapi import Body, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from shared import config
from shared.logging_utils import C, PrettyFormatter
from services.federation_view.replay import extract_gap_grade_steps, replay_topsis

# Silence des 4 tableaux ASCII que TopsisSelector.select() journalise à
# CHAQUE appel (lot 8b, §1.2) : ce processus rejoue potentiellement des
# dizaines de cycles à chaque clic dans l'historique — sans ce réglage, la
# console de federation_view serait inondée. Portée : CE PROCESSUS
# uniquement (un logging.getLogger() par nom est un singleton global au
# process Python, jamais partagé entre processus) — ni topsis.py ni
# services/decision_intelligence/ ne sont modifiés.
logging.getLogger("DecisionIntelligence.handler").setLevel(logging.WARNING)

# Timeout court : cette vue interroge des services potentiellement éteints
# (démo partielle, provider arrêté) et ne doit jamais bloquer l'utilisateur
# en attendant un pair mort — voir _get_json.
_TARGET_TIMEOUT_S: float = 5.0


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("FederationView")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(PrettyFormatter())
        logger.addHandler(h)
    logger.propagate = False
    return logger


logger = _setup_logger()

app = FastAPI(title="Federation View", version="1.0.0")

_target_lines = "\n".join(
    f"    {C.CYAN}{provider_id:<12}{C.RESET} hub={urls['hub']}  obs={urls['observability']}"
    for provider_id, urls in config.FEDERATION_VIEW_TARGETS.items()
)
logger.info(
    f"\n{'═'*60}\n"
    f"  🚀  {C.BOLD}Federation View — Démarrage{C.RESET}\n"
    f"  {C.YELLOW}Lecture + 2 actions opérateur (POST /api/intent, POST /api/reset){C.RESET}\n"
    f"  Cibles interrogées :\n"
    f"{_target_lines}\n"
    f"  Ajouter un provider N+1 = une entrée dans FEDERATION_VIEW_TARGETS, rien d'autre.\n"
    f"{'═'*60}"
)
logger.info(
    f"✅ Federation View prêt — port {C.CYAN}{config.FEDERATION_VIEW_PORT}{C.RESET}"
)


# ─────────────────────────────────────────────
#  Page HTML (lot 8b) — HTML/CSS/JS EN LIGNE, sans dépendance externe ni
#  CDN, exactement comme services/observability/app.py. Reprend la
#  structure/CSS/palette/libellés de la maquette validée
#  (federation_view_maquette.html). Toutes les données affichées viennent
#  des endpoints JSON ci-dessous — cette page ne fabrique jamais de
#  valeur : une donnée absente s'affiche "—".
# ─────────────────────────────────────────────

_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Federation View</title>
<style>
  :root{
    --bg:#0f1117; --card:#1a1d27; --card2:#151822; --border:#2d3148;
    --text:#e2e8f0; --muted:#94a3b8; --dim:#64748b;
    --p1:#3fd0c9; --p2:#f59e0b; --ok:#22c55e; --bad:#ef4444;
    --mono:'Cascadia Mono',Consolas,'SF Mono',monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.5}
  .wrap{max-width:1400px;margin:0 auto;padding:0 20px 60px}
  .mono{font-family:var(--mono);font-variant-numeric:tabular-nums}

  header{background:var(--card);border-bottom:1px solid var(--border);
         padding:14px 20px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
  .brand{font-size:16px;font-weight:600}
  .brand small{display:block;font-size:11px;color:var(--muted);font-weight:400}
  .pchip{display:flex;align-items:center;gap:7px;font-size:12px;
         border:1px solid var(--border);border-radius:6px;padding:5px 11px;background:var(--card2)}
  .pchip.unreachable{opacity:.5}
  .dot{width:9px;height:9px;border-radius:50%;flex:none}
  .role{font-size:10px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px}
  .role.act{background:rgba(34,197,94,.16);color:#4ade80}
  .role.stb{background:rgba(148,163,184,.14);color:var(--muted)}
  .readonly{margin-left:auto;font-size:11px;font-weight:600;letter-spacing:.6px;
        background:rgba(63,208,201,.12);color:#5eead4;border:1px solid rgba(63,208,201,.3);
        padding:4px 10px;border-radius:4px}

  /* ── barre temporelle ───────────────────────────────── */
  .timebar{background:var(--card2);border-bottom:1px solid var(--border);
           padding:10px 20px;display:flex;align-items:center;gap:9px;overflow-x:auto}
  .tlab{font-size:10px;font-weight:700;letter-spacing:.8px;color:var(--dim);
        text-transform:uppercase;flex:none;margin-right:4px}
  .cyc{border:1px solid var(--border);border-radius:6px;padding:6px 11px;font-size:11.5px;
       background:var(--card);display:flex;align-items:center;gap:7px;cursor:pointer;
       white-space:nowrap;flex:none;transition:.12s}
  .cyc:hover{border-color:var(--muted)}
  .cyc.sel{border-color:var(--p1);background:rgba(63,208,201,.1)}
  .cyc.live{border-color:var(--ok)}
  .cyc.live .pulse{width:7px;height:7px;border-radius:50%;background:var(--ok);flex:none}
  .path{font-size:9.5px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px}
  .path.A{background:rgba(34,197,94,.15);color:#4ade80}
  .path.B{background:rgba(245,158,11,.16);color:#fbbf24}
  .path.C{background:rgba(168,85,247,.16);color:#c084fc}
  .path.D{background:rgba(148,163,184,.16);color:var(--muted)}
  .path.DEPLOY{background:rgba(63,208,201,.16);color:var(--p1)}

  h2{font-size:11px;font-weight:700;letter-spacing:1.1px;color:var(--muted);
     text-transform:uppercase;margin:30px 0 12px}
  .panel{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px}
  .note{font-size:12.5px;color:var(--muted);line-height:1.6}
  .note b{color:var(--text)}
  .waiting{font-size:13px;color:var(--dim);padding:22px 0;text-align:center}

  /* pipeline */
  .flow{display:flex;overflow-x:auto;padding-bottom:6px}
  .step{flex:1;min-width:128px;position:relative;padding:0 5px}
  .step::after{content:'';position:absolute;top:17px;left:calc(50% + 19px);right:calc(-50% + 19px);
               height:2px;background:var(--border)}
  .step:last-child::after{display:none}
  .step.done::after{background:var(--p1)}
  .node{width:36px;height:36px;border-radius:50%;margin:0 auto 9px;display:grid;place-items:center;
        font-size:13px;font-weight:700;border:2px solid var(--border);background:var(--card2);
        color:var(--dim);position:relative;z-index:1}
  .step.done .node{border-color:var(--p1);background:rgba(63,208,201,.15);color:var(--p1)}
  .step.now .node{border-color:var(--p2);background:rgba(245,158,11,.18);color:var(--p2);
                  box-shadow:0 0 0 5px rgba(245,158,11,.13)}
  .slabel{text-align:center;font-size:11.5px;font-weight:600}
  .step.done .slabel,.step.now .slabel{color:var(--text)}
  .step:not(.done):not(.now) .slabel{color:var(--dim)}
  .sval{text-align:center;font-size:10.5px;color:var(--muted);margin-top:3px;min-height:15px}

  /* gate */
  .gate{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .gline{display:flex;align-items:center;gap:10px;padding:8px 11px;border-radius:7px;
         background:var(--card2);border:1px solid var(--border);font-size:12.5px;margin-bottom:7px}
  .gline:last-child{margin-bottom:0}
  .gkind{font-size:9.5px;font-weight:700;letter-spacing:.5px;padding:2px 7px;border-radius:3px;flex:none}
  .gkind.pri{background:rgba(63,208,201,.16);color:var(--p1)}
  .gkind.sec{background:rgba(148,163,184,.14);color:var(--muted)}
  .gres{margin-left:auto;font-weight:600;font-size:12px}

  /* onglets provider */
  .tabs{display:flex;gap:8px;margin-bottom:16px;border-bottom:1px solid var(--border);padding-bottom:0}
  .tab{border:1px solid var(--border);border-bottom:none;border-radius:7px 7px 0 0;
       padding:8px 15px;font-size:12.5px;cursor:pointer;background:var(--card2);
       color:var(--muted);display:flex;align-items:center;gap:7px;margin-bottom:-1px}
  .tab.on{background:var(--card);color:var(--text);font-weight:600}
  .tab[data-p="1"].on{border-top:2px solid var(--p1)}
  .tab[data-p="2"].on{border-top:2px solid var(--p2)}
  .pane{display:none}.pane.on{display:block}

  /* phases */
  .ph{display:flex;align-items:baseline;gap:9px;margin:0 0 9px}
  .phn{width:21px;height:21px;border-radius:50%;background:rgba(63,208,201,.15);color:var(--p1);
       font-size:11px;font-weight:700;display:grid;place-items:center;flex:none}
  .p2ctx .phn{background:rgba(245,158,11,.15);color:var(--p2)}
  .pht{font-size:13px;font-weight:600}
  .phf{font-size:11.5px;color:var(--dim);margin-left:auto;font-family:var(--mono)}
  .phase{padding:15px 0;border-bottom:1px solid var(--border)}
  .phase:last-child{border-bottom:none;padding-bottom:0}
  .phase:first-child{padding-top:0}
  .sub{font-size:11.5px;color:var(--muted);margin:0 0 10px 30px;line-height:1.55}
  .tw{margin-left:30px;overflow-x:auto}
  .crit{font-size:9.5px;font-weight:700;padding:1px 5px;border-radius:3px;margin-left:5px}
  .crit.c{background:rgba(239,68,68,.13);color:#f87171}
  .crit.b{background:rgba(34,197,94,.13);color:#4ade80}

  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{text-align:left;font-size:10px;letter-spacing:.7px;color:var(--dim);
     text-transform:uppercase;font-weight:700;padding:0 6px 7px;border-bottom:1px solid var(--border)}
  th.r,td.r{text-align:right}
  td{padding:7px 6px;border-bottom:1px solid rgba(45,49,72,.5)}
  tr:last-child td{border-bottom:none}
  tr.champ td{background:rgba(63,208,201,.06)}
  .p2ctx tr.champ td{background:rgba(245,158,11,.07)}
  .tag{font-size:10px;font-weight:700;padding:2px 6px;border-radius:3px;white-space:nowrap}
  .tag.ok{background:rgba(34,197,94,.15);color:#4ade80}
  .tag.no{background:rgba(239,68,68,.14);color:#f87171}
  .star{color:var(--p1);font-weight:700}
  .p2ctx .star{color:var(--p2)}

  .src{font-size:10.5px;color:var(--dim);border-left:2px solid var(--border);
       padding:3px 0 3px 9px;margin-bottom:14px;line-height:1.45}
  .src b{color:var(--muted)}

  /* arbitrage */
  .arb{display:grid;grid-template-columns:1fr auto 1fr;gap:22px;align-items:center}
  .offer{background:var(--card2);border:1px solid var(--border);border-radius:9px;padding:15px 17px}
  .offer.win{border-color:var(--p2);box-shadow:0 0 0 1px var(--p2)}
  .olab{font-size:11px;color:var(--muted);margin-bottom:5px}
  .ovm{font-size:15px;font-weight:600;margin-bottom:9px}
  .gg{font-size:30px;font-weight:600;letter-spacing:-.5px}
  .ggl{font-size:10px;letter-spacing:.7px;color:var(--dim);text-transform:uppercase;margin-top:1px}
  .vs{text-align:center;color:var(--dim);font-size:11px;letter-spacing:1px}
  .verdict{margin-top:18px;padding-top:15px;border-top:1px solid var(--border);
           display:flex;gap:26px;flex-wrap:wrap;align-items:baseline}
  .vitem b{display:block;font-size:10px;letter-spacing:.7px;color:var(--dim);
           text-transform:uppercase;font-weight:700;margin-bottom:3px}
  .vitem span{font-size:14px}
  .bar{height:7px;background:var(--card2);border-radius:4px;overflow:hidden;margin-top:7px;
       border:1px solid var(--border)}
  .bar i{display:block;height:100%;border-radius:3px}
  .formula{font-family:var(--mono);font-size:12.5px;background:var(--card2);border:1px solid var(--border);
           border-radius:7px;padding:11px 14px;margin:10px 0;color:var(--text);overflow-x:auto;
           white-space:pre;line-height:1.7}
  .cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .warn{padding:11px 13px;background:rgba(239,68,68,.07);border-left:2px solid var(--bad);
        border-radius:0 6px 6px 0;font-size:11.5px;color:var(--muted);line-height:1.55}
  .good{padding:11px 13px;background:rgba(34,197,94,.06);border-left:2px solid var(--ok);
        border-radius:0 6px 6px 0;font-size:11.5px;color:var(--muted);line-height:1.55}

  @media(max-width:900px){.cols,.arb,.gate{grid-template-columns:1fr}
    .sub,.tw{margin-left:0}.vs{padding:6px 0}}
</style>
</head>
<body>

<header id="section-header">
  <div class="brand">Federation View<small>arbitrage fédéré · pilotage et rejeu</small></div>
  <div id="providerChips" style="display:flex;gap:10px;flex-wrap:wrap"></div>
  <div class="readonly">CONTRÔLE + LECTURE</div>
</header>

<div class="timebar" id="section-timebar">
  <span class="tlab">Revenir à</span>
  <div id="timebarCycles" style="display:flex;align-items:center;gap:9px"></div>
</div>

<div class="wrap">

  <h2 id="section-controle">Panneau de contrôle</h2>
  <div class="panel">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <select id="ctlProvider" style="background:#0d1420;color:var(--text);border:1px solid var(--border);
              border-radius:8px;padding:9px 12px;font-family:inherit;font-size:13px"></select>
      <input id="ctlIntent" type="text" placeholder="Intention en langage naturel…"
             style="flex:1;min-width:280px;background:#0d1420;color:var(--text);border:1px solid var(--border);
                    border-radius:8px;padding:9px 12px;font-family:inherit;font-size:13px;outline:none">
      <button id="ctlSend" style="background:var(--p1);color:#fff;border:none;border-radius:8px;
              padding:9px 18px;font-weight:700;font-size:13px;cursor:pointer">Envoyer l'intention</button>
      <button id="ctlReset" style="background:transparent;color:var(--muted);border:1px solid var(--border);
              border-radius:8px;padding:9px 18px;font-weight:700;font-size:13px;cursor:pointer">Tout repasser en autonomous</button>
    </div>
    <div id="ctlMsg" class="sub" style="margin-top:10px">&nbsp;</div>
  </div>

  <h2 id="section-synthese">Synthèse par provider</h2>
  <div class="panel">
    <div id="summaryGrid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px"></div>
  </div>

  <h2 id="section-pipeline">Pipeline de décision <span id="pipelineTitle" class="mono"></span></h2>
  <div class="panel">
    <div class="flow" id="pipelineFlow"></div>
  </div>

  <h2 id="section-gate">Étape 2 — LA GATE : faut-il seulement négocier ?</h2>
  <div class="panel">
    <div id="gateBody" class="gate"></div>
  </div>

  <h2 id="section-topsis">Étape 3 — TOPSIS : départager les conformes, à l'intérieur de chaque provider</h2>
  <div class="panel">
    <div class="tabs" id="topsisTabs"></div>
    <div id="topsisPanes"></div>
    <div id="topsisConsistency"></div>
  </div>

  <h2 id="section-gapgrade">Étape 5 — Gap Grade : les 5 étapes de <span class="mono" style="text-transform:none">compute_gap_grade</span></h2>
  <div class="panel">
    <div class="tabs" id="ggTabs"></div>
    <div id="ggPanes"></div>
  </div>

  <h2 id="section-arbitrage">Étape 6 — L'arbitrage : la seule comparaison inter-provider possible</h2>
  <div class="panel">
    <div id="arbBody"></div>
  </div>

</div>

<script>
const REFRESH_MS = 750;
let pollTimer = null;
let selection = {mode: 'live'};   // ou {mode:'cycle', provider, cycle}
let lastCycles = [];
let liveCycleKey = null;   // "providerId#cycle" actuellement affiché en LIVE — évite un re-fetch/re-rendu à chaque tick si rien n'a changé

function esc(s){ return (s===null||s===undefined) ? '' : String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtNum(v, digits){
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toFixed(digits === undefined ? 4 : digits);
}
function val(v){ return (v === null || v === undefined || v === '') ? '—' : v; }
function dotColor(pid){ return pid === 'provider-2' ? 'var(--p2)' : 'var(--p1)'; }

async function fetchJSON(url){
  try{
    const r = await fetch(url);
    let body = null;
    try { body = await r.json(); } catch(e) { body = null; }
    if (!r.ok) return {__error:true, status:r.status, body: body || {}};
    return body || {};
  } catch(e){
    return {__error:true, status:0, body:{error:String(e)}};
  }
}

/* ── En-tête : providers ─────────────────────────────────────── */
function renderHeader(providers){
  const box = document.getElementById('providerChips');
  box.innerHTML = '';
  for (const pid of Object.keys(providers||{})){
    const info = providers[pid] || {};
    const chip = document.createElement('div');
    if (!info.reachable){
      chip.className = 'pchip unreachable';
      chip.innerHTML = `<span class="dot" style="background:${dotColor(pid)}"></span>${esc(pid)}`
        + `<span class="role stb">PROVIDER INJOIGNABLE</span>`;
    } else {
      const st = info.status || {};
      const role = st.role || 'standby';
      const roleCls = role === 'active' ? 'act' : 'stb';
      chip.className = 'pchip';
      chip.innerHTML = `<span class="dot" style="background:${dotColor(pid)}"></span>${esc(pid)}`
        + `<span class="role ${roleCls}">${esc(role.toUpperCase())}</span>`
        + `<span class="mono" style="color:var(--dim)">${esc(val(st.hosting_vm))}</span>`;
    }
    box.appendChild(chip);
  }
}

/* ── Barre temporelle ─────────────────────────────────────────── */
function renderTimebar(cycles){
  lastCycles = cycles || [];
  const box = document.getElementById('timebarCycles');
  box.innerHTML = '';

  const liveSel = selection.mode === 'live';
  const live = document.createElement('div');
  live.className = 'cyc live' + (liveSel ? ' sel' : '');
  live.innerHTML = `<span class="pulse"></span>LIVE`;
  live.addEventListener('click', selectLive);
  box.appendChild(live);

  for (const c of lastCycles){
    const chip = document.createElement('div');
    const sel = !liveSel && selection.provider === c.provider_id && selection.cycle === c.cycle;
    chip.className = 'cyc' + (sel ? ' sel' : '');
    const pathCls = c.path || 'D';
    let traj;
    if (c.decision === 'migrate' && (c.from_vm || c.to_vm)) traj = `${val(c.from_vm)} → ${val(c.to_vm)}`;
    else if (c.path === 'C') traj = 'aucune conforme';
    else if (c.path === 'D') traj = 'sans données';
    else traj = 'maintien';
    chip.innerHTML = `<span class="path ${esc(pathCls)}">${esc(pathCls)}</span>`
      + `<span class="mono">${esc(c.provider_id)}#${esc(c.cycle)}</span>`
      + `<span style="color:var(--muted)">${esc(traj)}</span>`;
    chip.addEventListener('click', () => selectCycle(c.provider_id, c.cycle));
    box.appendChild(chip);
  }
}

/* ── Sélection LIVE / historique (lot 10, §1) ─────────────────────
   LIVE : la page suit AUTOMATIQUEMENT le dernier cycle de /api/cycles,
   quel que soit le provider qui l'a produit, et re-sonde pour basculer
   dès qu'un cycle plus récent apparaît. HISTORIQUE : un clic sur une
   puce fige l'affichage sur ce cycle — plus aucune bascule automatique
   tant que l'utilisateur n'a pas recliqué sur LIVE. Dans les deux cas,
   le bandeau providers et la barre temporelle continuent d'être
   rafraîchis (refreshLive tourne en continu, sans interruption). ── */
function selectLive(){
  selection = {mode: 'live'};
  liveCycleKey = null;   // force le rendu immédiat du dernier cycle connu, même si c'est le même qu'avant
  renderTimebar(lastCycles);
  followLastCycle();
}

async function selectCycle(providerId, cycle){
  selection = {mode: 'cycle', provider: providerId, cycle: cycle};
  renderTimebar(lastCycles);
  const resp = await fetchJSON(`/api/cycle/${encodeURIComponent(providerId)}/${encodeURIComponent(cycle)}`);
  if (selection.mode !== 'cycle' || selection.provider !== providerId || selection.cycle !== cycle) return; // l'utilisateur a changé de sélection entre-temps
  if (resp.__error){
    renderCycleError(resp, providerId, cycle);
    return;
  }
  renderCycle(resp, {live: false});
}

/* Suivi automatique du dernier cycle en mode LIVE — appelé au clic sur
   LIVE (rendu immédiat depuis lastCycles déjà connu) et à chaque tick
   de refreshLive (bascule dès qu'un cycle plus récent apparaît). */
async function followLastCycle(){
  if (!lastCycles.length){
    liveCycleKey = null;
    renderWaitingSections();
    return;
  }
  const latest = lastCycles[0];   // /api/cycles est trié par cycle décroissant
  const key = `${latest.provider_id}#${latest.cycle}`;
  if (key === liveCycleKey) return;   // déjà affiché, rien à refaire

  const resp = await fetchJSON(`/api/cycle/${encodeURIComponent(latest.provider_id)}/${encodeURIComponent(latest.cycle)}`);
  if (selection.mode !== 'live') return;   // l'utilisateur est passé en historique pendant le fetch
  if (resp.__error){
    liveCycleKey = null;
    renderCycleError(resp, latest.provider_id, latest.cycle);
    return;
  }
  liveCycleKey = key;
  renderCycle(resp, {live: true});
}

function renderCycleError(resp, providerId, cycle){
  const msg = (resp.body && resp.body.error) ? resp.body.error : `erreur HTTP ${resp.status}`;
  document.getElementById('pipelineTitle').innerHTML =
    `— <span style="color:#f87171">${esc(msg)} (${esc(providerId)}#${esc(cycle)})</span>`;
  document.getElementById('pipelineFlow').innerHTML =
    `<div class="waiting" style="color:#f87171">${esc(msg)}</div>`;
  document.getElementById('gateBody').innerHTML = '';
  document.getElementById('topsisTabs').innerHTML = '';
  document.getElementById('topsisPanes').innerHTML = `<div class="waiting">${esc(msg)}</div>`;
  document.getElementById('topsisConsistency').innerHTML = '';
  document.getElementById('ggTabs').innerHTML = '';
  document.getElementById('ggPanes').innerHTML = `<div class="waiting">${esc(msg)}</div>`;
  document.getElementById('arbBody').innerHTML = `<div class="waiting">${esc(msg)}</div>`;
}

/* Cas limite (lot 10, §1) : /api/cycles est vide — aucune décision n'a
   encore été prise par aucun provider. Seul cas restant où l'on montre
   encore un message d'attente en mode LIVE. */
function renderWaitingSections(){
  document.getElementById('pipelineTitle').textContent = '(aucun cycle de décision archivé)';
  document.getElementById('pipelineFlow').innerHTML =
    '<div class="waiting">en attente du premier cycle de décision</div>';
  document.getElementById('gateBody').innerHTML =
    '<div class="waiting" style="grid-column:1/-1">en attente du premier cycle de décision</div>';
  document.getElementById('topsisTabs').innerHTML = '';
  document.getElementById('topsisPanes').innerHTML =
    '<div class="waiting">en attente du premier cycle de décision</div>';
  document.getElementById('topsisConsistency').innerHTML = '';
  document.getElementById('ggTabs').innerHTML = '';
  document.getElementById('ggPanes').innerHTML =
    '<div class="waiting">en attente du premier cycle de décision</div>';
  document.getElementById('arbBody').innerHTML =
    '<div class="waiting">en attente du premier cycle de décision</div>';
}

/* ── Rendu d'un cycle (LIVE suivi ou HISTORIQUE rejoué) ──────────── */
function renderCycle(resp, opts){
  opts = opts || {};
  const entry = resp.entry || {};
  const providerId = resp.provider_id;

  document.getElementById('pipelineTitle').innerHTML = opts.live
    ? `#${esc(resp.cycle)} <span style="color:var(--ok)">— dernier cycle, suivi en direct</span>`
    : `#${esc(resp.cycle)} <span style="color:var(--p1)">(rejoué depuis l'audit)</span>`;

  renderPipeline(entry, providerId);
  renderGate(entry);
  renderTopsis(resp, entry, providerId);
  renderGapGrade(resp, entry);
  renderArbitrage(resp, entry, providerId);

  // Synthèse : classement TOPSIS et Gap Grade de CHAQUE provider, extraits
  // des bids archivés du cycle affiché.
  const bidsByProvider = {};
  for (const b of ((entry.reasoning || {}).bids || [])){
    if (b && b.provider_id) bidsByProvider[b.provider_id] = b;
  }
  renderSummaryBids(bidsByProvider);
}

/* ── Pipeline 9 étapes ─────────────────────────────────────────── */
function renderPipeline(entry, providerId){
  const reasoning = entry.reasoning || {};
  const bids = reasoning.bids || [];
  const compliant = reasoning.compliant_vms || [];
  const isFederated = !!reasoning.federated;
  const decision = entry.decision;
  const path = entry.provider_path;
  const providerUsed = entry.provider_used;

  const steps = [
    {label: 'Violation',    val: val(reasoning.vm_active), done: true},
    {label: 'LA GATE',      val: 'ouverte',                done: true},
    {label: 'TOPSIS local', val: compliant.length ? compliant.length + ' conforme(s)' : 'aucune conforme',
     done: isFederated},
    {label: 'Broadcast',    val: isFederated ? (bids.length - 1) + ' pair(s)' : '—', done: isFederated},
    {label: 'Gap Grade',    val: bids.length ? bids.length + ' bid(s)' : '—', done: bids.length > 0},
    {label: 'Arbitrage',    val: path ? 'chemin ' + path : '—', done: !!path},
    {label: 'Verdict',      val: val(decision), done: !!path},
    {label: 'Migration',    val: decision === 'migrate' ? 'kubectl' : '—', done: decision === 'migrate'},
    {label: 'Award',        val: (decision === 'migrate' && providerUsed && providerUsed !== providerId)
                                  ? '→ ' + providerUsed : '—',
     done: decision === 'migrate' && providerUsed && providerUsed !== providerId},
  ];

  let lastDoneIdx = -1;
  steps.forEach((s, i) => { if (s.done) lastDoneIdx = i; });

  const box = document.getElementById('pipelineFlow');
  box.innerHTML = '';
  steps.forEach((s, i) => {
    const cls = i === lastDoneIdx ? 'now' : (s.done && i < lastDoneIdx ? 'done' : '');
    const div = document.createElement('div');
    div.className = 'step' + (cls ? ' ' + cls : '');
    div.innerHTML = `<div class="node">${i+1}</div><div class="slabel">${esc(s.label)}</div>`
      + `<div class="sval">${esc(s.val)}</div>`;
    box.appendChild(div);
  });
}

/* ── LA GATE (lot 10, §2) ─────────────────────────────────────────
   LA GATE s'ouvre si UNE SEULE des 7 prédictions ML dépasse le seuil
   (hub/orchestrator_core.py:548, any(p > threshold for p in preds)),
   alors que la conformité du Gap Grade se juge sur une valeur AGRÉGÉE.
   Elle est donc délibérément plus sensible — c'est ce qui la rend
   proactive — et peut s'ouvrir même quand les 3 SLOs affichés sont
   marqués « conforme » (ils le sont, sur l'agrégat ; le signal qui a
   ouvert la gate porte sur une prédiction individuelle, jamais
   ré-évaluée ici contre une mesure brute). ── */
function renderGate(entry){
  const slos = entry.slos_active || [];
  const reasoning = entry.reasoning || {};
  const vmActive = reasoning.vm_active;
  const violatedMetrics = entry.violated_metrics || [];
  const evals = reasoning.evaluations || [];
  const evalForActive = evals.find(e => e.vm_id === vmActive);
  const detail = (evalForActive && evalForActive.detail) || {};

  const linesBox = document.createElement('div');
  const breachedMetrics = [];

  if (!slos.length){
    linesBox.innerHTML = '<div class="waiting">aucun SLO archivé pour ce cycle</div>';
  } else {
    for (const slo of slos){
      const isPrimary = !!slo.is_primary;
      const d = detail[slo.metric];
      const breached = (d === undefined) ? null : (Number(d) > 0);
      if (isPrimary && breached) { breachedMetrics.push(slo.metric); }
      const kindCls = isPrimary ? 'pri' : 'sec';
      const kindLabel = isPrimary ? 'PRIMAIRE' : 'SECONDAIRE';
      const resText = breached === null ? '—' : (breached ? 'brèche' : 'conforme');
      const resColor = breached === null ? 'var(--muted)' : (breached ? '#f87171' : '#4ade80');
      const line = document.createElement('div');
      line.className = 'gline';
      line.innerHTML = `<span class="gkind ${kindCls}">${kindLabel}</span>`
        + `<span class="mono">${esc(slo.metric)} ${esc(slo.operator)} ${esc(slo.threshold)}${esc(slo.unit||'')}</span>`
        + `<span class="gres" style="color:${resColor}">${resText}</span>`;
      linesBox.appendChild(line);
    }
  }

  const resultBox = document.createElement('div');
  resultBox.style.display = 'flex';
  resultBox.style.alignItems = 'center';
  // Ordre de préférence des sources (§2) : violated_metrics de l'audit,
  // puis les évaluations archivées, puis — faute d'un signal de brèche
  // agrégée archivé — l'explication du signal proactif ML. Jamais de
  // ré-évaluation des seuils contre des mesures brutes.
  let explain;
  if (violatedMetrics.length){
    explain = `La brèche <b style="color:var(--text)">${esc(violatedMetrics.join(', '))}</b> l'ouvre.`;
  } else if (breachedMetrics.length){
    explain = `La brèche <b style="color:var(--text)">${esc(breachedMetrics.join(', '))}</b> (primaire) l'ouvre.`;
  } else {
    explain = `Gate ouverte par un signal proactif. Au moins une des 7 prédictions ML dépasse le seuil, `
      + `alors que la valeur agrégée reste conforme — LA GATE est volontairement plus sensible que le test `
      + `de conformité, c'est ce qui permet d'anticiper la violation au lieu de la subir.`;
  }
  resultBox.innerHTML = `
    <div style="background:var(--card2);border:1px solid var(--p1);border-radius:9px;padding:13px 17px;width:100%">
      <div style="font-size:11px;color:var(--muted);margin-bottom:5px">résultat de la gate</div>
      <div style="font-size:19px;font-weight:600;color:var(--p1)">OUVERTE</div>
      <div style="font-size:11.5px;color:var(--muted);margin-top:6px;line-height:1.5">${explain}</div>
    </div>`;

  const box = document.getElementById('gateBody');
  box.innerHTML = '';
  box.appendChild(linesBox);
  box.appendChild(resultBox);
}

/* ── TOPSIS (onglets) ─────────────────────────────────────────── */
function renderTopsis(resp, entry, providerId){
  const reasoning = entry.reasoning || {};
  const bids = reasoning.bids || [];
  const providerIds = [...new Set(bids.map(b => b.provider_id).filter(Boolean))];
  if (!providerIds.includes(providerId)) providerIds.unshift(providerId);

  const tabsBox = document.getElementById('topsisTabs');
  const panesBox = document.getElementById('topsisPanes');
  tabsBox.innerHTML = '';
  panesBox.innerHTML = '';

  providerIds.forEach((pid, idx) => {
    const num = idx === 0 ? '1' : '2';
    const tab = document.createElement('div');
    tab.className = 'tab' + (idx === 0 ? ' on' : '');
    tab.dataset.p = num; tab.dataset.g = 'tp';
    tab.innerHTML = `<span class="dot" style="background:${dotColor(pid)}"></span>${esc(pid)}`;
    tabsBox.appendChild(tab);

    const pane = document.createElement('div');
    pane.className = 'pane' + (idx === 0 ? ' on' : '') + (num === '2' ? ' p2ctx' : '');
    pane.dataset.g = 'tp'; pane.dataset.i = num;

    if (pid === providerId){
      pane.innerHTML = renderTopsisReplayHtml(resp.replay);
    } else {
      pane.innerHTML = renderTopsisPeerHtml(pid, bids.find(b => b.provider_id === pid));
    }
    panesBox.appendChild(pane);
  });

  wireTabs();

  const consBox = document.getElementById('topsisConsistency');
  consBox.innerHTML = '';
  if (resp.replay && resp.replay.consistent === false){
    const w = document.createElement('div');
    w.className = 'warn';
    w.style.marginTop = '18px';
    w.innerHTML = `<b style="color:#fca5a5">Divergence détectée entre le rejeu et le TOPSIS de production.</b> `
      + esc(resp.replay.warning || '');
    consBox.appendChild(w);
  }
}

function renderTopsisReplayHtml(replay){
  if (!replay || !replay.phases || !replay.phases.length){
    const reason = (replay && replay.reason) ? replay.reason : 'rejeu indisponible';
    return `<div class="waiting">${esc(reason)}</div>`;
  }
  const names = {matrice: 'Matrice de décision', normalisation: 'Normalisation min-max',
                 ponderation: 'Pondération', distances_et_score: 'Idéaux, distances, score'};
  let html = '';
  replay.phases.forEach((phase, i) => {
    html += `<div class="phase"><div class="ph"><span class="phn">${i+1}</span>`
      + `<span class="pht">${esc(names[phase.name] || phase.name)}</span></div>`;
    if (phase.name === 'distances_et_score'){
      html += '<div class="tw"><table><tr><th>VM</th><th class="r">d+</th><th class="r">d-</th><th class="r">score</th></tr>';
      const maxScore = Math.max(...phase.rows.map(r => r.score));
      for (const row of phase.rows){
        const champ = row.score === maxScore ? ' class="champ"' : '';
        const star = row.score === maxScore ? ' <span class="star">★</span>' : '';
        html += `<tr${champ}><td class="mono">${esc(row.vm_id)}${star}</td>`
          + `<td class="r mono">${fmtNum(row.d_plus)}</td><td class="r mono">${fmtNum(row.d_minus)}</td>`
          + `<td class="r mono">${fmtNum(row.score)}</td></tr>`;
      }
      html += '</table></div>';
    } else {
      const headers = phase.headers || [];
      html += '<div class="tw"><table><tr><th>VM</th>' + headers.map(h => `<th class="r">${esc(h)}</th>`).join('') + '</tr>';
      for (const row of phase.rows){
        html += `<tr><td class="mono">${esc(row.vm_id)}</td>`
          + row.values.map(v => `<td class="r mono">${fmtNum(v, 3)}</td>`).join('') + '</tr>';
      }
      html += '</table></div>';
    }
    html += '</div>';
  });
  return html;
}

function renderTopsisPeerHtml(pid, bid){
  const scores = bid && bid.placement_plan ? (bid.placement_plan.vm_scores || {}) : {};
  const entries = Object.entries(scores);
  let html = `<div class="note" style="margin-bottom:14px">Rejeu phase par phase indisponible :
    seules les mesures brutes de <b>l'initiateur</b> de ce cycle sont archivées dans cette entrée
    d'audit. Le classement TOPSIS FINAL de ${esc(pid)}, tel qu'il a réellement voté, reste
    consultable ci-dessous.</div>`;
  if (!entries.length){
    html += '<div class="waiting">aucun classement archivé pour ce provider sur ce cycle</div>';
    return html;
  }
  html += '<div class="tw" style="margin-left:0"><table><tr><th>VM</th><th class="r">score TOPSIS</th></tr>';
  for (const [vm, score] of entries){
    html += `<tr><td class="mono">${esc(vm)}</td><td class="r mono">${fmtNum(score)}</td></tr>`;
  }
  html += '</table></div>';
  return html;
}

/* ── Gap Grade (onglets) ──────────────────────────────────────── */
function renderGapGrade(resp, entry){
  const grades = resp.gap_grades || [];
  const slosActive = entry.slos_active || [];
  const tabsBox = document.getElementById('ggTabs');
  const panesBox = document.getElementById('ggPanes');
  tabsBox.innerHTML = '';
  panesBox.innerHTML = '';

  if (!grades.length){
    panesBox.innerHTML = '<div class="waiting">aucun bid archivé pour ce cycle (chemin mono-provider ou antérieur au lot 6a)</div>';
    return;
  }

  grades.forEach((g, idx) => {
    const num = idx === 0 ? '1' : '2';
    const tab = document.createElement('div');
    tab.className = 'tab' + (idx === 0 ? ' on' : '');
    tab.dataset.p = num; tab.dataset.g = 'gg';
    tab.innerHTML = `<span class="dot" style="background:${dotColor(g.provider_id)}"></span>`
      + `${esc(g.provider_id)} · ${esc(val(g.vm_id))}`;
    tabsBox.appendChild(tab);

    const pane = document.createElement('div');
    pane.className = 'pane' + (idx === 0 ? ' on' : '') + (num === '2' ? ' p2ctx' : '');
    pane.dataset.g = 'gg'; pane.dataset.i = num;
    pane.innerHTML = renderGapGradePaneHtml(g, slosActive);
    panesBox.appendChild(pane);
  });

  wireTabs();
}

/* Les 5 étapes de compute_gap_grade (lot 10, §3), toutes reconstruites
   depuis bid["gap_grade"]["detail"] (déjà δ après plancher — jamais
   recalculé depuis une mesure brute) et les SLOs archivés. Seule la
   normalisation des poids (étape 4) et le montage des termes de
   Tchebycheff (étape 5) sont recalculés ICI — à partir de nombres déjà
   archivés, jamais d'un seuil ré-évalué contre une mesure brute — pour
   pouvoir afficher wₙₒᵣₘ et vérifier G par la formule. Le G AFFICHÉ EN
   RÉFÉRENCE reste toujours celui archivé dans le bid (gap_grade.value),
   jamais substitué silencieusement par la reconstitution. */
function renderGapGradePaneHtml(g, slosActive){
  const steps = g.steps || [];
  let html = `<div class="note" style="margin-bottom:14px">Conformité archivée :
    <b>${g.is_compliant ? 'conforme' : 'non conforme'}</b> · Gap Grade final (archivé, servi à l'arbitrage)
    <b class="mono">${fmtNum(g.value)}</b>.</div>`;

  // ── Étape 1 · Filtrer les primaires ──────────────────────────────
  html += `<div class="phase"><div class="ph"><span class="phn">1</span>`
    + `<span class="pht">Filtrer les primaires</span><span class="phf">règle métier</span></div>`
    + `<div class="sub">Un SLO secondaire ne pèse <b>jamais</b> sur le Gap Grade — seuls les primaires retenus ci-dessous continuent aux étapes suivantes.</div>`;
  if (!slosActive.length){
    html += '<div class="sub">aucun SLO archivé pour ce cycle</div></div>';
  } else {
    html += '<div class="tw"><table><tr><th>SLO</th><th>Type</th><th>Statut</th></tr>';
    for (const slo of slosActive){
      const isPrimary = !!slo.is_primary;
      const retained = steps.some(s => s.metric === slo.metric);
      html += `<tr><td class="mono">${esc(slo.metric)} ${esc(slo.operator)} ${esc(slo.threshold)}</td>`
        + `<td><span class="gkind ${isPrimary ? 'pri' : 'sec'}">${isPrimary ? 'PRIMAIRE' : 'SECONDAIRE'}</span></td>`
        + `<td><span class="tag ${retained ? 'ok' : 'no'}">${retained ? '✓ retenu' : '✕ écarté'}</span></td></tr>`;
    }
    html += '</table></div>';
  }
  html += '</div>';

  if (!steps.length){
    html += '<div class="phase"><div class="sub">aucun SLO primaire retenu pour ce bid — Gap Grade non évaluable</div></div>';
    return html;
  }

  // ── Étape 2 · Écarts signés ───────────────────────────────────────
  html += `<div class="phase"><div class="ph"><span class="phn">2</span>`
    + `<span class="pht">Écarts signés</span><span class="phf">δ = (v − τ)/τ</span></div>`
    + `<div class="sub">δ lu tel quel dans le bid (déjà après plancher) — jamais recalculé depuis une mesure brute.</div>`
    + '<div class="tw"><table><tr><th>SLO</th><th class="r">Seuil τ</th><th class="r">δ</th></tr>';
  for (const s of steps){
    html += `<tr><td class="mono">${esc(s.metric)} ${esc(s.operator)}</td>`
      + `<td class="r mono">${fmtNum(s.threshold, 2)}</td><td class="r mono">${fmtNum(s.delta)}</td></tr>`;
  }
  html += '</table></div></div>';

  // ── Étape 3 · Plancher δ ≥ −1 ─────────────────────────────────────
  html += `<div class="phase"><div class="ph"><span class="phn">3</span>`
    + `<span class="pht">Plancher δ ≥ −1</span><span class="phf">DELTA_FLOOR = −1.0</span></div>`
    + '<div class="tw"><table><tr><th>SLO</th><th class="r">δ</th><th>Plancher</th></tr>';
  for (const s of steps){
    const floored = s.delta === -1.0;
    html += `<tr><td class="mono">${esc(s.metric)}</td><td class="r mono">${fmtNum(s.delta)}</td>`
      + `<td>${floored ? '<span class="tag no">plancher appliqué</span>' : '<span class="tag ok">inchangé</span>'}</td></tr>`;
  }
  html += '</table></div></div>';

  // ── Étape 4 · Normalisation des poids ────────────────────────────
  const totalWeightRaw = steps.reduce((sum, s) => sum + (Number(s.weight) || 0), 0);
  const useUniform = totalWeightRaw <= 0;
  const normWeights = steps.map(s =>
    useUniform ? (1 / steps.length) : ((Number(s.weight) || 0) / totalWeightRaw)
  );
  html += `<div class="phase"><div class="ph"><span class="phn">4</span>`
    + `<span class="pht">Normalisation des poids</span><span class="phf">wₙₒᵣₘ = wᵢ / Σw</span></div>`
    + '<div class="tw"><table><tr><th>SLO</th><th class="r">Poids brut</th><th class="r">Poids normalisé</th></tr>';
  steps.forEach((s, i) => {
    html += `<tr><td class="mono">${esc(s.metric)}</td><td class="r mono">${fmtNum(s.weight, 2)}</td>`
      + `<td class="r mono">${fmtNum(normWeights[i])}</td></tr>`;
  });
  html += '</table></div>';
  if (steps.length === 1){
    html += `<div class="sub">Un seul SLO primaire → poids normalisé 1.00 → G = δ exactement.</div>`;
  }
  html += '</div>';

  // ── Étape 5 · Tchebycheff ─────────────────────────────────────────
  const terms     = steps.map((s, i) => normWeights[i] * s.delta);
  const maxTerm    = Math.max(...terms);
  const sumTerms   = terms.reduce((a, b) => a + b, 0);
  const rho        = 0.1;
  const recomputed = (maxTerm + rho * sumTerms) / (1 + rho);
  const archived   = g.value;
  const diverges = (archived === null || archived === undefined)
    ? true
    : Math.abs(archived - recomputed) > 1e-4;

  html += `<div class="phase"><div class="ph"><span class="phn">5</span>`
    + `<span class="pht" style="color:var(--p1)">Tchebycheff</span>`
    + `<span class="phf">G = (max + ρ·Σ) / (1 + ρ)</span></div>`
    + `<div class="formula">termes wₙₒᵣₘ·δ = [${terms.map(t => fmtNum(t)).join(', ')}]\n`
    + `max(termes)                       = ${fmtNum(maxTerm)}\n`
    + `Σ(termes)                         = ${fmtNum(sumTerms)}\n`
    + `ρ                                 = ${fmtNum(rho, 2)}\n`
    + `G archivé (servi à l'arbitrage)   = <b>${fmtNum(archived)}</b>\n`
    + `G reconstitué depuis la formule   = ${fmtNum(recomputed)}</div>`;
  if (diverges){
    html += `<div class="warn" style="margin-top:10px">
      <b style="color:#fca5a5">Écart entre la valeur archivée et la formule reconstituée.</b>
      La valeur archivée ci-dessus reste la référence — celle qui a réellement servi à l'arbitrage — jamais substituée silencieusement.</div>`;
  }
  html += '</div>';

  return html;
}

/* ── Arbitrage ────────────────────────────────────────────────── */
function renderArbitrage(resp, entry, providerId){
  const grades = resp.gap_grades || [];
  const box = document.getElementById('arbBody');

  if (grades.length < 1){
    box.innerHTML = '<div class="waiting">aucun bid archivé pour ce cycle</div>';
    return;
  }

  const mine = grades.find(g => g.provider_id === providerId) || grades[0];
  const other = grades.find(g => g.provider_id !== mine.provider_id);
  const providerUsed = entry.provider_used;
  const decision = entry.decision;
  const path = entry.provider_path;

  let html = '<div class="arb">';
  html += offerHtml(mine, providerUsed === mine.provider_id);
  html += '<div class="vs">contre</div>';
  html += other ? offerHtml(other, providerUsed === (other && other.provider_id))
                : '<div class="offer"><div class="olab">aucune offre concurrente</div></div>';
  html += '</div>';

  const gapMine = mine.value;
  const gapOther = other ? other.value : null;
  const ecart = (gapMine !== null && gapMine !== undefined && gapOther !== null && gapOther !== undefined)
    ? Math.abs(gapMine - gapOther) : null;

  html += '<div class="verdict">';
  html += `<div class="vitem"><b>Écart</b><span class="mono">${fmtNum(ecart)}</span></div>`;
  html += `<div class="vitem"><b>Dead-band</b><span class="mono">— (non archivé)</span></div>`;
  html += `<div class="vitem"><b>Chemin</b><span><span class="path ${esc(path||'D')}">${esc(path||'—')}</span></span></div>`;
  html += `<div class="vitem"><b>Verdict</b><span class="mono">${esc(val(entry.from_vm))} → <b>${esc(val(entry.to_vm))}</b></span></div>`;
  const awardTxt = (decision === 'migrate' && providerUsed && providerUsed !== providerId)
    ? '→ ' + providerUsed : '—';
  html += `<div class="vitem"><b>Award</b><span class="mono">${esc(awardTxt)}</span></div>`;
  html += '</div>';

  box.innerHTML = html;
}

function offerHtml(g, isWinner){
  const pct = Math.min(100, Math.max(0, (Math.abs(g.value||0)) * 100));
  return `<div class="offer${isWinner ? ' win' : ''}">`
    + `<div class="olab">offre de ${esc(g.provider_id)}</div>`
    + `<div class="ovm mono">${esc(val(g.vm_id))}</div>`
    + `<div class="gg" style="color:${dotColor(g.provider_id)}">${fmtNum(g.value)}</div>`
    + `<div class="ggl">Gap Grade</div>`
    + `<div class="bar"><i style="width:${pct}%;background:${dotColor(g.provider_id)}"></i></div>`
    + `</div>`;
}

/* ── Onglets (identique à la maquette) ───────────────────────── */
function wireTabs(){
  document.querySelectorAll('.tab').forEach(t => {
    t.onclick = () => {
      const g = t.dataset.g, i = t.dataset.p;
      document.querySelectorAll(`.tab[data-g="${g}"]`).forEach(x => x.classList.toggle('on', x === t));
      document.querySelectorAll(`.pane[data-g="${g}"]`).forEach(p =>
        p.classList.toggle('on', p.dataset.i === i));
    };
  });
}

/* ── Panneau de contrôle + synthèse (bloc additif) ─────────────── */
let ctlProviderFilled = false;
let lastBidsByProvider = null;   // conservé : le grid est reconstruit à chaque tick

function renderSummaryBids(bidsByProvider){
  lastBidsByProvider = bidsByProvider;
  for (const pid of Object.keys(bidsByProvider || {})){
    const box = document.getElementById('sum-bid-' + pid);
    if (!box) continue;
    const bid    = bidsByProvider[pid] || {};
    const plan   = bid.placement_plan || {};
    const gg     = bid.gap_grade || {};
    const scores = plan.vm_scores || {};
    const rows = Object.keys(scores)
      .sort((a, b) => scores[b] - scores[a])
      .map(vm => {
        const champ = (vm === plan.vm_id);
        return `<tr${champ ? ' class="champ"' : ''}><td class="mono">${esc(vm)}`
             + `${champ ? ' <span class="star">★</span>' : ''}</td>`
             + `<td class="r mono">${fmtNum(scores[vm])}</td></tr>`;
      }).join('');
    box.innerHTML =
        `<div class="sub" style="margin-top:10px">TOPSIS — champion proposé : `
      + `<b class="mono">${esc(val(plan.vm_id))}</b></div>`
      + (rows
          ? `<div class="tw"><table><tr><th>VM</th><th class="r">score TOPSIS</th></tr>${rows}</table></div>`
          : `<div class="sub">aucun classement archivé (aucune VM conforme sur ce cycle)</div>`)
      + `<div class="sub" style="margin-top:8px">Gap Grade (Tchebycheff) : `
      + `<b class="mono">${fmtNum(gg.value)}</b> · ${gg.is_compliant ? 'conforme' : 'non conforme'}</div>`;
  }
}

function renderSummaryLive(providers){
  const pids = Object.keys(providers || {});
  const sel  = document.getElementById('ctlProvider');
  if (!ctlProviderFilled && pids.length){
    sel.innerHTML = pids.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
    ctlProviderFilled = true;
  }

  const grid = document.getElementById('summaryGrid');
  if (!pids.length){ grid.innerHTML = '<div class="waiting">aucun provider joignable</div>'; return; }

  const sloRow = s => `<tr><td class="mono">${esc(s.metric)} ${esc(s.operator)} `
    + `${fmtNum(s.threshold, 2)} ${esc(s.unit || '')}</td>`
    + `<td class="r mono">${Math.round((s.weight || 0) * 100)}%</td></tr>`;

  grid.innerHTML = pids.map(pid => {
    const p = providers[pid] || {};
    if (!p.reachable){
      return `<div class="phase"><div class="ph"><span class="pht">${esc(pid)}</span></div>`
           + `<div class="waiting">hub injoignable</div></div>`;
    }
    const st   = p.status || {};
    const slos = st.active_slos || [];
    const prim = slos.filter(s => s.is_primary);
    const sec  = slos.filter(s => !s.is_primary);
    return `<div class="phase">
      <div class="ph"><span class="dot" style="background:${dotColor(pid)}"></span>
        <span class="pht">${esc(pid)}</span>
        <span class="phf">${esc((st.role || '—').toUpperCase())} · ${esc(val(st.service_vm))}
          · ${esc(st.mode || '—')} · cycle ${esc(val(st.cycle))}</span></div>
      <div class="sub">SLOs PRIMAIRES — décident de la conformité et ouvrent la gate</div>
      ${prim.length ? `<div class="tw"><table>${prim.map(sloRow).join('')}</table></div>`
                    : '<div class="sub">aucun</div>'}
      <div class="sub" style="margin-top:8px">SLOs SECONDAIRES — pondèrent TOPSIS uniquement</div>
      ${sec.length ? `<div class="tw"><table>${sec.map(sloRow).join('')}</table></div>`
                   : '<div class="sub">aucun</div>'}
      <div id="sum-bid-${esc(pid)}"></div>
    </div>`;
  }).join('');

  // Le grid vient d'être reconstruit : on réinjecte le dernier bloc bids connu.
  if (lastBidsByProvider) renderSummaryBids(lastBidsByProvider);
}

async function ctlSendIntent(){
  const sel = document.getElementById('ctlProvider');
  const inp = document.getElementById('ctlIntent');
  const msg = document.getElementById('ctlMsg');
  const btn = document.getElementById('ctlSend');
  const txt = inp.value.trim();
  if (!txt){ inp.focus(); return; }
  btn.disabled = true;
  msg.textContent = 'envoi en cours (appel LLM, quelques secondes)…';
  const resp = await fetchJSON2('/api/intent', {provider_id: sel.value, intention: txt});
  if (resp.__error){
    msg.innerHTML = `<span style="color:#f87171">échec : ${esc((resp.body && resp.body.error) || resp.status)}</span>`;
  } else {
    const n = (resp.response && resp.response.slos_count);
    msg.innerHTML = `<span style="color:var(--ok)">intention acceptée par ${esc(resp.provider_id)}`
                  + `${n != null ? ' — ' + esc(n) + ' SLO(s)' : ''} · propagée aux pairs</span>`;
    inp.value = '';
  }
  btn.disabled = false;
}

async function ctlResetAll(){
  const msg = document.getElementById('ctlMsg');
  const btn = document.getElementById('ctlReset');
  btn.disabled = true;
  msg.textContent = 'réinitialisation…';
  const resp = await fetchJSON2('/api/reset', {});
  if (resp.__error){
    msg.innerHTML = `<span style="color:#f87171">échec de la réinitialisation</span>`;
  } else {
    const ok = (resp.reset || []).join(', ') || 'aucun';
    const ko = (resp.errors || []).length;
    msg.innerHTML = `<span style="color:var(--ok)">mode autonomous rétabli : ${esc(ok)}</span>`
                  + (ko ? ` <span style="color:#fbbf24">· ${ko} échec(s)</span>` : '');
  }
  btn.disabled = false;
}

/* POST JSON — pendant de fetchJSON (qui ne fait que du GET). */
async function fetchJSON2(url, body){
  try{
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                                body: JSON.stringify(body)});
    let parsed = null;
    try { parsed = await r.json(); } catch(e) { parsed = null; }
    if (!r.ok) return {__error:true, status:r.status, body: parsed || {}};
    return parsed || {};
  }catch(e){
    return {__error:true, status:0, body:{error:String(e)}};
  }
}

document.getElementById('ctlSend').addEventListener('click', ctlSendIntent);
document.getElementById('ctlReset').addEventListener('click', ctlResetAll);
document.getElementById('ctlIntent').addEventListener('keydown', e => { if (e.key === 'Enter') ctlSendIntent(); });

/* ── Polling (lot 10, §1) ──────────────────────────────────────────
   Tourne EN CONTINU, dans les deux modes — le bandeau providers et la
   barre temporelle ne doivent jamais geler, y compris pendant la
   consultation d'un cycle historique. Seul le suivi du DERNIER cycle
   (followLastCycle) est conditionné à selection.mode === 'live' : en
   historique, la sélection de l'utilisateur reste figée pendant que
   l'arrière-plan continue de se rafraîchir. ── */
async function refreshLive(){
  const state = await fetchJSON('/api/state');
  if (!state.__error){
    renderHeader(state.providers || {});
    renderSummaryLive(state.providers || {});
  }

  const cyclesResp = await fetchJSON('/api/cycles');
  if (!cyclesResp.__error){
    renderTimebar(cyclesResp.cycles || []);
    if (selection.mode === 'live') await followLastCycle();
  }
}

function startPolling(){
  if (pollTimer) return;
  refreshLive();
  pollTimer = setInterval(refreshLive, REFRESH_MS);
}

/* ── Démarrage ────────────────────────────────────────────────── */
renderWaitingSections();
startPolling();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────
#  Helpers réseau — best-effort, ne lèvent jamais
# ─────────────────────────────────────────────

async def _get_json(client: httpx.AsyncClient, url: str) -> Optional[Any]:
    """
    GET best-effort : None sur toute panne (timeout, connexion refusée,
    HTTP >= 400, corps non JSON) — jamais d'exception qui remonterait à
    l'appelant. Cible potentiellement injoignable = donnée absente, pas une
    erreur qui doit faire échouer la réponse globale de ce service.
    """
    try:
        r = await client.get(url, timeout=_TARGET_TIMEOUT_S)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE_HTML


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "healthy", "service": "federation_view"}


@app.get("/api/state")
async def get_state() -> Dict[str, Any]:
    """
    État live agrégé : pour chaque provider, son /status et son /data.
    Toutes les cibles interrogées EN PARALLÈLE. Un provider injoignable →
    son entrée vaut {"reachable": false, "error": "..."} ; les autres
    restent servis. Toujours HTTP 200 : ce service ne fait jamais échouer
    la réponse globale à cause d'UN pair mort.
    """
    provider_ids = list(config.FEDERATION_VIEW_TARGETS.keys())

    async with httpx.AsyncClient() as client:
        tasks = []
        for pid in provider_ids:
            urls = config.FEDERATION_VIEW_TARGETS[pid]
            tasks.append(_get_json(client, f"{urls['hub']}/status"))
            tasks.append(_get_json(client, f"{urls['hub']}/data"))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    providers: Dict[str, Any] = {}
    for i, pid in enumerate(provider_ids):
        status_res = results[2 * i]
        data_res   = results[2 * i + 1]
        if isinstance(status_res, BaseException):
            status_res = None
        if isinstance(data_res, BaseException):
            data_res = None

        if status_res is None and data_res is None:
            providers[pid] = {"reachable": False, "error": "hub injoignable"}
        else:
            providers[pid] = {"reachable": True, "status": status_res, "data": data_res}

    return {"providers": providers}


@app.post("/api/intent")
async def post_intent(payload: Dict[str, Any] = Body(...)) -> Any:
    """
    Envoie une intention à l'intent_manager du provider CHOISI par
    l'utilisateur. Volontairement explicite : l'opérateur décide qui reçoit,
    la propagation inter-providers (hub /intent → relais /intent/propagate)
    se charge ensuite d'aligner toute la fédération sur le même contrat.
    Timeout large : l'appel au LLM prend plusieurs secondes.
    """
    provider_id = payload.get("provider_id")
    intention   = (payload.get("intention") or "").strip()

    urls = config.FEDERATION_VIEW_TARGETS.get(provider_id)
    if urls is None:
        return JSONResponse(status_code=404,
                            content={"error": "provider inconnu", "provider_id": provider_id})
    if not intention:
        return JSONResponse(status_code=400, content={"error": "intention vide"})

    target = f"{urls['intent_manager']}/intent"
    logger.info(f"📨 /api/intent — {C.CYAN}{provider_id}{C.RESET} → {target}")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(target, json={"intention": intention}, timeout=60.0)
    except Exception as exc:
        return JSONResponse(status_code=502,
                            content={"error": f"intent_manager injoignable : {exc}", "target": target})

    if resp.status_code >= 400:
        return JSONResponse(status_code=resp.status_code,
                            content={"error": f"HTTP {resp.status_code}", "detail": resp.text})

    try:
        body = resp.json()
    except ValueError:
        body = {"raw_response": resp.text}

    return {"provider_id": provider_id, "response": body}


@app.post("/api/reset")
async def post_reset() -> Dict[str, Any]:
    """
    Repasse TOUS les providers en mode autonomous (POST /reset sur chaque hub).
    Diffusion parallèle, dégradation gracieuse : un hub injoignable alimente
    `errors` sans empêcher les autres d'être réinitialisés.
    """
    pids = list(config.FEDERATION_VIEW_TARGETS.keys())

    async with httpx.AsyncClient() as client:
        tasks = [
            client.post(f"{config.FEDERATION_VIEW_TARGETS[p]['hub']}/reset", timeout=10.0)
            for p in pids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    ok: List[str] = []
    ko: List[Dict[str, Any]] = []
    for pid, res in zip(pids, results):
        if isinstance(res, BaseException):
            ko.append({"provider_id": pid, "error": str(res)})
        elif res.status_code >= 400:
            ko.append({"provider_id": pid, "error": f"HTTP {res.status_code}"})
        else:
            ok.append(pid)

    logger.info(f"♻️  /api/reset — {C.GREEN}{len(ok)}{C.RESET} ok, {C.YELLOW}{len(ko)}{C.RESET} erreur(s)")
    return {"reset": ok, "errors": ko}


@app.get("/api/cycles")
async def get_cycles() -> Dict[str, Any]:
    """
    Liste navigable : tous les cycles de décision des deux /audit/log,
    fusionnés et triés par cycle DÉCROISSANT (le plus récent en premier).
    Un provider injoignable alimente "errors" — les autres restent servis,
    toujours HTTP 200.
    """
    provider_ids = list(config.FEDERATION_VIEW_TARGETS.keys())

    async with httpx.AsyncClient() as client:
        tasks = [
            _get_json(client, f"{config.FEDERATION_VIEW_TARGETS[pid]['observability']}/audit/log")
            for pid in provider_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    cycles: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for pid, res in zip(provider_ids, results):
        if isinstance(res, BaseException) or not res:
            errors.append({"provider_id": pid, "error": "observability injoignable"})
            continue
        for entry in res.get("log", []):
            cycles.append({
                "provider_id": pid,
                "cycle":       entry.get("cycle"),
                "path":        entry.get("provider_path"),
                "decision":    entry.get("decision"),
                "from_vm":     entry.get("from_vm"),
                "to_vm":       entry.get("to_vm"),
                "received_at": entry.get("received_at"),
            })

    cycles.sort(key=lambda c: c["cycle"] if c["cycle"] is not None else -1, reverse=True)

    return {"cycles": cycles, "errors": errors}


@app.get("/api/cycle/{provider_id}/{cycle}")
async def get_cycle(provider_id: str, cycle: int) -> Dict[str, Any]:
    """
    Rejeu complet d'UN cycle passé : l'entrée d'audit brute + replay_topsis
    (rejeu des 4 phases via le TOPSIS réel, voir replay.py) +
    extract_gap_grade_steps pour chaque bid archivé dans reasoning.bids
    (cycle fédéré) — [] si le cycle est mono-provider ou antérieur au lot 6a.

    Jamais 500 — mais TROIS causes d'échec distinctes (lot 8b, §1.1), pour
    que la page ne dise jamais « cycle inexistant » quand c'est en réalité
    observability qui est tombé :
        provider inconnu             → 404 {"error": "provider inconnu", ...}
        cycle absent du journal       → 404 {"error": "cycle absent du journal", ...}
        observability injoignable     → 503 {"error": "observability injoignable", ...}
    """
    urls = config.FEDERATION_VIEW_TARGETS.get(provider_id)
    if urls is None:
        return JSONResponse(
            status_code=404,
            content={"error": "provider inconnu", "provider_id": provider_id},
        )

    async with httpx.AsyncClient() as client:
        log_resp = await _get_json(client, f"{urls['observability']}/audit/log")

    if not log_resp:
        return JSONResponse(
            status_code=503,
            content={
                "error":  "observability injoignable",
                "detail": f"impossible de lire le journal de '{provider_id}' ({urls['observability']})",
            },
        )

    entry = next((e for e in log_resp.get("log", []) if e.get("cycle") == cycle), None)
    if entry is None:
        return JSONResponse(
            status_code=404,
            content={
                "error":       "cycle absent du journal",
                "provider_id": provider_id,
                "cycle":       cycle,
            },
        )

    slos_active = entry.get("slos_active") or []
    bids = ((entry.get("reasoning") or {}).get("bids")) or []

    return {
        "provider_id": provider_id,
        "cycle":       cycle,
        "entry":       entry,
        "replay":      replay_topsis(entry),
        "gap_grades":  [extract_gap_grade_steps(bid, slos_active) for bid in bids],
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.FEDERATION_VIEW_PORT)
