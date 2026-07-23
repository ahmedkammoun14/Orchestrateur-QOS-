"""
hub/orchestrator_core.py — QoS Orchestrator Hub (Hub-and-Spoke).
"""

import asyncio
import dataclasses
import logging
import time
import uvicorn
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, status, Body

from shared import config
from shared.logging_utils import C, PrettyFormatter
from shared.models import LatencyPayload, RTTMeasurement, SLO, SLOIntent
from shared.timing import StepProfiler
from shared.timing_writer import TimingWriter
from hub.provider_arbitration import (
    ProviderAssessment, ProviderOffer, evaluate_provider, negotiate,
)


def setup_logger() -> logging.Logger:
    log = logging.getLogger("OrchestratorCore")
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(PrettyFormatter())
        log.addHandler(h)
    log.propagate = False
    return log


logger = setup_logger()

_URLS = {
    "database":              f"http://{config.HUB_HOST}:{config.DATABASE_PORT}",
    "collector":             f"http://{config.HUB_HOST}:{config.COLLECTOR_PORT}",
    "history_loader":        f"http://{config.HUB_HOST}:{config.HISTORY_LOADER_PORT}",
    "ml_predictor":          f"http://{config.HUB_HOST}:{config.ML_PREDICTOR_PORT}",
    "metrics_manager":       f"http://{config.HUB_HOST}:{config.METRICS_MANAGER_PORT}",
    "decision_intelligence": f"http://{config.HUB_HOST}:{config.DECISION_INTELLIGENCE_PORT}",
    "observability":         f"http://{config.HUB_HOST}:{config.OBSERVABILITY_PORT}",
}

OPENSTACK_CLIENT_URL = f"http://{config.OPENSTACK_MASTER_IP}:{config.OPENSTACK_CLIENT_PORT}"


