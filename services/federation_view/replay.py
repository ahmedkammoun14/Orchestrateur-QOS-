"""
services/federation_view/replay.py — Rejeu du TOPSIS de production sur un
cycle d'audit archivé, et mise en forme du Gap Grade d'un bid.

Module PUR : aucune I/O, aucun réseau, aucun état. Reçoit un dict "entrée
d'audit" (le payload posté par le hub à observability POST /audit, tel
qu'archivé dans son /audit/log — voir hub/orchestrator_core.py::
_post_federated_audit) et le rejoue.

⚠️ RÈGLE ABSOLUE : ne JAMAIS réimplémenter TOPSIS. Ce module importe et
appelle les méthodes RÉELLES de production (TopsisSelector), phase par
phase, pour que l'affichage ne puisse jamais diverger silencieusement du
calcul qui a réellement décidé — voir le contrôle de non-divergence en §3.3
du lot 8a (replay_topsis, section "concordance").
"""

import math
from typing import Any, Dict, List, Optional

from shared import config
from services.decision_intelligence.topsis import TopsisSelector, _CAPACITY_METRICS

# Instance unique et sans état (TopsisSelector est stateless) — on ne s'en
# sert que comme porteuse des méthodes réelles (calculate_weighted_mean,
# _to_criterion_value, _minmax_normalise, select), jamais réimplémentées ici.
_SELECTOR = TopsisSelector()

# Tolérance du contrôle de non-divergence (§3.3) : le rejeu doit retomber
# EXACTEMENT sur les vm_scores que produirait select() sur les mêmes
# entrées, aux erreurs d'arrondi flottant près.
_CONSISTENCY_TOLERANCE: float = 1e-6


# ── §3.1 — Reconstruction des candidats ────────────────────────

