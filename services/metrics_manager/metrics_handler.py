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

    # --- Core MI Logic ---

    def compute_mi_scores(self, history: List[Dict[str, Any]]) -> Dict[str, float]:
        """
        Iterates over registry to compute Normalized Mutual Information for each metric.
        """
        mi_results = {}
        # Y is the ground truth: was there a violation in the system?
        y_vals = [1 if p.get("is_violation", False) else 0 for p in history]

        for metric in self.registry.keys():
            x_vals = [p.get(metric) for p in history if p.get(metric) is not None]
            
            # We need synchronized pairs (x, y) where x is available
            synced_y = [y_vals[i] for i, p in enumerate(history) if p.get(metric) is not None]

            if len(x_vals) < 5 or len(set(synced_y)) < 2:
                logger.debug(f"Insufficient data for MI on {metric}", extra={"event": "insufficient_data", "extra_data": {"metric": metric}})
                mi_results[metric] = 0.0
                continue

            score = self._compute_mi(x_vals, synced_y)
            mi_results[metric] = score
            
        logger.info("MI computation completed for all metrics", extra={"event": "mi_computation_done"})
        return mi_results

    def _compute_mi(self, x_vals: List[float], y_vals: List[int]) -> float:
        """
        Computes Normalized Mutual Information using 2x2 contingency table.
        Discretizes continuous X by median.
        """
        # 1. Discretization
        median_x = statistics.median(x_vals)
        x_discrete = [1 if x > median_x else 0 for x in x_vals]
        n = len(x_discrete)

        # 2. Contingency Table (2x2)
        # rows: X=0, X=1 | cols: Y=0, Y=1
        table = [[0, 0], [0, 0]]
        for i in range(n):
            table[x_discrete[i]][y_vals[i]] += 1

        # 3. Probabilities and Entropies
        p_x = [sum(table[0])/n, sum(table[1])/n]
        p_y = [ (table[0][0] + table[1][0])/n, (table[0][1] + table[1][1])/n ]
        
        h_x = self._entropy(p_x)
        h_y = self._entropy(p_y)

        # Joint Entropy
        p_xy = [cell/n for row in table for cell in row]
        h_xy = self._entropy(p_xy)

        # Mutual Information: I(X;Y) = H(X) + H(Y) - H(X,Y)
        mi = h_x + h_y - h_xy
        
        # 4. Normalization
        denom = max(h_x, h_y)
        if denom == 0:
            return 0.0
            
        return max(0.0, min(1.0, mi / denom))

    def _entropy(self, probs: List[float]) -> float:
        """Standard Shannon Entropy."""
        ent = 0.0
        for p in probs:
            if p > 0:
                ent -= p * math.log2(p)
        return ent

    # --- Adaptive Percentile Logic ---

    def _adaptive_percentile(self, vals: List[float]) -> Optional[float]:
        """
        Selects percentile based on Coefficient of Variation (CV).
        """
        if len(vals) < 5:
            return None

        mean = statistics.mean(vals)
        if mean == 0: return 0.0
        
        std = statistics.stdev(vals)
        cv = std / mean

        if cv < config.CV_LOW:
            p_rank = config.PERCENTILE_STABLE
        elif cv < config.CV_HIGH:
            p_rank = config.PERCENTILE_NORMAL
        else:
            p_rank = config.PERCENTILE_VOLATILE

        logger.debug(f"Adaptive percentile selected: {p_rank} (CV: {cv:.2f})", extra={"event": "adaptive_percentile"})
        
        # Calculate Percentile
        sorted_vals = sorted(vals)
        idx = int(len(sorted_vals) * (p_rank / 100))
        return sorted_vals[min(idx, len(sorted_vals)-1)]

    # --- SLO Selection & Validation ---

    def select_dynamic_slos(self, mi_scores: Dict[str, float], all_vals: Dict[str, List[float]], history: List[Dict[str, Any]]) -> Tuple[List[SLO], List[str]]:
        """
        Registry-driven SLO selection based on always_active and MI sensitivity.
        """
        final_slos = []
        active_metrics = []

        for metric, reg in self.registry.items():
            # Rule: always_active OR MI score > relative threshold
            is_sensitive = mi_scores.get(metric, 0.0) > config.MI_RELATIVE_THRESHOLD
            
            if reg["always_active"] or is_sensitive:
                active_metrics.append(metric)
                
                # Threshold logic
                vals = [p.get(metric) for p in history if p.get(metric) is not None]
                threshold = self._adaptive_percentile(vals)
                if threshold is None:
                    threshold = reg["default_threshold"]
                
                # Clamp to registry bounds
                threshold = self._clamp_to_bounds(metric, threshold)

                final_slos.append(SLO(
                    metric=metric,
                    operator=reg["operator"],
                    threshold=threshold,
                    unit=reg["unit"],
                    target=threshold * 0.9,
                    weight=mi_scores.get(metric, 0.1), # Initial weight from MI
                    window="5m"
                ))

        logger.info(f"Dynamic SLOs selected: {active_metrics}", extra={"event": "dynamic_slos_selected"})
        return self._normalize_weights(final_slos), active_metrics

    def validate_and_enrich_slos(self, slos: List[SLO], mi_scores: Dict[str, float]) -> Tuple[List[SLO], List[str]]:
        """
        Enforces physical bounds and updates weights from MI scores.
        """
        active_metrics = []
        for s in slos:
            active_metrics.append(s.metric)
            # Update Weight from latest MI (risk based)
            s.weight = max(0.01, mi_scores.get(s.metric, 0.1))
            
            # Physical bounds enforcement
            s.threshold = self._clamp_to_bounds(s.metric, s.threshold)
            s.target = min(s.target, s.threshold * 0.95)

        logger.info("SLOs validated and enriched", extra={"event": "slos_validated"})
        return self._normalize_weights(slos), active_metrics

    def _clamp_to_bounds(self, metric: str, val: float) -> float:
        if metric not in self.registry: return val
        b = self.registry[metric]["bounds"]
        return max(b["min"], min(b["max"], val))

    def _normalize_weights(self, slos: List[SLO]) -> List[SLO]:
        if not slos: return []
        total = sum(s.weight for s in slos)
        for s in slos:
            s.weight = round(s.weight / total, 2) if total > 0 else 1.0 / len(slos)
        return slos
