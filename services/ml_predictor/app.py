import json
import logging
import uvicorn
import httpx
import asyncio
from fastapi import FastAPI, HTTPException, status, Body
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from shared import config
from services.ml_predictor.predictor import PredictorHandler

# --- Structured Logging ---

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "ml_predictor",
            "event": getattr(record, "event", "generic"),
            "message": record.getMessage()
        })

logger = logging.getLogger("MLPredictor")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)

# --- App Implementation ---

app = FastAPI(title="ML Predictor", version="2.2.0")
predictor = PredictorHandler()

@app.on_event("startup")
async def startup_event():
    """Initializes hyperparameters from ML APIs."""
    await predictor.fetch_window_sizes()

@app.on_event("shutdown")
async def shutdown_event():
    """Cleans up resources."""
    await predictor.close()

@app.post("/predict")
async def predict(payload: Dict[str, Any] = Body(...)):
    """
    Main prediction endpoint.
    Delegates to PredictorHandler with strict validation.
    Returns predictions list of 7 values per metric.
    """
    if "latency_history" not in payload:
        logger.warning("Missing latency_history", extra={"event": "validation_failed"})
        raise HTTPException(status_code=400, detail="latency_history is mandatory")

    logger.info("New prediction request", extra={"event": "prediction_requested"})

    try:
        result = await predictor.handle(payload)

        if result.get("all_apis_down", False):
            logger.error("All ML APIs unreachable", extra={"event": "api_unavailable"})
            raise HTTPException(status_code=502, detail="All ML APIs are unreachable")

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Prediction logic error: {str(e)}", extra={"event": "internal_error"})
        raise HTTPException(status_code=500, detail="Internal predictor error")

@app.get("/health")
async def health():
    """Checks service and all 3 ML APIs."""
    checks = {
        "service": "healthy",
        "latency_api": "down",
        "cpu_api": "down",
        "ram_api": "down"
    }

    async def check_url(name, url):
        try:
            url_h = url.replace("/predict", "/health")
            async with httpx.AsyncClient() as client:
                r = await client.get(url_h, timeout=1.0)
                if r.status_code == 200:
                    checks[name] = "healthy"
        except Exception:
            pass

    await asyncio.gather(
        check_url("latency_api", config.ML_RTT_URL),
        check_url("cpu_api", config.ML_CPU_URL),
        check_url("ram_api", config.ML_RAM_URL)
    )

    return checks

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.ML_PREDICTOR_PORT)