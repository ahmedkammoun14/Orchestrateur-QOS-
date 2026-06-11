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


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level":     record.levelname,
            "service":   "orchestrator_core",
            "event":     getattr(record, "event", "generic"),
            "message":   record.getMessage(),
        }
        if hasattr(record, "extra_data"):
            log_record.update(record.extra_data)
        return json.dumps(log_record)


def setup_logger() -> logging.Logger:
    log = logging.getLogger("OrchestratorCore")
    log.setLevel(logging.INFO)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(JSONFormatter())
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

# URL openstack_client — tourne sur le master OpenStack
OPENSTACK_CLIENT_URL = f"http://{config.OPENSTACK_MASTER_IP}:{config.OPENSTACK_CLIENT_PORT}"


class OrchestratorState:
    def __init__(self) -> None:
        self._mode: str = "autonomous"
        self.service_vm: Optional[str] = next(iter(config.VM_REGISTRY)) if config.VM_REGISTRY else None
        self.last_migration_ts: Optional[float] = None
        self.bootstrap_cycles: int = 0
        self.BOOTSTRAP_MIN: int = 5
        self.current_slos: List[Dict[str, Any]] = []
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
        logger.error(f"POST {url} failed: {e}", extra={"event": "internal_error"})
        return None

def _threshold_map() -> Dict[str, float]:
    thr = {m: meta["default_threshold"] for m, meta in config.METRICS_REGISTRY.items()}
    for slo in state.current_slos:
        if slo.get("metric") in thr:
            thr[slo["metric"]] = float(slo["threshold"])
    return thr

def _is_violation(record: Dict[str, Any], thresholds: Dict[str, float]) -> bool:
    for metric, meta in config.METRICS_REGISTRY.items():
        val = record.get(metric)
        if val is None: continue
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


