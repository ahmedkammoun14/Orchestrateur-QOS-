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
                # Pour une violation proactive, la sévérité se base sur le pic prédit
                peak_val = max(preds) if preds else current_val
                ref_val  = peak_val if breach_type == "proactive" else current_val
                sev = self._severity(ref_val, threshold, slope)
                violations.append({
                    "metric":         metric,
                    "breach_type":    breach_type,
                    "severity":       sev,
                    "slope":          slope,
                    "time_to_breach": time_to_breach,
                    "current_val":    current_val,
                    "predicted_peak": peak_val,
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

        # Slope sur les prédictions (dérivée moyenne par pas)
        if len(preds) >= 2:
            slope = (preds[-1] - preds[0]) / (len(preds) - 1)
        else:
            slope = 0.0

        # Temps prédit avant breach : index du 1er pred > seuil
        # Initialiser à len(preds)+1 pour que la condition ≤ HORIZON_ALERT
        # ne se déclenche PAS si preds est vide ou si aucun pred ne breach.
        time_to_breach: int = len(preds) + 1
        for idx, p in enumerate(preds):
            if p > threshold:
                time_to_breach = idx
                break

        # Temps estimé par la trajectoire courante (dérivée linéaire)
        if slope > 0 and current_val < threshold:
            estimated_steps = (threshold - current_val) / slope
        else:
            estimated_steps = float("inf")

        proactive_threshold: float = threshold * proactive_factor

        # Condition ML 1 : une prédiction dépasse le seuil proactif
        pred_breach: bool = bool(preds) and any(p > proactive_threshold for p in preds)

        # Condition ML 2 : breach imminent selon les prédictions ou la trajectoire
        time_breach_imminent: bool = bool(preds) and (
            time_to_breach <= config.HORIZON_ALERT
            or estimated_steps <= config.HORIZON_ALERT
        )

        # Condition 3 : tendance montante significative
        _MIN_SLOPE: float = threshold * 0.05
        trend_rising: bool = slope > _MIN_SLOPE and current_val > threshold * 0.60

        # La décision est "proactive" dès que les prédictions ML confirment ou anticipent
        # la violation — même si la valeur courante est déjà réactive.
        # "réactif" ne s'affiche que si aucune prédiction n'est disponible.
        ml_driven: bool = pred_breach or time_breach_imminent

        logger.debug(
            f"🔍 _analyze — val={current_val:.2f} seuil={threshold:.2f} "
            f"slope={slope:.3f}/pas  TTB_ml={time_to_breach}  "
            f"TTB_traj={estimated_steps:.1f}  "
            f"pred_breach={pred_breach}  imminent={time_breach_imminent}  "
            f"trend={trend_rising}  ml_driven={ml_driven}"
        )

        if ml_driven or trend_rising:
            # ML ou tendance guide la décision → proactif (même si déjà en breach réactif)
            return "proactive", slope, time_to_breach
        if breach_reactive:
            # Aucune prédiction disponible, réaction à la mesure brute seulement
            return "reactive", slope, time_to_breach
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
        val = vm_data.get(payload_key)
        return float(val) if val is not None else 0.0