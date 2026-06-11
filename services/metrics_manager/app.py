import json
import logging
import uvicorn
from fastapi import FastAPI, HTTPException, status, Body
from datetime import datetime, timezone
from typing import Dict, Any, List

from shared import config
from shared.models import SLO
from services.metrics_manager.metrics_handler import MetricsHandler

# --- Structured Logging ---

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "metrics_manager",
            "event": getattr(record, "event", "generic_log"),
            "message": record.getMessage()
        }
        if hasattr(record, "extra_data"):
            log_record.update(record.extra_data)
        return json.dumps(log_record)

def setup_logger():
    logger = logging.getLogger("MetricsManager")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
    return logger

logger = setup_logger()

# --- App Definition ---

app = FastAPI(title="Metrics Manager", version="2.2.0")
handler = MetricsHandler()

@app.post("/compute", status_code=status.HTTP_200_OK)
async def compute(payload: Dict[str, Any] = Body(...)):
    """
    Autonomous Mode: Selects dynamic SLOs based on history and MI scores.
    """
    history = payload.get("history", [])
    if len(history) < 5:
        logger.warning("History too short for computation", extra={"event": "insufficient_data"})
        raise HTTPException(status_code=400, detail="At least 5 history points required")

    logger.info("Compute requested (Autonomous Mode)", extra={"event": "compute_requested"})

    try:
        # 1. MI Scores Calculation
        mi_scores = handler.compute_mi_scores(history)
        
        # 2. Dynamic SLO Selection
        all_vals = payload.get("all_vals", {})
        final_slos, active_metrics = handler.select_dynamic_slos(mi_scores, all_vals, history)

        return {
            "slos": [s.dict() for s in final_slos],
            "active_metrics": active_metrics,
            "mi_scores": mi_scores,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Computation failed: {str(e)}", extra={"event": "internal_error"})
        raise HTTPException(status_code=500, detail="Internal metrics computation error")

@app.post("/validate", status_code=status.HTTP_200_OK)
async def validate(payload: Dict[str, Any] = Body(...)):
    """
    Enhanced Mode: Validates provided SLOs and updates weights based on MI risk.
    """
    slos_raw = payload.get("slos", [])
    history = payload.get("history", [])
    
    if not slos_raw or not history:
        raise HTTPException(status_code=400, detail="SLOs and History are required")

    logger.info("Validation requested (Enhanced Mode)", extra={"event": "validate_requested"})

    try:
        # 1. MI Scores Calculation
        mi_scores = handler.compute_mi_scores(history)
        
        # 2. Validation & Enrichment
        slos = [SLO(**s) for s in slos_raw]
        final_slos, active_metrics = handler.validate_and_enrich_slos(slos, mi_scores)

        return {
            "slos": [s.dict() for s in final_slos],
            "active_metrics": active_metrics,
            "mi_scores": mi_scores,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Validation failed: {str(e)}", extra={"event": "internal_error"})
        raise HTTPException(status_code=500, detail="Internal metrics validation error")

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "metrics_manager"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.METRICS_MANAGER_PORT)