async def _sync_active_vm(client: httpx.AsyncClient) -> None:
    """
    Synchronise state.service_vm avec le pod réellement actif sur kubectl.
    Appelé au démarrage pour reprendre l'état réel après un redémarrage du hub.
    """
    try:
        r = await client.get(f"{OPENSTACK_CLIENT_URL}/active_vm", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            active_vm = data.get("active_vm")
            if active_vm and active_vm in config.VM_REGISTRY:
                state.service_vm = active_vm
                logger.info(
                    f"service_vm synced from kubectl: {state.service_vm}",
                    extra={"event": "service_vm_synced",
                           "extra_data": {"active_vm": active_vm, "cluster": data.get("cluster")}}
                )
            else:
                logger.warning(
                    "No active pod found on kubectl — keeping default service_vm",
                    extra={"event": "service_vm_sync_no_pod"}
                )
        else:
            logger.warning(
                f"openstack_client /active_vm returned {r.status_code}",
                extra={"event": "service_vm_sync_failed"}
            )
    except Exception as e:
        logger.warning(
            f"Could not sync active_vm from openstack_client: {e}",
            extra={"event": "service_vm_sync_failed"}
        )


async def _execute_kubectl_migration(client: httpx.AsyncClient, from_vm: str, to_vm: str) -> bool:
    """
    Appelle openstack_client /migrate pour exécuter la migration kubectl réelle.
    Retourne True si succès, False sinon.
    """
    try:
        resp = await client.post(
            f"{OPENSTACK_CLIENT_URL}/migrate",
            json={"from_vm": from_vm, "to_vm": to_vm},
            timeout=35.0  # kubectl peut prendre du temps
        )
        if resp.status_code == 200:
            data = resp.json()
            logger.info(
                f"kubectl migration OK: {from_vm} → {to_vm} on {data.get('cluster')}",
                extra={"event": "kubectl_migration_ok",
                       "extra_data": {"from_vm": from_vm, "to_vm": to_vm, "cluster": data.get("cluster")}}
            )
            return True
        else:
            logger.error(
                f"kubectl migration failed: HTTP {resp.status_code}",
                extra={"event": "kubectl_migration_failed",
                       "extra_data": {"from_vm": from_vm, "to_vm": to_vm, "status": resp.status_code}}
            )
            return False
    except Exception as e:
        logger.error(
            f"kubectl migration error: {e}",
            extra={"event": "kubectl_migration_failed",
                   "extra_data": {"from_vm": from_vm, "to_vm": to_vm, "error": str(e)}}
        )
        return False


async def _run_flow(measurements: List[RTTMeasurement], mode: str) -> None:
    if state._lock.locked():
        return

    async with state._lock:
        logger.info("Flow started", extra={"event": "flow_start",
                    "extra_data": {"cycle": state.cycle_count, "mode": mode}})

        async with httpx.AsyncClient() as client:
            vm_ids  = list(config.VM_REGISTRY.keys())
            now_iso = datetime.now(timezone.utc).isoformat()

            # --- ÉTAPE 1: SLOs ---
            if state.cycle_count < state.BOOTSTRAP_MIN:
                logger.info("Bootstrap phase", extra={"event": "bootstrap_phase"})
                state.current_slos = []
                for metric, meta in config.METRICS_REGISTRY.items():
                    state.current_slos.append({
                        "metric": metric, "operator": meta["operator"],
                        "threshold": meta["default_threshold"], "unit": meta["unit"],
                        "weight": 1.0 / len(config.METRICS_REGISTRY),
                        "target": meta["default_threshold"] * 0.9, "window": "5m"
                    })
                active_metrics = list(config.METRICS_REGISTRY.keys())
            else:
                h_res = await _post(client, f"{_URLS['history_loader']}/load", {
                    "vm_id": state.service_vm,
                    "metrics": list(config.METRICS_REGISTRY.keys()),
                    "size": config.HISTORY_WINDOW
                })
                svc_hists = h_res.get("histories", {}) if h_res else {}

                if mode == "enhanced":
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
                    logger.info("SLOs updated", extra={"event": "slos_updated"})
                else:
                    active_metrics = list(config.METRICS_REGISTRY.keys())

            # --- ÉTAPE 2: Persistance SLOs ---
            await _post(client, f"{_URLS['database']}/store/slos",
                        {"slos": state.current_slos, "timestamp": now_iso})

            # --- ÉTAPE 3: Collecte métriques — toujours cpu + ram ---
            collector_metrics = [m for m in config.METRICS_REGISTRY.keys() if m != "latency"]
            coll_res = await _post(client, f"{_URLS['collector']}/collect",
                                   {"active_metrics": collector_metrics, "cycle": state.cycle_count})
            results  = coll_res.get("results", []) if coll_res else []
            logger.info("Metrics collected", extra={"event": "metrics_collected"})

            # --- ÉTAPE 4: Persistance métriques ---
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

            # --- ÉTAPE 5: Calcul is_violation ---
            svc_data  = next((r for r in state.last_collected if r["vm_id"] == state.service_vm), None)
            violation = _is_violation(svc_data, _threshold_map()) if svc_data else False

            # --- ÉTAPE 6: Historiques pour TOUTES les VMs ---
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

            # --- ÉTAPE 7: Prédictions ML — TOUTES les VMs en parallèle ---
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
            logger.info("Predictions done", extra={"event": "predictions_done"})

            # Snapshot stable pour GET /data
            state.snapshot_collected   = list(state.last_collected)
            state.snapshot_predictions = dict(state.last_predictions)

            # --- ÉTAPE 8: Décision ---
            current_data = []
            for lc in state.last_collected:
                entry = {"vm_id": lc["vm_id"]}
                for m, meta in config.METRICS_REGISTRY.items():
                    entry[meta["payload_key"]] = lc.get(m)
                current_data.append(entry)

            di_payload = {
                "current_data":      current_data,
                "predictions_map":   state.last_predictions,
                "slos":              state.current_slos,
                "service_vm":        state.service_vm,
                "cooldown_active":   state.check_cooldown(),
                "migration_costs":   {vid: 0 for vid in vm_ids},
                "reliability_scores": {
                    vid: lc.get("reliability", 1.0)
                    for vid, lc in zip(vm_ids, state.last_collected)
                }
            }
            if di_payload["cooldown_active"]:
                logger.info("Cooldown active", extra={"event": "cooldown_active"})

            if not di_payload["cooldown_active"]:
                di_res = await _post(client, f"{_URLS['decision_intelligence']}/decide", di_payload)
            else:
                di_res = {
                    "decision": "stay", "from_vm": None, "to_vm": None,
                    "reason": "Cooldown active", "topsis_score": None, "breach_type": None
                }

            if di_res:
                state.last_decision = di_res
                logger.info("Decision made", extra={"event": "decision_made", "extra_data": di_res})

                if di_res.get("decision") == "migrate":
                    from_vm = di_res["from_vm"]
                    to_vm   = di_res["to_vm"]

                    # --- ÉTAPE 9: Persistance décision ---
                    await _post(client, f"{_URLS['database']}/store/decision", di_res)

                    # --- ÉTAPE 10a: Migration kubectl réelle via openstack_client ---
                    kubectl_ok = await _execute_kubectl_migration(client, from_vm, to_vm)
                    if not kubectl_ok:
                        logger.warning(
                            f"kubectl migration failed — state updated anyway",
                            extra={"event": "kubectl_migration_warning",
                                   "extra_data": {"from_vm": from_vm, "to_vm": to_vm}}
                        )

                    # --- ÉTAPE 10b: Mise à jour état interne ---
                    state.service_vm        = to_vm
                    state.last_migration_ts = time.monotonic()
                    logger.info(
                        f"Migration complete → {to_vm}",
                        extra={"event": "migration_executed",
                               "extra_data": {"from_vm": from_vm, "to_vm": to_vm, "kubectl_ok": kubectl_ok}}
                    )

        logger.info("Flow complete", extra={"event": "flow_complete"})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    success = True
    async with httpx.AsyncClient() as client:
        # Health check tous les services locaux
        for name, url in _URLS.items():
            try:
                r = await client.get(f"{url}/health", timeout=2.0)
                if r.status_code == 200:
                    logger.info(f"Service {name} OK", extra={"event": "health_check_ok"})
                else:
                    logger.error(f"Service {name} failed: {r.status_code}",
                                 extra={"event": "health_check_failed"})
                    success = False
            except Exception as e:
                logger.error(f"Service {name} unreachable: {e}",
                             extra={"event": "health_check_failed"})
                success = False

        # Health check openstack_client
        try:
            r = await client.get(f"{OPENSTACK_CLIENT_URL}/health", timeout=3.0)
            if r.status_code == 200:
                logger.info("Service openstack_client OK", extra={"event": "health_check_ok"})
            else:
                logger.warning("openstack_client unreachable — migrations won't be executed",
                               extra={"event": "health_check_warning"})
        except Exception as e:
            logger.warning(f"openstack_client unreachable: {e} — migrations won't be executed",
                           extra={"event": "health_check_warning"})

        # Synchroniser service_vm avec kubectl au démarrage
        await _sync_active_vm(client)

    if not success:
        logger.warning("Starting with some services down", extra={"event": "startup_warning"})

    logger.info("Core started", extra={"event": "core_started",
                "extra_data": {"service_vm": state.service_vm, "mode": state._mode}})
    yield
    logger.info("Core shutting down", extra={"event": "shutdown"})


app = FastAPI(title="QoS Orchestrator Core", version="2.0.0", lifespan=lifespan)


@app.post("/rtt", status_code=status.HTTP_200_OK)
async def receive_rtt(payload: LatencyPayload):
    state.cycle_count += 1
    asyncio.create_task(_run_flow(payload.measurements, state._mode))
    return {"status": "accepted", "cycle": state.cycle_count}


@app.post("/intent", status_code=status.HTTP_200_OK)
async def receive_intent(payload: Dict[str, Any] = Body(...)):
    state.current_slos = payload.get("slos", [])
    state._mode = "enhanced"
    logger.info("Intent applied", extra={"event": "intent_applied",
                "extra_data": {"intent_id": payload.get("intent_id")}})
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
        "last_decision":    state.last_decision.get("decision"),
        "timestamp":        datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "orchestrator_core"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.HUB_PORT)