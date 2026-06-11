import logging
from typing import List, Dict, Any, Optional
from shared.models import SLO
from shared import config

logger = logging.getLogger("SLOMerger")

class SLOMerger:
    """
    Advanced logic for merging new intentions with existing/active SLOs.
    Supports REPLACE, ADDITIVE, and REFINE strategies.
    """

    def merge(self, active_slos: List[SLO], new_slos: List[SLO], text: str) -> List[SLO]:
        """
        Determines the merge strategy based on the intention text and fuses the SLOs.
        """
        text_lower = text.lower()
        
        # 1. Determine Strategy
        if any(kw in text_lower for kw in ["plus strict", "moins strict", "affine", "plus de", "moins de"]):
            mode = "REFINE"
        elif any(kw in text_lower for kw in ["aussi", "et", "ajoute", "en plus"]):
            mode = "ADDITIVE"
        else:
            mode = "REPLACE"

        logger.info(f"Merge mode identified: {mode}", extra={"event": "merge_mode", "extra_data": {"mode": mode}})

        # 2. Execute Strategy
        if mode == "REPLACE":
            result = new_slos
        elif mode == "ADDITIVE":
            result = self._additive_merge(active_slos, new_slos)
        elif mode == "REFINE":
            result = self._refine_merge(active_slos, text_lower)
        else:
            result = new_slos

        # 3. Validation & Conflict Detection
        self._detect_conflicts(result)
        
        # 4. Final Weight Normalization
        return self._normalize_weights(result)

    def _additive_merge(self, active: List[SLO], new: List[SLO]) -> List[SLO]:
        """Adds new SLOs or updates existing ones if metrics match."""
        merged = {s.metric: s for s in active}
        for n in new:
            merged[n.metric] = n  # Overwrite or add
        return list(merged.values())

    def _refine_merge(self, active: List[SLO], text: str) -> List[SLO]:
        """Applies factors to existing thresholds."""
        factor = config.REFINE_STRICT if "plus strict" in text else config.REFINE_RELAX
        
        for s in active:
            # For latency and usage, 'stricter' means a lower threshold
            s.threshold = round(s.threshold * factor, 2)
            s.target = round(s.target * factor, 2)
            s.confidence = min(1.0, s.confidence * 0.9) # Slightly reduce confidence on manual refinement
            
        return active

    def _detect_conflicts(self, slos: List[SLO]):
        """Detects weird operator shifts or massive threshold gaps."""
        by_metric: Dict[str, List[SLO]] = {}
        for s in slos:
            by_metric.setdefault(s.metric, []).append(s)

        for metric, group in by_metric.items():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    s_old, s_new = group[i], group[j]
                    if s_old.operator != s_new.operator:
                        logger.warning(
                            f"Contradictory operators for {metric}",
                            extra={"event": "conflict_operator",
                                   "extra_data": {"metric": metric,
                                   "op1": s_old.operator,
                                   "op2": s_new.operator}}
                        )
                    if s_old.threshold > 0:
                        gap = abs(s_new.threshold - s_old.threshold) / s_old.threshold
                        if gap > 0.5:
                            logger.warning(
                                f"Threshold gap > 50% for {metric}",
                                extra={"event": "conflict_threshold",
                                       "extra_data": {"metric": metric,
                                       "gap": round(gap, 2)}}
                            )

    def _normalize_weights(self, slos: List[SLO]) -> List[SLO]:
        if not slos: return []
        total = sum(s.weight for s in slos)
        if total > 0:
            for s in slos:
                s.weight = round(s.weight / total, 2)
        return slos
