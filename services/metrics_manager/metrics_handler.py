import logging
import math
import statistics
from typing import List, Dict, Any, Tuple, Optional
from shared import config
from shared.models import SLO

logger = logging.getLogger("MetricsHandler")


class MetricsHandler:
    """
    Advanced statistics handler for MI scores, adaptive percentiles,
    and dynamic SLO management.
    """

    def __init__(self):
        self.registry = config.METRICS_REGISTRY

    # ── Core MI Logic ────────────────────────────────────────────────

    def compute_mi_scores(self, history: List[Dict[str, Any]]) -> Dict[str, float]:
        mi_results = {}
        y_vals = [1 if p.get("is_violation", False) else 0 for p in history]

        for metric in self.registry.keys():
            x_vals   = [p.get(metric) for p in history if p.get(metric) is not None]
            synced_y = [y_vals[i] for i, p in enumerate(history) if p.get(metric) is not None]

            if len(x_vals) < 5 or len(set(synced_y)) < 2:
                logger.debug(
                    f"🔍 Données insuffisantes pour MI — métrique : {metric} "
                    f"| points : {len(x_vals)}"
                )
                mi_results[metric] = 0.0
                continue

            score = self._compute_mi(x_vals, synced_y)
            mi_results[metric] = score
            logger.debug(
                f"🔍 MI calculé — {metric} : {score:.4f}"
            )

        logger.info(
            f"📊 Scores MI calculés — "
            + "  ".join(
                f"{m} : {v:.3f}" for m, v in mi_results.items()
            )
        )
        return mi_results

    def _compute_mi(self, x_vals: List[float], y_vals: List[int]) -> float:
        median_x   = statistics.median(x_vals)
        x_discrete = [1 if x > median_x else 0 for x in x_vals]
        n          = len(x_discrete)

        table = [[0, 0], [0, 0]]
        for i in range(n):
            table[x_discrete[i]][y_vals[i]] += 1

        p_x  = [sum(table[0]) / n, sum(table[1]) / n]
        p_y  = [(table[0][0] + table[1][0]) / n, (table[0][1] + table[1][1]) / n]
        h_x  = self._entropy(p_x)
        h_y  = self._entropy(p_y)
        p_xy = [cell / n for row in table for cell in row]
        h_xy = self._entropy(p_xy)
        mi   = h_x + h_y - h_xy

        denom = max(h_x, h_y)
        if denom == 0:
            return 0.0
        return max(0.0, min(1.0, mi / denom))

    def _entropy(self, probs: List[float]) -> float:
        ent = 0.0
        for p in probs:
            if p > 0:
                ent -= p * math.log2(p)
        return ent

    # ── Adaptive Percentile Logic ────────────────────────────────────

    def _adaptive_percentile(self, vals: List[float]) -> Optional[float]:
        if len(vals) < 5:
            return None

        mean = statistics.mean(vals)
        if mean == 0:
            return 0.0

        std = statistics.stdev(vals)
        cv  = std / mean

        if cv < config.CV_LOW:
            p_rank = config.PERCENTILE_STABLE
        elif cv < config.CV_HIGH:
            p_rank = config.PERCENTILE_NORMAL
        else:
            p_rank = config.PERCENTILE_VOLATILE

        logger.debug(
            f"🔍 Percentile adaptatif — P{p_rank} sélectionné "
            f"| CV = {cv:.3f}"
        )

        sorted_vals = sorted(vals)
        idx = int(len(sorted_vals) * (p_rank / 100))
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    # ── SLO Selection & Validation ───────────────────────────────────

    def select_dynamic_slos(
        self,
        mi_scores: Dict[str, float],
        all_vals: Dict[str, List[float]],
        history: List[Dict[str, Any]],
    ) -> Tuple[List[SLO], List[str]]:
        final_slos     = []
        active_metrics = []

        for metric, reg in self.registry.items():
            is_sensitive = mi_scores.get(metric, 0.0) > config.MI_RELATIVE_THRESHOLD

            if reg["always_active"] or is_sensitive:
                active_metrics.append(metric)

                vals      = [p.get(metric) for p in history if p.get(metric) is not None]
                threshold = self._adaptive_percentile(vals)
                if threshold is None:
                    threshold = reg["default_threshold"]
                    logger.debug(
                        f"🔍 Seuil par défaut utilisé pour {metric} "
                        f"(historique insuffisant)"
                    )

                threshold = self._clamp_to_bounds(metric, threshold)

                final_slos.append(SLO(
                    metric=metric,
                    operator=reg["operator"],
                    threshold=threshold,
                    unit=reg["unit"],
                    target=threshold * 0.9,
                    weight=mi_scores.get(metric, 0.1),
                    window="5m"
                ))

        normalized = self._normalize_weights(final_slos)
        logger.info(
            f"✅ SLOs dynamiques sélectionnés — "
            f"{len(normalized)} SLO(s) actif(s) "
            f"| métriques : {active_metrics}"
        )
        for s in normalized:
            logger.debug(
                f"🔍 SLO {s.metric:<12} seuil : {s.threshold:.2f} {s.unit:<3} "
                f"| opérateur : {s.operator}  poids : {s.weight:.2f}"
            )
        return normalized, active_metrics

    def validate_and_enrich_slos(
        self,
        slos: List[SLO],
        mi_scores: Dict[str, float],
    ) -> Tuple[List[SLO], List[str]]:
        active_metrics = []
        for s in slos:
            active_metrics.append(s.metric)
            s.weight    = max(0.01, mi_scores.get(s.metric, 0.1))
            s.threshold = self._clamp_to_bounds(s.metric, s.threshold)
            s.target    = min(s.target, s.threshold * 0.95)

        normalized = self._normalize_weights(slos)
        logger.info(
            f"✅ SLOs validés et enrichis — "
            f"{len(normalized)} SLO(s) "
            f"| métriques : {active_metrics}"
        )
        for s in normalized:
            logger.debug(
                f"🔍 SLO {s.metric:<12} seuil : {s.threshold:.2f} {s.unit:<3} "
                f"| poids MI : {s.weight:.2f}  cible : {s.target:.2f}"
            )
        return normalized, active_metrics

    def _clamp_to_bounds(self, metric: str, val: float) -> float:
        if metric not in self.registry:
            return val
        b = self.registry[metric]["bounds"]
        return max(b["min"], min(b["max"], val))

    def _normalize_weights(self, slos: List[SLO]) -> List[SLO]:
        if not slos:
            return []
        total = sum(s.weight for s in slos)
        for s in slos:
            s.weight = round(s.weight / total, 2) if total > 0 else 1.0 / len(slos)
        return slos