import json
import logging
import uvicorn
from fastapi import FastAPI, Body, HTTPException, status
from datetime import datetime, timezone
from typing import Any, Dict

from shared import config
from services.database.redis_client import RedisClient


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class _JSONFormatter(logging.Formatter):
    """Emits every log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level":     record.levelname,
            "service":   "database",
            "event":     getattr(record, "event", "generic"),
            "message":   record.getMessage(),
        })


def _setup_logger() -> logging.Logger:
    """Configure and return the shared DatabaseService logger."""
    logger = logging.getLogger("DatabaseService")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter())
        logger.addHandler(handler)
    return logger


logger = _setup_logger()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(title="Database Service", version="2.1.0")

# Logger is injected so RedisClient shares the same JSON-formatted handler.
redis_client = RedisClient(logger)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/store/metrics", status_code=status.HTTP_200_OK)
async def store_metrics(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    """
    Persist a per-VM metrics snapshot.

    Expected payload::

        {
            "vm_id":      str,
            "metrics":    {"latency": float|null, "cpu_usage": float|null, ...},
            "timestamp":  str  (ISO-8601),
            "reliability": float|null
        }

    Returns 200 on success, 400 on bad payload, 500 on Redis failure.
    """
    vm_id: str | None = payload.get("vm_id")
    metrics: Any = payload.get("metrics")

    if not vm_id or not isinstance(metrics, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="vm_id (str) and metrics (dict) are mandatory",
        )

    try:
        redis_client.store_metrics(
            vm_id=vm_id,
            metrics=metrics,
            timestamp=payload.get("timestamp"),
            reliability=payload.get("reliability"),
        )
        return {"status": "metrics_stored"}
    except Exception as exc:
        logger.error(str(exc), extra={"event": "internal_error"})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Redis write failure",
        )


@app.post("/store/slos", status_code=status.HTTP_200_OK)
async def store_slos(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    """
    Overwrite the active SLOs entry (permanent, no TTL).

    Expected payload::

        {
            "slos":      List[dict],
            "timestamp": str  (ISO-8601)
        }

    Returns 200 on success, 400 on bad payload, 500 on Redis failure.
    """
    slos = payload.get("slos")
    if slos is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="slos is mandatory",
        )

    timestamp: str = payload.get(
        "timestamp", datetime.now(timezone.utc).isoformat()
    )

    try:
        redis_client.store_slos(slos=slos, timestamp=timestamp)
        return {"status": "slos_stored"}
    except Exception as exc:
        logger.error(str(exc), extra={"event": "internal_error"})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Redis write failure",
        )


@app.post("/store/decision", status_code=status.HTTP_200_OK)
async def store_decision(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    """
    Append a migration decision to the FIFO decisions list (permanent).

    Expected payload::

        {
            "decision":  str,
            "from_vm":   str,
            "to_vm":     str,
            "reason":    str,
            "timestamp": str  (ISO-8601)
        }

    Returns 200 on success, 400 on bad payload, 500 on Redis failure.
    """
    if (
        not payload.get("decision")
        or not payload.get("from_vm")
        or not payload.get("to_vm")
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="decision, from_vm and to_vm are mandatory",
        )

    try:
        redis_client.store_decision(payload)
        return {"status": "decision_stored"}
    except Exception as exc:
        logger.error(str(exc), extra={"event": "internal_error"})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Redis write failure",
        )


@app.get("/health")
async def health() -> Dict[str, str]:
    """
    Return service liveness and Redis reachability.

    The service itself is always ``healthy`` if it can answer HTTP.
    ``redis`` reflects the PING result.
    """
    return {
        "status": "healthy",
        "redis": "connected" if redis_client.health_check() else "disconnected",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.DATABASE_PORT)
