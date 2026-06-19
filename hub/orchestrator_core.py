"""
hub/orchestrator_core.py — QoS Orchestrator Hub (Hub-and-Spoke).
"""

import asyncio
import json
import logging
import time
import uvicorn
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from fastapi import FastAPI, status, Body

from shared import config
from shared.models import IntentToHubPayload, LatencyPayload, RTTMeasurement


# ─────────────────────────────────────────────
#  ANSI color codes
# ─────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"


class PrettyFormatter(logging.Formatter):
    LEVEL_STYLES = {
        "INFO":    f"{C.BLUE}[INFO]{C.RESET}",
        "SUCCESS": f"{C.GREEN}[SUCCESS]{C.RESET}",
        "WARNING": f"{C.YELLOW}[WARNING]{C.RESET}",
        "ERROR":   f"{C.RED}[ERROR]{C.RESET}",
        "DEBUG":   f"{C.CYAN}[DEBUG]{C.RESET}",
        "CRITICAL":f"{C.RED}{C.BOLD}[CRITICAL]{C.RESET}",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts    = datetime.now(timezone.utc).strftime("%H:%M:%S")
        level = self.LEVEL_STYLES.get(record.levelname, f"[{record.levelname}]")
        msg   = record.getMessage()
        return f"{C.CYAN}{ts}{C.RESET}  {level}  {msg}"


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
                "rtt_ms":      coll.get("latency"),
                "cpu_usage":   coll.get("cpu_usage"),
                "ram_usage":   coll.get("ram_usage"),
                "reliability": coll.get("reliability"),
                "is_active":   (vm_id == self.service_vm),
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


async def _post(client: httpx.AsyncClient, url: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.post(url, json=payload, timeout=10.0)
        if resp.status_code in (200, 201, 202):
            return resp.json()
        return None
    except Exception as e:
        logger.error(f"❌ POST {C.CYAN}{url}{C.RESET} failed : {e}")
        return None

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
            thr[slo["metric"]] = float(slo["threshold"])
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

        async with httpx.AsyncClient() as client:
            vm_ids  = list(config.VM_REGISTRY.keys())
            now_iso = datetime.now(timezone.utc).isoformat()

            # ── ÉTAPE 1 : SLOs ──────────────────────────────────────────
            if state.cycle_count < state.BOOTSTRAP_MIN:
                # Bootstrap : SLOs primaires uniquement (objectif métier)
                # Les SLOs secondaires adaptatifs seront ajoutés par le
                # metrics_manager une fois l'historique suffisant.
                state.current_slos = _build_bootstrap_slos()
                active_metrics = [s["metric"] for s in state.current_slos]
                logger.info(
                    f"🟡 Phase bootstrap ({state.cycle_count}/{state.BOOTSTRAP_MIN}) "
                    f"— SLOs primaires uniquement : {C.GREEN}{active_metrics}{C.RESET}"
                )
            else:
                h_res = await _post(client, f"{_URLS['history_loader']}/load", {
                    "vm_id": state.service_vm,
                    "metrics": list(config.METRICS_REGISTRY.keys()),
                    "size": config.HISTORY_WINDOW
                })
                svc_hists = h_res.get("histories", {}) if h_res else {}

                if mode == "enhanced":
                    # Restaure les poids originaux de l'intention sur les
                    # SLOs primaires avant recalcul — évite l'effet cumulatif
                    # de dilution cycle après cycle (sinon
                    # weight_LLM × MI × MI × MI... à chaque appel, au lieu
                    # de toujours weight_LLM_original × MI_courant).
                    for slo in state.current_slos:
                        if slo.get("is_primary") and slo["metric"] in state.original_intent_weights:
                            slo["weight"] = state.original_intent_weights[slo["metric"]]

                    mm_payload = {"slos": state.current_slos, "history": _zip_histories(svc_hists, _threshold_map())}
                    mm_url = f"{_URLS['metrics_manager']}/validate"
                else:
                    mm_payload = {"history": _zip_histories(svc_hists, _threshold_map()), "all_vals": _all_vals(svc_hists)}
                    mm_url = f"{_URLS['metrics_manager']}/compute"

                mm_res = await _post(client, mm_url, mm_payload)
                if mm_res:
                    state.current_slos   = mm_res.get("slos", state.current_slos)
                    active_metrics       = mm_res.get("active_metrics", list(config.METRICS_REGISTRY.keys()))
                    state.last_mi_scores = mm_res.get("mi_scores", {})

                    # Distinction primaires / secondaires pour le log
                    primaries   = [s["metric"] for s in state.current_slos if s.get("is_primary")]
                    secondaries = [s["metric"] for s in state.current_slos if not s.get("is_primary")]
                    logger.info(
                        f"📋 SLOs mis à jour — {C.CYAN}{len(state.current_slos)}{C.RESET} SLO(s) actif(s) "
                        f"| primaires : {C.GREEN}{primaries}{C.RESET} "
                        f"| secondaires : {C.YELLOW}{secondaries}{C.RESET}"
                    )
                else:
                    # Fallback : conserver les SLOs courants si metrics_manager indisponible
                    active_metrics = [s["metric"] for s in state.current_slos]
                    logger.warning("⚠️  MetricsManager indisponible — SLOs précédents conservés")

            # ── ÉTAPE 2 : Persistance SLOs ──────────────────────────────
            await _post(client, f"{_URLS['database']}/store/slos",
                        {"slos": state.current_slos, "timestamp": now_iso})

            # ── ÉTAPE 3 : Collecte métriques ────────────────────────────
            collector_metrics = [m for m in config.METRICS_REGISTRY.keys() if m != "latency"]
            coll_res = await _post(client, f"{_URLS['collector']}/collect",
                                   {"active_metrics": collector_metrics, "cycle": state.cycle_count})
            results  = coll_res.get("results", []) if coll_res else []
            logger.info(
                f"📡 Métriques collectées — {C.CYAN}{len(results)}{C.RESET} VM(s) "
                f"| métriques : {C.CYAN}{collector_metrics}{C.RESET}"
            )

            # ── ÉTAPE 4 : Persistance métriques ─────────────────────────
            rtt_lookup = {m.vm_id: m for m in measurements}
            new_collected: List[Dict[str, Any]] = []

            persist_tasks = []
            for r in results:
                vm_id   = r["vm_id"]
                rtt_m   = rtt_lookup.get(vm_id)
                rtt_val = rtt_m.rtt_ms if rtt_m and rtt_m.reachable else None

                vm_metrics = {**{m: r.get(m) for m in collector_metrics}, "latency": rtt_val}
                new_collected.append({
                    "vm_id": vm_id, **vm_metrics,
                    "reliability": r.get("reliability"),
                    "reachable":   r.get("reachable"),
                })
                persist_tasks.append(_post(client, f"{_URLS['database']}/store/metrics", {
                    "vm_id": vm_id, "metrics": vm_metrics,
                    "timestamp": now_iso, "reliability": r.get("reliability")
                }))
            await asyncio.gather(*persist_tasks)
            state.last_collected = new_collected

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

            # ── ÉTAPE 5 : Vérification violations SLO ───────────────────
            svc_data  = next((r for r in state.last_collected if r["vm_id"] == state.service_vm), None)
            violation = _is_violation(svc_data, _threshold_map()) if svc_data else False
            if violation:
                logger.warning(
                    f"⚠️  Violation SLO détectée sur {C.YELLOW}{state.service_vm}{C.RESET} "
                    "— analyse de migration initiée"
                )
            else:
                logger.info(f"✅ SLOs respectés sur {C.GREEN}{state.service_vm}{C.RESET}")

            # ── ÉTAPE 6 : Historiques de toutes les VMs ─────────────────
            hist_tasks = [
                _post(client, f"{_URLS['history_loader']}/load", {
                    "vm_id": vid,
                    "metrics": list(config.METRICS_REGISTRY.keys()),
                    "size": config.HISTORY_WINDOW
                }) for vid in vm_ids
            ]
            hist_responses = await asyncio.gather(*hist_tasks)
            vm_histories = {
                vid: (res.get("histories", {}) if res else {})
                for vid, res in zip(vm_ids, hist_responses)
            }
            logger.info(
                f"📚 Historiques chargés pour {C.CYAN}{len(vm_ids)}{C.RESET} VM(s) "
                f"| fenêtre : {C.CYAN}{config.HISTORY_WINDOW}{C.RESET} points"
            )

            # ── ÉTAPE 7 : Prédictions ML (toutes VMs en parallèle) ──────
            new_predictions: Dict[str, Dict[str, Any]] = {vid: {} for vid in vm_ids}

            p_responses = await asyncio.gather(*[
                _post(client, f"{_URLS['ml_predictor']}/predict",
                      _ml_payload(vm_histories.get(vid, {})))
                for vid in vm_ids
            ])
            for vid, res in zip(vm_ids, p_responses):
                if res and not res.get("all_apis_down"):
                    new_predictions[vid] = _extract_predictions(res)

            state.last_predictions = new_predictions
            successful_preds = sum(1 for v in new_predictions.values() if v)
            logger.info(
                f"🤖 Prédictions ML générées — "
                f"{C.GREEN}{successful_preds}{C.RESET}/{len(vm_ids)} VM(s)"
            )
            for vid, preds in new_predictions.items():
                if preds:
                    lat  = preds.get("latency",   {}).get("predictions", ["N/A"])[0]
                    cpu  = preds.get("cpu_usage",  {}).get("predictions", ["N/A"])[0]
                    ram  = preds.get("ram_usage",  {}).get("predictions", ["N/A"])[0]
                    logger.debug(
                        f"🔍 Prédiction {C.BOLD}{vid}{C.RESET} — "
                        f"Latence: {C.CYAN}{lat} ms{C.RESET}  "
                        f"CPU: {C.CYAN}{cpu} %{C.RESET}  "
                        f"RAM: {C.CYAN}{ram} %{C.RESET}"
                    )

            state.snapshot_collected   = list(state.last_collected)
            state.snapshot_predictions = dict(state.last_predictions)

            # ── ÉTAPE 8 : Décision ───────────────────────────────────────
            current_data = []
            for lc in state.last_collected:
                entry = {"vm_id": lc["vm_id"]}
                for m, meta in config.METRICS_REGISTRY.items():
                    entry[meta["payload_key"]] = lc.get(m)
                current_data.append(entry)

            # Lookup explicite par vm_id — évite tout risque de désalignement
            # positionnel entre vm_ids et state.last_collected (ex. si une VM
            # est omise par le collector, ou si l'ordre change un jour).
            collected_lookup = {lc["vm_id"]: lc for lc in state.last_collected}

            di_payload = {
                "current_data":      current_data,
                "predictions_map":   state.last_predictions,
                "slos":              state.current_slos,
                "service_vm":        state.service_vm,
                "cooldown_active":   state.check_cooldown(),
                "migration_costs":   {vid: 0 for vid in vm_ids},
                "reliability_scores": {
                    vid: collected_lookup.get(vid, {}).get("reliability", 1.0)
                    for vid in vm_ids
                }
            }
            if di_payload["cooldown_active"]:
                elapsed = time.monotonic() - state.last_migration_ts
                remaining = config.MIGRATION_COOLDOWN_S - elapsed
                logger.warning(
                    f"⏳ Cooldown actif — migration bloquée "
                    f"({C.YELLOW}{remaining:.0f}s{C.RESET} restante(s))"
                )

            if not di_payload["cooldown_active"]:
                di_res = await _post(client, f"{_URLS['decision_intelligence']}/decide", di_payload)
            else:
                di_res = {
                    "decision": "stay", "from_vm": None, "to_vm": None,
                    "reason": "Cooldown active", "topsis_score": None, "breach_type": None
                }

            if di_res:
                state.last_decision = di_res
                decision    = di_res.get("decision", "?")
                reason      = di_res.get("reason", "—")
                topsis      = di_res.get("topsis_score")
                breach      = di_res.get("breach_type", "—")

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

                    await _post(client, f"{_URLS['database']}/store/decision", di_res)

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
    logger.info(
        f"\n{'═'*60}\n"
        f"  📥 Intent reçu — mode Enhanced activé\n"
        f"  {'ID':<16}: {C.CYAN}{intent_id}{C.RESET}\n"
        f"  {'SLOs injectés':<16}: {C.CYAN}{len(state.current_slos)}{C.RESET}\n"
        f"  {'Poids originaux':<16}: {C.CYAN}{state.original_intent_weights}{C.RESET}\n"
        f"{'═'*60}"
    )
    return {"status": "accepted", "mode": state._mode, "slos": len(state.current_slos)}


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


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "orchestrator_core"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.HUB_PORT)