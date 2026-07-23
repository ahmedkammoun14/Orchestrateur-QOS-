"""
services/observability/app.py — Tableau de bord temps réel QoS.

Endpoints :
  GET  /          → dashboard HTML (Chart.js + SSE)
  GET  /stream    → Server-Sent Events (métriques toutes les 2 s + audit)
  POST /audit     → reçoit un événement d'audit du hub
  GET  /audit/log → retourne l'historique complet des décisions
  GET  /health
"""

import asyncio
import json
import logging
import uvicorn
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx
from fastapi import FastAPI, Request, Body
from fastapi.responses import HTMLResponse, StreamingResponse

from shared import config
from shared.logging_utils import PrettyFormatter


def _setup_logger() -> logging.Logger:
    log = logging.getLogger("Observability")
    log.setLevel(logging.INFO)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(PrettyFormatter())
        log.addHandler(h)
    log.propagate = False
    return log


logger = _setup_logger()

# ── État en mémoire ───────────────────────────────────────────
_audit_log: deque = deque(maxlen=500)
_subscribers: List[asyncio.Queue] = []

# Compteur cumulé des chemins multi-provider (A/B/C/D) depuis le démarrage —
# c'est ce compteur qui permet de prouver en soutenance que les 5 cas de la
# spécification se produisent réellement. Les entrées mono-provider (flag
# MULTI_PROVIDER_ENABLED=False, sans "provider_path" dans le payload) ne sont
# comptées dans AUCUNE catégorie.
_PROVIDER_PATHS: List[str] = ["A", "B", "C", "D"]
_provider_path_counts: Dict[str, int] = {p: 0 for p in _PROVIDER_PATHS}

# ── Application ───────────────────────────────────────────────
app = FastAPI(title="QoS Observability", version="2.0.0")


# ─────────────────────────────────────────────────────────────
#  SSE — diffusion temps réel
# ─────────────────────────────────────────────────────────────

@app.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.append(queue)

    async def generator():
        # Envoie immédiatement l'historique d'audit pour hydratation initiale
        # (+ le compteur de chemins courant, pour qu'un client qui se connecte
        # en cours de session affiche tout de suite les totaux cumulés).
        snapshot = list(_audit_log)
        yield f"data: {json.dumps({'type': 'snapshot', 'log': snapshot, 'provider_path_counts': dict(_provider_path_counts)})}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.TimeoutError:
            yield "data: {\"type\":\"ping\"}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in _subscribers:
                _subscribers.remove(queue)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


async def _broadcast(event: Dict[str, Any]) -> None:
    for q in list(_subscribers):
        await q.put(event)


# ─────────────────────────────────────────────────────────────
#  Audit log — reçu depuis le hub après chaque décision
# ─────────────────────────────────────────────────────────────

