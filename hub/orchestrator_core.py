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
    GapGrade, PlacementPlan, ProviderAssessment, ProviderBid, ProviderOffer,
    build_gap_grade, candidates_for_provider, compute_gap_grade,
    evaluate_provider, negotiate, placement_action, slo_values_for_vm,
    to_slo_unit,
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
        # VM qui héberge réellement le service selon kubectl (vérité GLOBALE :
        # peut appartenir à un AUTRE provider que celui de ce processus).
        self.hosting_vm: Optional[str] = None
        # Rôle de ce processus. True par défaut = comportement historique
        # (mono-processus) tant qu'aucune synchronisation n'a eu lieu.
        self.is_active: bool = True
        self.last_migration_ts: Optional[float] = None
        # Horodatage du dernier /award reçu (monotonic) — utilisé par
        # _sync_active_vm pour ignorer une lecture kubectl encore périmée
        # pendant la fenêtre de grâce, voir config.AWARD_GRACE_PERIOD_S.
        self.last_award_ts: Optional[float] = None
        # Version de l'intention appliquée (epoch, assignée par le PREMIER hub
        # qui la reçoit du client). Sert d'ordre total pour la réplication :
        # une propagation en retard ne doit jamais écraser une intention plus
        # récente. Les hubs tournant sur la MÊME machine, time.time() est
        # directement comparable entre eux — aucune dérive d'horloge à gérer.
        self.intent_version: Optional[float] = None
        # Texte brut de la DERNIERE intention appliquee. Publie par /status
        # pour que le LLM puisse comparer l'intention precedente a la
        # nouvelle et decider si elles portent sur le meme usage
        # (merge_strategy additive) ou sur deux usages distincts (replace).
        # Sans lui, le LLM ne voit que des seuils chiffres et juge sur la
        # seule formulation de la phrase courante.
        self.last_intention_text: Optional[str] = None
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
        # Numéro du cycle EN COURS D'EXÉCUTION, figé à l'entrée de _run_flow.
        # `cycle_count` s'incrémente à chaque lot reçu, y compris ceux qui sont
        # abandonnés parce qu'un cycle tourne déjà : un cycle long le voit donc
        # changer sous ses pieds, et deux étapes du MÊME cycle s'étiquetaient
        # avec deux numéros différents. Symptôme : la feuille Prédictions et le
        # fichier de temps décalés d'une unité sur une partie des cycles.
        self.flow_cycle: int = 0
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
                # Un hub STANDBY n'heberge rien : service_vm y est un reliquat
                # de l'epoque ou il etait actif (la demission ne rafraichit que
                # hosting_vm). Sans ce garde, le payload annonce role="standby"
                # ET is_active=true — contradictoire — et le consommateur ne
                # peut pas retomber sur hosting_vm.
                "is_active":    (vm_id == self.service_vm) and self.is_active,
                "predictions": {
                    "latency":   preds.get("latency",   {}).get("predictions", []),
                    "cpu_usage": preds.get("cpu_usage", {}).get("predictions", []),
                    "ram_usage": preds.get("ram_usage", {}).get("predictions", []),
                }
            }
        return {
            "vms":           vms_data,
            "slos":          self.current_slos,
            # Publie le mode : sans lui, un tableau de bord STANDBY ne peut
            # pas l'afficher. Le frontend ne le recevait que par les audits,
            # emis uniquement par le hub ACTIF.
            "mode":          self._mode,
            "mi_scores":     self.last_mi_scores,
            "last_decision": self.last_decision,
            "cycle":         self.cycle_count,
            "role":          "active" if self.is_active else "standby",
            "hosting_vm":    self.hosting_vm,
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


async def _push_state_to_picar() -> None:
    """
    Pousse l'état de CE hub au bridge PiCar — appelé UNIQUEMENT par le hub
    ACTIF, en fin de cycle. Fire-and-forget avec client dédié, exactement
    comme _post_audit : une panne du bridge ne doit JAMAIS interrompre ni
    ralentir un cycle d'orchestration.

    Le bridge préfère cette valeur (précise : edge2b) à sa découverte par
    polling, qui retombe sur la VM canonique de kubectl (edge2) dès qu'un
    hub tarde à répondre. Purement informatif côté PiCar : aucune décision
    ne dépend de ce push.
    """
    if not config.PICAR_BRIDGE_URL:
        return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(
                f"{config.PICAR_BRIDGE_URL}/active-vm-push",
                json={
                    "provider_id":     config.PROVIDER_ID,
                    "service_vm":      state.service_vm,
                    "role":            "active" if state.is_active else "standby",
                    "last_decision":   state.last_decision.get("decision"),
                    "cycle":           state.cycle_count,
                    "cooldown_active": state.check_cooldown(),
                },
                timeout=2.0,
            )
    except Exception as e:
        logger.debug(f"🔇 État non transmis au bridge PiCar : {e}")

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

    Itère sur state.current_slos (et non sur METRICS_REGISTRY) pour disposer
    du SLO complet : son UNITÉ (conversion) et son OPÉRATEUR. Deux défauts
    corrigés ici, qui se combinaient pour déclarer une violation sur toute VM
    à chaque cycle en mode enhanced :
      • aucune conversion d'unité — on comparait cpu_usage = 50.0 (POURCENTAGE
        brut) à un seuil de 1.0 (CŒURS) ;
      • l'opérateur venait du REGISTRE ("<" pour cpu_usage) au lieu du SLO
        (">=" quand le LLM demande une disponibilité minimale).
    Résultat mesuré avant correctif : `50.0 >= 1.0` → violation sur une VM
    parfaitement saine, donc gate ouverte en permanence.

    ⚠️ LIMITE ASSUMÉE : appelée aussi par _zip_histories sur des
    enregistrements d'HISTORIQUE, qui ne portent pas la capacité de la VM
    (total_cores / total_ram_gb). to_slo_unit y retombe alors sur le % brut
    et la comparaison sous-détecte. Sans effet pratique : ce drapeau
    n'alimente que le calcul MI, lui-même court-circuité en mode enhanced
    (seul mode où un primaire est exprimé en unité absolue).
    """
    for slo in state.current_slos:
        if not slo.get("is_primary", False):
            continue
        metric = slo.get("metric")
        if metric not in config.METRICS_REGISTRY:
            continue
        val = record.get(metric)
        if val is None:
            continue
        meta = config.METRICS_REGISTRY[metric]
        # Même conversion que LA GATE proactive — une seule implémentation.
        val = to_slo_unit(metric, slo, record, val)
        thr = thresholds.get(metric, meta["default_threshold"])
        op  = slo.get("operator") or meta["operator"]
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
    """
    Synchronise le rôle ACTIF/STANDBY de ce processus depuis la vérité kubectl.

    kubectl connaît la VM qui héberge RÉELLEMENT le service, quel que soit le
    provider auquel elle appartient — state.hosting_vm capte donc toujours
    cette vérité globale. state.is_active/service_vm ne sont mis à jour que
    si l'on peut établir positivement le rôle ; toute incertitude (pod
    inconnu, kubectl injoignable) laisse ces deux champs inchangés, pour
    qu'une panne transitoire ne fasse jamais basculer un rôle.

    ⚠️ GRANULARITÉ : kubectl résout au NIVEAU DU NODE, pas de la VM. Les VMs
    simulées d'un même node physique (ex. edge2/edge2b/edge2c sur
    pop1-worker-2) sont indiscernables pour lui — il renvoie toujours la VM
    CANONIQUE du node (voir config.VM_NODE_GROUP). Adopter aveuglément cette
    valeur écraserait notre suivi plus fin (state.service_vm = edge2b) par
    la valeur canonique (edge2) à chaque cycle, empêchant toute migration
    stable À L'INTÉRIEUR d'un même node. On n'adopte donc la valeur de
    kubectl que lorsqu'elle est RÉELLEMENT plus sûre que notre suivi local
    (voir les trois conditions ci-dessous) — jamais par défaut.
    """
    try:
        r = await client.get(f"{OPENSTACK_CLIENT_URL}/active_vm", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            active_vm = data.get("active_vm")
            if active_vm and active_vm in config.ALL_VM_REGISTRY:
                was_active = state.is_active

                if config.PROVIDER_ID == "all":
                    state.hosting_vm = active_vm
                    state.is_active  = True
                else:
                    kubectl_says_active = (
                        config.PROVIDER_OF_VM.get(active_vm) == config.PROVIDER_ID
                    )
                    in_award_grace = (
                        state.last_award_ts is not None
                        and (time.monotonic() - state.last_award_ts) < config.AWARD_GRACE_PERIOD_S
                    )
                    if was_active and not kubectl_says_active and in_award_grace:
                        # On vient d'être promu par un award récent, mais kubectl
                        # n'a pas encore fini de propager la migration (ancien
                        # pod encore Terminating). Cette lecture est périmée —
                        # on l'ignore ENTIÈREMENT : ni is_active, ni hosting_vm,
                        # ni service_vm ne sont mis à jour depuis cette valeur.
                        logger.info(
                            f"🕒 Sync kubectl périmée ignorée (fenêtre de grâce award, "
                            f"{config.AWARD_GRACE_PERIOD_S:.0f}s) — rôle conservé : ACTIF"
                        )
                        return
                    state.hosting_vm = active_vm
                    state.is_active  = kubectl_says_active

                if state.is_active:
                    node_hote = config.VM_NODE_GROUP.get(active_vm)
                    mon_node  = config.VM_NODE_GROUP.get(state.service_vm)
                    # On adopte la VM canonique de kubectl SAUF si notre suivi
                    # local est plus précis, c'est-à-dire si notre service_vm
                    # est déjà sur le MÊME node qu'elle :
                    #   • not was_active      → on vient de passer STANDBY→ACTIF :
                    #                           notre service_vm est un reliquat,
                    #                           on adopte la vérité kubectl.
                    #   • node_hote is None   → VM inconnue de la table : rien à
                    #                           comparer, kubectl fait autorité
                    #                           (repli sûr).
                    #   • mon_node != node_hote → le service a changé de NODE
                    #                           (ex. cloud1 → edge1) : kubectl
                    #                           est plus juste que notre suivi.
                    #
                    # ⚠️ SAUF juste après NOTRE PROPRE migration. Un changement
                    # de node prend quelques secondes à se propager : pendant
                    # cette fenêtre kubectl décrit encore l'ANCIEN node, et le
                    # troisième cas ci-dessus rembobine service_vm vers la VM
                    # qu'on vient de quitter. Observé : intention 1 migre
                    # edge1(pop1) → cloud1(pop2) ; le sync suivant lit encore
                    # pop1, donc edge1, et l'écrit ; l'intention 2 part alors
                    # de edge1 au lieu de cloud1 — et la migration supprime le
                    # pod sur le mauvais cluster.
                    #
                    # La garde d'award ci-dessus ne couvre pas ce cas : elle
                    # exige `not kubectl_says_active`, ce qui n'arrive qu'aux
                    # promotions INTER-provider. Une migration LOCALE garde
                    # kubectl_says_active à True et passait donc au travers.
                    in_migration_grace = (
                        state.last_migration_ts is not None
                        and (time.monotonic() - state.last_migration_ts)
                            < config.AWARD_GRACE_PERIOD_S
                    )
                    node_changed = (mon_node != node_hote)

                    if (not was_active) or (node_hote is None) or (
                        node_changed and not in_migration_grace
                    ):
                        state.service_vm = active_vm
                    elif node_changed:
                        logger.info(
                            f"🕒 Sync kubectl périmée ignorée (migration récente, "
                            f"{config.AWARD_GRACE_PERIOD_S:.0f}s) — VM conservée : "
                            f"{C.GREEN}{state.service_vm}{C.RESET} "
                            f"(kubectl proposait {C.YELLOW}{active_vm}{C.RESET})"
                        )
                    # sinon : conserver state.service_vm — kubectl ne sait pas
                    # distinguer edge2 de edge2b, notre suivi si.

                role_tag = f"{C.GREEN}ACTIF{C.RESET}" if state.is_active else f"{C.YELLOW}STANDBY{C.RESET}"
                logger.info(
                    f"✅ VM active synchronisée depuis kubectl : "
                    f"{C.GREEN}{state.hosting_vm}{C.RESET} "
                    f"(cluster : {C.CYAN}{data.get('cluster')}{C.RESET}) "
                    f"| rôle : {role_tag}"
                )
            else:
                logger.warning(
                    "⚠️  Aucun pod actif trouvé sur kubectl — "
                    f"rôle et service_vm inchangés : {C.YELLOW}{state.service_vm}{C.RESET}"
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
    # Version d'intention au DÉBUT du calcul. Sert de jeton de concurrence
    # optimiste : si elle a changé quand metrics_manager répond, c'est qu'une
    # intention est arrivée entre-temps et le résultat de ce cycle est périmé
    # (voir la garde en fin de fonction).
    _intent_version_at_start = state.intent_version

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

    # ── Un STANDBY ne calcule PLUS ses SLOs ──────────────────────────
    # Seul le provider ACTIF héberge le service, donc seul lui dispose d'un
    # historique exploitable. Chez un STANDBY, state.service_vm désigne une VM
    # de l'AUTRE provider : history_loader renvoie 0 point et le MI ne peut pas
    # être calculé. Le calcul tournait quand même — dans le vide — et figeait ou
    # écrasait les SLOs, d'où deux contrats divergents entre providers.
    #
    # Le standby CONSERVE le contrat qu'il détient : celui reçu au dernier
    # broadcast (voir l'adoption dans POST /evaluate). Bénéfice secondaire :
    # deux appels HTTP de moins par cycle (history_loader + metrics_manager).
    if config.PROVIDER_ID != "all" and not state.is_active:
        ctx.active_metrics = [s["metric"] for s in state.current_slos]
        logger.info(
            f"🟡 STANDBY — SLOs non recalculés, contrat conservé : "
            f"{C.CYAN}{ctx.active_metrics}{C.RESET}"
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
        # ⚠️ N'envoyer que le CONTRAT DU LLM, jamais state.current_slos en
        # entier. Cette liste contient aussi les SLOs SECONDAIRES ajoutés par
        # metrics_manager au cycle précédent (étape 2 de
        # validate_and_enrich_slos), or son étape 1 force is_primary=True sur
        # TOUT ce qu'elle reçoit : les lui renvoyer les PROMEUT en primaires,
        # cycle après cycle. Constaté en production — une intention ayant
        # produit un seul SLO (latency) se retrouvait avec trois primaires
        # (latency, RAM, CPU) aux poids dérivants, faisant déclencher des
        # migrations sur des métriques jamais demandées par le client.
        # original_intent_weights est la liste exacte du contrat, figée à la
        # réception de l'intention (voir /intent).
        contract = (
            [slo for slo in state.current_slos
             if slo.get("metric") in state.original_intent_weights]
            if state.original_intent_weights
            else list(state.current_slos)   # repli : contrat inconnu, comportement antérieur
        )
        for slo in contract:
            if slo.get("is_primary") and slo["metric"] in state.original_intent_weights:
                slo["weight"] = state.original_intent_weights[slo["metric"]]
        mm_payload = {
            "slos":    contract,
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
    # ⚠️ COURSE INTENTION / CYCLE. `_step1_slos` a lu state.current_slos au
    # début du cycle, puis attendu metrics_manager pendant ~500 ms. Une
    # intention reçue DANS cet intervalle a déjà remplacé le contrat ; écrire
    # ici le résultat calculé sur l'ANCIEN contrat l'écraserait.
    #
    # Observé en campagne (UC4) : cycle #175 démarre à 12:58:07.849 avec
    # `latency < 50`, l'intention « maximum compute power » est livrée à
    # 12:58:08.0 et pose `cpu_usage >= 3.5, ram_usage >= 8`, puis le cycle
    # réécrit `latency + cpu_usage(MI)` à 12:58:08.382. Au cycle suivant, le
    # filtre par original_intent_weights — désormais {cpu_usage, ram_usage} —
    # ne retient que le SECONDAIRE `cpu_usage >= 1`, que metrics_manager
    # promeut en primaire, et la latence revient en secondaire avec un seuil
    # adaptatif. Le contrat client était perdu.
    #
    # Garde optimiste : si la version d'intention a changé pendant le calcul,
    # le contrat fraîchement reçu gagne et le résultat du cycle est ignoré.
    _intent_changed = (state.intent_version != _intent_version_at_start)

    if mm_res and not _intent_changed:
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
    elif _intent_changed:
        ctx.active_metrics = [s["metric"] for s in state.current_slos]
        logger.info(
            f"🔄 Intention reçue pendant le calcul des SLOs — résultat du cycle "
            f"ignoré, contrat conservé : {C.CYAN}{ctx.active_metrics}{C.RESET}"
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


def _step5_check_violations(ctx: _FlowContext) -> bool:
    """Log de cohérence : violation courante + signal proactif ML sur la VM active."""
    svc_data  = next((r for r in state.last_collected if r["vm_id"] == state.service_vm), None)
    violation = _is_violation(svc_data, _threshold_map()) if svc_data else False

    # Signal proactif : une prédiction ML dépasse le seuil proactif.
    # proactive_signals (tous les SLOs actifs) alimente UNIQUEMENT le log
    # ci-dessous, inchangé — proactive_signals_primary (SLOs is_primary
    # seulement) alimente la valeur de retour, voir plus bas.
    thresholds   = _threshold_map()
    svc_preds    = state.last_predictions.get(state.service_vm, {})
    proactive_signals         = []
    proactive_signals_primary = []
    for slo in state.current_slos:
        metric    = slo["metric"]
        threshold = thresholds.get(metric, float("inf"))
        # ⚠️ CONVERSION D'UNITÉ OBLIGATOIRE avant comparaison. Le seuil vient
        # du SLO — donc en cœurs/Go si le LLM l'a exprimé ainsi (enhanced) —
        # alors que le ML prédit TOUJOURS un POURCENTAGE d'usage pour
        # cpu_usage/ram_usage. Sans conversion on comparait 50.4 (%) à 0.6
        # (cœurs) : la brèche n'était JAMAIS détectée et un primaire du LLM ne
        # pouvait pas ouvrir la gate, alors même que la conformité
        # (evaluate_vm → _representative_value) convertissait bien et écartait
        # la VM. Le système refusait des VMs sur un critère qu'il ne savait
        # pas détecter.
        preds     = [
            to_slo_unit(metric, slo, svc_data or {}, p)
            for p in (svc_preds.get(metric, {}).get("predictions", []) or [])
            if p is not None
        ]
        op        = slo.get("operator", "<")
        if op in ("<", "<="):
            breach = any(p > threshold for p in preds)     # critère de COÛT
        elif op in (">", ">="):
            breach = any(p < threshold for p in preds)     # critère de BÉNÉFICE
        else:
            breach = False
        if breach:
            proactive_signals.append(metric)
            if slo.get("is_primary"):
                proactive_signals_primary.append(metric)

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

    # La valeur renvoyée est LA GATE qui ouvre le tour de fédération
    # (broadcast + arbitrage, voir _run_flow/_decide_federated) : elle ne
    # s'ouvre que sur une métrique PRIMAIRE — règle métier verrouillée du
    # projet, « cpu/ram secondaires ne déclenchent JAMAIS de migration ».
    # Les signaux secondaires restent loggués ci-dessus (proactive_signals,
    # inchangé) pour le diagnostic, mais n'ouvrent rien.
    #
    # ⚠️ LIMITE CONNUE : les prédictions sont comparées au seuil SANS
    # conversion d'unité. Si le LLM déclarait un SLO PRIMAIRE exprimé en
    # ressource absolue (unit "cores" ou "GB"), cette comparaison opposerait
    # un POURCENTAGE prédit à un nombre de CŒURS. Sans effet aujourd'hui (le
    # seul primaire en mode autonome est la latence, en ms des deux côtés),
    # mais à traiter avant tout usage d'un primaire en unité absolue — la
    # conversion existe déjà ailleurs (_representative_value /
    # _to_criterion_value, hub/provider_arbitration.py).
    return bool(violation or proactive_signals_primary)


async def _step6_load_histories(client: httpx.AsyncClient, ctx: _FlowContext) -> None:
    """Charge les historiques de toutes les VMs en parallèle.

    ⚠️ ML_HISTORY_WINDOW, et NON HISTORY_WINDOW : ces historiques alimentent
    les prédictions (_step7_predict), qui exigent au moins le window_size du
    modèle pour atteindre le Niveau 1 de predictor.py. HISTORY_WINDOW est
    calibrée pour le MI, qui a le besoin inverse (fenêtre courte). Les deux ont
    partagé la même valeur jusqu'au 24/08/2026 — voir shared/config.py.
    """
    hist_responses = await asyncio.gather(*[
        _post(client, f"{_URLS['history_loader']}/load", {
            "vm_id":   vid,
            "metrics": list(config.METRICS_REGISTRY.keys()),
            "size":    config.ML_HISTORY_WINDOW,
        }) for vid in ctx.vm_ids
    ])
    ctx.vm_histories = {
        vid: (res.get("histories", {}) if res else {})
        for vid, res in zip(ctx.vm_ids, hist_responses)
    }
    logger.info(
        f"📚 Historiques chargés pour {C.CYAN}{len(ctx.vm_ids)}{C.RESET} VM(s) "
        f"| fenêtre ML : {C.CYAN}{config.ML_HISTORY_WINDOW}{C.RESET} points"
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

    _log_prediction_pairs(client, new_predictions)


# Références fortes sur toutes les tâches détachées de ce module.
# `asyncio.create_task` ne garde qu'une référence FAIBLE : sans ce set, le
# ramasse-miettes peut détruire une tâche avant qu'elle ne s'achève, et son
# effet — écrire une ligne, pousser un audit, exécuter un cycle — n'a jamais
# lieu, sans la moindre erreur. Symptôme mesuré : 90 cycles manquants sur 303,
# par salves — un motif régulier, jamais aléatoire.
_detached_tasks: set = set()


def _spawn(coro) -> "asyncio.Task":
    """Lance une tâche détachée en la protégeant du ramasse-miettes."""
    task = asyncio.create_task(coro)
    _detached_tasks.add(task)
    task.add_done_callback(_detached_tasks.discard)
    return task


def _log_prediction_pairs(
    client:      httpx.AsyncClient,
    predictions: Dict[str, Dict[str, Any]],
) -> None:
    """
    Journalise, pour analyse HORS LIGNE, le couple (mesuré, prédit) de chaque
    métrique de chaque VM à l'instant où la décision va les utiliser.

    Pourquoi ici et pas dans la feuille Métriques : celle-ci est écrite à
    l'étape de persistance, AVANT que les prédictions du cycle existent. Le
    couple ne peut donc être constitué qu'après cette étape-ci.

    ⚠️ Sans attente volontaire (`create_task`, hors de tout `prof.step`) :
    cet appel ne doit ni allonger le cycle, ni polluer les mesures de temps
    de ce même cycle. Un échec est sans conséquence — c'est de la trace, pas
    un chemin de décision.
    """
    ts   = datetime.now(timezone.utc).isoformat()
    rows: List[list] = []
    for lc in state.last_collected:
        vm_id = lc.get("vm_id")
        preds = predictions.get(vm_id) or {}
        for metric in config.METRICS_REGISTRY:
            block     = preds.get(metric) or {}
            series    = block.get("predictions") or []
            predicted = series[0] if series else None
            rows.append([
                ts, state.flow_cycle, vm_id, metric,
                lc.get(metric), predicted, block.get("model"),
            ])

    if not rows:
        return

    async def _send() -> None:
        # Client DÉDIÉ, et non celui du cycle : l'appelant l'expose dans un
        # `async with` qui se referme à la fin du cycle, alors que cette tâche
        # détachée lui survit. Mesuré : avec le client du cycle, seuls 116
        # cycles sur 411 étaient journalisés — les autres mouraient sur un
        # client déjà fermé, silencieusement.
        try:
            async with httpx.AsyncClient() as c:
                await _post(c, f"{_URLS['database']}/store/predictions",
                            {"rows": rows})
        except Exception as exc:
            logger.debug(f"Journalisation mesuré/prédit ignorée : {exc}")

    _spawn(_send())


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


async def _step8_decide(
    client: httpx.AsyncClient,
    ctx: _FlowContext,
    prof: StepProfiler,
    violation_detected: bool = False,
) -> None:
    """
    Appelle decision_intelligence et exécute la migration si nécessaire.

    Aiguillage sur l'interrupteur multi-provider de shared/config.py (OFF par
    défaut) : le chemin mono-provider est le code d'origine, déplacé tel quel
    dans _decide_mono_provider — zéro changement de comportement tant que
    l'interrupteur n'est pas activé explicitement.

    `violation_detected` (défaut False, volontaire — sûr pour tout appelant
    qui ne le fournirait pas, notamment les tests existants) propage la gate
    calculée par _step5_check_violations jusqu'à _decide_federated, qui ne
    déclenche la fédération (broadcast + arbitrage) que sur violation.
    """
    # ── Garde-fou ACTIF / STANDBY ────────────────────────────────────
    # Un seul orchestrateur héberge le service (vérité : kubectl). Seul
    # celui-là décide et migre ; les autres se contentent d'observer et de
    # répondre aux requêtes du relais. Sans ce garde-fou, N orchestrateurs
    # déclencheraient N migrations kubectl concurrentes sur le même pod.
    # Neutre quand PROVIDER_ID == "all" (mode mono-processus historique).
    if config.PROVIDER_ID != "all" and not state.is_active:
        logger.info(
            f"🟡 STANDBY — service hébergé par "
            f"{C.CYAN}{state.hosting_vm}{C.RESET} — aucune décision prise"
        )
        state.last_decision = {
            "decision": "stay", "from_vm": None, "to_vm": None,
            "reason": f"STANDBY — service hébergé par {state.hosting_vm}",
            "topsis_score": None, "breach_type": None,
        }
        return

    current_data = _build_candidates(state.last_collected)
    if not config.MULTI_PROVIDER_ENABLED:
        await _decide_mono_provider(client, ctx, prof, current_data)
        return
    await _decide_federated(client, ctx, prof, violation_detected)


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
    _spawn(
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
    _spawn(
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
#  Décision fédérée (lot 6a) — broadcast + arbitre externe
# ─────────────────────────────────────────────────────────────────────────────

def _post_federated_audit(
    di_res:      Dict[str, Any],
    all_bids:    List[Dict[str, Any]],
    peer_errors: List[Dict[str, Any]],
    verdict:     Optional[Dict[str, Any]],
    my_provider: Optional[str],
    vm_active:   Optional[str],
    own_bid:     ProviderBid,
) -> None:
    """
    Poste l'audit du cycle fédéré à observability. Reprend la structure
    existante (cycle/mode/violated_metrics/slos_active/mi_scores/
    current_metrics) de _finalize_multi_provider_decision pour que le
    dashboard continue de fonctionner sans modification.

    Le bloc "reasoning" est à DOUBLE lecture :
      • les clés HISTORIQUES attendues par le panneau RAISONNEMENT du
        dashboard — provider_courant/vm_active/evaluations/compliant_vms/
        negotiation/topsis (voir _build_reasoning, chemin multi-provider
        d'origine, réutilisée ici telle quelle) — le hub s'adapte au
        dashboard, jamais l'inverse ;
      • les clés FÉDÉRÉES du lot 6a — federated/bids/considered/alert/
        peer_errors — CONSERVÉES telles quelles.

    L'évaluation locale (assessment) est recalculée ICI via evaluate_provider
    — fonction PURE, sans I/O, hub/provider_arbitration.py — avec exactement
    les mêmes entrées que celles utilisées pour construire `own_bid`
    (_build_local_bid, non modifiée) : rappel déterministe, sans effet de
    bord, _build_local_bid n'a pas besoin d'exposer son assessment interne.

    `vm_active` doit être la VM de service CAPTURÉE AVANT une éventuelle
    migration (l'appelant la fige tôt, voir _decide_federated) : au moment
    où cette fonction s'exécute, state.service_vm peut déjà avoir été
    réécrite par la migration qui précède cet appel dans le chemin fédéré.

    `verdict` peut être None (arbitre indisponible, étape ⑧) : le bloc
    reasoning reste alors PARTIEL — evaluations/compliant_vms renseignés,
    mais topsis/negotiation retombent sur None (rien à raconter côté
    verdict) — et provider_path/provider_used retombent aussi sur None.
    Toute la construction du bloc "reasoning" est protégée par un
    try/except : une erreur ici ne doit JAMAIS empêcher l'audit d'être
    posté ni interrompre le cycle (même garde que
    _finalize_multi_provider_decision).
    """
    try:
        candidates  = _build_candidates(state.last_collected)
        predictions = state.snapshot_predictions
        assessment  = evaluate_provider(my_provider, state.current_slos, candidates, predictions)

        if verdict is not None:
            offre_locale = {
                "provider_id":     my_provider,
                "vm_id":           own_bid.placement_plan.vm_id,
                "violation_score": own_bid.gap_grade.value,
            }
            winner_provider = verdict.get("winner_provider")
            # L'offre reçue n'a de sens que si le gagnant n'est pas nous-même —
            # sinon il n'y a rien eu à "recevoir", c'est notre propre bid qui gagne.
            offre_recue = None
            if winner_provider and winner_provider != my_provider:
                winning_bid = next(
                    (b for b in all_bids
                     if isinstance(b, dict) and b.get("provider_id") == winner_provider),
                    None,
                )
                if winning_bid:
                    offre_recue = {
                        "provider_id":     winner_provider,
                        "vm_id":           (winning_bid.get("placement_plan") or {}).get("vm_id"),
                        "violation_score": (winning_bid.get("gap_grade") or {}).get("value"),
                    }

            negotiation_data: Optional[Dict[str, Any]] = {
                "offre_locale":   offre_locale,
                "offre_recue":    offre_recue,
                "deadband":       verdict.get("deadband_applied"),
                "decision":       verdict.get("decision"),
                "provider_cible": winner_provider,
            }
            # Seul le classement TOPSIS de NOTRE bid : celui des pairs ne
            # traverse jamais la frontière décisionnelle (voir ProviderBid).
            topsis_data: Optional[Dict[str, Any]] = {
                "classement": own_bid.placement_plan.vm_scores or {},
                "retenue":    verdict.get("winner_vm"),
                "score":      own_bid.placement_plan.topsis_score,
            }
        else:
            negotiation_data = None
            topsis_data      = None

        reasoning: Optional[Dict[str, Any]] = {
            **_build_reasoning(
                my_provider, assessment, negotiation_data, topsis_data, vm_active=vm_active,
            ),
            "federated":   True,
            "bids":        all_bids,
            "considered":  verdict.get("considered") if verdict else None,
            "alert":       verdict.get("alert") if verdict else None,
            "peer_errors": peer_errors,
        }
    except Exception as exc:
        logger.warning(f"⚠️  Construction du bloc reasoning (audit fédéré) échouée : {exc}")
        reasoning = None

    audit_payload = {
        **di_res,
        "cycle":            state.cycle_count,
        "mode":             state._mode,
        "violated_metrics": di_res.get("violated_metrics", []),
        "slos_active":      state.current_slos,
        "mi_scores":        state.last_mi_scores,
        # Archivé pour permettre le REJEU des phases TOPSIS sur un cycle passé
        # (services/federation_view) : TOPSIS travaille sur les prédictions, pas
        # sur les mesures. Le chemin mono-provider expose déjà cette clé — même nom,
        # même forme, aucune nouveauté pour observability.
        "predictions_map":  state.snapshot_predictions,
        "current_metrics":  {
            lc["vm_id"]: {
                "latency":      lc.get("latency"),
                "cpu_usage":    lc.get("cpu_usage"),
                "ram_usage":    lc.get("ram_usage"),
                "total_cores":  lc.get("total_cores"),
                "total_ram_gb": lc.get("total_ram_gb"),
            } for lc in state.last_collected
        },
        "provider_path": verdict.get("path") if verdict else None,
        "provider_used": verdict.get("winner_provider") if verdict else None,
        "reasoning":     reasoning,
    }
    _spawn(
        _post_audit(f"{_URLS['observability']}/audit", audit_payload)
    )


async def _decide_federated(
    client:             httpx.AsyncClient,
    ctx:                _FlowContext,
    prof:               StepProfiler,
    violation_detected: bool,
) -> None:
    """
    Chemin fédéré (interrupteur MULTI_PROVIDER_ENABLED=true, lot 6a) : diffuse
    les SLOs à tous les pairs (relais /broadcast, lot 4), collecte leurs
    ProviderBid (le nôtre compris, calculé en local — lot 3), et fait
    trancher un microservice arbitre EXTERNE (placement_arbiter /arbitrate,
    lot 5). LE HUB NE DÉCIDE JAMAIS SEUL : si l'arbitre ne répond pas, la
    décision par défaut est STAY, jamais une migration à l'aveugle.

    Ne s'active QUE sur violation primaire détectée sur la VM active
    (`violation_detected`, propagé depuis _step5_check_violations) — c'est
    la gate qui évite de payer N appels HTTP à CHAQUE cycle alors que le cas
    majoritaire est « tout va bien » (calibrage θ=40 ms du temps de cycle).
    """
    # ── ① Cooldown — même garde que _decide_multi_provider ───────────────
    if state.check_cooldown():
        elapsed   = time.monotonic() - state.last_migration_ts
        remaining = config.MIGRATION_COOLDOWN_S - elapsed
        logger.warning(
            f"⏳ Cooldown actif — évaluation fédérée ignorée "
            f"({C.YELLOW}{remaining:.0f}s{C.RESET} restante(s))"
        )
        state.last_decision = {
            "decision": "stay", "from_vm": None, "to_vm": None,
            "reason": "Cooldown active", "topsis_score": None, "breach_type": None,
        }
        return

    # ── ② La gate — cœur de la sobriété du chemin fédéré ─────────────────
    # Sans elle, on déclencherait N appels HTTP à CHAQUE cycle (broadcast +
    # arbitrage) alors que le cas majoritaire est « tout va bien » — la
    # fédération ne s'active QUE sur violation primaire.
    if not violation_detected:
        state.last_decision = {
            "decision": "stay", "from_vm": None, "to_vm": None,
            "reason": "Aucune violation primaire",
            "topsis_score": None, "breach_type": None,
        }
        return

    # ── ③ Identités ────────────────────────────────────────────────────
    incumbent_vm       = state.service_vm
    incumbent_provider = config.PROVIDER_OF_VM.get(incumbent_vm)
    my_provider        = (
        config.PROVIDER_ID if config.PROVIDER_ID != "all" else incumbent_provider
    )
    intent_id = f"cycle-{state.cycle_count}"

    if incumbent_provider is None:
        # VM active hors registre : jamais planter, même repli que
        # _decide_multi_provider.
        logger.warning(
            f"⚠️  VM active '{incumbent_vm}' absente de PROVIDER_OF_VM — "
            f"repli sur la décision mono-provider pour ce cycle"
        )
        current_data = _build_candidates(state.last_collected)
        await _decide_mono_provider(client, ctx, prof, current_data)
        return

    # ── ④+⑤ Bid local ET broadcast — EN PARALLÈLE ────────────────────────
    # Deux opérations INDÉPENDANTES : _build_local_bid interroge
    # decision_intelligence et NE MODIFIE AUCUN CHAMP de state ; le broadcast
    # envoie state.current_slos au relais. Aucune n'a besoin du résultat de
    # l'autre. Les exécuter concurremment économise la durée de la plus courte.
    # Même motif que _run_flow, qui parallélise déjà SLOs et collecte — un seul
    # httpx.AsyncClient supporte des requêtes concurrentes.
    #
    # ⚠️ SEUL CHANGEMENT DE COMPORTEMENT, ASSUMÉ : si le bid local échoue, le
    # broadcast est désormais DÉJÀ parti — les pairs auront calculé un bid pour
    # rien. Sans danger : aucune décision n'est prise, on sort en STAY comme
    # avant. La sémantique d'erreur est strictement préservée.
    async def _local_bid_branch():
        with prof.step("local_bid"):
            return await _build_local_bid(
                client, state.current_slos, incumbent_vm, intent_id, prof
            )

    async def _broadcast_branch():
        with prof.step("broadcast"):
            return await _post(
                client, f"{config.PROVIDER_RELAY_SERVICE_URL}/broadcast",
                {
                    "slos":          state.current_slos,
                    "intent_id":     intent_id,
                    "incumbent_vm":  incumbent_vm,
                    "from_provider": my_provider,
                },
            )

    with prof.step("bid_and_broadcast_parallel"):
        own_bid, resp = await asyncio.gather(
            _local_bid_branch(), _broadcast_branch(), return_exceptions=True
        )

    # Le bid local est INDISPENSABLE : sans lui, l'initiateur ne participe pas à
    # sa propre enchère. Son échec reste fatal au tour, exactement comme avant.
    if isinstance(own_bid, BaseException):
        logger.error(f"❌ Échec de la construction du bid local : {own_bid}")
        state.last_decision = {
            "decision": "stay", "from_vm": None, "to_vm": None,
            "reason": f"Échec du bid local — STAY par sécurité ({own_bid})",
            "topsis_score": None, "breach_type": None,
        }
        return

    # Le broadcast, lui, n'est PAS fatal (comportement inchangé) : une exception
    # est traitée exactement comme un relais injoignable.
    if isinstance(resp, BaseException):
        logger.warning(f"⚠️  Broadcast en erreur : {resp}")
        resp = None

    if resp:
        peer_bids   = resp.get("bids", [])
        peer_errors = resp.get("errors", [])
    else:
        # Relais injoignable N'EST PAS fatal : on poursuit avec notre seul
        # bid — le système doit continuer à décider en local si la
        # fédération est coupée.
        logger.warning(
            "⚠️  Relais /broadcast injoignable — poursuite avec le seul bid local"
        )
        peer_bids   = []
        peer_errors = [{"provider_id": "*", "error": "relais injoignable"}]

    # ── ⑥ Fusion — notre bid TOUJOURS en tête ────────────────────────────
    # L'initiateur participe à sa propre enchère, il n'est pas qu'un
    # collecteur des offres des autres.
    all_bids = [own_bid.to_dict()] + peer_bids

    # ── ⑦ Arbitrage ───────────────────────────────────────────────────────
    with prof.step("arbitrate"):
        verdict = await _post(
            client, f"{config.PLACEMENT_ARBITER_SERVICE_URL}/arbitrate",
            {
                "bids":               all_bids,
                "incumbent_provider": incumbent_provider,
                "incumbent_vm":       incumbent_vm,
            },
        )

    # ── ⑧ Arbitre indisponible — SÉCURITÉ MAXIMALE ───────────────────────
    # LE HUB NE DÉCIDE JAMAIS SEUL. Si l'arbitre ne répond pas (ou répond
    # n'importe quoi), la décision par défaut est STAY — jamais une
    # migration à l'aveugle.
    if not isinstance(verdict, dict) or "decision" not in verdict:
        logger.error(
            "❌ Arbitre indisponible ou réponse invalide — "
            "STAY par sécurité (le hub ne décide jamais seul)"
        )
        di_res = {
            "decision": "stay", "from_vm": None, "to_vm": None,
            "reason": "Arbitre indisponible — STAY par sécurité",
            "topsis_score": None, "breach_type": None,
        }
        state.last_decision = di_res
        _post_federated_audit(
            di_res, all_bids, peer_errors, verdict=None,
            my_provider=my_provider, vm_active=incumbent_vm, own_bid=own_bid,
        )
        return

    # ── ⑨ Application du verdict ─────────────────────────────────────────
    decision  = verdict.get("decision")
    winner_vm = verdict.get("winner_vm")
    path      = verdict.get("path")

    di_res = {
        "decision":     decision,
        "from_vm":      state.service_vm if decision == "migrate" else None,
        "to_vm":        winner_vm,
        "reason":       verdict.get("reason"),
        # Volontairement None : les scores TOPSIS ne sont pas comparables
        # entre providers et n'ont AUCUN rôle dans cette décision.
        "topsis_score": None,
        "breach_type":  "federated",
    }
    state.last_decision = di_res

    logger.info(
        f"⚖️  Cycle #{state.cycle_count} — fédéré "
        f"| chemin {C.CYAN}{path}{C.RESET} "
        f"| gagnant {C.CYAN}{verdict.get('winner_provider')}{C.RESET}/{C.GREEN}{winner_vm}{C.RESET} "
        f"| gap_grade={C.CYAN}{verdict.get('gap_grade')}{C.RESET} "
        f"| bids reçus={C.CYAN}{len(all_bids)}{C.RESET} "
        f"| erreurs pairs={C.YELLOW}{len(peer_errors)}{C.RESET}"
    )

    if decision == "migrate" and winner_vm and winner_vm != state.service_vm:
        logger.info(
            f"\n{'═'*60}\n"
            f"  🎯 DÉCISION : {C.BOLD}{C.YELLOW}MIGRATION{C.RESET} (fédérée, chemin {path})\n"
            f"  {'Source':<16}: {C.RED}{state.service_vm}{C.RESET}\n"
            f"  {'Destination':<16}: {C.GREEN}{winner_vm}{C.RESET}\n"
            f"  {'Raison':<16}: {C.CYAN}{di_res['reason']}{C.RESET}\n"
            f"{'═'*60}"
        )

        with prof.step("store_decision"):
            await _post(client, f"{_URLS['database']}/store/decision", di_res)

        with prof.step("migration"):
            kubectl_ok = await _execute_kubectl_migration(
                client, state.service_vm, winner_vm
            )
        if not kubectl_ok:
            logger.warning(
                f"⚠️  Migration kubectl échouée — "
                f"état interne mis à jour malgré tout "
                f"({C.YELLOW}{state.service_vm}{C.RESET} → {C.GREEN}{winner_vm}{C.RESET})"
            )

        # ── Award (lot 7) : notifie le gagnant de la VM PRÉCISE retenue ──
        # OPTIMISATION, jamais une dépendance dure — protégée par un
        # try/except qui n'interrompt JAMAIS le cycle. On ne notifie que si
        # la migration kubectl a réellement réussi (on ne notifie pas d'un
        # placement qui n'a pas eu lieu), et jamais quand le gagnant est
        # nous-même (chemin A : rien à transmettre).
        winner_provider = verdict.get("winner_provider")
        if kubectl_ok and winner_provider and winner_provider != my_provider:
            try:
                award_payload: Dict[str, Any] = {
                    "target_provider": winner_provider,
                    "vm_id":           winner_vm,
                    "intent_id":       intent_id,
                    "from_provider":   my_provider,
                }
                # ── Réconciliation du contrat SLO ────────────────────
                # Le gagnant devient ACTIF : ce sont désormais SES SLOs qui
                # feront foi et qu'il diffusera au prochain tour fédéré. On
                # les lui transmet avec l'award pour qu'un pair ayant raté la
                # propagation de l'intention (réseau coupé) ne reprenne
                # JAMAIS le service avec un contrat périmé. Uniquement en
                # mode enhanced : en autonomous les SLOs sont recalculés
                # localement par le MI à chaque cycle, rien à transmettre.
                if state._mode == "enhanced":
                    award_payload["slo_contract"] = {
                        "slos":           state.current_slos,
                        "weights":        state.original_intent_weights,
                        "intent_version": state.intent_version,
                        "mode":           "enhanced",
                    }
                award_resp = await _post(
                    client, f"{config.PROVIDER_RELAY_SERVICE_URL}/award", award_payload,
                )
                # _post() capture déjà ses propres exceptions réseau et
                # renvoie None dans ce cas (voir son implémentation) ; le
                # relais /award, lui, répond toujours HTTP 200 avec
                # {"delivered": bool, ...} même en cas d'échec (voir §2.1
                # provider_relay/app.py) — c'est ce champ qui fait foi.
                if award_resp and award_resp.get("delivered"):
                    logger.info(
                        f"📨 Attribution transmise à {C.CYAN}{winner_provider}{C.RESET} "
                        f"→ {C.GREEN}{winner_vm}{C.RESET}"
                    )
                else:
                    reason = (award_resp or {}).get("error", "relais local injoignable")
                    logger.warning(
                        f"⚠️  Attribution non transmise à {C.YELLOW}{winner_provider}{C.RESET} : "
                        f"{reason} — le pair retombera sur la découverte kubectl"
                    )
            except Exception as exc:
                logger.warning(
                    f"⚠️  Attribution non transmise à {C.YELLOW}{winner_provider}{C.RESET} : {exc} "
                    "— le pair retombera sur la découverte kubectl"
                )

        # ── Démission du cédant (lot 9) — symétrique de l'award ──────────
        # L'award PROMEUT le gagnant ; sans ce bloc, rien ne DÉMET le
        # cédant : is_active ne repasse à False que dans _sync_active_vm,
        # soit jusqu'à ACTIVE_VM_SYNC_EVERY_N_CYCLES cycles plus tard. Dans
        # cette fenêtre, les deux orchestrateurs se croient actifs et
        # peuvent migrer le MÊME service en concurrence.
        #
        # INCONDITIONNEL, y compris si l'award n'a pas été délivré : entre
        # « deux actifs » (opérations kubectl concurrentes, ping-pong) et
        # « deux standby » (personne ne décide pendant quelques cycles, le
        # service continue de tourner, kubectl rétablit au prochain sync),
        # le second est strictement moins dangereux. Même philosophie que
        # la garde arbitre-indisponible : on préfère l'inaction au conflit.
        if kubectl_ok and winner_provider and winner_provider != my_provider:
            state.is_active  = False
            state.hosting_vm = winner_vm
            logger.info(
                f"🔻 Service cédé à {C.CYAN}{winner_provider}{C.RESET} — "
                f"passage en {C.YELLOW}STANDBY{C.RESET} "
                f"(VM hôte : {C.GREEN}{winner_vm}{C.RESET})"
            )

        state.service_vm        = winner_vm
        state.last_migration_ts = time.monotonic()
        logger.info(
            f"✅ Migration effectuée — "
            f"nouvelle VM active : {C.GREEN}{C.BOLD}{winner_vm}{C.RESET} "
            f"| kubectl : {'OK' if kubectl_ok else C.RED+'ÉCHEC'+C.RESET}"
        )
    else:
        logger.info(
            f"🟢 Décision : {C.GREEN}MAINTIEN{C.RESET} sur {C.CYAN}{state.service_vm}{C.RESET} "
            f"| raison : {di_res['reason']}"
        )

    # ── ⑩ Audit → observability ──────────────────────────────────────────
    _post_federated_audit(
        di_res, all_bids, peer_errors, verdict=verdict,
        my_provider=my_provider, vm_active=incumbent_vm, own_bid=own_bid,
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

    # Coût de la fédération. bid_and_broadcast_parallel est la DURÉE RÉELLE du
    # bloc concurrent (donc ≈ max(local_bid, broadcast), pas leur somme) ; on
    # lui ajoute l'arbitrage, qui est séquentiel après lui. Vaut None sur un
    # cycle non fédéré, ce qui distingue « pas de négociation » de « 0 ms ».
    _fed_parts = [d.get("bid_and_broadcast_parallel"), d.get("arbitrate")]
    fed_total = (round(sum(p for p in _fed_parts if p is not None), 3)
                 if any(p is not None for p in _fed_parts) else None)

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
        # Hôte RÉEL du service dans la fédération, lu depuis l'infrastructure.
        # `vm` est la vue locale de ce provider et reste figée tant qu'il est
        # STANDBY ; sans cette colonne, deux intentions consécutives semblent
        # partir de la même VM alors que le service a changé d'hôte.
        "hosting_vm":          state.hosting_vm or vm,
        "decision":            decision,
        "to_vm":               state.last_decision.get("to_vm"),
        # Hub
        "check_violations":    d.get("check_violations"),
        "migration":           d.get("migration"),
        "hub_total":           hub_total,
        # Collector
        # Les colonnes par VM sont dérivées du registre de CE processus
        # (config.VM_REGISTRY), jamais écrites en dur : avec 3 edges + 1 cloud
        # par provider, une liste figée edge1/edge2/cloud1/cloud2 laissait
        # edge1b et edge1c sans aucune colonne, et remplissait de vides les
        # deux colonnes appartenant à l'autre provider.
        **{f"collect_{vm_id}": d.get(f"collect_{vm_id}")
           for vm_id in config.VM_REGISTRY},
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
        # Fédération — mesurée par _decide_federated, jamais exportée jusqu'ici.
        # Vide sur les chemins mono/multi, ce qui est l'information voulue :
        # une colonne vide signifie « ce cycle n'a pas négocié ».
        "local_bid":                 d.get("local_bid"),
        "broadcast":                 d.get("broadcast"),
        "bid_and_broadcast_parallel": d.get("bid_and_broadcast_parallel"),
        "arbitrate":                 d.get("arbitrate"),
        "federation_total":          fed_total,
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
            _spawn(timing_writer_enhanced.write(row))
            logger.info(
                f"📊 Mesures enregistrées (intention {C.CYAN}{pit.get('intent_id')}{C.RESET}) "
                f"| réception → migration : {C.GREEN}{row['intention_total']} ms{C.RESET}"
            )
        # cycle enhanced sans migration → on conserve l'intention en attente
    elif mode == "autonomous":
        row = dict(base)
        row.update({
            "cycle":     state.flow_cycle,
            "timestamp": ts,
            "total":     d.get("total"),
        })
        _spawn(timing_writer_autonomous.write(row))
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
        # Fige le numéro de CE cycle : tout ce qui est journalisé plus bas
        # (temps, prédictions) doit porter la même étiquette, même si de
        # nouveaux lots arrivent et incrémentent le compteur entre-temps.
        state.flow_cycle = state.cycle_count

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
                    violation_detected = _step5_check_violations(ctx)   # après prédictions — log cohérent

                # Synchronisation kubectl paresseuse : appeler openstack_client coûte
                # cher (sous-processus kubectl sur le master). On ne paie ce coût que
                # lorsqu'il change quelque chose :
                #   • une violation est détectée → il faut savoir si l'on est ACTIF
                #     avant de décider quoi que ce soit ;
                #   • ou tous les N cycles → permet à un STANDBY de découvrir qu'il
                #     vient de recevoir le service après une migration inter-provider
                #     (sans ce battement, la découverte du rôle serait circulaire).
                if config.PROVIDER_ID != "all":
                    heartbeat = (
                        state.cycle_count % config.ACTIVE_VM_SYNC_EVERY_N_CYCLES == 0
                    )
                    if violation_detected or heartbeat:
                        with prof.step("sync_active_vm"):
                            await _sync_active_vm(client)

                with prof.step("decision_total"):
                    await _step8_decide(client, ctx, prof, violation_detected)

        _persist_timing(prof, mode, vm_for_row)

        # Seul le hub ACTIF pousse : le bridge PiCar reçoit ainsi une source
        # de vérité unique, sans avoir à départager deux réponses de polling.
        if state.is_active:
            _spawn(_push_state_to_picar())

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
    _spawn(_run_flow(payload.measurements, state._mode))
    return {"status": "accepted", "cycle": state.cycle_count}


@app.post("/intent", status_code=status.HTTP_200_OK)
async def receive_intent(payload: Dict[str, Any] = Body(...)):
    # ── Versionnement ────────────────────────────────────────────────
    # Le premier hub à recevoir l'intention (appelé par intent_manager) lui
    # assigne sa version ; la propagation la transporte ensuite telle quelle.
    incoming_version = payload.get("intent_version")
    if incoming_version is None:
        incoming_version = time.time()
    # Une intention en retard (propagation lente, pair qui rattrape après une
    # coupure) ne doit JAMAIS écraser une intention plus récente déjà appliquée.
    if state.intent_version is not None and incoming_version < state.intent_version:
        logger.warning(
            f"⏮️  Intention périmée ignorée "
            f"(version {incoming_version:.3f} < {state.intent_version:.3f}) — "
            f"SLOs courants conservés"
        )
        return {"status": "stale", "mode": state._mode, "slos": len(state.current_slos)}

    state.current_slos = payload.get("slos", [])
    # Capture les poids originaux du LLM pour chaque SLO primaire — base
    # fixe utilisée à chaque cycle suivant pour éviter la dilution
    # cumulative (voir restauration dans _run_flow, étape SLOs).
    state.original_intent_weights = {
        s["metric"]: s.get("weight", 1.0)
        for s in state.current_slos
        if s.get("is_primary", False)
    }
    state._mode          = "enhanced"
    state.intent_version = incoming_version
    # Ecrit APRES que le LLM ait deja tourne : au moment de son appel, le hub
    # contenait encore l'intention precedente — c'est precisement ce dont il
    # a besoin. On ne remplace que si le champ est fourni, pour ne pas
    # effacer la memoire lors d'un appel programmatique sans texte.
    _texte = payload.get("intention")
    if isinstance(_texte, str) and _texte.strip():
        state.last_intention_text = _texte.strip()
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

    # ── Propagation aux pairs (fire-and-forget) ──────────────────────
    # Les SLOs sont un contrat GLOBAL : un client ne sait pas quel provider
    # héberge le service (transparence à la localisation), il peut donc
    # envoyer son intention à n'importe lequel — y compris un STANDBY. Sans
    # cette propagation, le hub qui décide ne connaîtrait jamais le contrat.
    #
    # ⚠️ ANTI-BOUCLE : `propagate` vaut False sur le chemin entrant (voir
    # provider_relay /inbound/intent), sinon A propagerait à B qui
    # repropagerait à A indéfiniment.
    if payload.get("propagate", True) and config.PROVIDER_ID != "all":
        async def _propagate() -> None:
            try:
                async with httpx.AsyncClient() as c:
                    await c.post(
                        f"{config.PROVIDER_RELAY_SERVICE_URL}/intent/propagate",
                        json={
                            **payload,
                            "intent_version": incoming_version,
                            "from_provider":  config.PROVIDER_ID,
                        },
                        timeout=5.0,
                    )
            except Exception as exc:
                logger.warning(f"⚠️  Intention non propagée aux pairs : {exc}")
        _spawn(_propagate())

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


async def _build_local_bid(
    client:       httpx.AsyncClient,
    slos:         List[Dict[str, Any]],
    incumbent_vm: Optional[str],
    intent_id:    Optional[str] = None,
    prof:         Optional[StepProfiler] = None,
) -> ProviderBid:
    """
    Construit le BID unifié (PlacementPlan + Gap Grade) de CE provider pour
    un jeu de SLOs donné — la primitive d'échange de l'arbitrage N-providers
    (lot 5). Chaque orchestrateur élit LUI-MÊME la meilleure de SES VMs
    (TOPSIS si des VMs conformes existent, Gap Grade minimal sinon) et
    calcule LUI-MÊME son Gap Grade : les scores TOPSIS ne sont PAS
    comparables entre providers (normalisés min-max à l'intérieur de leur
    pool), le Gap Grade l'est (normalisé par le seuil SLO, global et
    partagé — voir hub/provider_arbitration.py).

    Extrait du corps de POST /evaluate (lot 3, ~lignes 1734-1896 avant ce
    lot) pour être appelable DIRECTEMENT par le cycle fédéré
    (_decide_federated, lot 6a) sans repasser par HTTP — le hub n'a aucune
    raison de s'appeler lui-même sur le réseau. `client` est fourni par
    l'appelant (jamais créé ici) pour rester dans le même cycle de vie HTTP
    que le reste de l'appelant.

    ⚠️ NE LÈVE JAMAIS d'HTTPException : les gardes d'entrée (slos absent,
    last_collected vide) restent dans l'endpoint /evaluate, AVANT l'appel à
    cette fonction. NE MODIFIE AUCUN CHAMP de `state`.
    """
    # ① Candidats — mêmes primitives que /intent/relay, aucune mutation de state.
    mon_provider = (
        config.PROVIDER_ID if config.PROVIDER_ID != "all"
        else config.PROVIDER_OF_VM.get(state.service_vm)
    )
    candidates  = _build_candidates(state.last_collected)
    owned       = candidates_for_provider(mon_provider, candidates)
    predictions = state.snapshot_predictions

    # ② Conformité — UNIQUEMENT compliant_vms/evaluable. best_effort_vm/score
    #    viennent de l'ANCIENNE formule (_excess, clampée à 0 dès qu'une VM
    #    est conforme) : les mélanger au nouveau Gap Grade signé produirait
    #    deux classements incohérents dans le même bid.
    assessment = evaluate_provider(mon_provider, slos, candidates, predictions)

    champion:     Optional[str]    = None
    topsis_score: Optional[float]  = None
    vm_scores:    Dict[str, float] = {}
    reason:       str              = ""

    # ③ Champion
    if assessment.compliant_vms:
        # CAS A — TOPSIS sur les VMs conformes. Montage IDENTIQUE à
        # /intent/relay, correctif incumbent_vm compris (voir son
        # commentaire détaillé là-bas : déclarer une VM CONFORME comme
        # service_vm court-circuite decision.py avant TOPSIS).
        restricted = [c for c in candidates if c["vm_id"] in assessment.compliant_vms]
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
            "slos":               slos,
            # Ce provider n'héberge pas encore le service ici : aucun cooldown
            # à respecter pour une prise en charge initiale.
            "service_vm":         decide_service_vm,
            "cooldown_active":    False,
            "reliability_scores": {
                lc["vm_id"]: lc.get("reliability", 1.0) for lc in state.last_collected
            },
            "cycle":              state.cycle_count,
            "mi_scores":          state.last_mi_scores,
        }
        # `decide_call` = durée TOTALE de l'appel au service de décision. Les
        # chemins mono/multi la mesurent chez eux ; sans ce wrapper, le chemin
        # fédéré laissait « Total DI » vide — et « Overhead HTTP+logs », qui
        # s'en déduit, vide aussi.
        _di_ctx = prof.step("decide_call") if prof is not None else nullcontext()
        with _di_ctx:
            di_res = await _post(
                client, f"{_URLS['decision_intelligence']}/decide", di_payload
            )

        # Fusionne les sous-mesures TOPSIS / détection renvoyées par
        # decision_intelligence. Sans cette ligne, le CHEMIN FÉDÉRÉ perd
        # silencieusement les 9 colonnes du bloc Decision Intelligence
        # (détection, filtrage, les 4 phases TOPSIS, totaux) — elles sont
        # calculées puis jetées, sans aucune erreur visible. Les chemins mono
        # et multi font déjà ce merge chez eux ; ici `prof` est optionnel car
        # l'endpoint /evaluate appelle cette fonction sans profileur.
        if prof is not None and di_res:
            prof.merge(di_res.get("timings"))

        if di_res and di_res.get("to_vm"):
            champion     = di_res["to_vm"]
            topsis_score = di_res.get("topsis_score")
            vm_scores    = di_res.get("vm_scores") or {}
            reason       = di_res.get("reason") or ""
        else:
            champion = assessment.compliant_vms[0]
            reason   = (
                "decision_intelligence indisponible ou sans candidat — "
                "repli sur la première VM conforme"
            )

    elif not assessment.evaluable or not owned:
        # CAS C — rien d'évaluable pour ce provider, ou aucun candidat.
        reason = "provider non évaluable (aucune métrique disponible) ou aucun candidat"

    else:
        # CAS B — aucune VM conforme : le provider ne propose AUCUNE VM
        # (règle métier : un provider ne peut proposer qu'une VM conforme,
        # jamais une offre "moins pire" non conforme — champion reste None).
        reason = "aucune VM conforme chez ce provider — aucune offre transmise"

    # ④ Valeurs du champion
    champion_candidate = next((c for c in candidates if c["vm_id"] == champion), None) or {}
    values = slo_values_for_vm(champion, slos, champion_candidate, predictions) if champion else {}

    # ⑤ Gap Grade
    is_compliant = champion in assessment.compliant_vms
    gap = build_gap_grade(slos, values, is_compliant, assessment.evaluable)

    # ⑥ Assemblage
    plan = PlacementPlan(
        provider_id  = mon_provider,
        vm_id        = champion,
        action       = placement_action(champion, incumbent_vm),
        topsis_score = topsis_score,
        vm_scores    = vm_scores,
        reason       = reason,
    )
    bid = ProviderBid(
        provider_id    = mon_provider,
        intent_id      = intent_id,
        placement_plan = plan,
        gap_grade      = gap,
        timestamp      = datetime.now(timezone.utc).isoformat(),
    )

    logger.info(
        f"📮 bid local — provider={C.CYAN}{mon_provider}{C.RESET} "
        f"| champion={C.GREEN}{champion}{C.RESET} "
        f"| gap_grade={C.CYAN}{gap.value}{C.RESET} "
        f"| conforme={(C.GREEN if is_compliant else C.YELLOW)}{is_compliant}{C.RESET}"
    )

    return bid


@app.post("/evaluate", status_code=status.HTTP_200_OK)
async def evaluate(payload: Dict[str, Any] = Body(...)):
    """
    Produit le BID unifié (PlacementPlan + Gap Grade) de CE provider pour un
    jeu de SLOs donné (voir _build_local_bid, qui porte toute la logique).

    ⚠️ UNE SEULE ÉCRITURE AUTORISÉE : l'adoption du contrat SLO reçu, et
    uniquement quand ce provider est STANDBY (voir le bloc ci-dessous). Cet
    endpoint NE DÉCLENCHE AUCUNE décision, AUCUNE migration, AUCUN appel
    kubectl — c'était le but de l'invariant d'origine, et il est préservé.
    Adopter le contrat aligne simplement la vue du pair sur celle de
    l'initiateur, ce que le broadcast fait déjà de fait au moment de décider.

    Depuis le lot 6a, ce n'est plus le SEUL appelant de _build_local_bid :
    le cycle fédéré (_decide_federated) l'appelle aussi, directement, sans
    passer par cet endpoint HTTP (le hub n'a aucune raison de s'appeler
    lui-même sur le réseau). Cet endpoint reste utile pour les appels
    EXTERNES (relais /broadcast → /inbound/evaluate → ICI, ou un curl de test).
    """
    slos: List[Any] = payload.get("slos") or []
    if not slos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'slos' absent ou vide",
        )

    if not state.last_collected:
        logger.warning("⚠️  /evaluate — hub pas encore chaud (last_collected vide)")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Aucune mesure collectée pour l'instant (last_collected vide)",
        )

    # ── Adoption du contrat reçu (STANDBY uniquement) ────────────────
    # Le broadcast transporte le contrat COMPLET de l'initiateur : primaires ET
    # secondaires, chacun avec son `weight` et son drapeau `is_primary`. On
    # l'enregistre TEL QUEL — le drapeau est donc préservé, et le filtre de
    # conformité (primaires uniquement) continue de fonctionner à l'identique.
    #
    # Seul un STANDBY adopte : le provider ACTIF est la SOURCE du contrat, il ne
    # doit jamais se le faire écraser par le payload d'un pair.
    if config.PROVIDER_ID != "all" and not state.is_active:
        state.current_slos = slos
        # ⚠️ Les poids d'origine DOIVENT suivre le contrat adopté. Sans cette
        # ligne, ils restaient ceux de l'intention PRÉCÉDENTE, et le filtre de
        # _step1_slos — qui ne garde du contrat que les métriques présentes
        # dans original_intent_weights — renvoyait une liste VIDE dès que la
        # nouvelle intention portait sur d'autres métriques.
        # Privé de contrat LLM, le provider repartait alors en comportement
        # autonome : seuil de latence adaptatif par percentile, et promotion
        # d'un secondaire MI en primaire. Observé en campagne (UC4) : une
        # intention « maximum compute power » adoptée par broadcast produisait
        # `latency < 82` non primaire + `cpu_usage >= 1` primaire, c'est-à-dire
        # ni les métriques demandées, ni les seuils du LLM.
        # Les deux autres chemins d'adoption (/intent et /award) le faisaient
        # déjà ; seul le broadcast était resté sans protection.
        state.original_intent_weights = {
            s["metric"]: s.get("weight", 1.0)
            for s in slos
            if s.get("is_primary", False)
        }
        _prim = [s.get("metric") for s in slos if s.get("is_primary")]
        _sec  = [s.get("metric") for s in slos if not s.get("is_primary")]
        logger.info(
            f"📥 Contrat SLO adopté depuis le broadcast — "
            f"primaires : {C.GREEN}{_prim}{C.RESET} | "
            f"secondaires : {C.YELLOW}{_sec}{C.RESET}"
        )

    intent_id:    Optional[str] = payload.get("intent_id")
    incumbent_vm: Optional[str] = payload.get("incumbent_vm")

    async with httpx.AsyncClient() as client:
        bid = await _build_local_bid(client, slos, incumbent_vm, intent_id)

    return bid.to_dict()


@app.post("/award", status_code=status.HTTP_200_OK)
async def award(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Reçoit un message d'ATTRIBUTION (award, lot 7) : le provider qui vient
    de remporter l'arbitrage federé (voir _decide_federated) nous notifie de
    la VM PRÉCISE qui héberge désormais le service.

    Pourquoi c'est nécessaire : sans ce message, le gagnant ne redécouvre le
    placement qu'au prochain _sync_active_vm → kubectl — qui résout au NODE,
    pas à la VM (shared/config.py::VM_NODE_GROUP, lot 1b) et renvoie donc la
    VM CANONIQUE du node (ex. "edge2" pour "edge2b"/"edge2c", qui partagent
    pop1-worker-2). state.service_vm serait alors faux jusqu'à ce que kubectl
    livre par hasard la bonne réponse, et la détection de violation comme
    l'audit porteraient sur la mauvaise VM entre-temps.

    Garde ESSENTIELLE (②) : un pair ne doit JAMAIS pouvoir nous faire pointer
    vers une VM hors de notre périmètre — sans elle, un award malveillant ou
    buggé venu d'un autre provider écraserait notre service_vm avec une VM
    qui n'est pas la nôtre.

    Ne PAS confondre avec _sync_active_vm : cet endpoint ne le remplace pas,
    il l'anticipe. La règle du lot 1b (adoption seulement si le node diffère
    ou si l'on vient de passer STANDBY→ACTIF) conservera d'elle-même vm_id
    au prochain sync, puisque is_active est désormais vrai et que le node
    correspond déjà — c'est cet invariant qui rend l'award durable au lieu
    d'être écrasé par le sync suivant.
    """
    vm_id:         Optional[str] = payload.get("vm_id")
    intent_id:     Optional[str] = payload.get("intent_id")
    from_provider: Optional[str] = payload.get("from_provider")

    # ① VM inconnue du registre global.
    if vm_id not in config.PROVIDER_OF_VM:
        logger.warning(f"⚠️  /award — VM inconnue de PROVIDER_OF_VM : {vm_id}")
        return {"accepted": False, "reason": "vm inconnue"}

    # ② Garde de périmètre — un pair ne doit JAMAIS pouvoir nous faire
    # pointer vers une VM qui ne nous appartient pas.
    if config.PROVIDER_ID != "all" and config.PROVIDER_OF_VM[vm_id] != config.PROVIDER_ID:
        logger.warning(
            f"⚠️  /award — VM '{vm_id}' hors de mon périmètre "
            f"(provider {config.PROVIDER_OF_VM[vm_id]}, je suis {config.PROVIDER_ID})"
        )
        return {"accepted": False, "reason": "vm hors de mon perimetre"}

    # ③ Attribution acceptée.
    state.service_vm = vm_id
    state.hosting_vm = vm_id
    state.is_active   = True
    state.last_award_ts = time.monotonic()

    # ── Réconciliation du contrat SLO (voir _decide_federated) ───────
    # Filet de sécurité de la réplication : si l'on avait raté la
    # propagation de l'intention, on l'adopte ici — au moment précis où nos
    # SLOs passent d'inertes (standby) à décisifs (actif). Même règle de
    # version que /intent : jamais adopter un contrat plus ancien.
    contract = payload.get("slo_contract")
    if isinstance(contract, dict):
        version = contract.get("intent_version")
        if version is not None and (
            state.intent_version is None or version >= state.intent_version
        ):
            state.current_slos            = contract.get("slos") or []
            state.original_intent_weights = contract.get("weights") or {}
            state.intent_version          = version
            state._mode                   = contract.get("mode") or state._mode
            logger.info(
                f"📜 Contrat SLO adopté depuis l'award — mode {C.CYAN}{state._mode}{C.RESET} "
                f"| {C.CYAN}{len(state.current_slos)}{C.RESET} SLO(s)"
            )

    logger.info(
        f"🏆 Attribution reçue de {C.CYAN}{from_provider}{C.RESET} "
        f"(intent {C.CYAN}{intent_id}{C.RESET}) — "
        f"service placé sur {C.GREEN}{vm_id}{C.RESET}"
    )
    return {"accepted": True, "vm_id": vm_id}


@app.get("/data")
async def get_data():
    return state.get_data_payload()


@app.get("/status")
async def get_status():
    return {
        "mode":             state._mode,
        "service_vm":       state.service_vm,
        "role":             "active" if state.is_active else "standby",
        "hosting_vm":       state.hosting_vm,
        "cycle":            state.cycle_count,
        "bootstrap_active": state.cycle_count < state.BOOTSTRAP_MIN,
        "cooldown_active":  state.check_cooldown(),
        "slos_count":       len(state.current_slos),
        "active_slos":      state.current_slos,
        "last_intention":   state.last_intention_text,
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
        state.intent_version         = None
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