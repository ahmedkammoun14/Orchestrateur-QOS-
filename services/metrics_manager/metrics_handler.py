import logging
import math
import statistics
from typing import List, Dict, Any, Tuple, Optional

import numpy as np
from scipy.special import digamma

from shared import config
from shared.models import SLO

logger = logging.getLogger("MetricsHandler")


def _fmt_table(title: str, headers: List[str], rows: List[List[str]]) -> str:
    col_w = [
        max(len(h), max((len(r[i]) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    top  = "┌" + "┬".join("─" * (w + 2) for w in col_w) + "┐"
    hdr  = "│" + "│".join(f" {h:<{w}} " for h, w in zip(headers, col_w)) + "│"
    sep  = "├" + "┼".join("─" * (w + 2) for w in col_w) + "┤"
    bot  = "└" + "┴".join("─" * (w + 2) for w in col_w) + "┘"
    data = ["│" + "│".join(f" {c:<{w}} " for c, w in zip(row, col_w)) + "│" for row in rows]
    return "\n  " + title + "\n  " + "\n  ".join([top, hdr, sep] + data + [bot])


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

    def compute_mi_scores(self, history: List[Dict[str, Any]], cycle: int = 0) -> Dict[str, float]:
        """
        Calcule le score MI entre chaque métrique et le signal de violation.
        Retourne un score normalisé dans [0, 1].
        """
        mi_results = {}
        y_vals = [1 if p.get("is_violation", False) else 0 for p in history]

        # ── Tableau historique des cycles ────────────────────────────
        metric_keys = list(self.registry.keys())
        hist_headers = ["Cycle"] + metric_keys + ["Violation"]
        hist_rows = []
        for idx, p in enumerate(history, start=1):
            row = [str(idx)]
            for m in metric_keys:
                v = p.get(m)
                row.append(f"{v:.1f}" if v is not None else "—")
            row.append("oui" if p.get("is_violation", False) else "non")
            hist_rows.append(row)
        logger.info(_fmt_table(
            f"📜 Historique des cycles ({len(history)} points)",
            hist_headers, hist_rows,
        ))

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

            score = self._compute_mi(x_vals, synced_y, metric, cycle)
            mi_results[metric] = score

        # ── Tableau récapitulatif MI ─────────────────────────────────
        mi_rows = [
            [m, f"{v:.4f}", "✅ retenu" if v > config.MI_RELATIVE_THRESHOLD else "❌ ignoré"]
            for m, v in mi_results.items()
        ]
        logger.info(_fmt_table(
            f"📊 Scores MI (seuil = {config.MI_RELATIVE_THRESHOLD})",
            ["Métrique", "MI score", f"Décision (> {config.MI_RELATIVE_THRESHOLD})"],
            mi_rows,
        ))
        return mi_results

    def _compute_mi(
        self,
        x_vals: List[float],
        y_vals: List[int],
        metric: str = "",
        cycle: int  = 0,
    ) -> float:
        """
        MI continue (X métrique continue, Y violation binaire).
        Estimateur différentiel Kozachenko-Leonenko (k-NN).
        MI(X;Y) = H(X) − H(X|Y),  normalisé par H(Y) → [0, 1].
        """
        x = np.array(x_vals, dtype=float)
        y = np.array(y_vals, dtype=int)
        n = len(x)

        classes, counts = np.unique(y, return_counts=True)
        if len(classes) < 2:
            return 0.0

        k = max(3, min(5, n // 10))

        sep = "═" * 62

        # ── Bannière cycle ────────────────────────────────────────
        logger.info(
            f"\n  {sep}\n"
            f"  🔬  MI k-NN — Cycle #{cycle} | Métrique : {metric}\n"
            f"  n = {n} points   k = {k} voisins (plus proches)\n"
            f"  {sep}"
        )

        # ── ÉTAPE 1 — H(X) entropie globale ──────────────────────
        hx, rk_all = self._knn_entropy_ex(x, k)
        valid_mask = rk_all > 0
        valid_rk   = rk_all[valid_mask]
        psi_n      = float(digamma(n))
        psi_k      = float(digamma(k))
        mean_log_r = float(np.mean(np.log(valid_rk))) if len(valid_rk) > 0 else 0.0

        if valid_rk.size > 0:
            max_idx = int(np.argmax(rk_all))
            min_rk  = float(valid_rk.min())
            min_idx = int(np.where(rk_all == min_rk)[0][0])
            tbl1 = _fmt_table(
                "Points représentatifs",
                ["Rôle", f"Valeur ({self.registry.get(metric, {}).get('unit','%')})", f"r_{k}", "ln(r_k)"],
                [
                    ["max r_k  ← outlier",
                     f"{x[max_idx]:.1f}",
                     f"{rk_all[max_idx]:.2f}",
                     f"{np.log(rk_all[max_idx]):.3f}"],
                    ["min r_k  ← cluster dense",
                     f"{x[min_idx]:.1f}",
                     f"{min_rk:.2f}",
                     f"{np.log(min_rk):.3f}"],
                ],
            )
        else:
            tbl1 = ""

        logger.info(
            f"\n  ÉTAPE 1 — H(X) : Entropie différentielle globale{tbl1}\n"
            f"  H(X) = ψ({n}) − ψ({k}) + ln(2) + mean(ln r_{k})\n"
            f"       = {psi_n:.3f} − {psi_k:.3f} + {np.log(2):.3f} + ({mean_log_r:.3f})\n"
            f"       = {hx:.3f} nats"
        )

        # ── ÉTAPES 2 & 3 — H(X|Y=c) par classe ──────────────────
        hx_given_y = 0.0
        class_info: List[Tuple[int, int, float]] = []  # (c, cnt, h_c)

        # Affiche violation (Y=1) avant normale (Y=0) pour suivre le schéma
        ordered = sorted(zip(classes, counts), key=lambda t: -t[0])
        step_num = 2
        for c, cnt in ordered:
            label = "violation" if c == 1 else "normale"
            x_c   = x[y == c]
            if len(x_c) > k + 1:
                h_c, rk_c = self._knn_entropy_ex(x_c, k)
                valid_rk_c = rk_c[rk_c > 0]
                psi_nc     = float(digamma(len(x_c)))
                mean_log_rc = float(np.mean(np.log(valid_rk_c))) if len(valid_rk_c) > 0 else 0.0
                max_c_idx  = int(np.argmax(rk_c))
                rk_range   = (f"r_{k} ∈ [{valid_rk_c.min():.1f} — {valid_rk_c.max():.1f}]"
                               if valid_rk_c.size > 0 else "")
                logger.info(
                    f"\n  ÉTAPE {step_num} — H(X|Y={c}) : Classe {label} (n={cnt})\n"
                    f"  Outlier max r_{k} = {rk_c[max_c_idx]:.2f} "
                    f"à val = {x_c[max_c_idx]:.1f}   {rk_range}\n"
                    f"  H(X|Y={c}) = ψ({cnt}) − ψ({k}) + ln(2) + mean(ln r_{k})\n"
                    f"            = {psi_nc:.3f} − {psi_k:.3f} + {np.log(2):.3f} + ({mean_log_rc:.3f})\n"
                    f"            = {h_c:.3f} nats"
                )
            else:
                h_c = hx
                logger.info(
                    f"\n  ÉTAPE {step_num} — H(X|Y={c}) : Classe {label} (n={cnt})\n"
                    f"  ⚠  n={cnt} ≤ k+1={k+1} → estimation conservatrice "
                    f"H(X|Y={c}) ← H(X) = {hx:.3f} nats"
                )
            hx_given_y += (cnt / n) * h_c
            class_info.append((c, cnt, h_c))
            step_num += 1

        # ── ÉTAPE 4 — H(X|Y) pondérée ────────────────────────────
        terms_str    = " + ".join(
            f"({cnt}/{n})×{h_c:.3f}" for _, cnt, h_c in class_info
        )
        weighted_str = " + ".join(
            f"{cnt/n:.3f}×{h_c:.3f}" for _, cnt, h_c in class_info
        )
        logger.info(
            f"\n  ÉTAPE 4 — H(X|Y) : Entropie conditionnelle pondérée\n"
            f"  H(X|Y) = {terms_str}\n"
            f"         = {weighted_str}\n"
            f"         = {hx_given_y:.3f} nats"
        )

        # ── ÉTAPE 5 — Score final ─────────────────────────────────
        mi_nats = hx - hx_given_y
        probs   = counts / n
        hy      = float(-np.sum(probs * np.log(probs + 1e-12)))
        score   = 0.0 if hy <= 0 else max(0.0, min(1.0, mi_nats / hy))
        hy_str  = " + ".join(f"{p:.3f}×ln({p:.3f})" for p in probs)
        verdict = "✅ retenu" if score > config.MI_RELATIVE_THRESHOLD else "❌ ignoré"
        logger.info(
            f"\n  ÉTAPE 5 — Score final\n"
            f"  MI    = H(X) − H(X|Y) = {hx:.3f} − {hx_given_y:.3f} = {mi_nats:.3f} nats\n"
            f"  H(Y)  = −[{hy_str}] = {hy:.3f} nats\n"
            f"  Score = MI / H(Y) = {mi_nats:.3f} / {hy:.3f} = {score:.3f}   {verdict}\n"
            f"  {sep}"
        )
        return score

    @staticmethod
    def _knn_entropy_ex(vals: np.ndarray, k: int) -> Tuple[float, np.ndarray]:
        """Kozachenko-Leonenko 1-D — retourne (entropie, r_k array)."""
        n = len(vals)
        if n <= k + 1:
            return 0.0, np.zeros(n)
        dists = np.abs(vals[:, None] - vals[None, :])
        np.fill_diagonal(dists, np.inf)
        r_k   = np.partition(dists, k - 1, axis=1)[:, k - 1]
        valid = r_k > 0
        if valid.sum() == 0:
            return 0.0, r_k
        log_r = np.where(valid, np.log(r_k), 0.0)
        return float(digamma(n) - digamma(k) + np.log(2) + np.mean(log_r[valid])), r_k

    @staticmethod
    def _knn_entropy(vals: np.ndarray, k: int) -> float:
        """Kozachenko-Leonenko 1-D — retourne entropie seule."""
        h, _ = MetricsHandler._knn_entropy_ex(vals, k)
        return h

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

        slo_rows = [
            [
                s.metric,
                "PRIMAIRE" if s.is_primary else "SECONDAIRE",
                s.operator,
                f"{s.threshold:.2f} {s.unit}",
                f"{s.weight:.2f}",
            ]
            for s in normalized
        ]
        logger.info(_fmt_table(
            f"✅ SLOs sélectionnés (mode AUTONOMOUS) — {len(normalized)} actif(s)",
            ["Métrique", "Type", "Opérateur", "Seuil", "Poids normalisé"],
            slo_rows,
        ))
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

        slo_rows = [
            [
                s.metric,
                "PRIMAIRE (LLM)" if s.is_primary else "SECONDAIRE",
                s.operator,
                f"{s.threshold:.2f} {s.unit}",
                f"{s.weight:.2f}",
            ]
            for s in normalized
        ]
        logger.info(_fmt_table(
            f"✅ SLOs validés (mode ENHANCED) — {len(normalized)} actif(s)",
            ["Métrique", "Type", "Opérateur", "Seuil", "Poids normalisé"],
            slo_rows,
        ))
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