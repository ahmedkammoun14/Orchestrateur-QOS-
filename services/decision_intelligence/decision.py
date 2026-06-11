import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.decision_intelligence.violation_detector import ViolationDetector
from services.decision_intelligence.topsis import TopsisSelector, vm_satisfies_slo

# Child logger — propagates to the "DecisionIntelligence" parent logger
# configured in app.py, inheriting its JSON formatter automatically.
logger = logging.getLogger("DecisionIntelligence.handler")


class DecisionHandler:
    """
    Orchestrates the full decision pipeline for one evaluation cycle.

    Pipeline
    --------
    1. Cooldown guard (defensive, primary check is in app.py).
    2. ViolationDetector.detect() — analyse service_vm across all SLOs.
    3. No violations → ``"stay"``.
    4. Filter candidate VMs:

       a. Exclude service_vm.
       b. Prefer VMs whose predicted means already satisfy **all** SLOs.
       c. Fallback to all non-service VMs if none qualify.

    5. TopsisSelector.select() — rank filtered candidates.
    6. Return ``"migrate"`` to best VM, or ``"stay"`` if no valid target.

    Stateless — no Redis, no external calls, no side effects.
    """

    def __init__(self) -> None:
        self._detector = ViolationDetector()
        self._topsis   = TopsisSelector()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decide(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate the core payload and emit a migration decision.

        Parameters
        ----------
        payload:
            Full decision payload as sent by the orchestrator core.

        Returns
        -------
        dict
            ``{"decision", "from_vm", "to_vm", "reason",
               "topsis_score", "breach_type", "timestamp"}``
        """
        current_data:       List[Dict] = payload["current_data"]
        predictions_map:    Dict       = payload.get("predictions_map", {})
        slos:               List[Dict] = payload.get("slos", [])
        service_vm:         str        = payload["service_vm"]
        cooldown_active:    bool       = bool(payload.get("cooldown_active", False))
        migration_costs:    Dict       = payload.get("migration_costs", {})
        reliability_scores: Dict       = payload.get("reliability_scores", {})

        ts: str = datetime.now(timezone.utc).isoformat()

        # ------------------------------------------------------------------
        # Step 1: Cooldown guard (defensive — app.py is the primary check)
        # ------------------------------------------------------------------
        if cooldown_active:
            logger.info("Cooldown active — staying", extra={})
            return self._build_stay("cooldown_active", None, None, ts)

        # ------------------------------------------------------------------
        # Step 2: Violation detection on service_vm
        # ------------------------------------------------------------------
        violations: List[Dict] = self._detector.detect(
            current_data, predictions_map, slos, service_vm
        )

        logger.info(
            f"Violations detected: {len(violations)} — "
            f"service_vm={service_vm!r} "
            f"metrics={[v['metric'] for v in violations]}"
        )

        # ------------------------------------------------------------------
        # Step 3: No violations → stay
        # ------------------------------------------------------------------
        if not violations:
            return self._build_stay(
                "No SLO violation detected", None, None, ts
            )

        # Dominant breach type: reactive > proactive
        breach_type: str = (
            "reactive"
            if any(v["breach_type"] == "reactive" for v in violations)
            else "proactive"
        )
        violated_metrics: List[str] = [v["metric"] for v in violations]

        # ------------------------------------------------------------------
        # Step 4: Filter candidate VMs
        # ------------------------------------------------------------------
        all_candidates: List[Dict] = [
            v for v in current_data if v["vm_id"] != service_vm
        ]

        if not all_candidates:
            return self._build_stay(
                f"{breach_type} violation on {violated_metrics} — "
                f"no migration targets available",
                breach_type,
                None,
                ts,
            )

        candidates = self._filter_candidates(
            all_candidates, predictions_map, slos
        )

        logger.info(
            f"TOPSIS candidates: {[c['vm_id'] for c in candidates]} "
            f"(preferred={len(candidates) < len(all_candidates)})",
            extra={}
        )

        # ------------------------------------------------------------------
        # Step 5: TOPSIS selection
        # ------------------------------------------------------------------
        best_candidate, topsis_score = self._topsis.select(
            candidates         = candidates,
            predictions_map    = predictions_map,
            slos               = slos,
            migration_costs    = migration_costs,
            reliability_scores = reliability_scores,
        )

        if not best_candidate:
            return self._build_stay(
                f"{breach_type} violation — TOPSIS returned no candidate",
                breach_type,
                None,
                ts,
            )

        to_vm: str = best_candidate["vm_id"]

        logger.info(
            f"TOPSIS selected {to_vm!r} score={topsis_score} "
            f"breach={breach_type!r}"
        )

        # ------------------------------------------------------------------
        # Step 6: Emit migrate decision
        # ------------------------------------------------------------------
        return {
            "decision":     "migrate",
            "from_vm":      service_vm,
            "to_vm":        to_vm,
            "reason":       (
                f"{breach_type} violation on "
                f"{', '.join(violated_metrics)} — "
                f"TOPSIS selected {to_vm!r} (score={topsis_score})"
            ),
            "topsis_score": topsis_score,
            "breach_type":  breach_type,
            "timestamp":    ts,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _filter_candidates(
        self,
        all_candidates: List[Dict[str, Any]],
        predictions_map: Dict[str, Any],
        slos: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Return a prioritised subset of candidate VMs.

        Preference: VMs whose weighted-mean predictions already satisfy
        **all** active SLOs.  If none qualify, falls back to the full
        *all_candidates* list so TOPSIS always has at least one option.

        Parameters
        ----------
        all_candidates:
            All VMs except service_vm.
        predictions_map:
            Predictions indexed by vm_id.
        slos:
            Active SLO list.
        """
        def _satisfies_all(vm_id: str) -> bool:
            for slo in slos:
                metric = slo["metric"]
                if metric not in [s["metric"] for s in slos]:
                    continue
                preds: List[float] = (
                    predictions_map
                    .get(vm_id, {})
                    .get(metric, {})
                    .get("predictions", [])
                )
                if not preds:
                    return False
                mean = self._topsis.calculate_weighted_mean(preds)
                if not vm_satisfies_slo(mean, slo):
                    return False
            return True

        preferred: List[Dict] = [
            c for c in all_candidates if _satisfies_all(c["vm_id"])
        ]
        return preferred if preferred else all_candidates

    @staticmethod
    def _build_stay(
        reason: str,
        breach_type: Optional[str],
        topsis_score: Optional[float],
        ts: str,
    ) -> Dict[str, Any]:
        """Build a normalised ``"stay"`` response dict."""
        return {
            "decision":     "stay",
            "from_vm":      None,
            "to_vm":        None,
            "reason":       reason,
            "topsis_score": topsis_score,
            "breach_type":  breach_type,
            "timestamp":    ts,
        }
