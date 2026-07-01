"""
shared/timing.py — Profilage des étapes du pipeline.

Fournit `StepProfiler` : un chronomètre par cycle (ou par intention) qui
enregistre, pour chaque étape, l'heure de début, l'heure de fin et la durée
en millisecondes, tout en produisant des logs enrichis.

Usage typique :

    prof = StepProfiler(logger=logger, context="Cycle #12")
    with prof.step("prediction"):
        await do_prediction()
    ...
    prof.durations()   # {"prediction": 12.34, ...}
    prof.as_dict()     # {"prediction": {"start": ..., "end": ..., "duration_ms": 12.34}}

Les durées sont mesurées avec `time.perf_counter()` (horloge monotone haute
résolution) ; les horodatages début/fin sont en UTC ISO-8601.
"""

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.logging_utils import C


# ─────────────────────────────────────────────────────────────────────────────
#  Étapes du pipeline exécutées EN PARALLÈLE (asyncio.gather sur les 4 VMs).
#  Pour ces étapes, la durée mesurée = la VM la plus lente (PAS la somme des 4).
#  Sert à documenter le parallélisme dans l'export Excel (légende + en-têtes).
# ─────────────────────────────────────────────────────────────────────────────
PARALLEL_STEPS: Dict[str, str] = {
    "persist_metrics": "Persistance des métriques des 4 VMs en parallèle (→ database /store/metrics)",
    "load_histories":  "Chargement de l'historique des 4 VMs en parallèle (→ history_loader /load)",
    "prediction":      "Prédictions ML des 4 VMs en parallèle (→ ml_predictor /predict)",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Ordre d'exécution des microservices dans un cycle d'orchestration
#  (hub `_run_flow`). Référence UNIQUE pour l'ordre des colonnes de l'export
#  Excel (voir timing_writer.py). « ∥ » = étapes lancées en parallèle
#  (asyncio.gather) : leur durée = la branche la plus lente, pas la somme.
#
#  Note : `check_violations` (Hub, ~0.4 ms) s'exécute juste avant la décision
#  DI ; le bloc Hub est néanmoins regroupé en fin de pipeline car son coût
#  dominant est la migration (action finale, après la décision).
# ─────────────────────────────────────────────────────────────────────────────
PIPELINE_ORDER: tuple = (
    "0. Intent Manager        — réception intention + LLM (mode enhanced, avant le cycle)",
    "1. Metrics Manager ∥ Collector — SLOs/MI  ∥  collecte CPU/RAM des 4 VMs",
    "2. Database              — persistance SLOs + métriques",
    "3. History Loader        — historique VM active puis 4 VMs",
    "4. ML Predictor          — prédiction 4 VMs",
    "5. Decision Intelligence — détection violation + TOPSIS",
    "6. Hub                   — migration (action) + store décision",
)


class StepProfiler:
    """Chronomètre cumulatif des étapes d'un pipeline (thread d'un seul cycle)."""

    def __init__(self, logger: Optional[logging.Logger] = None, context: str = "") -> None:
        self.logger = logger
        self.context = context
        # nom_étape → {"start": iso, "end": iso, "duration_ms": float}
        self.steps: Dict[str, Dict[str, Any]] = {}

    # ── Mesure ────────────────────────────────────────────────────────────

    @contextmanager
    def step(self, name: str):
        """Chronomètre le bloc englobé et enregistre début / fin / durée."""
        t0 = time.perf_counter()
        start_iso = datetime.now(timezone.utc).isoformat()
        self._log_start(name, start_iso)
        try:
            yield
        finally:
            dur_ms = (time.perf_counter() - t0) * 1000.0
            end_iso = datetime.now(timezone.utc).isoformat()
            self.steps[name] = {
                "start":       start_iso,
                "end":         end_iso,
                "duration_ms": round(dur_ms, 3),
            }
            self._log_end(name, end_iso, dur_ms)

    def record(
        self,
        name: str,
        duration_ms: float,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> None:
        """Enregistre une mesure déjà calculée ailleurs."""
        self.steps[name] = {
            "start":       start,
            "end":         end,
            "duration_ms": round(float(duration_ms), 3),
        }

    def merge(self, timings: Optional[Dict[str, Any]]) -> None:
        """
        Fusionne des mesures provenant d'un autre service (ex : decision_intelligence
        renvoie ses sous-étapes TOPSIS au hub).
        Accepte soit {name: {start, end, duration_ms}}, soit {name: duration_ms}.
        """
        if not timings:
            return
        for name, info in timings.items():
            if isinstance(info, dict):
                self.steps[name] = info
            else:
                self.steps[name] = {
                    "start":       None,
                    "end":         None,
                    "duration_ms": round(float(info), 3),
                }

    # ── Accès ─────────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[float]:
        s = self.steps.get(name)
        return s["duration_ms"] if s else None

    def durations(self) -> Dict[str, Optional[float]]:
        return {k: v.get("duration_ms") for k, v in self.steps.items()}

    def as_dict(self) -> Dict[str, Dict[str, Any]]:
        return dict(self.steps)

    # ── Logs ──────────────────────────────────────────────────────────────

    def _prefix(self) -> str:
        return f"[{self.context}] " if self.context else ""

    def _log_start(self, name: str, start_iso: str) -> None:
        if self.logger:
            self.logger.debug(
                f"⏱  {self._prefix()}{C.CYAN}{name}{C.RESET} — début {start_iso[11:23]}"
            )

    def _log_end(self, name: str, end_iso: str, dur_ms: float) -> None:
        if self.logger:
            self.logger.info(
                f"⏱  {self._prefix()}{C.BOLD}{name}{C.RESET} — "
                f"fin {end_iso[11:23]} | durée {C.GREEN}{dur_ms:.2f} ms{C.RESET}"
            )
