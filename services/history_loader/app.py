import json
import logging
import uvicorn
from fastapi import FastAPI, Body, HTTPException, status
from datetime import datetime, timezone
from typing import Any, Dict, List

from shared import config
from services.history_loader.history import HistoryReader


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class _JSONFormatter(logging.Formatter):
    """Emits every log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level":     record.levelname,
            "service":   "history_loader",
            "event":     getattr(record, "event", "generic"),
            "message":   record.getMessage(),
        })


def _setup_logger() -> logging.Logger:
    """Configure and return the shared HistoryLoader logger."""
    logger = logging.getLogger("HistoryLoader")
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

app = FastAPI(title="History Loader", version="1.0.0")

# Logger is injected so HistoryReader shares the same JSON-formatted handler.
reader = HistoryReader(logger)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/load", status_code=status.HTTP_200_OK)
async def load(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Load metric timeseries for a given VM from Redis.

    Called exclusively by the orchestrator core.  Read-only — never
    writes to Redis.

    Expected payload::

        {
            "vm_id":   str,               # mandatory
            "metrics": List[str],         # mandatory — e.g. ["latency", "cpu_usage"]
            "size":    int                # optional, fallback: config.HISTORY_WINDOW
        }

    Returns the timeseries for each requested metric that is also
    declared in ``METRICS_REGISTRY``.  Metrics with no data return an
    empty list — never a 404.

    Returns
    -------
    200
        ``{"vm_id", "histories", "sizes", "timestamp"}``
    400
        If ``vm_id`` or ``metrics`` are missing / invalid.
    """
    vm_id: str | None = payload.get("vm_id")
    metrics: Any = payload.get("metrics")

    if not vm_id or not isinstance(metrics, list) or not metrics:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="vm_id (str) and metrics (non-empty list) are mandatory",
        )

    size: int = int(payload.get("size") or config.HISTORY_WINDOW)

    try:
        result = reader.load(vm_id=vm_id, metrics=metrics, size=size)
        return result
    except Exception as exc:
        logger.error(str(exc), extra={"event": "internal_error"})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal history reader error",
        )


@app.get("/health")
async def health() -> Dict[str, str]:
    """
    Return service liveness and Redis reachability.

    The service is always ``healthy`` if it can answer HTTP.
    ``redis`` reflects the PING result.
    """
    return {
        "status": "healthy",
        "redis":  "connected" if reader.health_check() else "disconnected",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.HISTORY_LOADER_PORT)
