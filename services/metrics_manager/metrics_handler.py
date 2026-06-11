import logging
import math
import statistics
from typing import List, Dict, Any, Tuple, Optional

from shared import config
from shared.models import SLO

logger = logging.getLogger("MetricsHandler")


class MetricsHandler:
    """
    Gestionnaire statistique avancé pour scores MI, percentiles adaptatifs
    et gestion dynamique des SLOs.

    Architecture à deux niveaux :
      • SLOs PRIMAIRES → seuils FIXES (objectif métier ou LLM)
                         is_primary = True
      • SLOs SECONDAIRES → seuils ADAPTATIFS calculés par percentile
                           sur métriques corrélées via MI
                           is_primary = False
    """

    def __init__(self):
        self.registry = config.METRICS_REGISTRY

    # ─────────────────────────────────────────────────────────────
    # MI — Information Mutuelle Normalisée
    # ─────────────────────────────────────────────────────────────

    def compute_mi_scores(self, history: List[Dict[str, Any]]) -> Dict[str, float]:
        """
        Calcule le score MI entre chaque métrique et le signal de violation.
        Retourne un score normalisé dans [0, 1].
        """
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
            logger.debug(f"🔍 MI calculé — {metric} : {score:.4f}")

        logger.info(
            "📊 Scores MI calculés — "
            + "  ".join(f"{m} : {v:.3f}" for m, v in mi_results.items())
        )
        return mi_results

    def _compute_mi(self, x_vals: List[float], y_vals: List[int]) -> float:
        """MI normalisée via table de contingence 2×2 (discrétisation médiane)."""
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
        """Entropie de Shannon."""
        ent = 0.0
        for p in probs:
            if p > 0:
                ent -= p * math.log2(p)
        return ent

    # ─────────────────────────────────────────────────────────────
    # Percentile adaptatif — seuils dynamiques pour SLOs secondaires
    # ─────────────────────────────────────────────────────────────

    def _adaptive_percentile(self, vals: List[float]) -> Optional[float]:
        """
        Sélectionne automatiquement le percentile selon la volatilité (CV) :
          • CV < CV_LOW          → P_STABLE   (signal stable)
          • CV_LOW ≤ CV < CV_HIGH → P_NORMAL  (régime normal)
          • CV ≥ CV_HIGH         → P_VOLATILE (signal bruité)

        Cette logique n'est utilisée QUE pour les SLOs secondaires.
        Les SLOs primaires gardent leur seuil métier fixe.
        """
        if len(vals) < 5:
            return None

        mean = statistics.mean(vals)
        if mean == 0:
            return 0.0

        std = statistics.stdev(vals)
        cv  = std / mean

        if cv < config.CV_LOW:
            p_rank = config.PERCENTILE_STABLE
            regime = "stable"
        elif cv < config.CV_HIGH:
            p_rank = config.PERCENTILE_NORMAL
            regime = "normal"
        else:
            p_rank = config.PERCENTILE_VOLATILE
            regime = "volatile"

        logger.debug(
            f"🔍 Percentile adaptatif — P{p_rank} ({regime}) "
            f"| CV = {cv:.3f}"
        )

        sorted_vals = sorted(vals)
        idx = int(len(sorted_vals) * (p_rank / 100))
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    # ─────────────────────────────────────────────────────────────
    # Mode AUTONOMOUS — SLO primaire fixe + SLOs secondaires adaptatifs
    # ─────────────────────────────────────────────────────────────

    def select_dynamic_slos(
        self,
        mi_scores: Dict[str, float],
        all_vals: Dict[str, List[float]],
        history: List[Dict[str, Any]],
    ) -> Tuple[List[SLO], List[str]]:
        """
        Mode AUTONOMOUS :
          1. SLOs primaires (is_primary_objective=True dans registry)
             → seuil FIXE depuis default_threshold (objectif métier)
             → poids initial = 1.0
          2. Autres métriques : SLO secondaire SI MI > MI_RELATIVE_THRESHOLD
             → seuil ADAPTATIF (percentile selon volatilité)
             → poids initial = score MI
        """
        final_slos     = []
        active_metrics = []
        primary_metrics  : List[str] = []
        secondary_metrics: List[str] = []

        for metric, reg in self.registry.items():
            is_primary    = reg.get("is_primary_objective", False)
            mi_score      = mi_scores.get(metric, 0.0)
            is_correlated = mi_score > config.MI_RELATIVE_THRESHOLD

            # ── SLO PRIMAIRE : seuil métier fixe ─────────────────────
            if is_primary:
                threshold = self._clamp_to_bounds(metric, reg["default_threshold"])
                slo = SLO(
                    metric=metric,
                    operator=reg["operator"],
                    threshold=threshold,
                    unit=reg["unit"],
                    target=threshold * 0.9,
                    weight=1.0,
                    window="5m",
                    is_primary=True,
                )
                final_slos.append(slo)
                active_metrics.append(metric)
                primary_metrics.append(metric)
                logger.debug(
                    f"🎯 SLO PRIMAIRE — {metric} "
                    f"| seuil métier fixe : {threshold:.2f} {reg['unit']} "
                    f"| opérateur : {reg['operator']}"
                )
                continue

            # ── SLO SECONDAIRE : adaptatif SI corrélation MI ─────────
            if is_correlated:
                vals = [p.get(metric) for p in history if p.get(metric) is not None]
                threshold = self._adaptive_percentile(vals)
                if threshold is None:
                    logger.debug(
                        f"🔍 {metric} corrélée (MI={mi_score:.3f}) mais "
                        "historique insuffisant — SLO secondaire ignoré"
                    )
                    continue

                threshold = self._clamp_to_bounds(metric, threshold)
                slo = SLO(
                    metric=metric,
                    operator=reg["operator"],
                    threshold=threshold,
                    unit=reg["unit"],
                    target=threshold * 0.9,
                    weight=mi_score,
                    window="5m",
                    is_primary=False,
                )
                final_slos.append(slo)
                active_metrics.append(metric)
                secondary_metrics.append(metric)
                logger.debug(
                    f"📈 SLO SECONDAIRE — {metric} "
                    f"| seuil adaptatif : {threshold:.2f} {reg['unit']} "
                    f"| MI : {mi_score:.3f} | opérateur : {reg['operator']}"
                )
            else:
                logger.debug(
                    f"🔍 {metric} non corrélée (MI={mi_score:.3f} < "
                    f"{config.MI_RELATIVE_THRESHOLD}) — non incluse"
                )

        normalized = self._normalize_weights(final_slos)

        logger.info(
            f"✅ SLOs sélectionnés (mode AUTONOMOUS) — "
            f"{len(normalized)} SLO(s) actif(s) "
            f"| primaires : {primary_metrics} "
            f"| secondaires adaptatifs : {secondary_metrics}"
        )
        for s in normalized:
            tag = "PRIMAIRE  " if s.is_primary else "SECONDAIRE"
            logger.debug(
                f"🔍 [{tag}] {s.metric:<12} "
                f"seuil : {s.threshold:.2f} {s.unit:<3} "
                f"| opérateur : {s.operator}  poids : {s.weight:.2f}"
            )
        return normalized, active_metrics

    # ─────────────────────────────────────────────────────────────
    # Mode ENHANCED — SLOs du LLM (primaires) + SLOs secondaires MI
    # ─────────────────────────────────────────────────────────────

    def validate_and_enrich_slos(
        self,
        slos: List[SLO],
        mi_scores: Dict[str, float],
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[SLO], List[str]]:
        """
        Mode ENHANCED :
          1. Les SLOs fournis par le LLM deviennent les SLOs PRIMAIRES.
             → seuil et opérateur conservés tels quels (objectif utilisateur)
             → marqués is_primary=True
             → poids LLM × MI (combine intention + réalité observée)
          2. Pour les métriques non couvertes par le LLM mais corrélées
             via MI → ajout d'un SLO SECONDAIRE adaptatif.
        """
        final_slos       = []
        active_metrics   : List[str] = []
        primary_metrics  : List[str] = []
        secondary_metrics: List[str] = []

        # ── Étape 1 : SLOs primaires du LLM (seuils conservés) ──────
        covered_metrics: set = set()
        for s in slos:
            mi_score = mi_scores.get(s.metric, 0.1)

            s.is_primary = True
            s.threshold  = self._clamp_to_bounds(s.metric, s.threshold)
            s.target     = min(s.target, s.threshold * 0.95)
            # Combine poids LLM × MI pour ancrer sur la réalité observée
            s.weight     = max(0.01, s.weight * mi_score) if mi_score > 0 else max(0.01, s.weight)

            final_slos.append(s)
            active_metrics.append(s.metric)
            primary_metrics.append(s.metric)
            covered_metrics.add(s.metric)

            logger.debug(
                f"🎯 SLO PRIMAIRE (LLM) — {s.metric} "
                f"| seuil : {s.threshold:.2f} {s.unit} "
                f"| MI : {mi_score:.3f} | poids combiné : {s.weight:.3f}"
            )

        # ── Étape 2 : SLOs secondaires pour métriques corrélées non couvertes
        if history:
            for metric, reg in self.registry.items():
                if metric in covered_metrics:
                    continue

                mi_score = mi_scores.get(metric, 0.0)
                if mi_score <= config.MI_RELATIVE_THRESHOLD:
                    continue

                vals = [p.get(metric) for p in history if p.get(metric) is not None]
                threshold = self._adaptive_percentile(vals)
                if threshold is None:
                    logger.debug(
                        f"🔍 {metric} corrélée (MI={mi_score:.3f}) mais "
                        "historique insuffisant — SLO secondaire ignoré"
                    )
                    continue

                threshold = self._clamp_to_bounds(metric, threshold)
                slo = SLO(
                    metric=metric,
                    operator=reg["operator"],
                    threshold=threshold,
                    unit=reg["unit"],
                    target=threshold * 0.9,
                    weight=mi_score,
                    window="5m",
                    is_primary=False,
                )
                final_slos.append(slo)
                active_metrics.append(metric)
                secondary_metrics.append(metric)

                logger.debug(
                    f"📈 SLO SECONDAIRE — {metric} "
                    f"| seuil adaptatif : {threshold:.2f} {reg['unit']} "
                    f"| MI : {mi_score:.3f}"
                )

        normalized = self._normalize_weights(final_slos)

        logger.info(
            f"✅ SLOs validés (mode ENHANCED) — "
            f"{len(normalized)} SLO(s) actif(s) "
            f"| primaires (LLM) : {primary_metrics} "
            f"| secondaires adaptatifs : {secondary_metrics}"
        )
        for s in normalized:
            tag = "PRIMAIRE  " if s.is_primary else "SECONDAIRE"
            logger.debug(
                f"🔍 [{tag}] {s.metric:<12} "
                f"seuil : {s.threshold:.2f} {s.unit:<3} "
                f"| poids : {s.weight:.2f}  cible : {s.target:.2f}"
            )
        return normalized, active_metrics

    # ─────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────

    def _clamp_to_bounds(self, metric: str, val: float) -> float:
        """Borne un seuil aux limites physiques de la métrique."""
        if metric not in self.registry:
            return val
        b = self.registry[metric]["bounds"]
        return max(b["min"], min(b["max"], val))

    def _normalize_weights(self, slos: List[SLO]) -> List[SLO]:
        """Normalise les poids pour qu'ils somment à 1.0."""
        if not slos:
            return []
        total = sum(s.weight for s in slos)
        for s in slos:
            s.weight = round(s.weight / total, 2) if total > 0 else 1.0 / len(slos)
        return slos