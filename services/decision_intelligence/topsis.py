import math
import logging
from typing import Any, Dict, List, Optional, Tuple

from shared import config

logger = logging.getLogger("DecisionIntelligence.handler")

_BUDGET_WEIGHT:      float = 1.0
_MIGRATION_WEIGHT:   float = 0.5
_RELIABILITY_WEIGHT: float = 0.3


def vm_satisfies_slo(mean: float, slo: Dict[str, Any]) -> bool:
    """Return True if *mean* meets the SLO threshold given its operator."""
    op = slo["operator"]
    t_raw = slo.get("threshold")
    t = float(t_raw) if t_raw is not None else 0.0
    return (
        (op == "<"  and mean <  t) or
        (op == "<=" and mean <= t) or
        (op == ">"  and mean >  t) or
        (op == ">=" and mean >= t)
    )


class TopsisSelector:
    """
    7-step TOPSIS with min-max normalisation for VM candidate ranking.
    Stateless — no I/O, no side effects.
    """

    def select(
        self,
        candidates: List[Dict[str, Any]],
        predictions_map: Dict[str, Dict[str, Any]],
        slos: List[Dict[str, Any]],
        migration_costs: Dict[str, int],
        reliability_scores: Dict[str, float],
    ) -> Tuple[Dict[str, Any], float]:
        if not candidates:
            return {}, 0.0
        if len(candidates) == 1:
            logger.debug(
                f"🔍 TOPSIS — candidat unique : {candidates[0]['vm_id']} "
                "| score par défaut : 1.0"
            )
            return candidates[0], 1.0

        slo_map: Dict[str, Dict[str, Any]] = {
            s["metric"]: s
            for s in slos
            if s["metric"] in config.METRICS_REGISTRY
        }
        slo_metrics: List[str] = list(slo_map.keys())

        n_vm: int = len(candidates)
        n_cr: int = len(slo_metrics) + 3

        # Step 1 — Decision matrix
        matrix: List[List[float]] = []
        for cand in candidates:
            vm_id: str       = cand["vm_id"]
            row:   List[float] = []

            for metric in slo_metrics:
                meta  = config.METRICS_REGISTRY[metric]
                preds_raw: List[Any] = (
                    predictions_map
                    .get(vm_id, {})
                    .get(metric, {})
                    .get("predictions", [])
                )
                # Filtre toute valeur None résiduelle dans les prédictions
                preds: List[float] = [p for p in preds_raw if p is not None]

                if preds:
                    row.append(self.calculate_weighted_mean(preds))
                else:
                    payload_key: str = meta.get("payload_key", metric)
                    val = cand.get(payload_key)
                    row.append(float(val) if val is not None else float(meta["default_threshold"]))

            row.append(self._budget_score(vm_id, predictions_map, slos, slo_metrics))

            mig_cost = migration_costs.get(vm_id)
            row.append(float(mig_cost) if mig_cost is not None else 0.0)

            rel = reliability_scores.get(vm_id)
            row.append(1.0 - float(rel) if rel is not None else 1.0)

            matrix.append(row)

        # Steps 2–6
        norm_m  = self._minmax_normalise(matrix, n_vm, n_cr)
        weights = (
            [float(slo_map[m]["weight"]) if slo_map[m].get("weight") is not None else 0.0
             for m in slo_metrics]
            + [_BUDGET_WEIGHT, _MIGRATION_WEIGHT, _RELIABILITY_WEIGHT]
        )
        w_m = [
            [norm_m[i][j] * weights[j] for j in range(n_cr)]
            for i in range(n_vm)
        ]
        a_plus  = [min(w_m[i][j] for i in range(n_vm)) for j in range(n_cr)]
        a_minus = [max(w_m[i][j] for i in range(n_vm)) for j in range(n_cr)]
        d_plus  = [
            math.sqrt(sum((w_m[i][j] - a_plus[j])  ** 2 for j in range(n_cr)))
            for i in range(n_vm)
        ]
        d_minus = [
            math.sqrt(sum((w_m[i][j] - a_minus[j]) ** 2 for j in range(n_cr)))
            for i in range(n_vm)
        ]
        scores = [
            d_minus[i] / (d_plus[i] + d_minus[i])
            if (d_plus[i] + d_minus[i]) > 0 else 0.0
            for i in range(n_vm)
        ]

        # Step 7 — classement
        ranking = sorted(
            zip([c["vm_id"] for c in candidates], scores),
            key=lambda x: x[1],
            reverse=True,
        )
        logger.debug(
            "🔍 TOPSIS — classement : "
            + "  ".join(f"{vm}={score:.4f}" for vm, score in ranking)
        )

        best_idx: int = scores.index(max(scores))
        return candidates[best_idx], round(scores[best_idx], 4)

    def calculate_weighted_mean(self, preds: List[float]) -> float:
        if not preds:
            return 0.0
        n:       int       = len(preds)
        weights: List[int] = list(range(n, 0, -1))
        total:   int       = sum(weights)
        return sum(w * p for w, p in zip(weights, preds)) / total

    def _budget_score(
        self,
        vm_id: str,
        predictions_map: Dict[str, Dict[str, Any]],
        slos: List[Dict[str, Any]],
        slo_metrics: List[str],
    ) -> float:
        total: int     = 0
        satisfied: int = 0
        for metric in slo_metrics:
            slo = next((s for s in slos if s["metric"] == metric), None)
            if slo is None:
                continue
            total += 1
            preds_raw: List[Any] = (
                predictions_map
                .get(vm_id, {})
                .get(metric, {})
                .get("predictions", [])
            )
            preds: List[float] = [p for p in preds_raw if p is not None]
            if preds and vm_satisfies_slo(self.calculate_weighted_mean(preds), slo):
                satisfied += 1
        if total == 0:
            return 1.0
        return 1.0 - satisfied / total

    @staticmethod
    def _minmax_normalise(
        matrix: List[List[float]],
        n_vm: int,
        n_cr: int,
    ) -> List[List[float]]:
        norm: List[List[float]] = [[0.0] * n_cr for _ in range(n_vm)]
        for j in range(n_cr):
            col:     List[float] = [matrix[i][j] for i in range(n_vm)]
            col_min: float       = min(col)
            col_max: float       = max(col)
            span:    float       = col_max - col_min
            for i in range(n_vm):
                norm[i][j] = (matrix[i][j] - col_min) / span if span > 0 else 0.0
        return norm