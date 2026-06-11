import logging
import uvicorn
from fastapi import FastAPI, Request, Response, status
from datetime import datetime, timezone
from typing import Dict, Any

from services.latency_manager.latency_handler import LatencyHandler, PrettyFormatter
from shared import config


# ─────────────────────────────────────────────
#  ANSI color codes (réutilisés depuis handler)
# ─────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"


def _setup_app_logger() -> logging.Logger:
    logger = logging.getLogger("LatencyManager")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(PrettyFormatter())
        logger.addHandler(h)
    logger.propagate = False
    return logger


app_logger = _setup_app_logger()

# ─────────────────────────────────────────────
#  Application
# ─────────────────────────────────────────────

app_logger.info(
    f"\n{'═'*60}\n"
    f"  🚀  {C.BOLD}Latency Manager — Démarrage{C.RESET}\n"
    f"  {'Hub RTT URL':<16}: {C.CYAN}{config.HUB_RTT_URL}{C.RESET}\n"
    f"  {'Retry count':<16}: {C.CYAN}{config.POST_RETRY_COUNT}{C.RESET}\n"
    f"  {'Retry backoff':<16}: {C.CYAN}{config.POST_RETRY_BACKOFF}s{C.RESET}\n"
    f"{'═'*60}"
)

app = FastAPI(
    title="Latency Manager",
    description="Stateless proxy for PiCar RTT measurements forwarding.",
    version="1.1.0"
)

handler = LatencyHandler()

app_logger.info(f"✅ Latency Manager prêt — port {C.CYAN}{config.LATENCY_PORT}{C.RESET}")


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.post("/rtt")
async def receive_rtt(payload: Dict[str, Any], response: Response):
    success = await handler.handle(payload)

    if not success:
        measurements = payload.get("measurements")
        if not measurements or not isinstance(measurements, list) or len(measurements) == 0:
            response.status_code = status.HTTP_400_BAD_REQUEST
            return {"error": "Invalid payload: measurements is missing or empty"}

        response.status_code = status.HTTP_502_BAD_GATEWAY
        return {"error": "Failed to forward measurements to Hub"}

    response.status_code = status.HTTP_202_ACCEPTED
    return {"status": "forwarded", "cycle": payload.get("cycle")}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "latency_manager"}


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "services.latency_manager.app:app",
        host="0.0.0.0",
        port=config.LATENCY_PORT,
        reload=False
    )