def rebuild_candidates(audit_entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Reconstruit la liste de candidats à partir de current_metrics, en ne
    gardant que les VMs CONFORMES — lues dans
    audit_entry["reasoning"]["compliant_vms"]. C'est exactement ce que
    decision.py passe à select() : sans ce filtre, le rejeu ne
    correspondrait pas à ce qui a réellement été arbitré.

    Ordre préservé (celui de compliant_vms — déjà déterministe, voir
    hub/provider_arbitration.py::evaluate_provider). VM absente de
    current_metrics (mesure non archivée) : ignorée silencieusement plutôt
    que de fabriquer un candidat incomplet.
    """
    current_metrics = audit_entry.get("current_metrics") or {}
    reasoning        = audit_entry.get("reasoning") or {}
    compliant_vms    = reasoning.get("compliant_vms") or []

    candidates: List[Dict[str, Any]] = []
    for vm_id in compliant_vms:
        m = current_metrics.get(vm_id)
        if not m:
            continue
        candidates.append({
            "vm_id":        vm_id,
            "latency":      m.get("latency"),
            "cpu_usage":    m.get("cpu_usage"),
            "ram_usage":    m.get("ram_usage"),
            "total_cores":  m.get("total_cores"),
            "total_ram_gb": m.get("total_ram_gb"),
        })
    return candidates


# ── §3.2 — Rejeu des 4 phases TOPSIS ────────────────────────────

def replay_topsis(audit_entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    Rejoue les 4 phases TOPSIS (matrice, normalisation, pondération,
    idéaux/distances/score) en appelant les méthodes RÉELLES de
    TopsisSelector — jamais une réimplémentation.

    Cas limites (§3.4), traités explicitement, jamais de plantage :
      • predictions_map absent (cycle antérieur au lot 8a)
      • aucune VM conforme
      • une seule VM conforme (retour anticipé de select(), topsis.py:90-95)

    Contrôle de non-divergence (§3.3) : après calcul, appelle AUSSI
    TopsisSelector().select(...) sur les mêmes entrées et compare ses
    vm_scores à ceux recalculés ici, à 1e-6 près. Une divergence n'est
    JAMAIS une exception — elle est signalée via consistent=False et un
    "warning" explicite, pour que la page l'affiche au lieu de montrer
    silencieusement des chiffres faux si topsis.py a évolué depuis.
    """
    predictions_map = audit_entry.get("predictions_map")
    if predictions_map is None:
        return {"phases": [], "reason": "prédictions non archivées pour ce cycle"}

    candidates = rebuild_candidates(audit_entry)
    if not candidates:
        return {"phases": [], "reason": "aucune VM conforme"}
    if len(candidates) == 1:
        return {"phases": [], "reason": "candidat unique — score 1.0 par défaut"}

    slos: List[Dict[str, Any]] = audit_entry.get("slos_active") or []

    # ── Phase 0 — mêmes filtres que TopsisSelector.select() ──────────────
    slo_map: Dict[str, Dict[str, Any]] = {
        s["metric"]: s for s in slos if s["metric"] in config.METRICS_REGISTRY
    }
    slo_metrics: List[str] = list(slo_map.keys())
    n_vm = len(candidates)
    n_cr = len(slo_metrics)

    # ── Phase 1 — Matrice de décision ─────────────────────────────────────
    matrix: List[List[float]] = []
    for cand in candidates:
        vm_id = cand["vm_id"]
        row: List[float] = []
        for metric in slo_metrics:
            meta = config.METRICS_REGISTRY[metric]
            preds_raw = (
                predictions_map.get(vm_id, {}).get(metric, {}).get("predictions", [])
            )
            preds = [p for p in preds_raw if p is not None]
            if preds:
                raw_value = _SELECTOR.calculate_weighted_mean(preds)
            else:
                payload_key = meta.get("payload_key", metric)
                val = cand.get(payload_key)
                raw_value = float(val) if val is not None else float(meta["default_threshold"])
            row.append(_SELECTOR._to_criterion_value(metric, cand, raw_value))
        matrix.append(row)

    # ── Phase 2 — Normalisation min-max ───────────────────────────────────
    norm_m = _SELECTOR._minmax_normalise(matrix, n_vm, n_cr)

    # ── Phase 3 — Pondération ─────────────────────────────────────────────
    weights = [
        float(slo_map[m]["weight"]) if slo_map[m].get("weight") is not None else 0.0
        for m in slo_metrics
    ]
    w_m = [[norm_m[i][j] * weights[j] for j in range(n_cr)] for i in range(n_vm)]

    # ── Phase 4 — Idéaux, distances, score ────────────────────────────────
    is_benefit = [m in _CAPACITY_METRICS for m in slo_metrics]
    a_plus = [
        max(w_m[i][j] for i in range(n_vm)) if is_benefit[j]
        else min(w_m[i][j] for i in range(n_vm))
        for j in range(n_cr)
    ]
    a_minus = [
        min(w_m[i][j] for i in range(n_vm)) if is_benefit[j]
        else max(w_m[i][j] for i in range(n_vm))
        for j in range(n_cr)
    ]
    d_plus = [
        math.sqrt(sum((w_m[i][j] - a_plus[j]) ** 2 for j in range(n_cr)))
        for i in range(n_vm)
    ]
    d_minus = [
        math.sqrt(sum((w_m[i][j] - a_minus[j]) ** 2 for j in range(n_cr)))
        for i in range(n_vm)
    ]
    scores = [
        d_minus[i] / (d_plus[i] + d_minus[i]) if (d_plus[i] + d_minus[i]) > 0 else 0.0
        for i in range(n_vm)
    ]
    vm_scores: Dict[str, float] = {
        candidates[i]["vm_id"]: round(scores[i], 4) for i in range(n_vm)
    }

    phases = [
        {
            "name":    "matrice",
            "headers": slo_metrics,
            "rows": [
                {"vm_id": candidates[i]["vm_id"], "values": matrix[i]}
                for i in range(n_vm)
            ],
        },
        {
            "name":    "normalisation",
            "headers": slo_metrics,
            "rows": [
                {"vm_id": candidates[i]["vm_id"], "values": norm_m[i]}
                for i in range(n_vm)
            ],
        },
        {
            "name":    "ponderation",
            "headers": slo_metrics,
            "weights": weights,
            "rows": [
                {"vm_id": candidates[i]["vm_id"], "values": w_m[i]}
                for i in range(n_vm)
            ],
        },
        {
            "name": "distances_et_score",
            "rows": [
                {
                    "vm_id":   candidates[i]["vm_id"],
                    "d_plus":  round(d_plus[i], 4),
                    "d_minus": round(d_minus[i], 4),
                    "score":   vm_scores[candidates[i]["vm_id"]],
                }
                for i in range(n_vm)
            ],
        },
    ]

    result: Dict[str, Any] = {"phases": phases, "vm_scores": vm_scores}

    # ── §3.3 — Contrôle de non-divergence, OBLIGATOIRE ────────────────────
    try:
        _, _, prod_vm_scores = _SELECTOR.select(candidates, predictions_map, slos, {})
        divergences = []
        for vm_id, replayed_score in vm_scores.items():
            prod_score = prod_vm_scores.get(vm_id)
            if prod_score is None or abs(prod_score - replayed_score) > _CONSISTENCY_TOLERANCE:
                divergences.append(f"{vm_id}: rejoué={replayed_score} vs production={prod_score}")
        if divergences:
            result["consistent"] = False
            result["warning"]    = "; ".join(divergences)
        else:
            result["consistent"] = True
    except Exception as exc:
        result["consistent"] = False
        result["warning"]    = f"contrôle de non-divergence en échec : {exc}"

    return result


# ── §3.5 — Étapes du Gap Grade d'un bid (aucun recalcul) ───────

def extract_gap_grade_steps(bid: Dict[str, Any], slos_active: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Met en forme le Gap Grade d'UN bid pour l'affichage — AUCUN RECALCUL.
    Lit bid["gap_grade"]["detail"] (les δ signés après plancher, par
    métrique, déjà calculés par hub/provider_arbitration.py::build_gap_grade
    au moment de la décision) et bid["gap_grade"]["value"]. Les valeurs
    affichées sont exactement celles qui ont servi à l'arbitrage — jamais
    recalculées ici, ce serait le même risque de divergence silencieuse que
    pour TOPSIS (voir replay_topsis).

    Ne retient que les SLOs PRIMAIRES de slos_active (seuls ceux-là
    contribuent au Gap Grade — voir compute_gap_grade).
    """
    gap_grade = bid.get("gap_grade") or {}
    detail:  Dict[str, float] = gap_grade.get("detail") or {}
    placement_plan = bid.get("placement_plan") or {}

    steps: List[Dict[str, Any]] = []
    for slo in (slos_active or []):
        if not slo.get("is_primary"):
            continue
        metric = slo.get("metric")
        if metric not in detail:
            continue
        steps.append({
            "metric":    metric,
            "operator":  slo.get("operator"),
            "threshold": slo.get("threshold"),
            "weight":    slo.get("weight"),
            "delta":     detail.get(metric),
        })

    return {
        "provider_id":  bid.get("provider_id"),
        "vm_id":        placement_plan.get("vm_id"),
        "value":        gap_grade.get("value"),
        "is_compliant": gap_grade.get("is_compliant"),
        "steps":        steps,
    }
