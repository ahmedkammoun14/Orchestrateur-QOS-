import os
import socket
import logging
import psutil
import uvicorn
from datetime import datetime, timezone
from typing import Dict, Any, Callable
from fastapi import FastAPI, HTTPException

# --- CONFIGURATION ---

# Identification of the VM (env var or fallback to hostname)
VM_ID = os.getenv("VM_ID", socket.gethostname())
# Listening port (env var or fallback to 8200)
PORT = int(os.getenv("PORT", 8200))

# --- EXTENSIBILITY REGISTRY ---

# To add a metric, simply add a key and a lambda function returning a float.
# No other code changes are required.
METRICS_MAP: Dict[str, Callable[[], float]] = {
    "cpu_usage": lambda: psutil.cpu_percent(interval=0.5),
    "ram_usage": lambda: psutil.virtual_memory().percent
}

# --- LOGGING ---

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("VM_Agent")

# --- APP DEFINITION ---

app = FastAPI(
    title=f"VM Agent - {VM_ID}",
    description="Standalone system metrics exporter for OpenStack VMs.",
    version="1.1.0"
)

@app.get("/metrics")
async def get_metrics() -> Dict[str, Any]:
    """
    Collects system metrics dynamically from METRICS_MAP.

    Returns:
        JSON payload with vm_id, metrics, and timestamp.
    Raises:
        HTTPException 500 if collection fails.
    """
    payload = {
        "vm_id": VM_ID,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    try:
        for metric_name, collector in METRICS_MAP.items():
            payload[metric_name] = collector()

        logger.info(f"metrics_collected: {VM_ID}")
        return payload

    except Exception as e:
        logger.error(f"metrics_failed: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Internal error during metrics collection: {str(e)}"
        )

@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """
    Basic health status of the agent.
    """
    return {
        "status": "healthy",
        "vm_id": VM_ID,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

if __name__ == "__main__":
    logger.info(f"agent_started: {VM_ID} listening on 0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)