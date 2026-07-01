import math
import logging
from typing import Any, Dict, List, Optional, Tuple

from shared import config
from shared.timing import StepProfiler

logger = logging.getLogger("DecisionIntelligence.handler")


def _fmt_table(title: str, headers: List[str], rows: List[List[str]]) -> str:
    """Formate un tableau ASCII avec bordures pour le logging."""
    col_w = [
        max(len(h), max((len(r[i]) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    top    = "┌" + "┬".join("─" * (w + 2) for w in col_w) + "┐"
    hdr    = "│" + "│".join(f" {h:<{w}} " for h, w in zip(headers, col_w)) + "│"
    sep    = "├" + "┼".join("─" * (w + 2) for w in col_w) + "┤"
    bot    = "└" + "┴".join("─" * (w + 2) for w in col_w) + "┘"
    data   = ["│" + "│".join(f" {c:<{w}} " for c, w in zip(row, col_w)) + "│" for row in rows]
    lines  = [top, hdr, sep] + data + [bot]
    return "\n  " + title + "\n  " + "\n  ".join(lines)

_BUDGET_WEIGHT:      float = 1.0
_RELIABILITY_WEIGHT: float = 0.3

# Métriques dont la valeur brute (%) est convertie en disponibilité absolue
# (capacité_totale × (1 - usage/100)) avant d'entrer dans TOPSIS. Nécessaire
# car deux VMs à la même charge % n'ont pas la même marge réelle si leurs
# capacités diffèrent (edge plus petit que cloud). La capacité est déclarée
# par chaque VM elle-même (champ total_cores/total_ram_gb renvoyé par son
# /metrics et propagé par le collector) — logique fédération/service mesh,
# aucune valeur figée côté orchestrateur.
# Ces critères deviennent des critères "bénéfice" (plus haut = meilleur),
# contrairement aux autres (latence, etc.) qui restent des critères "coût"
# (plus bas = meilleur).
_CAPACITY_METRICS: Dict[str, str] = {
    "cpu_usage": "total_cores",
    "ram_usage": "total_ram_gb",
}


def vm_satisfies_slo(mean: float, slo: Dict[str, Any]) -> bool:
    """Return True if *mean* meets the SLO threshold given its operator."""
    op = slo["operator"]
    t_raw = slo.get("threshold")
    t = float(t_raw) if t_raw is not None else 0.0
    return (
        (op == "<"  and mean <  t) or
        (op == "<=" and mean <= t) or
        (op == ">"  and mean >  t) or
        (op == ">=" and mean >= t)
    )


class TopsisSelector:
    """
    7-step TOPSIS with min-max normalisation for VM candidate ranking.
    Critères : métriques SLO (latency/cpu/ram), budget de conformité,
    fiabilité. Le coût de migration a été retiré — non instrumenté,
    aucune mesure réelle disponible (voir notes de conception).
    Stateless — no I/O, no side effects.
    """

    def select(
        self,
        candidates: List[Dict[str, Any]],
        predictions_map: Dict[str, Dict[str, Any]],
        slos: List[Dict[str, Any]],
        reliability_scores: Dict[str, float],
        profiler: Optional[StepProfiler] = None,
    ) -> Tuple[Dict[str, Any], float, Dict[str, float]]:
        # Profiler optionnel : si fourni, chronomètre les 4 phases TOPSIS.
        # Sinon, un profiler silencieux est utilisé pour ne pas dupliquer la logique.
        prof = profiler or StepProfiler()

        if not candidates:
            return {}, 0.0, {}
        if len(candidates) == 1:
            logger.debug(
                f"🔍 TOPSIS — candidat unique : {candidates[0]['vm_id']} "
                "| score par défaut : 1.0"
            )
            return candidates[0], 1.0, {candidates[0]["vm_id"]: 1.0}

        slo_map: Dict[str, Dict[str, Any]] = {
            s["metric"]: s
            for s in slos
            if s["metric"] in config.METRICS_REGISTRY
        }
        slo_metrics: List[str] = list(slo_map.keys())

        n_vm: int = len(candidates)
        n_cr: int = len(slo_metrics)  # métriques SLO uniquement

        # ── Phase 1 — Matrice de décision ────────────────────────────
        with prof.step("topsis_matrix"):
            matrix: List[List[float]] = []
            for cand in candidates:
                vm_id: str       = cand["vm_id"]
                row:   List[float] = []

                for metric in slo_metrics:
                    meta  = config.METRICS_REGISTRY[metric]
                    preds_raw: List[Any] = (
                        predictions_map
                        .get(vm_id, {})
                        .get(metric, {})
                        .get("predictions", [])
                    )
                    preds: List[float] = [p for p in preds_raw if p is not None]

                    if preds:
                        raw_value = self.calculate_weighted_mean(preds)
                    else:
                        payload_key: str = meta.get("payload_key", metric)
                        val = cand.get(payload_key)
                        raw_value = float(val) if val is not None else float(meta["default_threshold"])

                    row.append(self._to_criterion_value(metric, cand, raw_value))

                matrix.append(row)

        # ── Table 1 : Prédictions des candidats (hors mesure) ────────
        all_headers = slo_metrics
        pred_headers = ["VM"] + [
            f"{m} (dispo)" if m in _CAPACITY_METRICS else m
            for m in all_headers
        ]
        pred_rows = [
            [candidates[i]["vm_id"]] + [f"{matrix[i][j]:.3f}" for j in range(n_cr)]
            for i in range(n_vm)
        ]
        logger.info(_fmt_table("📊 Prédictions des candidats", pred_headers, pred_rows))

        # ── Phase 2 — Normalisation min-max ──────────────────────────
        with prof.step("topsis_norm"):
            norm_m = self._minmax_normalise(matrix, n_vm, n_cr)

        # ── Table 2 : Normalisation min-max (hors mesure) ────────────
        norm_headers = ["VM"] + [f"{h} norm" for h in all_headers]
        norm_rows = [
            [candidates[i]["vm_id"]] + [f"{norm_m[i][j]:.3f}" for j in range(n_cr)]
            for i in range(n_vm)
        ]
        logger.info(_fmt_table("📐 Normalisation min-max", norm_headers, norm_rows))

        # ── Phase 3 — Pondération ────────────────────────────────────
        with prof.step("topsis_weight"):
            weights = [
                float(slo_map[m]["weight"]) if slo_map[m].get("weight") is not None else 0.0
                for m in slo_metrics
            ]
            w_m = [
                [norm_m[i][j] * weights[j] for j in range(n_cr)]
                for i in range(n_vm)
            ]

        # ── Table 3 : Pondération (hors mesure) ──────────────────────
        pond_headers = ["VM"] + [f"{h} (x{weights[j]:.2f})" for j, h in enumerate(all_headers)]
        pond_rows = [
            [candidates[i]["vm_id"]] + [f"{w_m[i][j]:.3f}" for j in range(n_cr)]
            for i in range(n_vm)
        ]
        logger.info(_fmt_table("⚖️  Pondération", pond_headers, pond_rows))

        # ── Phase 4 — Solutions idéales, distances et score ──────────
        # Critère "coût" (latence, etc.) : plus bas = meilleur → idéal = min.
        # Critère "bénéfice" (disponibilité cpu/ram) : plus haut = meilleur → idéal = max.
        with prof.step("topsis_dist"):
            is_benefit = [m in _CAPACITY_METRICS for m in slo_metrics]
            a_plus  = [
                max(w_m[i][j] for i in range(n_vm)) if is_benefit[j]
                else min(w_m[i][j] for i in range(n_vm))
                for j in range(n_cr)
            ]
            a_minus = [
                min(w_m[i][j] for i in range(n_vm)) if is_benefit[j]
                else max(w_m[i][j] for i in range(n_vm))
                for j in range(n_cr)
            ]
            d_plus  = [
                math.sqrt(sum((w_m[i][j] - a_plus[j])  ** 2 for j in range(n_cr)))
                for i in range(n_vm)
            ]
            d_minus = [
                math.sqrt(sum((w_m[i][j] - a_minus[j]) ** 2 for j in range(n_cr)))
                for i in range(n_vm)
            ]
            scores = [
                d_minus[i] / (d_plus[i] + d_minus[i])
                if (d_plus[i] + d_minus[i]) > 0 else 0.0
                for i in range(n_vm)
            ]
            best_idx: int = scores.index(max(scores))

        # ── Table 4 : Distances et score (hors mesure) ───────────────
        dist_rows = [
            [candidates[i]["vm_id"], f"{d_plus[i]:.4f}", f"{d_minus[i]:.4f}", f"{scores[i]:.4f}"]
            for i in range(n_vm)
        ]
        logger.info(_fmt_table("📏 Distances et score", ["VM", "d+", "d-", "score"], dist_rows))

        ranking = sorted(
            zip([c["vm_id"] for c in candidates], scores),
            key=lambda x: x[1],
            reverse=True,
        )
        logger.info(
            "🏆 TOPSIS classement : "
            + "  ".join(f"{vm}={score:.4f}" for vm, score in ranking)
        )

        vm_scores: Dict[str, float] = {
            candidates[i]["vm_id"]: round(scores[i], 4) for i in range(n_vm)
        }
        return candidates[best_idx], round(scores[best_idx], 4), vm_scores

    @staticmethod
    def _to_criterion_value(metric: str, cand: Dict[str, Any], raw_value: float) -> float:
        """
        Convertit un usage % en disponibilité absolue pour les métriques
        capacité-dépendantes (cpu_usage, ram_usage), afin que la comparaison
        entre VMs de capacités différentes (edge vs cloud) porte sur la
        marge réelle plutôt que sur un pourcentage relatif à chacune.

        La capacité est lue directement sur le candidat (déclarée par la VM
        elle-même via /metrics, propagée par le collector) — pas de valeur
        figée côté orchestrateur. Si la VM ne l'a pas fournie (injoignable,
        agent non mis à jour), on retombe sur le % brut sans conversion.
        """
        capacity_key = _CAPACITY_METRICS.get(metric)
        if capacity_key is None:
            return raw_value
        capacity = cand.get(capacity_key)
        if not capacity:
            return raw_value
        return float(capacity) * (1.0 - raw_value / 100.0)

    def calculate_weighted_mean(self, preds: List[float]) -> float:
        if not preds:
            return 0.0
        n:       int       = len(preds)
        weights: List[int] = list(range(n, 0, -1))
        total:   int       = sum(weights)
        return sum(w * p for w, p in zip(weights, preds)) / total

    def _budget_score(
        self,
        vm_id: str,
        predictions_map: Dict[str, Dict[str, Any]],
        slos: List[Dict[str, Any]],
        slo_metrics: List[str],
    ) -> float:
        total: int     = 0
        satisfied: int = 0
        for metric in slo_metrics:
            slo = next((s for s in slos if s["metric"] == metric), None)
            if slo is None:
                continue
            total += 1
            preds_raw: List[Any] = (
                predictions_map
                .get(vm_id, {})
                .get(metric, {})
                .get("predictions", [])
            )
            preds: List[float] = [p for p in preds_raw if p is not None]
            if preds and vm_satisfies_slo(self.calculate_weighted_mean(preds), slo):
                satisfied += 1
        if total == 0:
            return 1.0
        return 1.0 - satisfied / total

    @staticmethod
    def _minmax_normalise(
        matrix: List[List[float]],
        n_vm: int,
        n_cr: int,
    ) -> List[List[float]]:
        norm: List[List[float]] = [[0.0] * n_cr for _ in range(n_vm)]
        for j in range(n_cr):
            col:     List[float] = [matrix[i][j] for i in range(n_vm)]
            col_min: float       = min(col)
            col_max: float       = max(col)
            span:    float       = col_max - col_min
            for i in range(n_vm):
                norm[i][j] = (matrix[i][j] - col_min) / span if span > 0 else 0.0
        return norm