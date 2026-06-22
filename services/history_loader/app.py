import logging
import uvicorn
from fastapi import FastAPI, Body, HTTPException, status
from typing import Any, Dict

from shared import config
from shared.logging_utils import C, PrettyFormatter
from services.history_loader.history import HistoryReader


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("HistoryLoader")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(PrettyFormatter())
        logger.addHandler(handler)
    return logger


logger = _setup_logger()

# ─────────────────────────────────────────────
#  Application
# ─────────────────────────────────────────────

logger.info(
    f"\n{'═'*60}\n"
    f"  🚀  {C.BOLD}History Loader — Démarrage{C.RESET}\n"
    f"  {'Redis':<16}: {C.CYAN}{config.REDIS_HOST}:{config.REDIS_PORT}{C.RESET}\n"
    f"  {'Mode':<16}: {C.CYAN}lecture seule{C.RESET}\n"
    f"{'═'*60}"
)

app = FastAPI(title="History Loader", version="1.0.0")
reader = HistoryReader(logger)

logger.info(f"✅ History Loader prêt — port {C.CYAN}{config.HISTORY_LOADER_PORT}{C.RESET}")


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.post("/load", status_code=status.HTTP_200_OK)
async def load(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    vm_id: str | None = payload.get("vm_id")
    metrics: Any = payload.get("metrics")

    if not vm_id or not isinstance(metrics, list) or not metrics:
        logger.warning(
            f"⚠️  /load — payload invalide "
            f"| vm_id={vm_id} metrics_type={type(metrics).__name__}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="vm_id (str) and metrics (non-empty list) are mandatory",
        )

    size: int = int(payload.get("size") or config.HISTORY_WINDOW)

    try:
        result = reader.load(vm_id=vm_id, metrics=metrics, size=size)
        return result
    except Exception as exc:
        logger.error(f"❌ /load — erreur interne : {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal history reader error",
        )


@app.get("/health")
async def health() -> Dict[str, str]:
    redis_status = "connected" if reader.health_check() else "disconnected"
    if redis_status == "disconnected":
        logger.warning("⚠️  /health — Redis déconnecté")
    return {
        "status": "healthy",
        "redis":  redis_status,
    }


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.HISTORY_LOADER_PORT)