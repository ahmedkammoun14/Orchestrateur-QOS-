import json
import logging
import uvicorn
from fastapi import FastAPI, Body, HTTPException, status
from datetime import datetime, timezone
from typing import Any, Dict

from shared import config
from services.decision_intelligence.decision import DecisionHandler


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class _JSONFormatter(logging.Formatter):
    """Emits every log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level":     record.levelname,
            "service":   "decision_intelligence",
            "event":     getattr(record, "event", "generic"),
            "message":   record.getMessage(),
        })


def _setup_logger() -> logging.Logger:
    """
    Configure the root logger for this service.

    Uses the name ``"DecisionIntelligence"`` so that child loggers in
    ``decision.py`` (``"DecisionIntelligence.handler"``) automatically
    inherit this handler via propagation without a separate setup.
    """
    logger = logging.getLogger("DecisionIntelligence")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(_JSONFormatter())
        logger.addHandler(h)
    logger.propagate = False   # prevent double-emission to root logger
    return logger


logger = _setup_logger()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(title="Decision Intelligence", version="2.0.0")

# Stateless handler — safe to instantiate once at module level.
handler = DecisionHandler()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/decide", status_code=status.HTTP_200_OK)
async def decide(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Evaluate SLO violations and return the optimal VM action.

    Called **exclusively** by the orchestrator core.
    Pure computation — zero Redis, zero DB, zero OpenStack calls.

    Expected payload::

        {
            "current_data":       List[{vm_id, rtt_ms, ...}],   # mandatory
            "slos":               List[{metric, operator,
                                        threshold, weight}],     # mandatory
            "service_vm":         str,                           # mandatory
            "predictions_map":    {vm_id: {metric: {...}}},
            "cooldown_active":    bool,
            "migration_costs":    {vm_id: int},
            "reliability_scores": {vm_id: float}
        }

    Fast path: if ``cooldown_active`` is ``True``, returns immediately
    without invoking the violation detector or TOPSIS.

    Returns
    -------
    200
        ``{"decision", "from_vm", "to_vm", "reason",
           "topsis_score", "breach_type", "timestamp"}``
    400
        If ``current_data``, ``slos``, or ``service_vm`` are missing.
    500
        On unexpected engine error.
    """
    if (
        not payload.get("current_data")
        or not payload.get("slos")
        or not payload.get("service_vm")
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="current_data, slos and service_vm are mandatory",
        )

    logger.info(
        f"Decision requested — service_vm={payload.get('service_vm')!r} "
        f"candidates={[v['vm_id'] for v in payload.get('current_data', [])]} "
        f"slos={[s['metric'] for s in payload.get('slos', [])]}",
        extra={"event": "decision_requested"},
    )

    # ------------------------------------------------------------------
    # Fast cooldown path — no computation needed
    # ------------------------------------------------------------------
    if payload.get("cooldown_active"):
        logger.info(
            "Cooldown active — returning stay immediately",
            extra={"event": "cooldown_active"},
        )
        return {
            "decision":     "stay",
            "from_vm":      None,
            "to_vm":        None,
            "reason":       "cooldown_active",
            "topsis_score": None,
            "breach_type":  None,
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Standard decision path
    # ------------------------------------------------------------------
    try:
        result: Dict[str, Any] = handler.decide(payload)

        event = (
            "decision_migrate" if result["decision"] == "migrate"
            else "decision_stay"
        )
        logger.info(
            f"Decision: {result['decision']!r} | "
            f"breach={result.get('breach_type')!r} | "
            f"to_vm={result.get('to_vm')!r} | "
            f"score={result.get('topsis_score')}",
            extra={"event": event},
        )
        return result

    except Exception as exc:
        logger.error(str(exc), extra={"event": "internal_error"})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal decision engine error",
        )


@app.get("/health")
async def health() -> Dict[str, str]:
    """
    Service liveness check.

    No external dependencies — always healthy while the process is up.
    """
    return {"status": "healthy", "service": "decision_intelligence"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.DECISION_INTELLIGENCE_PORT)
