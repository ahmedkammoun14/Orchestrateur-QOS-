import json
import logging
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, status, Response
from datetime import datetime, timezone
from typing import Dict, Any

from shared import config
from shared.models import IntentToHubPayload
from services.intent_manager.llm_handler import LLMHandler

# --- Structured Logging ---

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "intent_manager",
            "event": getattr(record, "event", "generic_log"),
            "message": record.getMessage()
        }
        if hasattr(record, "extra_data"):
            log_record.update(record.extra_data)
        return json.dumps(log_record)

def setup_logger():
    logger = logging.getLogger("IntentManager")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
    return logger

logger = setup_logger()

# --- App Definition ---

app = FastAPI(title="Intent Manager", version="2.0.0")
handler = LLMHandler()

@app.post("/intent", status_code=status.HTTP_202_ACCEPTED)
async def process_intent(payload: Dict[str, Any]):
    """
    Receives user intention and processes it through the LLM cascade.
    """
    intention = payload.get("intention")
    if not intention or not intention.strip():
        logger.warning("Empty intention received", extra={"event": "validation_failed"})
        raise HTTPException(status_code=400, detail="Intention field is required and cannot be empty")

    logger.info(f"Intention received: {intention}", extra={"event": "intent_received"})

    try:
        # 1. Processing (LLM + Cascade + Merge)
        slos = await handler.handle(payload)
        
        if not slos:
            raise HTTPException(status_code=422, detail="Could not extract valid SLOs from intention")

        # 2. Forward to Hub
        hub_payload = {
            "intent_id": payload.get("intent_id", "gen-" + datetime.now().strftime("%H%M%S")),
            "intention": intention,
            "slos": [s.dict() for s in slos],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    config.HUB_INTENT_URL,
                    json=hub_payload,
                    timeout=config.POST_TIMEOUT
                )
                if resp.status_code in (200, 201, 202):
                    logger.info("Successfully forwarded to Hub", extra={"event": "forwarded_to_core"})
                    return {"status": "accepted", "slos_count": len(slos)}
                else:
                    logger.error(f"Hub rejected intent: {resp.status_code}", extra={"event": "hub_rejected"})
                    raise HTTPException(status_code=502, detail="Hub rejected the processed intent")
            except Exception as e:
                logger.error(f"Hub connection failed: {str(e)}", extra={"event": "internal_error"})
                raise HTTPException(status_code=502, detail="Could not connect to Hub")

    except HTTPException: raise
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", extra={"event": "internal_error"})
        raise HTTPException(status_code=500, detail="Internal server error during processing")

@app.get("/health")
async def health():
    """Checks service and Ollama reachability."""
    health_status = {"service": "healthy", "ollama": "unreachable"}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{config.OLLAMA_URL}/api/tags", timeout=2.0)
            if resp.status_code == 200:
                health_status["ollama"] = "healthy"
    except: pass
    
    return health_status

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.INTENT_MANAGER_PORT)
