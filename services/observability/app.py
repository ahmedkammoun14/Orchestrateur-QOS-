import json
import logging
import threading
import uvicorn
from datetime import datetime, timezone
from typing import Dict

from fastapi import FastAPI
from shared import config
from services.observability.visualizer import QoSDashboard


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level":     record.levelname,
            "service":   "observability",
            "event":     getattr(record, "event", "generic"),
            "message":   record.getMessage(),
        })

def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("Observability")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(_JSONFormatter())
        logger.addHandler(h)
    logger.propagate = False
    return logger

logger = _setup_logger()

app = FastAPI(title="QoS Observability", version="1.0.0")

@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "healthy", "service": "observability"}


if __name__ == "__main__":
    # FastAPI dans un thread secondaire
    t = threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=config.OBSERVABILITY_PORT),
        daemon=True,
        name="uvicorn-thread"
    )
    t.start()
    logger.info(f"Uvicorn thread launched on port {config.OBSERVABILITY_PORT}", extra={"event": "startup"})

    # Dashboard matplotlib dans le main thread
    dashboard = QoSDashboard(logger)
    dashboard.run()