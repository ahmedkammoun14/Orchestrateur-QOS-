from typing import Any, Dict, List, Tuple
import logging

from shared import config

logger = logging.getLogger("DecisionIntelligence.handler")

_HIGH_UNCERTAINTY_THRESHOLD: float = 0.50
_HIGH_UNCERTAINTY_FACTOR:    float = 0.90


class ViolationDetector:
    """
    Unified SLO violation analyser for a single VM.
    Stateless — no I/O, no side effects.
    """

    def detect(
        self,
        current_data: List[Dict[str, Any]],
        predictions_map: Dict[str, Dict[str, Any]],
        slos: List[Dict[str, Any]],
        service_vm: str,
    ) -> List[Dict[str, Any]]:
        vm_data:  Dict[str, Any] = next(
            (v for v in current_data if v["vm_id"] == service_vm), {}
        )
        vm_preds: Dict[str, Any] = predictions_map.get(service_vm, {})

        violations: List[Dict[str, Any]] = []

        for slo in slos:
            metric: str = slo["metric"]
            if metric not in config.METRICS_REGISTRY:
                continue

            current_val:  float         = self._get_current_val(vm_data, metric)
            metric_entry: Dict[str, Any] = vm_preds.get(metric, {})
            preds:        List[float]   = metric_entry.get("predictions", [])
            uncertainty:  float         = float(metric_entry.get("uncertainty", 0.0))

            proactive_factor: float = (
                _HIGH_UNCERTAINTY_FACTOR
                if uncertainty > _HIGH_UNCERTAINTY_THRESHOLD
                else config.PROACTIVE_FACTOR
            )

            threshold: float = float(slo["threshold"])
            breach_type, slope, time_to_breach = self._analyze(
                preds, threshold, current_val, proactive_factor
            )

            if breach_type != "none":
                sev = self._severity(current_val, threshold, slope)
                violations.append({
                    "metric":         metric,
                    "breach_type":    breach_type,
                    "severity":       sev,
                    "slope":          slope,
                    "time_to_breach": time_to_breach,
                    "current_val":    current_val,
                    "threshold":      threshold,
                })
                logger.debug(
                    f"⚠️  Violation — {metric:<12} "
                    f"type : {breach_type:<10} "
                    f"val : {current_val:.2f} / seuil : {threshold:.2f} "
                    f"sévérité : {sev:.3f}  TTB : {time_to_breach}"
                )

        return violations

    def _analyze(
        self,
        preds: List[float],
        threshold: float,
        current_val: float,
        proactive_factor: float,
    ) -> Tuple[str, float, int]:
        breach_reactive: bool = current_val > threshold
        slope: float = (preds[-1] - preds[0]) if len(preds) >= 2 else 0.0

        time_to_breach: int = len(preds)
        for idx, p in enumerate(preds):
            if p > threshold:
                time_to_breach = idx
                break

        proactive_threshold: float = threshold * proactive_factor
        cond1: bool = any(p > proactive_threshold for p in preds)
        cond2: bool = time_to_breach <= config.HORIZON_ALERT
        cond3: bool = current_val > threshold * 0.70
        breach_proactive: bool = (cond1 or cond2) and cond3 and not breach_reactive

        if breach_reactive:
            return "reactive", slope, time_to_breach
        if breach_proactive:
            return "proactive", slope, time_to_breach
        return "none", slope, time_to_breach

    @staticmethod
    def _severity(current_val: float, threshold: float, slope: float) -> float:
        if threshold <= 0:
            return 0.0
        excess:      float = max(0.0, current_val - threshold) / threshold
        slope_bonus: float = max(0.0, slope) / threshold
        return excess + 0.3 * slope_bonus

    def _get_current_val(self, vm_data: Dict[str, Any], metric: str) -> float:
        payload_key: str = config.METRICS_REGISTRY[metric].get("payload_key", metric)
        return float(vm_data.get(payload_key, 0.0))