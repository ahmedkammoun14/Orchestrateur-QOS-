import logging
import uvicorn
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, status, Body
from typing import Dict, Any

from shared import config
from shared.logging_utils import C, PrettyFormatter
from shared.models import SLO
from services.metrics_manager.metrics_handler import MetricsHandler


def setup_logger():
    logger = logging.getLogger("MetricsManager")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(PrettyFormatter())
        logger.addHandler(h)
    sub = logging.getLogger("MetricsHandler")
    sub.setLevel(logging.DEBUG)
    if not sub.handlers:
        h2 = logging.StreamHandler()
        h2.setFormatter(PrettyFormatter())
        sub.addHandler(h2)
    sub.propagate = False
    return logger


logger = setup_logger()

# ─────────────────────────────────────────────
#  Application
# ─────────────────────────────────────────────

# Récupère le seuil primaire pour l'afficher dans la bannière (objectif métier)
_primary_objectives = {
    m: reg["default_threshold"]
    for m, reg in config.METRICS_REGISTRY.items()
    if reg.get("is_primary_objective", False)
}

logger.info(
    f"\n{'═'*60}\n"
    f"  🚀  {C.BOLD}Metrics Manager — Démarrage{C.RESET}\n"
    f"  {'MI threshold':<20}: {C.CYAN}{config.MI_RELATIVE_THRESHOLD}{C.RESET}\n"
    f"  {'History window':<20}: {C.CYAN}{config.HISTORY_WINDOW} points{C.RESET}\n"
    f"  {'Métriques registry':<20}: {C.CYAN}{list(config.METRICS_REGISTRY.keys())}{C.RESET}\n"
    f"  {'Objectifs primaires':<20}: {C.GREEN}{_primary_objectives}{C.RESET}\n"
    f"  {'Percentiles':<20}: {C.CYAN}stable=P{config.PERCENTILE_STABLE} "
    f"normal=P{config.PERCENTILE_NORMAL} volatile=P{config.PERCENTILE_VOLATILE}{C.RESET}\n"
    f"{'═'*60}"
)

app     = FastAPI(title="Metrics Manager", version="2.3.0")
handler = MetricsHandler()

logger.info(f"✅ Metrics Manager prêt — port {C.CYAN}{config.METRICS_MANAGER_PORT}{C.RESET}")


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.post("/compute", status_code=status.HTTP_200_OK)
async def compute(payload: Dict[str, Any] = Body(...)):
    """
    Mode AUTONOMOUS — génération automatique des SLOs :
      • SLO primaire fixe (objectif métier depuis METRICS_REGISTRY)
      • SLOs secondaires adaptatifs (percentile) sur métriques
        corrélées via MI.
    """
    history = payload.get("history", [])
    if len(history) < 5:
        logger.warning(
            f"⚠️  /compute — historique insuffisant "
            f"| points reçus : {len(history)} (minimum : 5)"
        )
        raise HTTPException(status_code=400, detail="At least 5 history points required")

    cycle = int(payload.get("cycle", 0))
    logger.info(
        f"\n{'═'*60}\n"
        f"  ⚙️  Metrics Manager — Cycle #{C.BOLD}{cycle}{C.RESET} | Mode {C.BOLD}AUTONOMOUS{C.RESET}\n"
        f"  Historique : {C.CYAN}{len(history)}{C.RESET} points\n"
        f"{'═'*60}"
    )

    try:
        mi_scores = handler.compute_mi_scores(history, cycle=cycle, include_primaries=False)
        all_vals  = payload.get("all_vals", {})
        final_slos, active_metrics = handler.select_dynamic_slos(mi_scores, all_vals, history)

        return {
            "slos":           [s.dict() for s in final_slos],
            "active_metrics": active_metrics,
            "mi_scores":      mi_scores,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"❌ /compute — erreur interne : {e}")
        raise HTTPException(status_code=500, detail="Internal metrics computation error")


@app.post("/validate", status_code=status.HTTP_200_OK)
async def validate(payload: Dict[str, Any] = Body(...)):
    """
    Mode ENHANCED — enrichissement des SLOs fournis par le LLM :
      • Les SLOs du LLM deviennent les SLOs primaires (seuils conservés).
      • Pour les métriques non couvertes mais corrélées via MI,
        ajout automatique d'un SLO secondaire adaptatif.
    """
    slos_raw = payload.get("slos", [])
    history  = payload.get("history", [])

    if not slos_raw or not history:
        logger.warning(
            f"⚠️  /validate — payload incomplet "
            f"| slos={len(slos_raw)} history={len(history)}"
        )
        raise HTTPException(status_code=400, detail="SLOs and History are required")

    cycle = int(payload.get("cycle", 0))
    logger.info(
        f"\n{'═'*60}\n"
        f"  🔎 Metrics Manager — Cycle #{C.BOLD}{cycle}{C.RESET} | Mode {C.BOLD}ENHANCED{C.RESET}\n"
        f"  SLOs LLM : {C.CYAN}{len(slos_raw)}{C.RESET}   Historique : {C.CYAN}{len(history)}{C.RESET} points\n"
        f"{'═'*60}"
    )

    try:
        # Les SLOs LLM (primaires) reçoivent poids=1.0 — MI inutile pour eux.
        # On calcule uniquement le MI des métriques secondaires (non couvertes par le LLM).
        llm_metrics = {s["metric"] for s in slos_raw}
        mi_scores = handler.compute_mi_scores(
            history, cycle=cycle, skip_metrics=llm_metrics
        )
        slos      = [SLO(**s) for s in slos_raw]
        # Passage de l'historique pour permettre la génération de SLOs
        # secondaires adaptatifs sur les métriques corrélées non couvertes
        final_slos, active_metrics = handler.validate_and_enrich_slos(
            slos, mi_scores, history
        )

        return {
            "slos":           [s.dict() for s in final_slos],
            "active_metrics": active_metrics,
            "mi_scores":      mi_scores,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"❌ /validate — erreur interne : {e}")
        raise HTTPException(status_code=500, detail="Internal metrics validation error")


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "metrics_manager"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.METRICS_MANAGER_PORT)