@app.post("/audit", status_code=200)
async def receive_audit(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    payload.setdefault("received_at", datetime.now(timezone.utc).isoformat())

    # Compteur de chemins : lu sur le payload REÇU, jamais écrit dedans —
    # l'entrée stockée dans _audit_log reste, à received_at près, identique
    # à ce que le hub a envoyé (non-régression du mode mono-provider, qui
    # n'a pas de clé "provider_path").
    provider_path = payload.get("provider_path")
    if provider_path in _provider_path_counts:
        _provider_path_counts[provider_path] += 1

    _audit_log.append(payload)
    await _broadcast({
        "type": "audit",
        "data": payload,
        "provider_path_counts": dict(_provider_path_counts),
    })
    logger.info(
        f"Audit reçu — cycle={payload.get('cycle')} "
        f"decision={payload.get('decision')} "
        f"breach={payload.get('breach_type')}"
        + (f" | chemin={provider_path} provider={payload.get('provider_used')}"
           if provider_path else "")
    )
    return {"status": "ok"}


@app.get("/audit/log")
async def get_audit_log() -> Dict[str, Any]:
    return {
        "count": len(_audit_log),
        "log": list(_audit_log),
        "provider_path_counts": dict(_provider_path_counts),
    }


# ─────────────────────────────────────────────────────────────
#  Polling hub /data → broadcast métriques toutes les 2 s
# ─────────────────────────────────────────────────────────────

async def _poll_hub() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(f"{config.CORE_URL}/data", timeout=3.0)
                if r.status_code == 200:
                    data = r.json()
                    await _broadcast({"type": "metrics", "data": data})
            except Exception:
                pass
            await asyncio.sleep(2.0)


# ─────────────────────────────────────────────────────────────
#  Dashboard HTML
# ─────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QoS Orchestrator — Tableau de bord</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2d3148;
    --green: #22c55e; --red: #ef4444; --yellow: #f59e0b;
    --blue: #3b82f6; --purple: #8b5cf6; --text: #e2e8f0;
    --muted: #94a3b8; --proactive: #f59e0b; --reactive: #ef4444;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }

  /* ── Header ── */
  header {
    background: var(--card); border-bottom: 1px solid var(--border);
    padding: 12px 24px; display: flex; align-items: center; gap: 32px; flex-wrap: wrap;
  }
  header h1 { font-size: 16px; font-weight: 700; color: #fff; white-space: nowrap; }
  .stat { display: flex; flex-direction: column; gap: 2px; }
  .stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }
  .stat-value { font-size: 15px; font-weight: 600; }
  .badge {
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-size: 12px; font-weight: 700; letter-spacing: .3px;
  }
  .badge-migrate { background: #7f1d1d; color: #fca5a5; }
  .badge-stay    { background: #14532d; color: #86efac; }
  .badge-proactive { background: #78350f; color: #fcd34d; }
  .badge-reactive  { background: #7f1d1d; color: #fca5a5; }
  .badge-none      { background: #1e293b; color: var(--muted); }
  /* Chemins multi-provider (A/B/C/D) — voir _PROVIDER_PATHS côté backend */
  .badge-path-a { background: #14532d; color: #4ade80; }
  .badge-path-b { background: #78350f; color: #fbbf24; }
  .badge-path-c { background: #4c1d75; color: #c084fc; }
  .badge-path-d { background: #7f1d1d; color: #f87171; }
  #conn-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); display: inline-block; margin-right: 4px; }
  #conn-dot.live { background: var(--green); box-shadow: 0 0 6px var(--green); }

  /* ── Layout ── */
  main { padding: 20px 24px; display: flex; flex-direction: column; gap: 20px; }
  .section-title { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }

  /* ── VM cards ── */
  .vm-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
  .vm-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px; transition: border-color .2s;
  }
  .vm-card.active { border-color: var(--green); }
  .vm-card.violation { border-color: var(--red); background: #1f0a0a; }
  .vm-card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .vm-name { font-weight: 700; font-size: 14px; }
  .vm-tag { font-size: 10px; padding: 2px 7px; border-radius: 8px; font-weight: 600; }
  .vm-tag.active { background: #14532d; color: #86efac; }
  .vm-tag.idle   { background: #1e293b; color: var(--muted); }
  .vm-metrics { display: flex; flex-direction: column; gap: 6px; }
  .metric-row { display: flex; justify-content: space-between; align-items: center; }
  .metric-name { color: var(--muted); font-size: 12px; }
  .metric-val  { font-size: 13px; font-weight: 600; }
  .metric-bar  { width: 100%; height: 3px; background: #2d3148; border-radius: 2px; margin-top: 2px; }
  .metric-bar-fill { height: 100%; border-radius: 2px; transition: width .4s; }
  .pred-row { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-top: 4px; }

  /* ── Charts ── */
  .chart-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px;
  }
  .chart-card canvas { width: 100% !important; }

  /* ── Audit log ── */
  .audit-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .audit-table th {
    text-align: left; padding: 6px 10px; background: #12151f;
    color: var(--muted); font-weight: 600; border-bottom: 1px solid var(--border);
    font-size: 10px; text-transform: uppercase;
  }
  .audit-table td { padding: 7px 10px; border-bottom: 1px solid #1e2235; vertical-align: top; }
  .audit-table tr:hover td { background: #1f2333; }
  .audit-wrap { max-height: 320px; overflow-y: auto; border-radius: 10px; border: 1px solid var(--border); }
  .mono { font-family: 'Consolas', monospace; font-size: 11px; }

  /* ── Raisonnement du cycle ── */
  .reasoning-header { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; font-size: 13px; color: var(--muted); }
  .reasoning-step { margin-bottom: 14px; }
  .reasoning-step:last-child { margin-bottom: 0; }
  .reasoning-step-title { font-size: 12px; font-weight: 700; color: #fff; margin-bottom: 6px; }
  .reasoning-row { display: flex; gap: 10px; align-items: baseline; font-size: 12px; padding: 3px 0; color: var(--text); }
  .reasoning-empty { color: #64748b; font-size: 12px; }
</style>
</head>
<body>

<header>
  <h1>🔬 QoS Orchestrator</h1>
  <div class="stat">
    <span class="stat-label">Connexion</span>
    <span class="stat-value"><span id="conn-dot"></span><span id="conn-status">—</span></span>
  </div>
  <div class="stat">
    <span class="stat-label">Cycle</span>
    <span class="stat-value" id="h-cycle">—</span>
  </div>
  <div class="stat">
    <span class="stat-label">VM active</span>
    <span class="stat-value" id="h-vm">—</span>
  </div>
  <div class="stat">
    <span class="stat-label">Provider actif</span>
    <span class="stat-value" id="h-provider">—</span>
  </div>
  <div class="stat">
    <span class="stat-label">Dernière décision</span>
    <span class="stat-value" id="h-decision"><span class="badge badge-none">—</span></span>
  </div>
  <div class="stat">
    <span class="stat-label">Type violation</span>
    <span class="stat-value" id="h-breach"><span class="badge badge-none">—</span></span>
  </div>
  <div class="stat">
    <span class="stat-label">Score TOPSIS</span>
    <span class="stat-value" id="h-topsis">—</span>
  </div>
  <div class="stat">
    <span class="stat-label">Mode</span>
    <span class="stat-value" id="h-mode">—</span>
  </div>
  <div class="stat">
    <span class="stat-label">Migrations proactives / réactives</span>
    <span class="stat-value">
      <span class="badge badge-proactive" id="h-proactive-count">0</span>
      <span class="badge badge-reactive" id="h-reactive-count">0</span>
    </span>
  </div>
  <div class="stat">
    <span class="stat-label">Chemins multi-provider</span>
    <span class="stat-value">
      <span class="badge badge-none" id="h-path-a" title="Le provider courant avait des VMs conformes — TOPSIS a départagé">INTRA 0</span>
      <span class="badge badge-none" id="h-path-b" title="Aucune VM conforme chez le provider courant — passation vers l'autre provider">INTER 0</span>
      <span class="badge badge-none" id="h-path-c" title="Aucun provider conforme — décision par négociation sur le score de violation">NÉGO 0</span>
      <span class="badge badge-none" id="h-path-d" title="Aucun provider ne peut satisfaire les SLOs — le service reste en place">IMPOSSIBLE 0</span>
    </span>
  </div>
</header>

<main>

  <!-- VMs -->
  <div>
    <div class="section-title">État des VMs — Métriques temps réel</div>
    <div class="vm-grid" id="vm-grid"></div>
  </div>

  <!-- SLO weights -->
  <div class="chart-card">
    <div class="section-title">Poids SLOs actifs (TOPSIS)</div>
    <canvas id="chart-slo" height="160"></canvas>
  </div>

  <!-- Raisonnement du cycle -->
  <div class="chart-card">
    <div class="section-title">Raisonnement du cycle</div>
    <div id="reasoning-panel">
      <span style="color:#64748b">Mode mono-provider — raisonnement multi-provider non actif</span>
    </div>
  </div>

  <!-- Audit log -->
  <div>
    <div class="section-title">Audit log — Décisions de l'orchestrateur</div>
    <div class="audit-wrap">
      <table class="audit-table">
        <thead>
          <tr>
            <th>Heure</th><th>Cycle</th><th>Décision</th><th>Type</th>
            <th>Provider</th>
            <th>VM source</th><th>VM cible</th><th>TOPSIS</th>
            <th>Métriques</th><th>Raison</th>
          </tr>
        </thead>
        <tbody id="audit-body"></tbody>
      </table>
    </div>
  </div>

</main>

<script>
// ── État local ────────────────────────────────────────────────
const VMs = """ + json.dumps(list(config.VM_REGISTRY.keys())) + """;
const METRICS = """ + json.dumps(list(config.METRICS_REGISTRY.keys())) + """;
const SLO_DEFAULTS = """ + json.dumps({
    m: config.METRICS_REGISTRY[m]["default_threshold"]
    for m in config.METRICS_REGISTRY
}) + """;
const UNITS = """ + json.dumps({
    m: config.METRICS_REGISTRY[m]["unit"]
    for m in config.METRICS_REGISTRY
}) + """;
// Carte VM → provider et VMs de chaque provider (partition transversale) —
// injectées depuis shared/config.py pour que le JS affiche "pour quel
// provider l'orchestrateur travaille" sans dupliquer la topologie ici.
const PROVIDER_OF_VM = """ + json.dumps(config.PROVIDER_OF_VM) + """;
const PROVIDER_REGISTRY = """ + json.dumps(config.PROVIDER_REGISTRY) + """;

const COLORS = { latency: '#3b82f6', cpu_usage: '#f59e0b', ram_usage: '#22c55e' };
// Couleur distinctive par provider, réutilisée partout où provider_used ou
// le provider actif apparaît (tuile d'en-tête, colonne Provider du journal)
// pour que l'œil suive un même provider d'un endroit à l'autre du dashboard.
const PROVIDER_COLORS = { 'provider-1': '#3fd0c9', 'provider-2': '#f59e0b' };
const METRIC_LABELS = { latency: 'Latence', cpu_usage: 'CPU', ram_usage: 'RAM' };
// cpu_usage/ram_usage en mode enhanced (intention LLM) sont exprimés en
// ressource absolue (cœurs/Go) avec operator ">=" — pas un % de charge.
// Ce mapping permet de convertir le % brut mesuré en disponibilité réelle
// de la VM (même formule que TopsisSelector._to_criterion_value côté backend).
const CAPACITY_KEYS = { cpu_usage: 'total_cores', ram_usage: 'total_ram_gb' };
const HIST_LEN = 60;

let sloWeightHist = {}; // metric → [weights]
let sloWeightCycles = [];
let currentSlos = [];
let latencyThreshold = SLO_DEFAULTS['latency'] || 300;
let lastKnownLatency = {}; // vm_id → dernière latence mesurée connue (pour comparer "VM la plus proche" vs choix TOPSIS)

METRICS.forEach(m => { sloWeightHist[m] = []; });

// ── Charts ────────────────────────────────────────────────────
const sloCtx = document.getElementById('chart-slo').getContext('2d');
const sloChart = new Chart(sloCtx, {
  type: 'line',
  data: {
    labels: [],
    datasets: METRICS.map(m => ({
      label: m,
      data: [],
      borderColor: COLORS[m] || '#94a3b8',
      backgroundColor: 'transparent',
      borderWidth: 2, pointRadius: 0, tension: 0.3,
    }))
  },
  options: {
    animation: false, responsive: true,
    plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
    scales: {
      x: { ticks: { color: '#64748b', maxTicksLimit: 8 }, grid: { color: '#1e2235' } },
      y: { min: 0, max: 1.05, ticks: { color: '#64748b' }, grid: { color: '#1e2235' } }
    }
  }
});

// ── VM Cards ──────────────────────────────────────────────────
function buildVmCards() {
  const grid = document.getElementById('vm-grid');
  grid.innerHTML = '';
  VMs.forEach(vm => {
    const card = document.createElement('div');
    card.className = 'vm-card'; card.id = 'card-' + vm;
    card.innerHTML = `
      <div class="vm-card-header">
        <span class="vm-name">${vm}</span>
        <span class="vm-tag idle" id="tag-${vm}">INACTIF</span>
      </div>
      <div class="vm-metrics" id="metrics-${vm}">
        ${METRICS.map(m => `
          <div>
            <div class="metric-row">
              <span class="metric-name">${METRIC_LABELS[m] || m}</span>
              <span class="metric-val" id="val-${vm}-${m}">—</span>
            </div>
            <div class="metric-bar"><div class="metric-bar-fill" id="bar-${vm}-${m}" style="width:0%;background:${COLORS[m]||'#3b82f6'}"></div></div>
            <div class="pred-row">
              <span>Prédit</span>
              <span id="pred-${vm}-${m}">—</span>
            </div>
          </div>
        `).join('')}
      </div>`;
    grid.appendChild(card);
  });
}
buildVmCards();

// Met à jour la tuile "Provider actif" d'après la VM active courante.
// Sans PROVIDER_OF_VM (VM inconnue du registre) : affichage neutre "—",
// jamais de valeur incorrecte ou d'exception.
function updateActiveProviderTile(activeVm) {
  const el = document.getElementById('h-provider');
  const providerId = PROVIDER_OF_VM[activeVm];
  if (providerId) {
    const vms = (PROVIDER_REGISTRY[providerId]?.vms || []).join(' · ');
    el.textContent = providerId;
    el.style.color = PROVIDER_COLORS[providerId] || '';
    el.title = vms;
  } else {
    el.textContent = '—';
    el.style.color = '';
    el.title = '';
  }
}

// ── Mise à jour métriques ──────────────────────────────────────
function updateMetrics(data) {
  const vms = data.vms || {};
  const slos = data.slos || [];
  const cycle = data.cycle || 0;
  currentSlos = slos;

  // Header
  const status = data.status || {};
  document.getElementById('h-cycle').textContent = cycle;
  const activeVm = Object.entries(vms).find(([,d]) => d.is_active)?.[0] || '—';
  document.getElementById('h-vm').textContent = activeVm;
  updateActiveProviderTile(activeVm);

  // Seuil latence depuis SLOs actifs
  const latSlo = slos.find(s => s.metric === 'latency');
  if (latSlo) latencyThreshold = parseFloat(latSlo.threshold);

  // VM cards
  Object.entries(vms).forEach(([vm, vmData]) => {
    const isActive = vmData.is_active;
    const card = document.getElementById('card-' + vm);
    if (!card) return;
    const latVal = vmData.rtt_ms;
    const inViolation = latVal !== null && latVal !== undefined && latVal > latencyThreshold;
    card.className = 'vm-card' + (isActive ? ' active' : '') + (inViolation ? ' violation' : '');

    const tag = document.getElementById('tag-' + vm);
    tag.textContent = isActive ? 'ACTIF' : 'INACTIF';
    tag.className = 'vm-tag ' + (isActive ? 'active' : 'idle');

    if (latVal !== null && latVal !== undefined) lastKnownLatency[vm] = latVal;

    const metricMap = { latency: latVal, cpu_usage: vmData.cpu_usage, ram_usage: vmData.ram_usage };
    const preds = vmData.predictions || {};

    METRICS.forEach(m => {
      const val = metricMap[m];
      const el = document.getElementById('val-' + vm + '-' + m);
      const bar = document.getElementById('bar-' + vm + '-' + m);
      const predEl = document.getElementById('pred-' + vm + '-' + m);
      if (!el) return;

      const slo = slos.find(s => s.metric === m);
      const thr = slo ? parseFloat(slo.threshold) : (SLO_DEFAULTS[m] || 100);
      // Mode enhanced (LLM) : cpu_usage/ram_usage en cœurs/Go, operator ">=".
      // Il faut convertir le % brut en disponibilité réelle (capacité de
      // CETTE vm) avant de comparer — sinon on compare un % à un nombre de
      // cœurs. La direction de violation s'inverse aussi : sous le seuil =
      // violation (au lieu de : au-dessus = violation pour "<").
      const capKey  = CAPACITY_KEYS[m];
      const isFloor = !!(slo && (slo.operator === '>=' || slo.operator === '>') && capKey);
      const unit    = slo ? slo.unit : (UNITS[m] || '');

      const toDisplay = (raw) => {
        if (raw === null || raw === undefined) return null;
        if (!isFloor) return raw;
        const capacity = vmData[capKey];
        if (!capacity) return raw; // capacité inconnue — pas de conversion possible
        return capacity * (1 - raw / 100);
      };
      const isBad = (displayVal) => isFloor ? displayVal < thr : displayVal > thr;

      const dVal = toDisplay(val);

      if (dVal !== null && dVal !== undefined) {
        el.textContent = dVal.toFixed(isFloor ? 2 : 1) + ' ' + unit;
        el.style.color = isBad(dVal) ? '#ef4444' : '#22c55e';
        bar.style.width = Math.min(100, Math.max(0, (dVal / thr) * 100)) + '%';
        bar.style.background = isBad(dVal) ? '#ef4444' : (COLORS[m] || '#3b82f6');
      } else {
        el.textContent = '—'; bar.style.width = '0%';
      }

      const mp = (preds[m] || []).map(toDisplay).filter(p => p !== null);
      if (mp.length > 0) {
        const firstPred = mp[0].toFixed(isFloor ? 2 : 1);
        // Le "pire cas" prédit est le pic pour un plafond, le creux pour un plancher.
        const worstPred = (isFloor ? Math.min(...mp) : Math.max(...mp)).toFixed(isFloor ? 2 : 1);
        predEl.textContent = `${firstPred} → ${worstPred} ${unit}`;
        predEl.style.color = isBad(parseFloat(worstPred)) ? '#f59e0b' : '#94a3b8';
      } else {
        predEl.textContent = '—';
      }
    });

  });

  // SLO weights
  const weightMap = {};
  slos.forEach(s => { weightMap[s.metric] = s.weight || 0; });
  sloWeightCycles.push(cycle);
  if (sloWeightCycles.length > HIST_LEN) sloWeightCycles.shift();
  METRICS.forEach(m => {
    sloWeightHist[m].push(weightMap[m] || 0);
    if (sloWeightHist[m].length > HIST_LEN) sloWeightHist[m].shift();
  });

  sloChart.data.labels = sloWeightCycles;
  METRICS.forEach((m, i) => { sloChart.data.datasets[i].data = sloWeightHist[m]; });
  sloChart.update('none');
}

// ── Audit log ─────────────────────────────────────────────────
let auditCount = 0;
let proactiveMigrateCount = 0;
let reactiveMigrateCount = 0;
const seenMigrations = new Set();  // clé cycle → évite double comptage (snapshot + live)

// Libellés/couleurs des types de violation. Étendu avec
// inter_provider_negotiation (chemins B/C) : sans cette entrée, breachFr
// valait '' et produisait une phrase trouée ("Violation  détectée...").
const BREACH_FR = {
  proactive: 'proactive',
  reactive:  'réactive',
  inter_provider_negotiation: 'inter-provider',
};

// Libellés / infobulles des chemins multi-provider (voir provider_arbitration.py).
// Même texte que les infobulles du compteur d'en-tête (une seule source de vérité).
const PATH_LABELS = { A: 'INTRA', B: 'INTER', C: 'NÉGO', D: 'IMPOSSIBLE' };
const PATH_TITLES = {
  A: "Le provider courant avait des VMs conformes — TOPSIS a départagé.",
  B: "Aucune VM conforme chez le provider courant — passation vers l'autre provider.",
  C: "Aucun provider conforme — décision par négociation sur le score de violation.",
  D: "Aucun provider ne peut satisfaire les SLOs — le service reste en place.",
};

// Badge de chemin affiché dans la colonne "Type" du journal d'audit. Le
// provider (provider_used) a sa PROPRE colonne dédiée (voir providerCellHtml)
// — pas de doublon ici. Retourne '' quand provider_path est absent (mode
// mono-provider) : AUCUN badge, AUCUN texte ajouté — compatibilité stricte.
function pathBadgeHtml(e) {
  if (!e.provider_path) return '';
  const cls = 'badge-path-' + e.provider_path.toLowerCase();
  const label = PATH_LABELS[e.provider_path] || e.provider_path;
  const title = PATH_TITLES[e.provider_path] || '';
  return ` <span class="badge ${cls}" title="${title}">${label}</span>`;
}

// Cellule de la colonne "Provider" du journal. Vide (jamais "—", jamais
// "undefined") quand provider_used est absent — mode mono-provider inchangé.
// Coloré selon PROVIDER_COLORS pour que l'œil suive le même provider entre
// la tuile d'en-tête et le journal.
function providerCellHtml(e) {
  if (!e.provider_used) return '';
  const color = PROVIDER_COLORS[e.provider_used] || '#e2e8f0';
  return `<span style="color:${color};font-weight:600">${e.provider_used}</span>`;
}

// Traduit le "reason" brut (anglais, venant du backend decision_intelligence)
// en français, et ajoute l'explication "VM la plus proche vs choix TOPSIS"
// pour les migrations : compare to_vm à la VM ayant la latence la plus basse
// connue au moment de la décision (lastKnownLatency, alimenté par updateMetrics).
function translateReason(e) {
  // ── Chemins B / C / D (multi-provider, hors chemin A) ────────────────
  // Phrase structurée à partir des champs disponibles (provider_used, to_vm,
  // from_vm), COMPLÉTÉE par le "reason" du hub — jamais remplacée par lui.
  // Le chemin A n'entre pas ici : il retombe sur la logique historique
  // ci-dessous, strictement inchangée (comme le mode mono-provider, qui n'a
  // pas de provider_path du tout).
  if (e.provider_path && e.provider_path !== 'A') {
    const used = e.provider_used || '—';

    // Chemin C (négociation) : format à part, en 2 lignes — un en-tête
    // gras/coloré ("qui a gagné"), puis le "reason" du hub tel quel (raison
    // du POURQUOI). Sans "reason", seul l'en-tête est affiché.
    if (e.provider_path === 'C') {
      const color = PROVIDER_COLORS[e.provider_used] || '#a855f7';
      const header = `<strong style="color:${color}">NÉGOCIATION — ${used} l'emporte</strong>`;
      return e.reason ? `${header}<br><span style="color:#94a3b8">${e.reason}</span>` : header;
    }

    let structured;
    if (e.provider_path === 'B') {
      structured = `Aucune VM conforme localement — passation vers ${used}`
                 + (e.to_vm ? `, qui a sélectionné ${e.to_vm} par TOPSIS.` : '.');
    } else {
      structured = `Aucun provider ne peut satisfaire les SLOs — le service reste sur `
                 + `${e.from_vm || e.to_vm || '—'}.`;
    }
    return e.reason ? `${structured} (${e.reason})` : structured;
  }

  // ── Chemin A / mode mono-provider : comportement INCHANGÉ ────────────
  const breachFr = BREACH_FR[e.breach_type] || '';
  const toVm = e.to_vm;

  let base;
  if (/Secondary-only violation/.test(e.reason || '')) {
    base = `Violation ${breachFr} sur une métrique secondaire (CPU/RAM) seulement — pas de migration (seule la latence déclenche une migration).`;
  } else if (/Cooldown active/i.test(e.reason || '')) {
    base = `Cooldown actif — migration temporairement bloquée.`;
  } else if (/No SLO violation/i.test(e.reason || '')) {
    base = `Aucune violation de SLO détectée.`;
  } else if (/still best candidate/.test(e.reason || '')) {
    base = `Violation ${breachFr} détectée, mais ${e.from_vm} reste le meilleur candidat (TOPSIS) — maintien.`;
  } else if (toVm) {
    // Cas migration effective : "{breach} violation on {metrics} — TOPSIS selected 'to_vm' (score=...)"
    const knownVms = Object.keys(lastKnownLatency).filter(v => lastKnownLatency[v] != null);
    let nearestVm = null;
    knownVms.forEach(v => {
      if (nearestVm === null || lastKnownLatency[v] < lastKnownLatency[nearestVm]) nearestVm = v;
    });
    if (nearestVm && nearestVm === toVm) {
      base = `Violation ${breachFr} détectée — migration vers ${toVm}, la VM la plus proche (la latence domine la décision).`;
    } else if (nearestVm) {
      base = `Violation ${breachFr} détectée — la VM la plus proche était ${nearestVm}, mais le score TOPSIS `
           + `(CPU/RAM pris en compte) a favorisé ${toVm} à la place.`;
    } else {
      base = `Violation ${breachFr} détectée — TOPSIS a sélectionné ${toVm} (score=${e.topsis_score ?? '—'}).`;
    }
  } else {
    base = e.reason || '—';
  }
  return base;
}

// ── Panneau "Raisonnement du cycle" ─────────────────────────────
//
// Affiche le raisonnement de la DERNIÈRE entrée d'audit possédant un bloc
// "reasoning" (voir hub/orchestrator_core.py::_build_reasoning) — c'est-à-
// dire uniquement les cycles où config.MULTI_PROVIDER_ENABLED est actif.
// Compatibilité stricte : tant qu'aucune entrée de ce type n'est arrivée,
// le panneau garde son message statique HTML par défaut ("mode
// mono-provider") — aucune fonction ci-dessous n'est même appelée.
let lastReasoningEntry = null;

// Ne remplace l'entrée retenue que par une plus récente (cycle supérieur
// ou égal) : le snapshot initial arrive en ordre ANTI-chronologique
// (le plus récent en premier), il ne faut pas laisser une entrée plus
// ancienne écraser ensuite la plus récente déjà affichée.
function considerForReasoningPanel(e) {
  if (!e || !e.reasoning) return;
  const prevCycle = lastReasoningEntry ? (lastReasoningEntry.cycle ?? -Infinity) : -Infinity;
  const thisCycle = e.cycle ?? -Infinity;
  if (thisCycle >= prevCycle) {
    lastReasoningEntry = e;
    renderReasoningPanel(e);
  }
}

// Étape 2 : violation détectée. Sur le chemin A / mono-provider,
// violated_metrics + breach_type (proactif/réactif) sont fiables — c'est
// decision.py (ViolationDetector) qui les a produits. Sur les chemins
// B/C/D, decision.py n'est PAS appelé sur ce provider : violated_metrics
// est toujours vide et breach_type vaut "inter_provider_*" (le mécanisme,
// pas le type de violation). On retombe alors sur le détail par métrique
// de l'évaluation du provider courant (reasoning.evaluations[].detail),
// seule donnée disponible pour cette VM — sans qualificatif
// proactif/réactif, qui n'est pas déterminable dans ce cas.
function renderStep2(e, r) {
  const violated = e.violated_metrics || [];
  if (violated.length && (e.breach_type === 'proactive' || e.breach_type === 'reactive')) {
    const label = (BREACH_FR[e.breach_type] || e.breach_type).toUpperCase();
    return violated.map(m =>
      `<div class="reasoning-row"><span class="mono">${e.from_vm || '—'}</span> : `
      + `<span class="mono">${METRIC_LABELS[m.metric] || m.metric}</span> → `
      + `<span style="color:#f59e0b;font-weight:600">${label}</span></div>`
    ).join('');
  }
  // from_vm n'est renseigné que sur une MIGRATION (convention du hub) : sur un
  // maintien il vaut null et l'évaluation de la VM active restait introuvable,
  // d'où un « Aucune violation détectée » trompeur juste au-dessus d'une VM
  // affichée non conforme à l'étape 3. reasoning.vm_active comble ce trou.
  const vmActive = e.from_vm || r.vm_active;
  const activeEval = r.evaluations.find(ev => ev.vm_id === vmActive);
  if (activeEval && activeEval.detail) {
    const excesses = Object.entries(activeEval.detail).filter(([, v]) => v > 0);
    if (excesses.length) {
      return excesses.map(([metric, excess]) =>
        `<div class="reasoning-row"><span class="mono">${vmActive}</span> : `
        + `<span class="mono">${METRIC_LABELS[metric] || metric}</span> `
        + `<span style="color:#f59e0b">(excès ${excess.toFixed(3)})</span></div>`
      ).join('');
    }
  }
  return '<div class="reasoning-empty">Aucune violation détectée</div>';
}

// Étape 4 : dépend du chemin. A/B → classement TOPSIS (reasoning.topsis) ;
// C/D → négociation (reasoning.negotiation). "information indisponible"
// plutôt qu'une cellule muette quand la donnée manque.
function renderStep4(e, r) {
  if (e.provider_path === 'A' || e.provider_path === 'B') {
    const classement = (r.topsis && r.topsis.classement) || {};
    const entries = Object.entries(classement).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      return { title: 'TOPSIS départage les conformes', body: '<div class="reasoning-empty">information indisponible</div>' };
    }
    const retenue = r.topsis ? r.topsis.retenue : null;
    const body = entries.map(([vm, score]) =>
      `<div class="reasoning-row"><span class="mono">${vm}</span> `
      + `<span class="mono">${score.toFixed(4)}</span> `
      + (vm === retenue ? '<span style="color:#22c55e;font-weight:600">← retenue</span>' : '')
      + `</div>`
    ).join('');
    return { title: 'TOPSIS départage les conformes', body };
  }

  const neg = r.negotiation;
  const title = 'Négociation' + (neg && neg.provider_cible ? ` avec ${neg.provider_cible}` : '');
  if (!neg) {
    return { title, body: '<div class="reasoning-empty">information indisponible</div>' };
  }
  const fmtOffer = (o) => o ? `${o.vm_id} <span class="mono">${o.violation_score.toFixed(4)}</span>` : 'information indisponible';
  const body = `
    <div class="reasoning-row"><span style="color:#94a3b8">offre locale</span> ${fmtOffer(neg.offre_locale)}</div>
    <div class="reasoning-row"><span style="color:#94a3b8">offre reçue</span> ${fmtOffer(neg.offre_recue)}</div>
    <div class="reasoning-row"><span style="color:#94a3b8">dead-band</span> <span class="mono">${neg.deadband != null ? neg.deadband.toFixed(4) : 'information indisponible'}</span></div>
    <div class="reasoning-row">→ ${neg.decision || 'information indisponible'}</div>
  `;
  return { title, body };
}

function renderReasoningPanel(e) {
  const panel = document.getElementById('reasoning-panel');
  const r = e.reasoning;

  const pathCls   = e.provider_path ? 'badge-path-' + e.provider_path.toLowerCase() : 'badge-none';
  const pathLabel = e.provider_path ? (PATH_LABELS[e.provider_path] || e.provider_path) : '—';
  const provColor = PROVIDER_COLORS[r.provider_courant] || '#e2e8f0';

  // Étape 1 : SLOs actifs — primaire/secondaire, poids, MI si disponible.
  const slos = e.slos_active || [];
  const miScores = e.mi_scores || {};
  const step1 = slos.map(s => {
    const mi = miScores[s.metric];
    const kind = s.is_primary ? 'primaire'
               : (mi != null ? `secondaire · MI ${mi.toFixed(2)}` : 'secondaire');
    const pct = Math.round((s.weight || 0) * 100);
    return `<div class="reasoning-row">
      <span class="mono">${METRIC_LABELS[s.metric] || s.metric}</span>
      <span class="mono" style="color:#94a3b8">${s.operator} ${s.threshold != null ? s.threshold.toFixed(1) : '—'}${s.unit || ''}</span>
      <span style="color:#94a3b8">${kind}</span>
      <span style="color:#f59e0b">poids ${pct}%</span>
    </div>`;
  }).join('') || '<div class="reasoning-empty">Aucun SLO actif</div>';

  // Étape 3 : évaluation du provider courant, triée par violation croissante.
  const evals = (r.evaluations || []).slice().sort((a, b) => a.violation_score - b.violation_score);
  const nConform = evals.filter(x => x.is_compliant).length;
  const step3 = evals.map(x =>
    `<div class="reasoning-row"><span class="mono">${x.vm_id}</span> `
    + `<span class="mono">violation ${x.violation_score.toFixed(3)}</span> `
    + (x.is_compliant
        ? '<span style="color:#22c55e">conforme</span>'
        : '<span style="color:#ef4444">non conforme</span>')
    + `</div>`
  ).join('') || '<div class="reasoning-empty">Aucune évaluation disponible</div>';
  const step3Summary = evals.length
    ? (nConform > 0 ? `→ ${nConform} VM(s) conforme(s)` : '→ aucune VM conforme')
    : '';

  const step4 = renderStep4(e, r);

  const decLabel = e.decision === 'migrate' ? 'MIGRATION' : 'MAINTIEN';
  const decColor = e.decision === 'migrate' ? '#fca5a5' : '#86efac';
  // Sur un MAINTIEN, to_vm et from_vm sont tous deux null (convention du hub) :
  // on affichait « MAINTIEN sur — ». reasoning.vm_active donne la VM réellement
  // conservée.
  const vmLine   = e.decision === 'migrate'
    ? `${e.from_vm || r.vm_active || '—'} → ${e.to_vm || '—'}`
    : `sur ${e.to_vm || e.from_vm || r.vm_active || '—'}`;

  panel.innerHTML = `
    <div class="reasoning-header">
      <span>Cycle #${e.cycle ?? '—'}</span>
      <span class="badge ${pathCls}">${pathLabel}</span>
      <span style="color:${provColor};font-weight:600">${r.provider_courant || ''}</span>
    </div>
    <div class="reasoning-step">
      <div class="reasoning-step-title">1. SLOs actifs</div>
      ${step1}
    </div>
    <div class="reasoning-step">
      <div class="reasoning-step-title">2. Violation détectée</div>
      ${renderStep2(e, r)}
    </div>
    <div class="reasoning-step">
      <div class="reasoning-step-title">3. Évaluation de ${r.provider_courant || '—'}</div>
      ${step3}
      <div style="margin-top:4px;color:#94a3b8">${step3Summary}</div>
    </div>
    <div class="reasoning-step">
      <div class="reasoning-step-title">4. ${step4.title}</div>
      ${step4.body}
    </div>
    <div class="reasoning-step">
      <div class="reasoning-step-title">5. DÉCISION : <span style="color:${decColor}">${decLabel}</span> ${vmLine}</div>
      <div style="color:#94a3b8;margin-top:4px">${e.reason || ''}</div>
    </div>
  `;
}

function addAuditRows(entries, prepend = false) {
  const tbody = document.getElementById('audit-body');
  entries.forEach(e => {
    considerForReasoningPanel(e);   // AVANT le filtre d'affichage du tableau —
                                    // le panneau doit refléter TOUT cycle multi-
                                    // provider, y compris les STAY que le journal masque.

    const dec = e.decision || '—';
    const breach = e.breach_type || 'none';

    if (dec === 'migrate' && e.cycle != null && !seenMigrations.has(e.cycle)) {
      seenMigrations.add(e.cycle);
      if (breach === 'proactive') proactiveMigrateCount++;
      else if (breach === 'reactive') reactiveMigrateCount++;
      document.getElementById('h-proactive-count').textContent = proactiveMigrateCount;
      document.getElementById('h-reactive-count').textContent = reactiveMigrateCount;
    }

    // Journal = les événements PORTEURS D'INFORMATION.
    //  - migrations : toujours affichées (comportement historique) ;
    //  - MAINTIEN   : masqué, SAUF le chemin D (PLACEMENT_IMPOSSIBLE), qui
    //    est le seul STAY signifiant — il prouve qu'aucun provider ne
    //    pouvait tenir les SLOs.
    // Un STAY de routine (chemins A ou C gagnés localement) n'apprend rien
    // et saturait le journal — c'était le problème signalé.
    //
    // Effet sur le mono-provider : provider_path est absent donc
    // "!== 'D'" est vrai → le filtre redevient dec !== 'migrate', EXACTEMENT
    // le comportement d'origine.
    if (dec !== 'migrate' && e.provider_path !== 'D') return;

    const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString('fr-FR') : '—';
    const decBadge = dec === 'migrate'
      ? `<span class="badge badge-migrate">MIGRATION</span>`
      : `<span class="badge badge-stay">MAINTIEN</span>`;
    const breachBadge = breach === 'reactive'
      ? `<span class="badge badge-reactive">réactif</span>`
      : breach === 'proactive'
        ? `<span class="badge badge-proactive">proactif</span>`
        : `<span class="badge badge-none">—</span>`;
    const metrics = (e.violated_metrics || [])
      .map(m => `${METRIC_LABELS[m.metric] || m.metric} (${(m.weight ?? 0).toFixed(2)})`)
      .join(', ') || '—';
    const score = e.topsis_score != null ? parseFloat(e.topsis_score).toFixed(4) : '—';
    const reason = translateReason(e);

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono">${ts}</td>
      <td>${e.cycle ?? '—'}</td>
      <td>${decBadge}</td>
      <td>${breachBadge}${pathBadgeHtml(e)}</td>
      <td class="mono">${providerCellHtml(e)}</td>
      <td class="mono">${e.from_vm || '—'}</td>
      <td class="mono" style="color:#22c55e">${e.to_vm || '—'}</td>
      <td class="mono">${score}</td>
      <td class="mono" style="color:#f59e0b">${metrics}</td>
      <td style="color:#94a3b8;font-size:11px">${reason}</td>`;
    if (prepend) tbody.insertBefore(tr, tbody.firstChild);
    else tbody.appendChild(tr);
  });
  // Garder max 100 lignes
  while (tbody.rows.length > 100) tbody.deleteRow(tbody.rows.length - 1);
}

function updateHeader(auditData) {
  const dec = auditData.decision || '—';
  const breach = auditData.breach_type || 'none';
  const score = auditData.topsis_score;
  document.getElementById('h-decision').innerHTML =
    dec === 'migrate' ? '<span class="badge badge-migrate">MIGRATION</span>'
                      : '<span class="badge badge-stay">MAINTIEN</span>';
  document.getElementById('h-breach').innerHTML =
    breach === 'reactive'  ? '<span class="badge badge-reactive">réactif</span>' :
    breach === 'proactive' ? '<span class="badge badge-proactive">proactif</span>' :
                             '<span class="badge badge-none">—</span>';
  document.getElementById('h-topsis').textContent = score != null ? parseFloat(score).toFixed(4) : '—';
  document.getElementById('h-mode').textContent = auditData.mode || '—';
}

// Compteur cumulé des chemins multi-provider — alimenté par le backend
// (provider_path_counts, joint à chaque événement "audit"/"snapshot"), pas
// recalculé côté client : un seul endroit fait autorité sur le total.
// Chaque catégorie à ZÉRO reste grisée (badge-none) : l'œil va directement
// à ce qui s'est réellement produit plutôt qu'à 4 badges pleine couleur
// dont la plupart valent 0 en début de session.
const PATH_COUNT_IDS = { A: 'h-path-a', B: 'h-path-b', C: 'h-path-c', D: 'h-path-d' };

function updatePathCounts(counts) {
  if (!counts) return;
  Object.keys(PATH_LABELS).forEach(key => {
    const el = document.getElementById(PATH_COUNT_IDS[key]);
    const n = counts[key] || 0;
    el.textContent = PATH_LABELS[key] + ' ' + n;
    el.className = 'badge ' + (n > 0 ? 'badge-path-' + key.toLowerCase() : 'badge-none');
  });
}

// ── SSE ───────────────────────────────────────────────────────
const dot = document.getElementById('conn-dot');
const connStatus = document.getElementById('conn-status');
let evtSource;

function connect() {
  evtSource = new EventSource('/stream');
  evtSource.onopen = () => {
    dot.className = 'live'; connStatus.textContent = 'En direct';
  };
  evtSource.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'metrics') updateMetrics(msg.data);
    else if (msg.type === 'audit') {
      addAuditRows([msg.data], true);
      updateHeader(msg.data);
      updatePathCounts(msg.provider_path_counts);
    }
    else if (msg.type === 'snapshot') {
      addAuditRows((msg.log || []).reverse(), false);
      updatePathCounts(msg.provider_path_counts);
    }
  };
  evtSource.onerror = () => {
    dot.className = ''; connStatus.textContent = 'Reconnexion...';
    evtSource.close();
    setTimeout(connect, 3000);
  };
}
connect();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


# ─────────────────────────────────────────────────────────────
#  Santé
# ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "healthy", "service": "observability"}


# ─────────────────────────────────────────────────────────────
#  Lifespan
# ─────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(_poll_hub())
    logger.info(
        f"Observability démarré — port {config.OBSERVABILITY_PORT} "
        f"| dashboard : http://localhost:{config.OBSERVABILITY_PORT}/"
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.OBSERVABILITY_PORT)
