import logging
import uvicorn
from datetime import datetime, timezone
from fastapi import FastAPI, Body, HTTPException, status
from typing import Any, Dict

from shared import config
from shared.logging_utils import C, PrettyFormatter
from services.decision_intelligence.decision import DecisionHandler


def _setup_logger() -> logging.Logger:
    """
    Configure le logger parent 'DecisionIntelligence'.
    Les enfants (decision.py → 'DecisionIntelligence.handler') héritent
    automatiquement via propagation.
    """
    logger = logging.getLogger("DecisionIntelligence")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(PrettyFormatter())
        logger.addHandler(h)
    logger.propagate = False
    return logger


logger = _setup_logger()

# ─────────────────────────────────────────────
#  Application
# ─────────────────────────────────────────────

logger.info(
    f"\n{'═'*60}\n"
    f"  🚀  {C.BOLD}Decision Intelligence — Démarrage{C.RESET}\n"
    f"  {'Proactive factor':<18}: {C.CYAN}{config.PROACTIVE_FACTOR}{C.RESET}\n"
    f"  {'Horizon alert':<18}: {C.CYAN}{config.HORIZON_ALERT} pas{C.RESET}\n"
    f"  {'Métriques':<18}: {C.CYAN}{list(config.METRICS_REGISTRY.keys())}{C.RESET}\n"
    f"{'═'*60}"
)

app     = FastAPI(title="Decision Intelligence", version="2.0.0")
handler = DecisionHandler()

logger.info(
    f"✅ Decision Intelligence prêt — port {C.CYAN}{config.DECISION_INTELLIGENCE_PORT}{C.RESET}"
)


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.post("/decide", status_code=status.HTTP_200_OK)
async def decide(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    if (
        not payload.get("current_data")
        or not payload.get("slos")
        or not payload.get("service_vm")
    ):
        logger.warning(
            "⚠️  /decide — payload incomplet "
            "| champs requis : current_data, slos, service_vm"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="current_data, slos and service_vm are mandatory",
        )

    service_vm  = payload.get("service_vm")
    candidates  = [v["vm_id"] for v in payload.get("current_data", [])]
    slo_metrics = [s["metric"] for s in payload.get("slos", [])]
    logger.info(
        f"📥 /decide — service_vm : {C.CYAN}{service_vm}{C.RESET} "
        f"| candidats : {C.CYAN}{candidates}{C.RESET} "
        f"| SLOs : {C.CYAN}{slo_metrics}{C.RESET}"
    )

    # Fast path cooldown
    if payload.get("cooldown_active"):
        logger.info("⏳ Cooldown actif — retour STAY immédiat (fast path)")
        return {
            "decision":     "stay",
            "from_vm":      None,
            "to_vm":        None,
            "reason":       "cooldown_active",
            "topsis_score": None,
            "breach_type":  None,
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }

    try:
        result: Dict[str, Any] = handler.decide(payload)

        decision = result["decision"]
        if decision == "migrate":
            logger.info(
                f"✅ /decide → {C.YELLOW}{C.BOLD}MIGRATE{C.RESET} "
                f"| {result['from_vm']} → {C.GREEN}{result['to_vm']}{C.RESET} "
                f"| score TOPSIS : {C.CYAN}{result.get('topsis_score')}{C.RESET} "
                f"| breach : {result.get('breach_type')}"
            )
        else:
            logger.info(
                f"✅ /decide → {C.GREEN}STAY{C.RESET} "
                f"| raison : {result.get('reason')}"
            )

        return result

    except Exception as exc:
        logger.error(f"❌ /decide — erreur interne : {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal decision engine error",
        )


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "healthy", "service": "decision_intelligence"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.DECISION_INTELLIGENCE_PORT)