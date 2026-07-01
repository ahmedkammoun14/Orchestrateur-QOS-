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
        snapshot = list(_audit_log)
        yield f"data: {json.dumps({'type': 'snapshot', 'log': snapshot})}\n\n"
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
    _audit_log.append(payload)
    await _broadcast({"type": "audit", "data": payload})
    logger.info(
        f"Audit reçu — cycle={payload.get('cycle')} "
        f"decision={payload.get('decision')} "
        f"breach={payload.get('breach_type')}"
    )
    return {"status": "ok"}


@app.get("/audit/log")
async def get_audit_log() -> Dict[str, Any]:
    return {"count": len(_audit_log), "log": list(_audit_log)}


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
<title>QoS Orchestrator — Dashboard</title>
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

  /* ── Charts row ── */
  .charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
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

  /* ── SLO weights ── */
  .slo-grid { display: flex; flex-direction: column; gap: 8px; }
  .slo-row { display: flex; align-items: center; gap: 10px; }
  .slo-name { width: 90px; font-size: 12px; color: var(--muted); }
  .slo-bar-bg { flex: 1; height: 8px; background: #2d3148; border-radius: 4px; }
  .slo-bar-fill { height: 100%; border-radius: 4px; transition: width .5s; }
  .slo-weight-val { width: 40px; text-align: right; font-size: 12px; font-weight: 600; }
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
</header>

<main>

  <!-- VMs -->
  <div>
    <div class="section-title">État des VMs — Métriques temps réel</div>
    <div class="vm-grid" id="vm-grid"></div>
  </div>

  <!-- Charts -->
  <div class="charts-row">
    <div class="chart-card">
      <div class="section-title">Latence — historique & prédictions</div>
      <canvas id="chart-latency" height="160"></canvas>
    </div>
    <div class="chart-card">
      <div class="section-title">Poids SLOs actifs (TOPSIS)</div>
      <canvas id="chart-slo" height="160"></canvas>
    </div>
  </div>

  <!-- SLO weights details -->
  <div class="chart-card">
    <div class="section-title">Détail des SLOs actifs</div>
    <div class="slo-grid" id="slo-detail"></div>
  </div>

  <!-- Audit log -->
  <div>
    <div class="section-title">Audit log — Décisions de l'orchestrateur</div>
    <div class="audit-wrap">
      <table class="audit-table">
        <thead>
          <tr>
            <th>Heure</th><th>Cycle</th><th>Décision</th><th>Type</th>
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

const COLORS = { latency: '#3b82f6', cpu_usage: '#f59e0b', ram_usage: '#22c55e' };
const HIST_LEN = 60;

let latencyHist = {}; // vm_id → [values]
let latencyPredHist = {}; // vm_id → [first_pred]
let sloWeightHist = {}; // metric → [weights]
let cycleHist = [];
let sloWeightCycles = [];
let currentSlos = [];
let latencyThreshold = SLO_DEFAULTS['latency'] || 300;

VMs.forEach(v => { latencyHist[v] = []; latencyPredHist[v] = []; });
METRICS.forEach(m => { sloWeightHist[m] = []; });

// ── Charts ────────────────────────────────────────────────────
const latCtx = document.getElementById('chart-latency').getContext('2d');
const latChart = new Chart(latCtx, {
  type: 'line',
  data: {
    labels: [],
    datasets: VMs.map((v, i) => ({
      label: v,
      data: [],
      borderColor: ['#3b82f6','#f59e0b','#22c55e','#8b5cf6'][i],
      backgroundColor: 'transparent',
      borderWidth: 1.8, pointRadius: 0, tension: 0.3,
    })).concat([{
      label: 'Seuil SLO',
      data: [],
      borderColor: '#ef4444', borderDash: [6,3],
      backgroundColor: 'transparent',
      borderWidth: 1.5, pointRadius: 0,
    }])
  },
  options: {
    animation: false, responsive: true,
    plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
    scales: {
      x: { ticks: { color: '#64748b', maxTicksLimit: 8 }, grid: { color: '#1e2235' } },
      y: { ticks: { color: '#64748b' }, grid: { color: '#1e2235' }, min: 0 }
    }
  }
});

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
        <span class="vm-tag idle" id="tag-${vm}">IDLE</span>
      </div>
      <div class="vm-metrics" id="metrics-${vm}">
        ${METRICS.map(m => `
          <div>
            <div class="metric-row">
              <span class="metric-name">${m}</span>
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
    tag.textContent = isActive ? 'ACTIF' : 'IDLE';
    tag.className = 'vm-tag ' + (isActive ? 'active' : 'idle');

    const metricMap = { latency: latVal, cpu_usage: vmData.cpu_usage, ram_usage: vmData.ram_usage };
    const preds = vmData.predictions || {};

    METRICS.forEach(m => {
      const val = metricMap[m];
      const el = document.getElementById('val-' + vm + '-' + m);
      const bar = document.getElementById('bar-' + vm + '-' + m);
      const predEl = document.getElementById('pred-' + vm + '-' + m);
      if (!el) return;

      const unit = UNITS[m] || '';
      const slo = slos.find(s => s.metric === m);
      const thr = slo ? parseFloat(slo.threshold) : (SLO_DEFAULTS[m] || 100);

      if (val !== null && val !== undefined) {
        el.textContent = val.toFixed(1) + ' ' + unit;
        el.style.color = val > thr ? '#ef4444' : '#22c55e';
        bar.style.width = Math.min(100, (val / thr) * 100) + '%';
        bar.style.background = val > thr ? '#ef4444' : (COLORS[m] || '#3b82f6');
      } else {
        el.textContent = '—'; bar.style.width = '0%';
      }

      const mp = (preds[m] || []);
      if (mp.length > 0) {
        const firstPred = mp[0].toFixed(1);
        const maxPred = Math.max(...mp).toFixed(1);
        predEl.textContent = `${firstPred} → ${maxPred} ${unit}`;
        predEl.style.color = parseFloat(maxPred) > thr ? '#f59e0b' : '#94a3b8';
      } else {
        predEl.textContent = '—';
      }
    });

    // Historique latence pour chart
    if (latVal !== null && latVal !== undefined) {
      if (!latencyHist[vm]) latencyHist[vm] = [];
      latencyHist[vm].push(latVal);
      if (latencyHist[vm].length > HIST_LEN) latencyHist[vm].shift();
    }
  });

  // Cycles
  cycleHist.push(cycle);
  if (cycleHist.length > HIST_LEN) cycleHist.shift();

  // Chart latence
  const maxLen = Math.max(...VMs.map(v => (latencyHist[v]||[]).length));
  latChart.data.labels = cycleHist.slice(-maxLen);
  VMs.forEach((v, i) => {
    latChart.data.datasets[i].data = latencyHist[v] || [];
  });
  latChart.data.datasets[VMs.length].data = Array(cycleHist.slice(-maxLen).length).fill(latencyThreshold);
  latChart.update('none');

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

  // SLO detail bars
  const sloDetail = document.getElementById('slo-detail');
  sloDetail.innerHTML = slos.map(s => {
    const pct = Math.round((s.weight || 0) * 100);
    const color = COLORS[s.metric] || '#94a3b8';
    return `<div class="slo-row">
      <span class="slo-name">${s.metric}</span>
      <div class="slo-bar-bg"><div class="slo-bar-fill" style="width:${pct}%;background:${color}"></div></div>
      <span class="slo-weight-val" style="color:${color}">${pct}%</span>
      <span style="font-size:11px;color:#94a3b8;margin-left:8px">${s.operator} ${s.threshold?.toFixed(1)} ${s.unit || ''} ${s.is_primary ? '★' : ''}</span>
    </div>`;
  }).join('') || '<span style="color:#64748b">Aucun SLO actif</span>';
}

// ── Audit log ─────────────────────────────────────────────────
let auditCount = 0;
let proactiveMigrateCount = 0;
let reactiveMigrateCount = 0;
const seenMigrations = new Set();  // clé cycle → évite double comptage (snapshot + live)

function addAuditRows(entries, prepend = false) {
  const tbody = document.getElementById('audit-body');
  entries.forEach(e => {
    const dec = e.decision || '—';
    const breach = e.breach_type || 'none';

    if (dec === 'migrate' && e.cycle != null && !seenMigrations.has(e.cycle)) {
      seenMigrations.add(e.cycle);
      if (breach === 'proactive') proactiveMigrateCount++;
      else if (breach === 'reactive') reactiveMigrateCount++;
      document.getElementById('h-proactive-count').textContent = proactiveMigrateCount;
      document.getElementById('h-reactive-count').textContent = reactiveMigrateCount;
    }
    const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString('fr-FR') : '—';
    const decBadge = dec === 'migrate'
      ? `<span class="badge badge-migrate">MIGRATE</span>`
      : `<span class="badge badge-stay">STAY</span>`;
    const breachBadge = breach === 'reactive'
      ? `<span class="badge badge-reactive">réactif</span>`
      : breach === 'proactive'
        ? `<span class="badge badge-proactive">proactif</span>`
        : `<span class="badge badge-none">—</span>`;
    const metrics = (e.violated_metrics || [])
      .map(m => `${m.metric} (${(m.weight ?? 0).toFixed(2)})`)
      .join(', ') || '—';
    const score = e.topsis_score != null ? parseFloat(e.topsis_score).toFixed(4) : '—';
    const reason = (e.reason || '').substring(0, 60);

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono">${ts}</td>
      <td>${e.cycle ?? '—'}</td>
      <td>${decBadge}</td>
      <td>${breachBadge}</td>
      <td class="mono">${e.from_vm || '—'}</td>
      <td class="mono" style="color:${dec==='migrate'?'#22c55e':'inherit'}">${e.to_vm || '—'}</td>
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
    dec === 'migrate' ? '<span class="badge badge-migrate">MIGRATE</span>'
                      : '<span class="badge badge-stay">STAY</span>';
  document.getElementById('h-breach').innerHTML =
    breach === 'reactive'  ? '<span class="badge badge-reactive">réactif</span>' :
    breach === 'proactive' ? '<span class="badge badge-proactive">proactif</span>' :
                             '<span class="badge badge-none">—</span>';
  document.getElementById('h-topsis').textContent = score != null ? parseFloat(score).toFixed(4) : '—';
  document.getElementById('h-mode').textContent = auditData.mode || '—';
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
    else if (msg.type === 'audit') { addAuditRows([msg.data], true); updateHeader(msg.data); }
    else if (msg.type === 'snapshot') addAuditRows((msg.log || []).reverse(), false);
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
