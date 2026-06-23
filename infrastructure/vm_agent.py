import math
import os
import socket
import time
import logging
import psutil
import uvicorn
from datetime import datetime, timezone
from typing import Dict, Any, Callable
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# --- CONFIGURATION ---

VM_ID   = os.getenv("VM_ID",   socket.gethostname())
VM_TYPE = os.getenv("VM_TYPE", "edge")
PORT    = int(os.getenv("PORT", 8200))

# Position fixe de la VM sur la carte du simulateur HTML (en cm)
VM_X = float(os.getenv("VM_X", "0.0"))
VM_Y = float(os.getenv("VM_Y", "0.0"))

# Modèle de latence : latency_ms = BASE_MS + K_MS_PER_CM * distance_cm
_default_k     = 20.0 if VM_TYPE == "edge" else 40.0
VM_BASE_MS     = float(os.getenv("VM_BASE_MS",     "0.0"))
VM_K_MS_PER_CM = float(os.getenv("VM_K_MS_PER_CM", str(_default_k)))

# --- METRICS REGISTRY ---

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
    description="Metrics exporter + position-based latency simulator.",
    version="2.0.0"
)


class CarPosition(BaseModel):
    x: float
    y: float


@app.get("/metrics")
async def get_metrics() -> Dict[str, Any]:
    payload = {
        "vm_id":     VM_ID,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    try:
        for metric_name, collector in METRICS_MAP.items():
            payload[metric_name] = collector()
        logger.info(f"metrics_collected: {VM_ID}")
        return payload
    except Exception as e:
        logger.error(f"metrics_failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    return {
        "status":    "healthy",
        "vm_id":     VM_ID,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@app.post("/ping")
async def ping(pos: CarPosition) -> Dict[str, Any]:
    """
    Reçoit la position (x, y) du PiCar depuis picar_bridge.py.
    Calcule la distance voiture <-> VM, simule la latence (time.sleep),
    et répond avec latency_ms et distance_cm pour affichage dans le HTML.
    """
    dist_cm    = math.hypot(pos.x - VM_X, pos.y - VM_Y)
    latency_ms = VM_BASE_MS + VM_K_MS_PER_CM * dist_cm

    time.sleep(latency_ms / 1000.0)

    logger.info(
        f"ping  car=({pos.x:.1f},{pos.y:.1f})  "
        f"dist={dist_cm:.1f}cm  latence={latency_ms:.0f}ms"
    )

    return {
        "vm":          VM_ID,
        "type":        VM_TYPE,
        "vm_pos":      [VM_X, VM_Y],
        "car_pos":     [pos.x, pos.y],
        "distance_cm": round(dist_cm, 2),
        "latency_ms":  round(latency_ms, 1),
    }


if __name__ == "__main__":
    logger.info(
        f"agent_started: {VM_ID} ({VM_TYPE})  port={PORT}  "
        f"pos=({VM_X},{VM_Y})  modele={VM_BASE_MS}+{VM_K_MS_PER_CM}*dist ms"
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT)