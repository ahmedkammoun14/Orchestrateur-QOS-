import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared import config
from shared.timing import StepProfiler
from services.decision_intelligence.violation_detector import ViolationDetector
from services.decision_intelligence.topsis import TopsisSelector, vm_satisfies_slo

logger = logging.getLogger("DecisionIntelligence.handler")

# Marge d'hystérésis anti-ping-pong (dead-band). On ne migre que si le
# meilleur candidat dépasse la VM ACTIVE de cette fraction. À 0.0, la règle
# « meilleur > actif » est déjà le comportement par défaut et produit le
# va-et-vient edge↔cloud ; 0.05 = 5 % de marge nette pour le stopper.
_MIGRATION_MARGIN: float = 0.05


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
        reliability_scores: Dict       = payload.get("reliability_scores", {})
        cycle:              int        = int(payload.get("cycle", 0))
        mi_scores:          Dict       = payload.get("mi_scores", {})

        ts: str = datetime.now(timezone.utc).isoformat()

        # Profiler des sous-étapes de la décision (détection, filtrage, TOPSIS).
        # Ses mesures sont renvoyées au hub via la clé "timings" du résultat.
        prof = StepProfiler(logger=logger, context=f"DI #{cycle}")

        def _ret(result: Dict[str, Any]) -> Dict[str, Any]:
            result["timings"] = prof.as_dict()
            return result

        # ── Bannière traçabilité cycle ────────────────────────────────
        sep = "═" * 62
        slo_lines = []
        for s in slos:
            mi_val = mi_scores.get(s["metric"])
            kind   = "PRIMAIRE" if s.get("is_primary") else f"SECONDAIRE MI={mi_val:.3f}" if mi_val is not None else "SECONDAIRE"
            slo_lines.append(
                f"    • {s['metric']:<12} seuil={s['threshold']:.1f}{s.get('unit','')}  "
                f"poids={s['weight']:.2f}  [{kind}]"
            )
        slo_block = "\n".join(slo_lines) if slo_lines else "    (aucun SLO reçu)"
        logger.info(
            f"\n  {sep}\n"
            f"  🎯  Decision Intelligence — Cycle #{cycle}\n"
            f"  VM active : {service_vm}\n"
            f"  SLOs issus du Cycle #{cycle} (Metrics Manager) :\n"
            f"{slo_block}\n"
            f"  {sep}"
        )

        # ── Étape 1 : Cooldown (défensif) ────────────────────────────
        if cooldown_active:
            logger.info("⏳ Cooldown actif — décision STAY immédiate")
            return _ret(self._build_stay(
                "cooldown_active", None, None, ts, self._slos_detail(slos)
            ))

        # ── Étape 2 : Détection de violations sur service_vm ─────────
        with prof.step("violation_detection"):
            violations: List[Dict] = self._detector.detect(
                current_data, predictions_map, slos, service_vm
            )

        if violations:
            summary = "  ".join(
                f"{v['metric']}({v['breach_type']},sev={v['severity']:.3f})"
                for v in violations
            )
            logger.info(
                f"⚠️  {len(violations)} violation(s) détectée(s) "
                f"sur {service_vm} — {summary}"
            )
        else:
            logger.info(
                f"✅ Aucune violation SLO sur {service_vm} — décision STAY"
            )

        # ── Étape 3 : GATE — migration déclenchée UNIQUEMENT par une
        #     violation de la métrique PRIMAIRE (latency en autonomous).
        #     Une violation secondaire seule (cpu/ram) ne déclenche PAS :
        #     ce sont des co-facteurs corrélés, pas l'objectif métier. ──
        primary_metrics: set = {s["metric"] for s in slos if s.get("is_primary")}
        primary_violations: List[Dict] = [
            v for v in violations if v["metric"] in primary_metrics
        ]

        if not primary_violations:
            reason = (
                "Secondary-only violation (no primary/latency breach) — no migration"
                if violations else "No SLO violation detected"
            )
            logger.info(f"✅ {reason} — décision STAY")
            return _ret(self._build_stay(
                reason, None, None, ts, self._slos_detail(slos)
            ))

        # Toute violation détectée (réelle ou prédite par le ML) est
        # directement actionnable — pas de filtre de sévérité : dès que
        # ViolationDetector signale une violation, TOPSIS est évalué pour
        # voir s'il existe une meilleure VM disponible.
        #
        # Le type affiché (proactif/réactif) reflète le statut du SLO
        # PRIMAIRE (latency), pas "la pire métrique parmi toutes" : un
        # seuil adaptatif secondaire (ex. ram_usage) colle souvent de très
        # près à la valeur réelle et bascule en réactif au moindre bruit de
        # mesure, ce qui masquait une détection proactive parfaitement
        # valide sur la métrique qui compte réellement pour la démo.
        primary_metric = next(
            (s["metric"] for s in slos if s.get("is_primary")), None
        )
        primary_violation = next(
            (v for v in violations if v["metric"] == primary_metric), None
        )
        if primary_violation is not None:
            breach_type = primary_violation["breach_type"]
        else:
            breach_type = (
                "reactive"
                if any(v["breach_type"] == "reactive" for v in violations)
                else "proactive"
            )
        violated_metrics: List[str] = [v["metric"] for v in violations]
        slos_detail: List[Dict[str, Any]] = self._slos_detail(slos)

        # ── Étape 4 : Candidats = toutes les VMs (VM active incluse) ──────
        # La VM active est incluse dans le pool TOPSIS pour comparer si rester
        # vaut mieux que migrer. Si TOPSIS la sélectionne → STAY (cf. étape 6).
        all_candidates: List[Dict] = list(current_data)

        if not all_candidates:
            logger.warning(
                f"⚠️  Violation {breach_type} sur {violated_metrics} "
                "— aucune VM cible disponible"
            )
            return _ret(self._build_stay(
                f"{breach_type} violation on {violated_metrics} — "
                "no migration targets available",
                breach_type, None, ts, slos_detail,
            ))

        with prof.step("candidate_filter"):
            candidates = self._filter_candidates(all_candidates, predictions_map, slos)
        preferred  = len(candidates) < len(all_candidates)
        logger.info(
            f"🔎 Candidats TOPSIS : {[c['vm_id'] for c in candidates]} "
            f"| {'SLOs pré-satisfaits' if preferred else 'fallback tous candidats (VM active incluse)'}"
        )

        # ── Étape 5 : Sélection TOPSIS ───────────────────────────────
        with prof.step("topsis_total"):
            best_candidate, topsis_score, vm_scores = self._topsis.select(
                candidates         = candidates,
                predictions_map    = predictions_map,
                slos               = slos,
                reliability_scores = reliability_scores,
                profiler           = prof,
            )

        if not best_candidate:
            logger.warning(
                f"⚠️  TOPSIS n'a retourné aucun candidat "
                f"— violation {breach_type} non résolue"
            )
            return _ret(self._build_stay(
                f"{breach_type} violation — TOPSIS returned no candidate",
                breach_type, None, ts, slos_detail,
            ))

        to_vm: str = best_candidate["vm_id"]
        logger.info(
            f"\n{'─'*50}\n"
            f"  🎯 TOPSIS — meilleur candidat : {to_vm}\n"
            f"  {'Score':<14}: {topsis_score}\n"
            f"  {'Type breach':<14}: {breach_type}\n"
            f"  {'Métriques':<14}: {', '.join(violated_metrics)}\n"
            f"{'─'*50}"
        )

        # ── Étape 6 : Hystérésis anti-ping-pong ──────────────────────
        # On ne migre que si le meilleur candidat dépasse la VM ACTIVE
        # d'une marge nette (_MIGRATION_MARGIN). Sinon STAY. Si la VM active
        # n'est pas dans le pool (elle ne satisfait aucun SLO alors que des
        # VMs « propres » existent), active_score=0 → on migre librement
        # vers la VM propre (on ne reste pas sur une VM qui viole).
        active_score: float = vm_scores.get(service_vm, 0.0)
        if to_vm == service_vm or topsis_score <= active_score * (1.0 + _MIGRATION_MARGIN):
            reason = (
                f"{breach_type} violation but {service_vm} is still best "
                f"candidate (score={topsis_score})"
                if to_vm == service_vm else
                f"{breach_type} violation — best {to_vm} ({topsis_score}) "
                f"n'excède pas l'actif {service_vm} ({active_score}) "
                f"de la marge {_MIGRATION_MARGIN:.0%} → STAY"
            )
            logger.info(f"🟢 {reason}")
            return _ret(self._build_stay(
                reason, breach_type, topsis_score, ts, slos_detail, vm_scores,
            ))

        # ── Étape 7 : Décision MIGRATE ───────────────────────────────
        return _ret({
            "decision":         "migrate",
            "from_vm":          service_vm,
            "to_vm":            to_vm,
            "reason":       (
                f"{breach_type} violation on "
                f"{', '.join(violated_metrics)} — "
                f"TOPSIS selected {to_vm!r} (score={topsis_score})"
            ),
            "topsis_score":     topsis_score,
            "breach_type":      breach_type,
            "violated_metrics": slos_detail,
            "timestamp":        ts,
            # Classement TOPSIS complet (toutes les VMs du pool, pas
            # seulement la gagnante) — additif, nécessaire pour justifier
            # "pourquoi cette VM plutôt qu'une autre" côté dashboard.
            "vm_scores":        vm_scores,
        })

    def _filter_candidates(
        self,
        all_candidates: List[Dict[str, Any]],
        predictions_map: Dict[str, Any],
        slos: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        def _satisfies_all(cand: Dict[str, Any]) -> bool:
            vm_id = cand["vm_id"]
            for slo in slos:
                metric = slo["metric"]
                if metric not in config.METRICS_REGISTRY:
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
                # SLO exprimé en ressource absolue (cœurs/Go, intention LLM
                # mode enhanced) plutôt qu'en % (mode autonomous) : convertir
                # la prédiction brute (%) en disponibilité réelle de CETTE VM
                # avant comparaison — sinon on compare un % à un nombre de
                # cœurs, ce qui n'a pas de sens. Même conversion que celle
                # déjà utilisée côté scoring TOPSIS (_to_criterion_value).
                if slo.get("unit") in ("cores", "GB"):
                    mean = self._topsis._to_criterion_value(metric, cand, mean)
                if not vm_satisfies_slo(mean, slo):
                    return False
            return True

        preferred: List[Dict] = [
            c for c in all_candidates if _satisfies_all(c)
        ]
        return preferred if preferred else all_candidates

    @staticmethod
    def _slos_detail(slos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Nom + poids TOPSIS normalisé de CHAQUE SLO actif ce cycle (somme=1),
        violé ou non — pour affichage dans la colonne Métriques du dashboard."""
        return [
            {"metric": s["metric"], "weight": round(float(s.get("weight") or 0.0), 3)}
            for s in slos
        ]

    @staticmethod
    def _build_stay(
        reason: str,
        breach_type: Optional[str],
        topsis_score: Optional[float],
        ts: str,
        violated_metrics: Optional[List[str]] = None,
        vm_scores: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """
        `vm_scores` : classement TOPSIS complet, uniquement disponible pour le
        STAY issu de l'hystérésis (TOPSIS a réellement tourné). Défaut `None`
        pour que les appels existants (cooldown, absence de violation,
        absence de candidat — TOPSIS jamais atteint) restent inchangés : la
        clé est alors ABSENTE du dict renvoyé, pas de régression de forme.
        """
        result: Dict[str, Any] = {
            "decision":         "stay",
            "from_vm":          None,
            "to_vm":            None,
            "reason":           reason,
            "topsis_score":     topsis_score,
            "breach_type":      breach_type,
            "violated_metrics": violated_metrics or [],
            "timestamp":        ts,
        }
        if vm_scores is not None:
            result["vm_scores"] = vm_scores
        return result