class OrchestratorState:
    def __init__(self) -> None:
        self._mode: str = "autonomous"
        self.service_vm: Optional[str] = next(iter(config.VM_REGISTRY)) if config.VM_REGISTRY else None
        self.last_migration_ts: Optional[float] = None
        self.bootstrap_cycles: int = 0
        self.BOOTSTRAP_MIN: int = 5
        self.current_slos: List[Dict[str, Any]] = []
        # Poids originaux des SLOs primaires issus de la dernière intention
        # (mode enhanced) — préservés pour éviter la dilution cumulative
        # du poids cycle après cycle. Réinitialisés à chaque nouvelle
        # intention reçue via /intent.
        self.original_intent_weights: Dict[str, float] = {}
        # Timing de réception d'intention (mode enhanced) en attente d'être
        # consommé par le prochain cycle pour produire une ligne de mesures
        # « par intention ». Voir _persist_timing.
        self.pending_intent_timing: Optional[Dict[str, Any]] = None
        self.cycle_count: int = 0
        self.last_decision: Dict[str, Any] = {}
        self.last_mi_scores: Dict[str, float] = {}
        self.last_collected: List[Dict[str, Any]] = []
        self.last_predictions: Dict[str, Dict[str, Any]] = {}
        self.snapshot_collected: List[Dict[str, Any]] = []
        self.snapshot_predictions: Dict[str, Dict[str, Any]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    def check_cooldown(self) -> bool:
        if self.last_migration_ts is None:
            return False
        elapsed = time.monotonic() - self.last_migration_ts
        return elapsed < config.MIGRATION_COOLDOWN_S

    def get_data_payload(self) -> Dict[str, Any]:
        vms_data = {}
        collected_map = {r["vm_id"]: r for r in self.snapshot_collected}
        for vm_id in config.VM_REGISTRY:
            coll  = collected_map.get(vm_id, {})
            preds = self.snapshot_predictions.get(vm_id, {})
            vms_data[vm_id] = {
                "rtt_ms":       coll.get("latency"),
                "cpu_usage":    coll.get("cpu_usage"),
                "ram_usage":    coll.get("ram_usage"),
                "reliability":  coll.get("reliability"),
                "total_cores":  coll.get("total_cores"),
                "total_ram_gb": coll.get("total_ram_gb"),
                "is_active":    (vm_id == self.service_vm),
                "predictions": {
                    "latency":   preds.get("latency",   {}).get("predictions", []),
                    "cpu_usage": preds.get("cpu_usage", {}).get("predictions", []),
                    "ram_usage": preds.get("ram_usage", {}).get("predictions", []),
                }
            }
        return {
            "vms":           vms_data,
            "slos":          self.current_slos,
            "mi_scores":     self.last_mi_scores,
            "last_decision": self.last_decision,
            "cycle":         self.cycle_count,
        }


state = OrchestratorState()

# Writers Excel des mesures de performance — un fichier par mode :
#   • autonomous → une ligne par cycle
#   • enhanced   → une ligne par intention traitée
_TIMING_MAX_BYTES = config.TIMING_EXCEL_MAX_MB * 1024 * 1024
timing_writer_autonomous = TimingWriter.for_autonomous(config.TIMING_EXCEL_AUTONOMOUS_PATH, _TIMING_MAX_BYTES)
timing_writer_enhanced   = TimingWriter.for_enhanced(config.TIMING_EXCEL_ENHANCED_PATH, _TIMING_MAX_BYTES)


async def _post(client: httpx.AsyncClient, url: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.post(url, json=payload, timeout=10.0)
        if resp.status_code in (200, 201, 202):
            return resp.json()
        return None
    except Exception as e:
        logger.error(f"❌ POST {C.CYAN}{url}{C.RESET} failed : {e}")
        return None


async def _post_audit(url: str, payload: Dict[str, Any]) -> None:
    """
    Envoi best-effort de l'audit à observability avec un client DÉDIÉ.
    Fire-and-forget : ne doit PAS réutiliser le client du cycle (qui se ferme
    en fin de _run_flow → erreur 'client has been closed').
    """
    try:
        async with httpx.AsyncClient() as c:
            await c.post(url, json=payload, timeout=10.0)
    except Exception as e:
        logger.debug(f"🔇 Audit non transmis à observability : {e}")

def _threshold_map() -> Dict[str, float]:
    """
    Construit la table des seuils utilisée pour calculer is_violation
    dans les historiques.
    - Pour les métriques primaires : seuil métier fixe (registry)
    - Pour les autres : seuil du SLO actif si présent, sinon
      utilise une borne très permissive (max) pour éviter de marquer
      faussement des violations sur métriques non corrélées.
    """
    thr = {}
    for m, meta in config.METRICS_REGISTRY.items():
        if meta.get("is_primary_objective", False):
            thr[m] = meta["default_threshold"]
        else:
            # Borne permissive — sera écrasée si un SLO secondaire est actif
            thr[m] = meta["bounds"]["max"]

    for slo in state.current_slos:
        if slo.get("metric") in thr:
            # Utilise la cible (target) comme seuil de détection si disponible,
            # cohérent avec le ViolationDetector du DI.
            target_val = slo.get("target")
            thr[slo["metric"]] = float(target_val) if target_val is not None else float(slo["threshold"])
    return thr

def _is_violation(record: Dict[str, Any], thresholds: Dict[str, float]) -> bool:
    """
    Détecte une violation sur l'enregistrement en se basant UNIQUEMENT
    sur les SLOs primaires (objectif métier courant — fixe en autonomous,
    défini par l'intention en enhanced), pas sur les SLOs secondaires
    adaptatifs.
    """
    active_metrics = {s["metric"] for s in state.current_slos if s.get("is_primary", False)}
    for metric, meta in config.METRICS_REGISTRY.items():
        # Ne considère que les métriques qui ont un SLO primaire actif
        if metric not in active_metrics:
            continue
        val = record.get(metric)
        if val is None:
            continue
        thr = thresholds.get(metric, meta["default_threshold"])
        op  = meta["operator"]
        if (op == "<"  and val >= thr) or (op == "<=" and val >  thr) or \
           (op == ">"  and val <= thr) or (op == ">=" and val <  thr):
            return True
    return False

def _zip_histories(histories, thresholds):
    metrics = list(config.METRICS_REGISTRY.keys())
    lengths = [len(histories[m]) for m in metrics if m in histories and histories[m]]
    n = min(lengths) if lengths else 0
    records = []
    for i in range(n):
        rec = {m: histories[m][i]["value"] for m in metrics if m in histories and i < len(histories[m])}
        rec["is_violation"] = _is_violation(rec, thresholds)
        records.append(rec)
    return records

def _all_vals(histories):
    return {m: [h["value"] for h in histories[m]] for m in config.METRICS_REGISTRY if m in histories}

def _ml_payload(histories: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    payload = {"latency_history": [{"value": h["value"]} for h in histories.get("latency", [])]}
    for m, f in {"cpu_usage": "cpu_history", "ram_usage": "ram_history"}.items():
        if m in histories and histories[m]:
            payload[f] = [{"value": h["value"]} for h in histories[m]]
    return payload

def _extract_predictions(res: Dict[str, Any]) -> Dict[str, Any]:
    field_map = {"predicted_latency": "latency", "predicted_cpu": "cpu_usage", "predicted_ram": "ram_usage"}
    norm = {}
    for f, m in field_map.items():
        data = res.get(f)
        if data and "predictions" in data:
            norm[m] = data
    return norm


def _build_bootstrap_slos() -> List[Dict[str, Any]]:
    """
    Construit les SLOs initiaux pendant la phase bootstrap.

    Ne génère QUE des SLOs primaires (is_primary_objective=True
    dans METRICS_REGISTRY) avec leur seuil métier fixe.

    Les métriques non primaires (cpu_usage, ram_usage en autonomous)
    seront ajoutées dynamiquement par le metrics_manager APRÈS
    le bootstrap, uniquement si MI détecte une corrélation.
    """
    slos = []
    for metric, meta in config.METRICS_REGISTRY.items():
        if not meta.get("is_primary_objective", False):
            continue
        slos.append({
            "metric":     metric,
            "operator":   meta["operator"],
            "threshold":  meta["default_threshold"],
            "unit":       meta["unit"],
            "weight":     1.0,
            "target":     meta["default_threshold"] * 0.9,
            "window":     "5m",
            "is_primary": True,
        })
    return slos


async def _sync_active_vm(client: httpx.AsyncClient) -> None:
    try:
        r = await client.get(f"{OPENSTACK_CLIENT_URL}/active_vm", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            active_vm = data.get("active_vm")
            if active_vm and active_vm in config.VM_REGISTRY:
                state.service_vm = active_vm
                logger.info(
                    f"✅ VM active synchronisée depuis kubectl : "
                    f"{C.GREEN}{state.service_vm}{C.RESET} "
                    f"(cluster : {C.CYAN}{data.get('cluster')}{C.RESET})"
                )
            else:
                logger.warning(
                    "⚠️  Aucun pod actif trouvé sur kubectl — "
                    f"service_vm par défaut conservé : {C.YELLOW}{state.service_vm}{C.RESET}"
                )
        else:
            logger.warning(
                f"⚠️  openstack_client /active_vm a retourné HTTP {r.status_code} "
                "— synchronisation ignorée"
            )
    except Exception as e:
        logger.warning(
            f"⚠️  Impossible de synchroniser active_vm depuis openstack_client : {e}"
        )


async def _execute_kubectl_migration(client: httpx.AsyncClient, from_vm: str, to_vm: str) -> bool:
    try:
        resp = await client.post(
            f"{OPENSTACK_CLIENT_URL}/migrate",
            json={"from_vm": from_vm, "to_vm": to_vm},
            timeout=35.0
        )
        if resp.status_code == 200:
            data = resp.json()
            logger.info(
                f"✅ Migration kubectl réussie : "
                f"{C.YELLOW}{from_vm}{C.RESET} → {C.GREEN}{to_vm}{C.RESET} "
                f"| cluster : {C.CYAN}{data.get('cluster')}{C.RESET}"
            )
            return True
        else:
            logger.error(
                f"❌ Échec migration kubectl : HTTP {resp.status_code} "
                f"| {C.YELLOW}{from_vm}{C.RESET} → {C.RED}{to_vm}{C.RESET}"
            )
            return False
    except Exception as e:
        logger.error(
            f"❌ Erreur lors de la migration kubectl : {e} "
            f"| {C.YELLOW}{from_vm}{C.RESET} → {C.RED}{to_vm}{C.RESET}"
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  Context de cycle — variables locales partagées entre les 8 étapes du flow
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class _FlowContext:
    vm_ids:            List[str]
    now_iso:           str
    active_metrics:    List[str]            = dataclasses.field(default_factory=list)
    collector_metrics: List[str]            = dataclasses.field(default_factory=list)
    results:           List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    new_collected:     List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    vm_histories:      Dict[str, Any]       = dataclasses.field(default_factory=dict)
    new_predictions:   Dict[str, Any]       = dataclasses.field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
#  Étapes du flow — chacune correspond à un bloc numéroté de l'ancien _run_flow
# ─────────────────────────────────────────────────────────────────────────────

async def _step1_slos(client: httpx.AsyncClient, ctx: _FlowContext, mode: str,
                      prof: Optional[StepProfiler] = None) -> None:
    """Calcule / met à jour les SLOs actifs (bootstrap ou metrics_manager)."""
    def _t(name: str):
        return prof.step(name) if prof is not None else nullcontext()
    if state.cycle_count < state.BOOTSTRAP_MIN:
        state.current_slos = _build_bootstrap_slos()
        ctx.active_metrics = [s["metric"] for s in state.current_slos]
        logger.info(
            f"🟡 Phase bootstrap ({state.cycle_count}/{state.BOOTSTRAP_MIN}) "
            f"— SLOs primaires uniquement : {C.GREEN}{ctx.active_metrics}{C.RESET}"
        )
        return

    with _t("slos_load_hist"):
        h_res = await _post(client, f"{_URLS['history_loader']}/load", {
            "vm_id":   state.service_vm,
            "metrics": list(config.METRICS_REGISTRY.keys()),
            "size":    config.HISTORY_WINDOW,
        })
    svc_hists = h_res.get("histories", {}) if h_res else {}

    if mode == "enhanced":
        for slo in state.current_slos:
            if slo.get("is_primary") and slo["metric"] in state.original_intent_weights:
                slo["weight"] = state.original_intent_weights[slo["metric"]]
        mm_payload = {
            "slos":    state.current_slos,
            "history": _zip_histories(svc_hists, _threshold_map()),
            "cycle":   state.cycle_count,
        }
        mm_url = f"{_URLS['metrics_manager']}/validate"
    else:
        mm_payload = {
            "history":  _zip_histories(svc_hists, _threshold_map()),
            "all_vals": _all_vals(svc_hists),
            "cycle":    state.cycle_count,
        }
        mm_url = f"{_URLS['metrics_manager']}/compute"

    with _t("slos_mm"):
        mm_res = await _post(client, mm_url, mm_payload)
    if prof is not None and mm_res:
        _mm_timings = mm_res.get("timings", {})
        if _mm_timings:
            logger.debug(f"MM timings reçus : {list(_mm_timings.keys())}")
        else:
            logger.warning("⚠️  MM response sans 'timings' — relancer le service metrics_manager")
        prof.merge(_mm_timings)   # absorbe mi_compute + mi_slos
    if mm_res:
        state.current_slos   = mm_res.get("slos", state.current_slos)
        ctx.active_metrics   = mm_res.get("active_metrics", list(config.METRICS_REGISTRY.keys()))
        state.last_mi_scores = mm_res.get("mi_scores", {})

        primaries   = [s["metric"] for s in state.current_slos if s.get("is_primary")]
        secondaries = [s["metric"] for s in state.current_slos if not s.get("is_primary")]
        logger.info(
            f"📋 SLOs mis à jour — {C.CYAN}{len(state.current_slos)}{C.RESET} SLO(s) actif(s) "
            f"| primaires : {C.GREEN}{primaries}{C.RESET} "
            f"| secondaires : {C.YELLOW}{secondaries}{C.RESET}"
        )
    else:
        ctx.active_metrics = [s["metric"] for s in state.current_slos]
        logger.warning("⚠️  MetricsManager indisponible — SLOs précédents conservés")


async def _step2_persist_slos(client: httpx.AsyncClient, ctx: _FlowContext) -> None:
    """Persiste les SLOs courants dans la base de données."""
    await _post(client, f"{_URLS['database']}/store/slos",
                {"slos": state.current_slos, "timestamp": ctx.now_iso})


async def _step3_collect(client: httpx.AsyncClient, ctx: _FlowContext,
                         prof: Optional[StepProfiler] = None) -> None:
    """Collecte les métriques (CPU, RAM) via le collector pour toutes les VMs."""
    ctx.collector_metrics = [m for m in config.METRICS_REGISTRY.keys() if m != "latency"]
    coll_res   = await _post(client, f"{_URLS['collector']}/collect",
                             {"active_metrics": ctx.collector_metrics, "cycle": state.cycle_count})
    ctx.results = coll_res.get("results", []) if coll_res else []

    # Temps de collecte par VM (mesuré dans le collector, collecte parallèle)
    if prof is not None and coll_res:
        per_vm = coll_res.get("collect_timings") or {}
        for vm_id in config.VM_REGISTRY:
            prof.record(f"collect_{vm_id}", per_vm.get(vm_id) or 0.0)
        prof.record("collect_max", coll_res.get("collect_max_ms") or 0.0)
    logger.info(
        f"📡 Métriques collectées — {C.CYAN}{len(ctx.results)}{C.RESET} VM(s) "
        f"| métriques : {C.CYAN}{ctx.collector_metrics}{C.RESET}"
    )


async def _step4_persist_metrics(
    client: httpx.AsyncClient,
    ctx: _FlowContext,
    measurements: List[RTTMeasurement],
) -> None:
    """Fusionne RTT + métriques collectées, persiste chaque VM en parallèle."""
    rtt_lookup    = {m.vm_id: m for m in measurements}
    new_collected: List[Dict[str, Any]] = []
    persist_tasks = []

    for r in ctx.results:
        vm_id   = r["vm_id"]
        rtt_m   = rtt_lookup.get(vm_id)
        rtt_val = rtt_m.rtt_ms if rtt_m and rtt_m.reachable else None

        vm_metrics = {**{m: r.get(m) for m in ctx.collector_metrics}, "latency": rtt_val}
        new_collected.append({
            "vm_id": vm_id, **vm_metrics,
            "reliability":  r.get("reliability"),
            "reachable":    r.get("reachable"),
            # Capacité déclarée par la VM elle-même (fédération / service
            # mesh), propagée par le collector — transite par le hub comme
            # toute donnée de decision_intelligence (architecture en étoile).
            "total_cores":  r.get("total_cores"),
            "total_ram_gb": r.get("total_ram_gb"),
        })
        persist_tasks.append(_post(client, f"{_URLS['database']}/store/metrics", {
            "vm_id": vm_id, "metrics": vm_metrics,
            "timestamp": ctx.now_iso, "reliability": r.get("reliability"),
        }))

    await asyncio.gather(*persist_tasks)
    state.last_collected = new_collected
    ctx.new_collected    = new_collected

    for entry in new_collected:
        vm_id = entry["vm_id"]
        tag   = f"{C.GREEN}[ACTIVE]{C.RESET}" if vm_id == state.service_vm else f"{C.CYAN}[IDLE]{C.RESET}"
        logger.debug(
            f"🔍 {tag} {C.BOLD}{vm_id}{C.RESET} — "
            f"RTT: {C.CYAN}{entry.get('latency')} ms{C.RESET}  "
            f"CPU: {C.CYAN}{entry.get('cpu_usage')} %{C.RESET}  "
            f"RAM: {C.CYAN}{entry.get('ram_usage')} %{C.RESET}  "
            f"Fiabilité: {C.CYAN}{entry.get('reliability')}{C.RESET}"
        )


def _step5_check_violations(ctx: _FlowContext) -> None:
    """Log de cohérence : violation courante + signal proactif ML sur la VM active."""
    svc_data  = next((r for r in state.last_collected if r["vm_id"] == state.service_vm), None)
    violation = _is_violation(svc_data, _threshold_map()) if svc_data else False

    # Signal proactif : une prédiction ML dépasse le seuil proactif
    thresholds   = _threshold_map()
    svc_preds    = state.last_predictions.get(state.service_vm, {})
    proactive_signals = []
    for slo in state.current_slos:
        metric    = slo["metric"]
        threshold = thresholds.get(metric, float("inf"))
        preds     = svc_preds.get(metric, {}).get("predictions", [])
        if any(p > threshold for p in preds):
            proactive_signals.append(metric)

    if violation:
        logger.warning(
            f"⚠️  Violation SLO réactive sur {C.YELLOW}{state.service_vm}{C.RESET} "
            "— migration imminente"
        )
    elif proactive_signals:
        logger.warning(
            f"🟡 Signal proactif ML sur {C.YELLOW}{state.service_vm}{C.RESET} "
            f"| métriques : {C.CYAN}{proactive_signals}{C.RESET} "
            "— décision proactive en cours"
        )
    else:
        logger.info(f"✅ SLOs respectés sur {C.GREEN}{state.service_vm}{C.RESET}")


async def _step6_load_histories(client: httpx.AsyncClient, ctx: _FlowContext) -> None:
    """Charge les historiques de toutes les VMs en parallèle."""
    hist_responses = await asyncio.gather(*[
        _post(client, f"{_URLS['history_loader']}/load", {
            "vm_id":   vid,
            "metrics": list(config.METRICS_REGISTRY.keys()),
            "size":    config.HISTORY_WINDOW,
        }) for vid in ctx.vm_ids
    ])
    ctx.vm_histories = {
        vid: (res.get("histories", {}) if res else {})
        for vid, res in zip(ctx.vm_ids, hist_responses)
    }
    logger.info(
        f"📚 Historiques chargés pour {C.CYAN}{len(ctx.vm_ids)}{C.RESET} VM(s) "
        f"| fenêtre : {C.CYAN}{config.HISTORY_WINDOW}{C.RESET} points"
    )


async def _step7_predict(client: httpx.AsyncClient, ctx: _FlowContext) -> None:
    """Génère les prédictions ML pour toutes les VMs en parallèle."""
    new_predictions: Dict[str, Dict[str, Any]] = {vid: {} for vid in ctx.vm_ids}

    p_responses = await asyncio.gather(*[
        _post(client, f"{_URLS['ml_predictor']}/predict",
              _ml_payload(ctx.vm_histories.get(vid, {})))
        for vid in ctx.vm_ids
    ])
    for vid, res in zip(ctx.vm_ids, p_responses):
        if res and not res.get("all_apis_down"):
            new_predictions[vid] = _extract_predictions(res)

    state.last_predictions = new_predictions
    ctx.new_predictions    = new_predictions

    successful_preds = sum(1 for v in new_predictions.values() if v)
    logger.info(
        f"🤖 Prédictions ML générées — "
        f"{C.GREEN}{successful_preds}{C.RESET}/{len(ctx.vm_ids)} VM(s)"
    )
    for vid, preds in new_predictions.items():
        if preds:
            lat = preds.get("latency",   {}).get("predictions", ["N/A"])[0]
            cpu = preds.get("cpu_usage", {}).get("predictions", ["N/A"])[0]
            ram = preds.get("ram_usage", {}).get("predictions", ["N/A"])[0]
            logger.debug(
                f"🔍 Prédiction {C.BOLD}{vid}{C.RESET} — "
                f"Latence: {C.CYAN}{lat} ms{C.RESET}  "
                f"CPU: {C.CYAN}{cpu} %{C.RESET}  "
                f"RAM: {C.CYAN}{ram} %{C.RESET}"
            )

    state.snapshot_collected   = list(state.last_collected)
    state.snapshot_predictions = dict(state.last_predictions)


def _build_candidates(collected: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Construit la liste de candidats attendue par decision_intelligence et par
    provider_arbitration, à partir des mesures du collector.

    Extrait de _step8_decide pour être partagé avec le point de réception de
    la passation inter-provider, sans dupliquer la convention payload_key
    (latency → rtt_ms, etc.).
    """
    current_data = []
    for lc in collected:
        entry = {"vm_id": lc["vm_id"]}
        for m, meta in config.METRICS_REGISTRY.items():
            entry[meta["payload_key"]] = lc.get(m)
        # Capacité déclarée par la VM (fédération / service mesh) — nécessaire
        # à TOPSIS pour convertir cpu_usage/ram_usage (%) en disponibilité
        # absolue. Transite tel quel depuis le collector via le hub.
        entry["total_cores"]  = lc.get("total_cores")
        entry["total_ram_gb"] = lc.get("total_ram_gb")
        current_data.append(entry)
    return current_data


async def _step8_decide(client: httpx.AsyncClient, ctx: _FlowContext, prof: StepProfiler) -> None:
    """
    Appelle decision_intelligence et exécute la migration si nécessaire.

    Aiguillage sur l'interrupteur multi-provider de shared/config.py (OFF par
    défaut) : le chemin mono-provider est le code d'origine, déplacé tel quel
    dans _decide_mono_provider — zéro changement de comportement tant que
    l'interrupteur n'est pas activé explicitement.
    """
    current_data = _build_candidates(state.last_collected)
    if not config.MULTI_PROVIDER_ENABLED:
        await _decide_mono_provider(client, ctx, prof, current_data)
        return
    await _decide_multi_provider(client, ctx, prof, current_data)


async def _decide_mono_provider(
    client: httpx.AsyncClient,
    ctx: _FlowContext,
    prof: StepProfiler,
    current_data: List[Dict[str, Any]],
) -> None:
    """
    Corps ORIGINAL de _step8_decide, déplacé sans aucune modification de
    logique (seule current_data, calculée par l'appelant, est passée en
    paramètre au lieu d'être recalculée ici). C'est le chemin actif par
    défaut (interrupteur multi-provider OFF) — toute divergence par rapport
    au comportement historique est un bug.
    """
    # Lookup explicite par vm_id — évite tout risque de désalignement positionnel
    # entre vm_ids et state.last_collected.
    collected_lookup = {lc["vm_id"]: lc for lc in state.last_collected}

    di_payload = {
        "current_data":       current_data,
        "predictions_map":    state.last_predictions,
        "slos":               state.current_slos,
        "service_vm":         state.service_vm,
        "cooldown_active":    state.check_cooldown(),
        "reliability_scores": {
            vid: collected_lookup.get(vid, {}).get("reliability", 1.0)
            for vid in ctx.vm_ids
        },
        "cycle":              state.cycle_count,
        "mi_scores":          state.last_mi_scores,
    }
    if di_payload["cooldown_active"]:
        elapsed   = time.monotonic() - state.last_migration_ts
        remaining = config.MIGRATION_COOLDOWN_S - elapsed
        logger.warning(
            f"⏳ Cooldown actif — migration bloquée "
            f"({C.YELLOW}{remaining:.0f}s{C.RESET} restante(s))"
        )

    if not di_payload["cooldown_active"]:
        with prof.step("decide_call"):
            di_res = await _post(client, f"{_URLS['decision_intelligence']}/decide", di_payload)
    else:
        di_res = {
            "decision": "stay", "from_vm": None, "to_vm": None,
            "reason": "Cooldown active", "topsis_score": None, "breach_type": None,
        }

    if not di_res:
        return

    # Fusionne les sous-mesures TOPSIS / détection renvoyées par decision_intelligence
    prof.merge(di_res.get("timings"))

    state.last_decision = di_res
    decision = di_res.get("decision", "?")
    reason   = di_res.get("reason", "—")
    topsis   = di_res.get("topsis_score")
    breach   = di_res.get("breach_type", "—")

    # ── Audit → observability (toutes les décisions, pas seulement MIGRATE) ──
    audit_payload = {
        **di_res,
        "cycle":            state.cycle_count,
        "mode":             state._mode,
        "violated_metrics": di_res.get("violated_metrics", []),
        "slos_active":      state.current_slos,
        "mi_scores":        state.last_mi_scores,
        "current_metrics":  {
            lc["vm_id"]: {
                "latency":   lc.get("latency"),
                "cpu_usage": lc.get("cpu_usage"),
                "ram_usage": lc.get("ram_usage"),
            } for lc in state.last_collected
        },
    }
    asyncio.create_task(
        _post_audit(f"{_URLS['observability']}/audit", audit_payload)
    )

    if decision == "migrate":
        from_vm = di_res["from_vm"]
        to_vm   = di_res["to_vm"]
        logger.info(
            f"\n{'═'*60}\n"
            f"  🎯 DÉCISION : {C.BOLD}{C.YELLOW}MIGRATION{C.RESET}\n"
            f"  {'Source':<16}: {C.RED}{from_vm}{C.RESET}\n"
            f"  {'Destination':<16}: {C.GREEN}{to_vm}{C.RESET}\n"
            f"  {'Raison':<16}: {C.CYAN}{reason}{C.RESET}\n"
            f"  {'Score TOPSIS':<16}: {C.CYAN}{topsis}{C.RESET}\n"
            f"  {'Type violation':<16}: {C.YELLOW}{breach}{C.RESET}\n"
            f"{'═'*60}"
        )

        with prof.step("store_decision"):
            await _post(client, f"{_URLS['database']}/store/decision", di_res)

        # Le temps de migration = wall-clock de l'appel HTTP. openstack_client étant
        # synchrone (200 OK renvoyé seulement après la fin de la migration), cette
        # durée englobe toute la migration kubectl (date réponse − date requête).
        with prof.step("migration"):
            kubectl_ok = await _execute_kubectl_migration(client, from_vm, to_vm)
        if not kubectl_ok:
            logger.warning(
                f"⚠️  Migration kubectl échouée — "
                f"état interne mis à jour malgré tout "
                f"({C.YELLOW}{from_vm}{C.RESET} → {C.GREEN}{to_vm}{C.RESET})"
            )

        state.service_vm        = to_vm
        state.last_migration_ts = time.monotonic()
        logger.info(
            f"✅ Migration effectuée — "
            f"nouvelle VM active : {C.GREEN}{C.BOLD}{to_vm}{C.RESET} "
            f"| kubectl : {'OK' if kubectl_ok else C.RED+'ÉCHEC'+C.RESET}"
        )
    else:
        logger.info(
            f"🟢 Décision : {C.GREEN}MAINTIEN{C.RESET} sur {C.CYAN}{state.service_vm}{C.RESET} "
            f"| raison : {reason}"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Décision multi-provider (interrupteur ON — shared/config.py)
# ─────────────────────────────────────────────────────────────────────────────

# Valeur par défaut du sous-bloc "topsis" du reasoning : chemins C et D ne
# font jamais tourner TOPSIS (négociation sur le score de violation, pas de
# classement) — ce dict neutre évite un "None" au lecteur du dashboard, qui
# peut toujours lire classement/retenue/score sans test d'existence.
def _empty_topsis_reasoning() -> Dict[str, Any]:
    return {"classement": {}, "retenue": None, "score": None}


def _build_reasoning(
    current_provider: str,
    assessment: ProviderAssessment,
    negotiation_data: Optional[Dict[str, Any]],
    topsis_data: Dict[str, Any],
    vm_active: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Construit le bloc "reasoning" de l'audit multi-provider : la CHAÎNE DE
    RAISONNEMENT qui a mené à la décision (évaluation VM par VM, sous-
    ensemble conforme retenu, classement TOPSIS complet, éventuelle
    négociation) — pas seulement son résultat. Objectif : rendre la décision
    auditable a posteriori (dashboard, debug, soutenance) sans avoir à
    recouper les logs bruts du hub.

    PUREMENT INFORMATIF : ce bloc ne pèse jamais sur la décision elle-même,
    déjà prise par l'appelant au moment où cette fonction est invoquée.

    Tuples → listes, objets → dicts : le résultat part sur le réseau vers
    observability (POST /audit), il doit être json.dumps-able tel quel.

    `vm_active` = VM hébergeant le service AU MOMENT DE LA DÉCISION. Sur les
    chemins multi-provider, di_res["from_vm"] vaut None quand la décision est
    un maintien (convention : from_vm/to_vm ne sont renseignés que sur une
    migration). Le dashboard n'avait alors aucun moyen de savoir sur quelle VM
    porter l'étape « violation détectée » ni quoi afficher en étape « décision ».
    Ce champ le lui donne, sans toucher à la sémantique de from_vm/to_vm.
    """
    return {
        "provider_courant": current_provider,
        "vm_active":        vm_active,
        "evaluations": [
            {
                "vm_id":           ev.vm_id,
                "violation_score": ev.violation_score,
                "is_compliant":    ev.is_compliant,
                "evaluable":       ev.evaluable,
                "detail":          dict(ev.detail),
            }
            for ev in assessment.evaluations
        ],
        "compliant_vms": list(assessment.compliant_vms),
        "negotiation":   negotiation_data,
        "topsis":        topsis_data,
    }


async def _finalize_multi_provider_decision(
    client: httpx.AsyncClient,
    prof: StepProfiler,
    di_res: Dict[str, Any],
    provider_path: str,
    provider_used: str,
    current_provider: str,
    assessment: ProviderAssessment,
    negotiation_data: Optional[Dict[str, Any]] = None,
    topsis_data: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Fin commune aux chemins A/B/C/D de la décision multi-provider : logging,
    mise à jour de l'état, audit, puis migration effective si nécessaire.
    Reprend la même séquence que la fin de _decide_mono_provider
    (store/decision → kubectl → mise à jour de state) pour ne pas diverger
    du comportement de migration existant.

    `current_provider`/`assessment`/`negotiation_data`/`topsis_data`
    alimentent le bloc d'audit "reasoning" (voir _build_reasoning).
    `topsis_data` par défaut à None (chemins C/D, qui ne font jamais tourner
    TOPSIS) → repli sur _empty_topsis_reasoning(). Sa construction est
    protégée par un try/except : une erreur ici (structure inattendue, champ
    manquant) ne doit JAMAIS empêcher l'audit d'être posté ni interrompre le
    cycle — ce bloc est un bonus informatif, pas un prérequis du cycle.
    """
    state.last_decision = di_res
    decision = di_res.get("decision", "?")
    reason   = di_res.get("reason", "—")

    logger.info(
        f"🌐 Cycle #{state.cycle_count} — multi-provider "
        f"| chemin {C.CYAN}{provider_path}{C.RESET} "
        f"| provider {C.CYAN}{provider_used}{C.RESET} "
        f"| décision : {(C.YELLOW if decision == 'migrate' else C.GREEN)}{decision}{C.RESET} "
        f"| VM finale : {C.GREEN}{di_res.get('to_vm') or state.service_vm}{C.RESET} "
        f"| raison : {reason}"
    )

    try:
        # state.service_vm est encore la VM SOURCE ici : l'audit est posté
        # avant l'exécution de la migration (plus bas), donc ce champ décrit
        # bien la VM sur laquelle la décision a été prise.
        reasoning: Optional[Dict[str, Any]] = _build_reasoning(
            current_provider, assessment, negotiation_data,
            topsis_data if topsis_data is not None else _empty_topsis_reasoning(),
            vm_active=state.service_vm,
        )
    except Exception as exc:
        logger.warning(f"⚠️  Construction du bloc reasoning (audit) échouée : {exc}")
        reasoning = None

    audit_payload = {
        **di_res,
        "cycle":            state.cycle_count,
        "mode":              state._mode,
        "violated_metrics": di_res.get("violated_metrics", []),
        "slos_active":      state.current_slos,
        "mi_scores":        state.last_mi_scores,
        "current_metrics":  {
            lc["vm_id"]: {
                "latency":   lc.get("latency"),
                "cpu_usage": lc.get("cpu_usage"),
                "ram_usage": lc.get("ram_usage"),
            } for lc in state.last_collected
        },
        "provider_path": provider_path,
        "provider_used": provider_used,
        "reasoning":     reasoning,
    }
    asyncio.create_task(
        _post_audit(f"{_URLS['observability']}/audit", audit_payload)
    )

    if decision == "migrate":
        from_vm = di_res["from_vm"]
        to_vm   = di_res["to_vm"]
        logger.info(
            f"\n{'═'*60}\n"
            f"  🎯 DÉCISION : {C.BOLD}{C.YELLOW}MIGRATION{C.RESET} "
            f"(multi-provider, chemin {provider_path})\n"
            f"  {'Source':<16}: {C.RED}{from_vm}{C.RESET}\n"
            f"  {'Destination':<16}: {C.GREEN}{to_vm}{C.RESET}\n"
            f"  {'Raison':<16}: {C.CYAN}{reason}{C.RESET}\n"
            f"{'═'*60}"
        )

        with prof.step("store_decision"):
            await _post(client, f"{_URLS['database']}/store/decision", di_res)

        # Même séquence que le chemin mono-provider : durée = wall-clock de
        # l'appel HTTP synchrone à openstack_client.
        with prof.step("migration"):
            kubectl_ok = await _execute_kubectl_migration(client, from_vm, to_vm)
        if not kubectl_ok:
            logger.warning(
                f"⚠️  Migration kubectl échouée — "
                f"état interne mis à jour malgré tout "
                f"({C.YELLOW}{from_vm}{C.RESET} → {C.GREEN}{to_vm}{C.RESET})"
            )

        state.service_vm        = to_vm
        state.last_migration_ts = time.monotonic()
        logger.info(
            f"✅ Migration effectuée — "
            f"nouvelle VM active : {C.GREEN}{C.BOLD}{to_vm}{C.RESET} "
            f"| kubectl : {'OK' if kubectl_ok else C.RED+'ÉCHEC'+C.RESET}"
        )
    else:
        logger.info(
            f"🟢 Décision : {C.GREEN}MAINTIEN{C.RESET} sur {C.CYAN}{state.service_vm}{C.RESET} "
            f"| raison : {reason}"
        )


async def _decide_multi_provider(
    client: httpx.AsyncClient,
    ctx: _FlowContext,
    prof: StepProfiler,
    current_data: List[Dict[str, Any]],
) -> None:
    """
    Machine à états multi-provider (voir hub/provider_arbitration.py). Le
    provider COURANT — celui qui héberge state.service_vm — est évalué en
    premier :

      A. il a des VMs conformes     → TOPSIS restreint à CES VMs (intra-provider)
      B/C. aucune conforme chez lui  → passation via provider_relay ; la
           négociation renvoyée décide seule de la migration
      D. rien d'exploitable          → STAY, PLACEMENT_IMPOSSIBLE

    TOPSIS ne tourne JAMAIS sur un pool mêlant deux providers, ni sur des VMs
    non conformes : chaque provider départage SES PROPRES conformes, jamais
    ceux de l'autre (isolation — voir candidates_for_provider).
    """
    # Le cooldown est vérifié AVANT toute évaluation ou passation : une
    # migration inter-provider est encore plus coûteuse qu'une migration
    # intra-provider, elle doit respecter le même garde-fou anti-ping-pong.
    cooldown_active = state.check_cooldown()
    if cooldown_active:
        elapsed   = time.monotonic() - state.last_migration_ts
        remaining = config.MIGRATION_COOLDOWN_S - elapsed
        logger.warning(
            f"⏳ Cooldown actif — évaluation multi-provider ignorée "
            f"({C.YELLOW}{remaining:.0f}s{C.RESET} restante(s))"
        )
        state.last_decision = {
            "decision": "stay", "from_vm": None, "to_vm": None,
            "reason": "Cooldown active", "topsis_score": None, "breach_type": None,
        }
        return

    current_provider = config.PROVIDER_OF_VM.get(state.service_vm)
    if current_provider is None:
        # VM active hors registre (ex. configuration en cours de migration
        # topologique) : jamais planter, repli sur le comportement historique.
        logger.warning(
            f"⚠️  VM active '{state.service_vm}' absente de PROVIDER_OF_VM — "
            f"repli sur la décision mono-provider pour ce cycle"
        )
        await _decide_mono_provider(client, ctx, prof, current_data)
        return

    assessment = evaluate_provider(
        current_provider, state.current_slos, current_data, state.snapshot_predictions
    )

    collected_lookup   = {lc["vm_id"]: lc for lc in state.last_collected}
    reliability_scores = {
        vid: collected_lookup.get(vid, {}).get("reliability", 1.0)
        for vid in ctx.vm_ids
    }

    # ── Chemin A : TOPSIS intra-provider sur les seules VMs conformes ─────
    if assessment.compliant_vms:
        restricted = [c for c in current_data if c["vm_id"] in assessment.compliant_vms]
        di_payload = {
            "current_data":       restricted,
            "predictions_map":    state.last_predictions,
            "slos":               state.current_slos,
            "service_vm":         state.service_vm,
            "cooldown_active":    cooldown_active,
            "reliability_scores": reliability_scores,
            "cycle":              state.cycle_count,
            "mi_scores":          state.last_mi_scores,
        }
        with prof.step("decide_call"):
            di_res = await _post(client, f"{_URLS['decision_intelligence']}/decide", di_payload)

        if di_res:
            prof.merge(di_res.get("timings"))
        else:
            logger.warning(
                "⚠️  Chemin A — decision_intelligence n'a pas répondu, STAY"
            )
            di_res = {
                "decision": "stay", "from_vm": None, "to_vm": None,
                "reason": "decision_intelligence indisponible (chemin A)",
                "topsis_score": None, "breach_type": None,
            }

        # Classement TOPSIS complet : vm_scores n'est présent que si
        # decision.py a réellement fait tourner TOPSIS (donc absent sur le
        # repli "decision_intelligence indisponible" ci-dessus) — {} sinon.
        topsis_data = {
            "classement": di_res.get("vm_scores") or {},
            "retenue":    di_res.get("to_vm"),
            "score":      di_res.get("topsis_score"),
        }
        await _finalize_multi_provider_decision(
            client, prof, di_res, "A", current_provider, current_provider, assessment,
            None, topsis_data,
        )
        return

    # ── Chemins B / C / D : passation à l'autre provider ──────────────────
    other_provider = next(
        (p for p in config.PROVIDER_REGISTRY if p != current_provider), None
    )
    if other_provider is None:
        logger.warning("⚠️  Aucun autre provider déclaré — PLACEMENT_IMPOSSIBLE")
        await _finalize_multi_provider_decision(
            client, prof,
            {
                "decision": "stay", "from_vm": None, "to_vm": None,
                "reason": "PLACEMENT_IMPOSSIBLE — aucun autre provider disponible",
                "topsis_score": None, "breach_type": None,
            },
            "D", current_provider, current_provider, assessment,
        )
        return

    try:
        intent = SLOIntent(
            intent_id=f"cycle-{state.cycle_count}",
            slos=[SLO(**s) for s in state.current_slos],
            mode=state._mode,
            created_at=datetime.now(timezone.utc).isoformat(),
            attempted_providers=(current_provider,),
        )
    except (ValueError, TypeError) as exc:
        # Ex. aucun SLO actif : rien à négocier. Ne jamais planter le cycle
        # pour ça — traité comme une impossibilité de placement.
        logger.warning(
            f"⚠️  Impossible de construire l'intention pour la passation : {exc}"
        )
        await _finalize_multi_provider_decision(
            client, prof,
            {
                "decision": "stay", "from_vm": None, "to_vm": None,
                "reason": f"PLACEMENT_IMPOSSIBLE — intention invalide ({exc})",
                "topsis_score": None, "breach_type": None,
            },
            "D", current_provider, current_provider, assessment,
        )
        return

    offer = assessment.to_offer()
    handoff_payload = {
        "slo_intent":         intent.to_dict(),
        "offer":              offer.to_dict() if offer else None,
        "target_provider":    other_provider,
        "from_provider":      current_provider,
        "incumbent_provider": current_provider,
        # VM réellement active (en violation) chez l'émetteur. Le receveur en
        # a besoin pour que TOPSIS s'exécute réellement — voir le
        # commentaire détaillé dans /intent/relay.
        "incumbent_vm":       state.service_vm,
    }

    with prof.step("handoff_call"):
        try:
            handoff_res = await _post(
                client, f"{config.PROVIDER_RELAY_SERVICE_URL}/handoff", handoff_payload
            )
        except Exception as exc:
            # _post capture déjà les erreurs réseau normales et renvoie None ;
            # ce filet couvre le cas résiduel d'une exception inattendue qui
            # échapperait malgré tout — une passation ratée ne doit JAMAIS
            # interrompre le cycle ni empêcher l'audit d'être posté.
            logger.warning(f"⚠️  Appel au relais /handoff a levé une exception : {exc}")
            handoff_res = None

    if not handoff_res:
        # Relais injoignable, ou refus (409 anti-boucle, 502…) : une passation
        # ratée ne doit jamais interrompre le cycle — STAY et on réessaiera
        # au prochain cycle.
        logger.warning(
            f"⚠️  Passation vers {other_provider} impossible "
            f"(relais indisponible ou refus) — STAY"
        )
        await _finalize_multi_provider_decision(
            client, prof,
            {
                "decision": "stay", "from_vm": None, "to_vm": None,
                "reason": f"PLACEMENT_IMPOSSIBLE — passation vers {other_provider} refusée",
                "topsis_score": None, "breach_type": None,
            },
            "D", current_provider, current_provider, assessment,
        )
        return

    negotiation   = handoff_res.get("negotiation") or {}
    local_topsis  = handoff_res.get("local_topsis") or {}
    decision_kind = negotiation.get("decision")

    # Bloc "negotiation" du reasoning d'audit : reprend les données déjà lues
    # ci-dessus (negotiation, local_topsis) plus l'offre que NOUS avons émise
    # (offer) et le provider contacté (other_provider) — rien de neuf n'est
    # recalculé, seulement rassemblé pour le dashboard.
    negotiation_data: Dict[str, Any] = {
        "offre_locale":   offer.to_dict() if offer else None,
        "offre_recue":    handoff_res.get("local_offer"),
        "deadband":       negotiation.get("deadband_applied"),
        "decision":       negotiation.get("decision"),
        "provider_cible": other_provider,
    }

    if decision_kind == "prend_local_conforme":
        # ── Chemin B : l'autre provider a des conformes, il choisit lui-même
        # (isolation) sa VM par TOPSIS — nous ne faisons qu'appliquer son choix.
        to_vm = local_topsis.get("to_vm")
        di_res = {
            "decision":     "migrate" if to_vm and to_vm != state.service_vm else "stay",
            "from_vm":      state.service_vm if to_vm else None,
            "to_vm":        to_vm,
            "reason":       local_topsis.get("reason") or negotiation.get("reason"),
            "topsis_score": local_topsis.get("topsis_score"),
            "breach_type":  "inter_provider_handoff",
        }
        # Classement TOPSIS du RECEVEUR (isolation : c'est lui qui a fait
        # tourner TOPSIS sur ses propres conformes) — voir /intent/relay,
        # qui transmet vm_scores dans local_topsis quand decision.py y a
        # réellement répondu.
        topsis_data = {
            "classement": local_topsis.get("vm_scores") or {},
            "retenue":    to_vm,
            "score":      local_topsis.get("topsis_score"),
        }
        await _finalize_multi_provider_decision(
            client, prof, di_res, "B", other_provider, current_provider, assessment,
            negotiation_data, topsis_data,
        )
        return

    if decision_kind == "prend_local_meilleure":
        # ── Chemin C : personne n'est conforme, l'offre de l'autre gagne.
        to_vm = negotiation.get("winning_vm")
        di_res = {
            "decision":     "migrate" if to_vm and to_vm != state.service_vm else "stay",
            "from_vm":      state.service_vm if to_vm else None,
            "to_vm":        to_vm,
            "reason":       negotiation.get("reason"),
            "topsis_score": negotiation.get("offered_score"),
            "breach_type":  "inter_provider_negotiation",
        }
        await _finalize_multi_provider_decision(
            client, prof, di_res, "C", other_provider, current_provider, assessment,
            negotiation_data,
        )
        return

    if decision_kind == "cede_a_l_offre":
        # ── Chemin C : personne n'est conforme, NOTRE offre gagne — c'est
        # NOTRE best_effort_vm (calculé localement), pas une valeur reçue.
        best_vm = assessment.best_effort_vm
        if best_vm and best_vm != state.service_vm:
            di_res = {
                "decision": "migrate", "from_vm": state.service_vm, "to_vm": best_vm,
                "reason": negotiation.get("reason") or "Négociation gagnée localement",
                "topsis_score": assessment.best_effort_score,
                "breach_type": "inter_provider_negotiation",
            }
        else:
            di_res = {
                "decision": "stay", "from_vm": None, "to_vm": None,
                "reason": negotiation.get("reason")
                          or "Négociation gagnée localement — VM déjà active",
                "topsis_score": assessment.best_effort_score, "breach_type": None,
            }
        await _finalize_multi_provider_decision(
            client, prof, di_res, "C", current_provider, current_provider, assessment,
            negotiation_data,
        )
        return

    # ── Chemin D : aucune_option, ou décision inattendue ───────────────────
    di_res = {
        "decision": "stay", "from_vm": None, "to_vm": None,
        "reason": (
            f"PLACEMENT_IMPOSSIBLE — {negotiation.get('reason') or 'aucune option exploitable'}"
        ),
        "topsis_score": None, "breach_type": None,
    }
    await _finalize_multi_provider_decision(
        client, prof, di_res, "D", current_provider, current_provider, assessment,
        negotiation_data,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Persistance des mesures de performance
# ─────────────────────────────────────────────────────────────────────────────

def _persist_timing(prof: StepProfiler, mode: str, vm: Optional[str]) -> None:
    """
    Écrit une ligne de mesures dans le fichier Excel selon le mode :
      • AUTONOMOUS → feuille « par cycle » : une ligne par cycle.
                     Total = durée du cycle (collecte → décision/migration).
      • ENHANCED   → feuille « par intention » : une ligne par intention, écrite
                     sur le cycle qui EXÉCUTE la migration déclenchée par cette
                     intention. Total = réception intention → migration
                     (réception LLM + cycle de migration). Tant qu'aucune
                     migration n'a lieu, l'intention reste en attente (pas de ligne).
    """
    d        = prof.durations()
    decision = state.last_decision.get("decision")
    ts       = prof.steps.get("total", {}).get("start") or datetime.now(timezone.utc).isoformat()

    # Totaux par microservice (agrégation sans double-comptage des sous-étapes)
    hub_total = round((d.get("check_violations") or 0.0) + (d.get("migration") or 0.0), 3)
    db_total  = round((d.get("persist_slos") or 0.0) + (d.get("persist_metrics") or 0.0)
                      + (d.get("store_decision") or 0.0), 3)
    hl_total  = round((d.get("slos_load_hist") or 0.0) + (d.get("load_histories") or 0.0), 3)

    # Overhead réseau (valeurs dérivées — non mesurées directement)
    _c_tot = d.get("collect")
    _c_max = d.get("collect_max")
    collect_overhead = (round(_c_tot - _c_max, 3)
                        if _c_tot is not None and _c_max is not None else None)

    _dc = d.get("decide_call")
    di_overhead = (round(_dc - ((d.get("violation_detection") or 0.0)
                                + (d.get("candidate_filter")    or 0.0)
                                + (d.get("topsis_total")        or 0.0)), 3)
                   if _dc is not None else None)

    # SOMME = somme des totaux par microservice
    _somme = round(
        hub_total
        + (d.get("collect")    or 0.0)   # Collector total
        + db_total
        + hl_total
        + (d.get("prediction") or 0.0)   # ML Predictor
        + (d.get("slos_mm")    or 0.0)   # Metrics Manager total
        + (d.get("decide_call") or 0.0)  # Decision Intelligence total
        , 3
    )

    base: Dict[str, Any] = {
        "vm":                  vm,
        "decision":            decision,
        "to_vm":               state.last_decision.get("to_vm"),
        # Hub
        "check_violations":    d.get("check_violations"),
        "migration":           d.get("migration"),
        "hub_total":           hub_total,
        # Collector
        "collect_edge1":       d.get("collect_edge1"),
        "collect_edge2":       d.get("collect_edge2"),
        "collect_cloud1":      d.get("collect_cloud1"),
        "collect_cloud2":      d.get("collect_cloud2"),
        "collect_max":         d.get("collect_max"),
        "collect":             d.get("collect"),
        "collect_overhead":    collect_overhead,
        # Database
        "persist_slos":        d.get("persist_slos"),
        "persist_metrics":     d.get("persist_metrics"),
        "store_decision":      d.get("store_decision"),
        "db_total":            db_total,
        # History Loader
        "slos_load_hist":      d.get("slos_load_hist"),
        "load_histories":      d.get("load_histories"),
        "hl_total":            hl_total,
        # ML Predictor
        "prediction":          d.get("prediction"),
        # Metrics Manager (sous-étapes retournées par le service)
        "mi_compute":          d.get("mi_compute"),
        "mi_slos":             d.get("mi_slos"),
        "slos_mm":             d.get("slos_mm"),
        # Decision Intelligence
        "violation_detection": d.get("violation_detection"),
        "candidate_filter":    d.get("candidate_filter"),
        "topsis_total":        d.get("topsis_total"),
        "topsis_matrix":       d.get("topsis_matrix"),
        "topsis_norm":         d.get("topsis_norm"),
        "topsis_weight":       d.get("topsis_weight"),
        "topsis_dist":         d.get("topsis_dist"),
        "decide_call":         d.get("decide_call"),
        "di_overhead":         di_overhead,
        # Résumé
        "somme":               _somme,
    }

    if mode == "enhanced":
        # Une ligne par intention, produite sur le cycle qui exécute la migration.
        if state.pending_intent_timing and decision == "migrate":
            pit = state.pending_intent_timing
            state.pending_intent_timing = None
            reception_ms = pit.get("reception_ms")
            cycle_total  = d.get("total")
            row = dict(base)
            row.update({
                "intent_id":        pit.get("intent_id"),
                "timestamp":        ts,
                "intention":        (pit.get("intention") or "")[:80],
                "intent_reception": reception_ms,
                "cycle_total":      cycle_total,
                "intention_total":  round((reception_ms or 0.0) + (cycle_total or 0.0), 3),
            })
            asyncio.create_task(timing_writer_enhanced.write(row))
            logger.info(
                f"📊 Mesures enregistrées (intention {C.CYAN}{pit.get('intent_id')}{C.RESET}) "
                f"| réception → migration : {C.GREEN}{row['intention_total']} ms{C.RESET}"
            )
        # cycle enhanced sans migration → on conserve l'intention en attente
    elif mode == "autonomous":
        row = dict(base)
        row.update({
            "cycle":     state.cycle_count,
            "timestamp": ts,
            "total":     d.get("total"),
        })
        asyncio.create_task(timing_writer_autonomous.write(row))
        logger.info(
            f"📊 Mesures enregistrées (cycle {C.CYAN}{state.cycle_count}{C.RESET}) "
            f"| total : {C.GREEN}{d.get('total')} ms{C.RESET}"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Coordinateur du cycle — orchestre les 8 étapes séquentiellement
# ─────────────────────────────────────────────────────────────────────────────

async def _run_flow(measurements: List[RTTMeasurement], mode: str) -> None:
    if state._lock.locked():
        return

    async with state._lock:
        logger.info(
            f"{'═'*60}\n"
            f"   🔄  Cycle #{C.BOLD}{state.cycle_count}{C.RESET}  |  "
            f"Mode : {C.CYAN}{mode}{C.RESET}  |  "
            f"VM active : {C.GREEN}{state.service_vm}{C.RESET}\n"
            f"{'═'*60}"
        )

        prof = StepProfiler(logger=logger, context=f"Cycle #{state.cycle_count}")
        vm_for_row = state.service_vm  # VM qui sert ce cycle (avant migration éventuelle)

        with prof.step("total"):
            async with httpx.AsyncClient() as client:
                ctx = _FlowContext(
                    vm_ids=list(config.VM_REGISTRY.keys()),
                    now_iso=datetime.now(timezone.utc).isoformat(),
                )
                # SLOs/MI (step1+2) et collecte des métriques VM (step3+4) ne
                # dépendent l'un de l'autre en rien — seule la suite du cycle
                # (load_histories → prediction → decide) a besoin des deux.
                # Parallélisés pour réduire le temps de cycle total (voir étude
                # de calibrage θ=60ms/v=1.2cm/s : chaque seconde gagnée ici
                # restaure une prédiction ML utile sur l'horizon de 7).
                async def _slos_branch() -> None:
                    with prof.step("slos_mi"):
                        await _step1_slos(client, ctx, mode, prof)
                    with prof.step("persist_slos"):
                        await _step2_persist_slos(client, ctx)

                async def _collect_branch() -> None:
                    with prof.step("collect"):
                        await _step3_collect(client, ctx, prof)
                    with prof.step("persist_metrics"):
                        await _step4_persist_metrics(client, ctx, measurements)

                with prof.step("slos_and_collect_parallel"):
                    await asyncio.gather(_slos_branch(), _collect_branch())
                with prof.step("load_histories"):
                    await _step6_load_histories(client, ctx)
                with prof.step("prediction"):
                    await _step7_predict(client, ctx)
                with prof.step("check_violations"):
                    _step5_check_violations(ctx)   # après prédictions — log cohérent
                with prof.step("decision_total"):
                    await _step8_decide(client, ctx, prof)

        _persist_timing(prof, mode, vm_for_row)

        logger.info(
            f"✅ Cycle #{C.BOLD}{state.cycle_count}{C.RESET} terminé\n"
            f"{'─'*60}"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(
        f"\n{'═'*60}\n"
        f"  🚀  {C.BOLD}QoS Orchestrator Core — Démarrage{C.RESET}\n"
        f"{'═'*60}"
    )

    # Affichage des objectifs primaires
    primary_objectives = {
        m: f"{meta['default_threshold']} {meta['unit']}"
        for m, meta in config.METRICS_REGISTRY.items()
        if meta.get("is_primary_objective", False)
    }
    logger.info(
        f"🎯 Objectifs métier primaires : {C.GREEN}{primary_objectives}{C.RESET}"
    )

    success = True
    async with httpx.AsyncClient() as client:
        logger.info("🔍 Vérification de l'état des services dépendants...")
        for name, url in _URLS.items():
            try:
                r = await client.get(f"{url}/health", timeout=2.0)
                if r.status_code == 200:
                    logger.info(f"  ✅ {C.GREEN}{name:<25}{C.RESET} opérationnel")
                else:
                    logger.error(f"  ❌ {C.RED}{name:<25}{C.RESET} HTTP {r.status_code}")
                    success = False
            except Exception as e:
                logger.error(f"  ❌ {C.RED}{name:<25}{C.RESET} injoignable — {e}")
                success = False

        try:
            r = await client.get(f"{OPENSTACK_CLIENT_URL}/health", timeout=3.0)
            if r.status_code == 200:
                logger.info(f"  ✅ {C.GREEN}{'openstack_client':<25}{C.RESET} opérationnel")
            else:
                logger.warning(
                    f"  ⚠️  {C.YELLOW}openstack_client{C.RESET} injoignable "
                    "— les migrations ne seront pas exécutées"
                )
        except Exception as e:
            logger.warning(
                f"  ⚠️  {C.YELLOW}openstack_client{C.RESET} injoignable : {e} "
                "— les migrations ne seront pas exécutées"
            )

        await _sync_active_vm(client)

    if not success:
        logger.warning(
            "⚠️  Certains services sont indisponibles au démarrage — "
            "l'orchestrateur démarre en mode dégradé"
        )

    logger.info(
        f"\n{'═'*60}\n"
        f"  {C.GREEN}✅  Orchestrateur prêt{C.RESET}\n"
        f"  {'Mode':<16}: {C.CYAN}{state._mode}{C.RESET}\n"
        f"  {'VM active':<16}: {C.GREEN}{state.service_vm}{C.RESET}\n"
        f"  {'Bootstrap min':<16}: {C.CYAN}{state.BOOTSTRAP_MIN} cycles{C.RESET}\n"
        f"  {'Cooldown':<16}: {C.CYAN}{config.MIGRATION_COOLDOWN_S}s{C.RESET}\n"
        f"{'═'*60}\n"
    )
    yield
    logger.info(
        f"\n{'─'*60}\n"
        f"  🛑  {C.YELLOW}Arrêt de l'orchestrateur en cours...{C.RESET}\n"
        f"{'─'*60}"
    )


app = FastAPI(title="QoS Orchestrator Core", version="2.1.0", lifespan=lifespan)


@app.post("/rtt", status_code=status.HTTP_200_OK)
async def receive_rtt(payload: LatencyPayload):
    state.cycle_count += 1
    asyncio.create_task(_run_flow(payload.measurements, state._mode))
    return {"status": "accepted", "cycle": state.cycle_count}


@app.post("/intent", status_code=status.HTTP_200_OK)
async def receive_intent(payload: Dict[str, Any] = Body(...)):
    state.current_slos = payload.get("slos", [])
    # Capture les poids originaux du LLM pour chaque SLO primaire — base
    # fixe utilisée à chaque cycle suivant pour éviter la dilution
    # cumulative (voir restauration dans _run_flow, étape SLOs).
    state.original_intent_weights = {
        s["metric"]: s.get("weight", 1.0)
        for s in state.current_slos
        if s.get("is_primary", False)
    }
    state._mode = "enhanced"
    intent_id = payload.get("intent_id", "—")

    # Mémorise le timing de réception (mesuré par intent_manager) pour qu'il soit
    # consommé par le prochain cycle et produise une ligne « par intention ».
    timing = payload.get("timing", {}) or {}
    state.pending_intent_timing = {
        "intent_id":    intent_id,
        "intention":    payload.get("intention", ""),
        "reception_ms": timing.get("reception_ms"),
        "llm_ms":       timing.get("llm_ms"),
    }

    logger.info(
        f"\n{'═'*60}\n"
        f"  📥 Intent reçu — mode Enhanced activé\n"
        f"  {'ID':<16}: {C.CYAN}{intent_id}{C.RESET}\n"
        f"  {'SLOs injectés':<16}: {C.CYAN}{len(state.current_slos)}{C.RESET}\n"
        f"  {'Poids originaux':<16}: {C.CYAN}{state.original_intent_weights}{C.RESET}\n"
        f"  {'Réception':<16}: {C.CYAN}{timing.get('reception_ms')} ms{C.RESET}\n"
        f"{'═'*60}"
    )
    return {"status": "accepted", "mode": state._mode, "slos": len(state.current_slos)}


@app.post("/intent/relay", status_code=status.HTTP_200_OK)
async def receive_intent_relay(payload: Dict[str, Any] = Body(...)):
    """
    Point de réception d'une passation inter-provider (fédération).

    ⚠️ ENDPOINT PUREMENT OBSERVATOIRE. Il évalue la situation du provider
    demandé (`acting_as_provider`) au regard de l'intention reçue et renvoie
    le verdict de négociation — mais il NE MODIFIE AUCUN CHAMP de `state` et
    NE DÉCLENCHE AUCUNE MIGRATION. Il lit, calcule, répond.

    Le branchement de ce verdict sur une migration réelle est l'objet d'une
    étape ultérieure. Tant qu'il n'existe pas, cet endpoint est dormant du
    point de vue du cycle d'orchestration : ni `_run_flow` ni `_step8_decide`
    ne l'appellent — il n'est atteint que par un appel externe explicite
    (le microservice `provider_relay`, ou un `curl` de test).
    """
    acting_as_provider: Optional[str] = payload.get("acting_as_provider")
    if acting_as_provider not in config.PROVIDER_REGISTRY:
        logger.warning(
            f"⚠️  /intent/relay — acting_as_provider inconnu : {acting_as_provider}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"acting_as_provider '{acting_as_provider}' inconnu de "
                f"PROVIDER_REGISTRY {sorted(config.PROVIDER_REGISTRY.keys())}"
            ),
        )

    if not state.last_collected:
        logger.warning("⚠️  /intent/relay — hub pas encore chaud (last_collected vide)")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Aucune mesure collectée pour l'instant (last_collected vide)",
        )

    intent = SLOIntent.from_dict(payload["slo_intent"])
    offer_data     = payload.get("offer")
    received_offer = ProviderOffer.from_dict(offer_data) if offer_data else None
    incumbent_provider: Optional[str] = payload.get("incumbent_provider")
    # VM réellement active chez l'émetteur (en violation de ses SLOs) — voir
    # le commentaire détaillé plus bas, à l'endroit où elle est utilisée.
    incumbent_vm: Optional[str] = payload.get("incumbent_vm")

    # Évaluation PURE (hub/provider_arbitration.py) : lit l'état courant du
    # hub sans jamais le modifier. Mêmes candidats et mêmes prédictions que
    # ceux qu'utiliserait le cycle d'orchestration normal pour ce cycle.
    candidates  = _build_candidates(state.last_collected)
    predictions = state.snapshot_predictions

    assessment = evaluate_provider(
        acting_as_provider, list(intent.slos), candidates, predictions
    )
    result = negotiate(
        assessment, received_offer, incumbent_provider_id=incumbent_provider
    )

    # Trace de relais : marque acting_as_provider comme tenté, sans doubler
    # l'entrée si l'appelant l'avait déjà fait avant l'envoi.
    updated_intent = (
        intent if intent.has_attempted(acting_as_provider)
        else intent.with_attempt(acting_as_provider)
    )

    local_offer = assessment.to_offer()

    # ── TOPSIS local, si des VMs conformes existent ────────────────────────
    # Le receveur choisit LUI-MÊME sa VM parmi ses propres conformes —
    # isolation : acting_as_provider ne décide jamais pour l'autre provider,
    # et réciproquement personne ne décide à sa place ici.
    local_topsis: Optional[Dict[str, Any]] = None
    if assessment.compliant_vms:
        restricted = [c for c in candidates if c["vm_id"] in assessment.compliant_vms]

        # ── VM active à déclarer à decision_intelligence ────────────────
        # ⚠️ CORRECTIF : declarer une VM CONFORME comme "service_vm" court-
        # circuite TOPSIS. decision.py commence par chercher une violation
        # SUR la VM active ; si elle est conforme (ce qui est toujours le
        # cas ici, restricted ne contenant QUE des conformes), il ne trouve
        # rien, franchit sa barrière "if not primary_violations:" et renvoie
        # {"decision": "stay", "to_vm": None} sans jamais atteindre TOPSIS.
        # Résultat observé en prod : le receveur retombe systématiquement
        # sur le repli "première VM conforme" au lieu du meilleur choix
        # TOPSIS, même avec plusieurs VMs conformes disponibles.
        #
        # Solution : déclarer la VM RÉELLEMENT active — celle de l'émetteur,
        # qui VIOLE ses SLOs (c'est précisément pour ça qu'il a passé la
        # main). decision.py y détecte une vraie violation, filtre les
        # candidats sur les conformes (_filter_candidates), et TOPSIS
        # s'exécute sur restricted. L'hystérésis _MIGRATION_MARGIN ne bloque
        # rien : incumbent_vm est absente de `restricted` (elle n'est pas
        # conforme chez CE provider), donc active_score = vm_scores.get(...,
        # 0.0) = 0 — la migration est libre.
        #
        # Il faut aussi INJECTER cette VM dans current_data : sans ses
        # métriques, decision.py ne trouve pas de ligne correspondante
        # (vm_data = {}, valeurs à 0.0) et ne détecte toujours aucune
        # violation. Elle vient de `candidates` (_build_candidates), pas de
        # `restricted` — c'est la VM de L'AUTRE provider, jamais dans le
        # pool restreint à CE provider.
        #
        # Divulgation acceptée : le receveur apprend l'identifiant de la VM
        # active de l'émetteur. Divulgation marginale — il connaissait déjà
        # `incumbent_provider`, donc déjà quel provider est en difficulté.
        #
        # Repli sûr : si incumbent_vm est absent, ou introuvable dans les
        # mesures collectées (VM inconnue, hub pas synchronisé...), on
        # revient au comportement précédent — jamais d'exception.
        incumbent_candidate = next(
            (c for c in candidates if c["vm_id"] == incumbent_vm), None
        ) if incumbent_vm else None

        if incumbent_candidate is not None:
            decide_current_data = restricted + [incumbent_candidate]
            decide_service_vm   = incumbent_vm
        else:
            decide_current_data = restricted
            decide_service_vm   = assessment.compliant_vms[0]

        di_payload = {
            "current_data":       decide_current_data,
            "predictions_map":    predictions,
            "slos":               [s.dict() for s in intent.slos],
            # Le receveur n'héberge pas encore le service : aucun cooldown à
            # respecter pour une prise en charge initiale — sinon TOPSIS ne
            # tournerait jamais sur ce chemin.
            "service_vm":         decide_service_vm,
            "cooldown_active":    False,
            "reliability_scores": {
                lc["vm_id"]: lc.get("reliability", 1.0) for lc in state.last_collected
            },
            "cycle":              state.cycle_count,
            "mi_scores":          state.last_mi_scores,
        }
        async with httpx.AsyncClient() as di_client:
            di_res = await _post(
                di_client, f"{_URLS['decision_intelligence']}/decide", di_payload
            )

        if di_res and di_res.get("to_vm"):
            local_topsis = {
                "to_vm":        di_res["to_vm"],
                "topsis_score": di_res.get("topsis_score"),
                "reason":       di_res.get("reason"),
                # Classement complet (toutes les VMs conformes du receveur,
                # pas seulement la gagnante) — transmis à l'émetteur pour
                # que son bloc d'audit "reasoning.topsis" (chemin B) puisse
                # justifier "pourquoi cette VM plutôt qu'une autre" côté
                # RECEVEUR. {} si decision.py ne l'a pas renvoyé (ancien
                # format, ou chemin qui n'a jamais atteint TOPSIS).
                "vm_scores":    di_res.get("vm_scores") or {},
            }
        else:
            # decision_intelligence indisponible ou sans candidat : repli
            # déterministe sur la première VM conforme, signalé dans reason.
            local_topsis = {
                "to_vm":        assessment.compliant_vms[0],
                "topsis_score": None,
                "reason": (
                    "decision_intelligence indisponible ou sans candidat — "
                    "repli sur la première VM conforme"
                ),
                "vm_scores":    {},
            }

    logger.info(
        f"📡 /intent/relay — acting_as={C.CYAN}{acting_as_provider}{C.RESET} "
        f"| décision : {C.YELLOW}{result.decision.value}{C.RESET} "
        f"| {result.reason}"
    )

    return {
        "acting_as_provider":  acting_as_provider,
        "negotiation":         result.to_dict(),
        "local_offer":         local_offer.to_dict() if local_offer else None,
        "local_topsis":        local_topsis,
        "attempted_providers": list(updated_intent.attempted_providers),
        "timestamp":           datetime.now(timezone.utc).isoformat(),
    }


@app.get("/data")
async def get_data():
    return state.get_data_payload()


@app.get("/status")
async def get_status():
    return {
        "mode":             state._mode,
        "service_vm":       state.service_vm,
        "cycle":            state.cycle_count,
        "bootstrap_active": state.cycle_count < state.BOOTSTRAP_MIN,
        "cooldown_active":  state.check_cooldown(),
        "slos_count":       len(state.current_slos),
        "active_slos":      state.current_slos,
        "last_decision":    state.last_decision.get("decision"),
        "timestamp":        datetime.now(timezone.utc).isoformat(),
    }


@app.post("/reset", status_code=status.HTTP_200_OK)
async def reset():
    async with state._lock:
        state._mode                  = "autonomous"
        state.current_slos           = _build_bootstrap_slos()
        state.original_intent_weights = {}
        state.pending_intent_timing  = None
        state.last_decision          = {}
        state.cycle_count            = 0
        state.last_migration_ts      = None
        state.bootstrap_cycles       = 0
    logger.info(
        f"\n{'═'*60}\n"
        f"  🔄  Reset — retour en mode {C.CYAN}autonomous{C.RESET}\n"
        f"  SLOs bootstrap restaurés : {C.GREEN}{[s['metric'] for s in state.current_slos]}{C.RESET}\n"
        f"{'═'*60}"
    )
    return {"status": "reset", "mode": state._mode, "slos_count": len(state.current_slos)}


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "orchestrator_core"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.HUB_PORT)