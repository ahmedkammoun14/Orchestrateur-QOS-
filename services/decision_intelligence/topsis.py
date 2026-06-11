import math
from typing import Any, Dict, List, Optional, Tuple

from shared import config

# Fixed weights for the three auxiliary criteria (not SLO-derived).
# All criteria are treated as lower-is-better (see Step 4 rationale).
_BUDGET_WEIGHT:    float = 1.0
_MIGRATION_WEIGHT: float = 0.5
_RELIABILITY_WEIGHT: float = 0.3


def vm_satisfies_slo(mean: float, slo: Dict[str, Any]) -> bool:
    """Return True if *mean* meets the SLO threshold given its operator."""
    op, t = slo["operator"], float(slo["threshold"])
    return (
        (op == "<"  and mean <  t) or
        (op == "<=" and mean <= t) or
        (op == ">"  and mean >  t) or
        (op == ">=" and mean >= t)
    )


class TopsisSelector:
    """
    7-step TOPSIS with min-max normalisation for VM candidate ranking.

    Decision matrix
    ---------------
    Rows   = candidate VMs (service_vm excluded — already filtered by caller).
    Columns (all lower-is-better):

        ┌─────────────────────────────────────────────────────────┐
        │  SLO metrics     │ weighted mean of predictions         │
        │  budget_score    │ 1 − (fraction of SLOs satisfied)     │
        │  migration_cost  │ raw cost from migration_costs dict   │
        │  unreliability   │ 1 − reliability_score                │
        └─────────────────────────────────────────────────────────┘

    The 7 steps
    -----------
    1. Build raw decision matrix.
    2. Min-max normalisation per column: (v − min) / (max − min).
       All-equal column → 0.0.
    3. Weight each column (SLO weights from SLO dicts; fixed weights
       for auxiliary columns).
    4. A+ = min per column, A− = max per column  (lower-is-better).
    5. D+ = Euclidean distance to A+; D− = distance to A−.
    6. Closeness score = D− / (D+ + D−).
    7. Return candidate with highest score.

    Stateless — no I/O, no side effects.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(
        self,
        candidates: List[Dict[str, Any]],
        predictions_map: Dict[str, Dict[str, Any]],
        slos: List[Dict[str, Any]],
        migration_costs: Dict[str, int],
        reliability_scores: Dict[str, float],
    ) -> Tuple[Dict[str, Any], float]:
        """
        Select the best migration target from *candidates*.

        Parameters
        ----------
        candidates:
            VM dicts already filtered by the caller (service_vm excluded).
            Each dict must have ``vm_id`` and current metric fields.
        predictions_map:
            ``{vm_id: {metric: {"predictions": [7 floats], ...}}}``
        slos:
            Active SLOs; only metrics in ``METRICS_REGISTRY`` are used.
        migration_costs:
            ``{vm_id: int}`` — raw migration cost per target VM.
        reliability_scores:
            ``{vm_id: float}`` in [0, 1].

        Returns
        -------
        (best_candidate_dict, topsis_score)
            The optimal candidate and its TOPSIS closeness score in [0, 1].
        """
        if not candidates:
            return {}, 0.0
        if len(candidates) == 1:
            return candidates[0], 1.0

        # Active SLO metrics only (filtered against METRICS_REGISTRY)
        slo_map: Dict[str, Dict[str, Any]] = {
            s["metric"]: s
            for s in slos
            if s["metric"] in config.METRICS_REGISTRY
        }
        slo_metrics: List[str] = list(slo_map.keys())

        n_vm: int = len(candidates)
        n_cr: int = len(slo_metrics) + 3   # SLOs + budget + migration + unreliability

        # ------------------------------------------------------------------
        # Step 1 — Decision matrix
        # ------------------------------------------------------------------
        matrix: List[List[float]] = []

        for cand in candidates:
            vm_id: str = cand["vm_id"]
            row:   List[float] = []

            # SLO metric columns: weighted mean of predictions
            for metric in slo_metrics:
                meta  = config.METRICS_REGISTRY[metric]
                preds: List[float] = (
                    predictions_map
                    .get(vm_id, {})
                    .get(metric, {})
                    .get("predictions", [])
                )
                if preds:
                    row.append(self.calculate_weighted_mean(preds))
                else:
                    # Fallback: current measured value via payload_key
                    payload_key: str = meta.get("payload_key", metric)
                    row.append(
                        float(cand.get(payload_key, meta["default_threshold"]))
                    )

            # Budget score: 1 − (fraction of SLOs already satisfied)
            row.append(self._budget_score(vm_id, predictions_map, slos, slo_metrics))

            # Migration cost
            row.append(float(migration_costs.get(vm_id, 0)))

            # Unreliability: lower reliability → higher penalty
            row.append(1.0 - float(reliability_scores.get(vm_id, 0.0)))

            matrix.append(row)

        # ------------------------------------------------------------------
        # Step 2 — Min-max normalisation
        # ------------------------------------------------------------------
        norm_m: List[List[float]] = self._minmax_normalise(matrix, n_vm, n_cr)

        # ------------------------------------------------------------------
        # Step 3 — Weight each column
        # ------------------------------------------------------------------
        weights: List[float] = (
            [float(slo_map[m]["weight"]) for m in slo_metrics]
            + [_BUDGET_WEIGHT, _MIGRATION_WEIGHT, _RELIABILITY_WEIGHT]
        )

        w_m: List[List[float]] = [
            [norm_m[i][j] * weights[j] for j in range(n_cr)]
            for i in range(n_vm)
        ]

        # ------------------------------------------------------------------
        # Step 4 — Ideal solutions (all criteria: lower-is-better)
        # ------------------------------------------------------------------
        a_plus:  List[float] = [min(w_m[i][j] for i in range(n_vm)) for j in range(n_cr)]
        a_minus: List[float] = [max(w_m[i][j] for i in range(n_vm)) for j in range(n_cr)]

        # ------------------------------------------------------------------
        # Steps 5 & 6 — Distances and closeness scores
        # ------------------------------------------------------------------
        d_plus:  List[float] = [
            math.sqrt(sum((w_m[i][j] - a_plus[j])  ** 2 for j in range(n_cr)))
            for i in range(n_vm)
        ]
        d_minus: List[float] = [
            math.sqrt(sum((w_m[i][j] - a_minus[j]) ** 2 for j in range(n_cr)))
            for i in range(n_vm)
        ]

        scores: List[float] = [
            d_minus[i] / (d_plus[i] + d_minus[i])
            if (d_plus[i] + d_minus[i]) > 0 else 0.0
            for i in range(n_vm)
        ]

        # ------------------------------------------------------------------
        # Step 7 — Select best candidate
        # ------------------------------------------------------------------
        best_idx: int = scores.index(max(scores))
        return candidates[best_idx], round(scores[best_idx], 4)

    def calculate_weighted_mean(self, preds: List[float]) -> float:
        """
        Compute the weighted mean of a prediction series.

        Weights are **decreasing**: the nearest-future step (``preds[0]``)
        receives the highest weight ``n``, and the farthest step
        (``preds[-1]``) receives weight ``1``.  This reflects higher
        confidence in near-term predictions.

        Parameters
        ----------
        preds:
            Ordered prediction values (index 0 = 1 step ahead).

        Returns
        -------
        float
            Weighted mean, or ``0.0`` for an empty list.
        """
        if not preds:
            return 0.0
        n: int          = len(preds)
        weights: List[int] = list(range(n, 0, -1))     # [n, n-1, ..., 1]
        total: int      = sum(weights)
        return sum(w * p for w, p in zip(weights, preds)) / total

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _budget_score(
        self,
        vm_id: str,
        predictions_map: Dict[str, Dict[str, Any]],
        slos: List[Dict[str, Any]],
        slo_metrics: List[str],
    ) -> float:
        """
        Compute ``1 − bonus_slo`` for *vm_id*.

        ``bonus_slo`` = fraction of SLOs already satisfied by this VM
        based on weighted-mean predictions.  A VM satisfying all SLOs
        gets ``budget_score = 0`` (best); one satisfying none gets ``1``
        (worst).  Operator-aware: uses the SLO's operator field.

        Returns 1.0 (worst) when no SLO data is available.
        """
        total: int     = 0
        satisfied: int = 0

        for metric in slo_metrics:
            slo = next((s for s in slos if s["metric"] == metric), None)
            if slo is None:
                continue
            total += 1
            preds: List[float] = (
                predictions_map
                .get(vm_id, {})
                .get(metric, {})
                .get("predictions", [])
            )
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
        """
        Apply min-max normalisation column-wise.

        Each value is mapped to ``[0, 1]`` via ``(v − min) / (max − min)``.
        For an all-equal column (``max == min``), every normalised value
        is ``0.0``.

        Parameters
        ----------
        matrix:
            Raw decision matrix  (n_vm × n_cr).
        n_vm:
            Number of VM rows.
        n_cr:
            Number of criterion columns.

        Returns
        -------
        List[List[float]]
            Normalised matrix of the same shape.
        """
        norm: List[List[float]] = [[0.0] * n_cr for _ in range(n_vm)]

        for j in range(n_cr):
            col: List[float] = [matrix[i][j] for i in range(n_vm)]
            col_min: float   = min(col)
            col_max: float   = max(col)
            span: float      = col_max - col_min

            for i in range(n_vm):
                norm[i][j] = (matrix[i][j] - col_min) / span if span > 0 else 0.0

        return norm
