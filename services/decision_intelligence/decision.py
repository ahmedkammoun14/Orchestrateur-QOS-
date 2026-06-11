import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.decision_intelligence.violation_detector import ViolationDetector
from services.decision_intelligence.topsis import TopsisSelector, vm_satisfies_slo

logger = logging.getLogger("DecisionIntelligence.handler")


class DecisionHandler:
    """
    Orchestrates the full decision pipeline for one evaluation cycle.
    Stateless — no Redis, no external calls, no side effects.
    """

    def __init__(self) -> None:
        self._detector = ViolationDetector()
        self._topsis   = TopsisSelector()

    def decide(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        current_data:       List[Dict] = payload["current_data"]
        predictions_map:    Dict       = payload.get("predictions_map", {})
        slos:               List[Dict] = payload.get("slos", [])
        service_vm:         str        = payload["service_vm"]
        cooldown_active:    bool       = bool(payload.get("cooldown_active", False))
        migration_costs:    Dict       = payload.get("migration_costs", {})
        reliability_scores: Dict       = payload.get("reliability_scores", {})

        ts: str = datetime.now(timezone.utc).isoformat()

        # ── Étape 1 : Cooldown (défensif) ────────────────────────────
        if cooldown_active:
            logger.info("⏳ Cooldown actif — décision STAY immédiate")
            return self._build_stay("cooldown_active", None, None, ts)

        # ── Étape 2 : Détection de violations sur service_vm ─────────
        violations: List[Dict] = self._detector.detect(
            current_data, predictions_map, slos, service_vm
        )

        if violations:
            summary = "  ".join(
                f"{v['metric']}({v['breach_type']})" for v in violations
            )
            logger.info(
                f"⚠️  {len(violations)} violation(s) détectée(s) "
                f"sur {service_vm} — {summary}"
            )
        else:
            logger.info(
                f"✅ Aucune violation SLO sur {service_vm} — décision STAY"
            )

        # ── Étape 3 : Aucune violation → stay ────────────────────────
        if not violations:
            return self._build_stay("No SLO violation detected", None, None, ts)

        breach_type: str = (
            "reactive"
            if any(v["breach_type"] == "reactive" for v in violations)
            else "proactive"
        )
        violated_metrics: List[str] = [v["metric"] for v in violations]

        # ── Étape 4 : Filtrage des candidats ─────────────────────────
        all_candidates: List[Dict] = [
            v for v in current_data if v["vm_id"] != service_vm
        ]

        if not all_candidates:
            logger.warning(
                f"⚠️  Violation {breach_type} sur {violated_metrics} "
                "— aucune VM cible disponible"
            )
            return self._build_stay(
                f"{breach_type} violation on {violated_metrics} — "
                "no migration targets available",
                breach_type, None, ts,
            )

        candidates = self._filter_candidates(all_candidates, predictions_map, slos)
        preferred  = len(candidates) < len(all_candidates)
        logger.info(
            f"🔎 Candidats TOPSIS : {[c['vm_id'] for c in candidates]} "
            f"| {'SLOs pré-satisfaits' if preferred else 'fallback tous candidats'}"
        )

        # ── Étape 5 : Sélection TOPSIS ───────────────────────────────
        best_candidate, topsis_score = self._topsis.select(
            candidates         = candidates,
            predictions_map    = predictions_map,
            slos               = slos,
            migration_costs    = migration_costs,
            reliability_scores = reliability_scores,
        )

        if not best_candidate:
            logger.warning(
                f"⚠️  TOPSIS n'a retourné aucun candidat "
                f"— violation {breach_type} non résolue"
            )
            return self._build_stay(
                f"{breach_type} violation — TOPSIS returned no candidate",
                breach_type, None, ts,
            )

        to_vm: str = best_candidate["vm_id"]
        logger.info(
            f"\n{'─'*50}\n"
            f"  🎯 TOPSIS — meilleur candidat : {to_vm}\n"
            f"  {'Score':<14}: {topsis_score}\n"
            f"  {'Type breach':<14}: {breach_type}\n"
            f"  {'Métriques':<14}: {', '.join(violated_metrics)}\n"
            f"{'─'*50}"
        )

        # ── Étape 6 : Décision MIGRATE ───────────────────────────────
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

    def _filter_candidates(
        self,
        all_candidates: List[Dict[str, Any]],
        predictions_map: Dict[str, Any],
        slos: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
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
        return {
            "decision":     "stay",
            "from_vm":      None,
            "to_vm":        None,
            "reason":       reason,
            "topsis_score": topsis_score,
            "breach_type":  breach_type,
            "timestamp":    ts,
        }