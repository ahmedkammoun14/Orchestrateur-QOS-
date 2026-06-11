from typing import Any, Dict, List, Optional, Tuple

from shared import config

# When uncertainty > this threshold, use the aggressive proactive factor.
_HIGH_UNCERTAINTY_THRESHOLD: float = 0.50
# Aggressive proactive factor applied under high uncertainty.
# Proactive detection fires when any prediction exceeds threshold * factor.
# Lower factor → earlier detection.  Hardcoded per algorithm specification.
_HIGH_UNCERTAINTY_FACTOR: float = 0.90


class ViolationDetector:
    """
    Unified SLO violation analyser for a single VM.

    ``detect()`` evaluates *service_vm* for every active SLO and returns
    a list of violation descriptors — one per breached metric.  Each
    descriptor carries the breach type (reactive / proactive), its
    severity score, the current slope of predictions, and the estimated
    time-to-breach.

    All metric-name ↔ payload-field mappings are resolved via
    ``METRICS_REGISTRY["payload_key"]`` — no field name is ever
    hardcoded.

    Stateless — no I/O, no side effects.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(
        self,
        current_data: List[Dict[str, Any]],
        predictions_map: Dict[str, Dict[str, Any]],
        slos: List[Dict[str, Any]],
        service_vm: str,
    ) -> List[Dict[str, Any]]:
        """
        Analyse *service_vm* against every active SLO.

        For each SLO:

        1. Resolves the current measured value via ``METRICS_REGISTRY``
           (``payload_key`` field).
        2. Retrieves the 7-step predictions and uncertainty from
           *predictions_map*.
        3. Selects the proactive factor based on uncertainty level.
        4. Delegates to ``_analyze()`` to classify the breach.
        5. If a breach is found, computes severity and appends a
           violation descriptor.

        Parameters
        ----------
        current_data:
            Per-VM current measurements as received in the core payload.
        predictions_map:
            ``{vm_id: {metric: {"predictions": [7 floats],
            "uncertainty": float}}}``
        slos:
            Active SLO list.  Each entry must have ``metric``,
            ``operator``, and ``threshold``.
        service_vm:
            VM identifier to evaluate.

        Returns
        -------
        List[dict]
            One dict per violated metric::

                {
                    "metric":          str,
                    "breach_type":     "reactive" | "proactive",
                    "severity":        float,
                    "slope":           float,
                    "time_to_breach":  int,
                    "current_val":     float,
                    "threshold":       float,
                }

            Empty list → no violations.
        """
        vm_data: Dict[str, Any] = next(
            (v for v in current_data if v["vm_id"] == service_vm), {}
        )
        vm_preds: Dict[str, Any] = predictions_map.get(service_vm, {})

        violations: List[Dict[str, Any]] = []

        for slo in slos:
            metric: str = slo["metric"]
            if metric not in config.METRICS_REGISTRY:
                continue

            current_val: float = self._get_current_val(vm_data, metric)

            metric_entry: Dict[str, Any] = vm_preds.get(metric, {})
            preds:        List[float]     = metric_entry.get("predictions", [])
            uncertainty:  float           = float(metric_entry.get("uncertainty", 0.0))

            # Choose proactive sensitivity based on prediction confidence
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
                violations.append({
                    "metric":         metric,
                    "breach_type":    breach_type,
                    "severity":       self._severity(current_val, threshold, slope),
                    "slope":          slope,
                    "time_to_breach": time_to_breach,
                    "current_val":    current_val,
                    "threshold":      threshold,
                })

        return violations

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _analyze(
        self,
        preds: List[float],
        threshold: float,
        current_val: float,
        proactive_factor: float,
    ) -> Tuple[str, float, int]:
        """
        Classify the breach type for one metric.

        Parameters
        ----------
        preds:
            7-step ahead predictions.
        threshold:
            SLO threshold value.
        current_val:
            Current measured value.
        proactive_factor:
            Fraction of threshold used as the proactive detection trigger.
            ``proactive_threshold = threshold * proactive_factor``

        Returns
        -------
        (breach_type, slope, time_to_breach)
            ``breach_type`` is ``"reactive"``, ``"proactive"``, or ``"none"``.
            ``slope`` is ``preds[-1] - preds[0]`` (0.0 if fewer than 2 preds).
            ``time_to_breach`` is the first prediction index exceeding
            *threshold*, or ``len(preds)`` when no breach is forecast.
        """
        # Reactive: current value already exceeds threshold
        breach_reactive: bool = current_val > threshold

        # Slope of prediction series (positive = degrading trend)
        slope: float = (preds[-1] - preds[0]) if len(preds) >= 2 else 0.0

        # First step where prediction exceeds threshold
        time_to_breach: int = len(preds)
        for idx, p in enumerate(preds):
            if p > threshold:
                time_to_breach = idx
                break

        # Proactive conditions
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
        """
        Compute a dimensionless severity score for a violated SLO.

        Combines the relative excess over threshold with a slope penalty
        that accounts for how fast the metric is degrading.

        Parameters
        ----------
        current_val:
            Current measured value.
        threshold:
            SLO threshold.
        slope:
            Trend slope (``preds[-1] - preds[0]``).

        Returns
        -------
        float
            ``excess + 0.3 * slope_bonus`` where
            ``excess = max(0, current_val - threshold) / threshold`` and
            ``slope_bonus = max(0, slope) / threshold``.
        """
        if threshold <= 0:
            return 0.0
        excess:      float = max(0.0, current_val - threshold) / threshold
        slope_bonus: float = max(0.0, slope)      / threshold
        return excess + 0.3 * slope_bonus

    def _get_current_val(self, vm_data: Dict[str, Any], metric: str) -> float:
        """
        Extract the current measured value for *metric* from *vm_data*.

        Uses ``METRICS_REGISTRY[metric]["payload_key"]`` to resolve the
        actual field name in the payload dict — never hardcoded.

        Parameters
        ----------
        vm_data:
            Single VM entry from ``current_data``.
        metric:
            Metric name as declared in ``METRICS_REGISTRY``.

        Returns
        -------
        float
            Measured value, or ``0.0`` if the field is absent.
        """
        payload_key: str = config.METRICS_REGISTRY[metric].get("payload_key", metric)
        return float(vm_data.get(payload_key, 0.0))
