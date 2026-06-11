import logging
import uvicorn
from fastapi import FastAPI, Body, HTTPException, status, Request
from datetime import datetime, timezone
from typing import Any, Dict

from shared import config
from services.database.redis_client import RedisClient


# ─────────────────────────────────────────────
#  ANSI color codes
# ─────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"


class PrettyFormatter(logging.Formatter):
    LEVEL_STYLES = {
        "INFO":     f"{C.BLUE}[INFO]{C.RESET}",
        "SUCCESS":  f"{C.GREEN}[SUCCESS]{C.RESET}",
        "WARNING":  f"{C.YELLOW}[WARNING]{C.RESET}",
        "ERROR":    f"{C.RED}[ERROR]{C.RESET}",
        "DEBUG":    f"{C.CYAN}[DEBUG]{C.RESET}",
        "CRITICAL": f"{C.RED}{C.BOLD}[CRITICAL]{C.RESET}",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts    = datetime.now(timezone.utc).strftime("%H:%M:%S")
        level = self.LEVEL_STYLES.get(record.levelname, f"[{record.levelname}]")
        return f"{C.CYAN}{ts}{C.RESET}  {level}  {record.getMessage()}"


def _setup_logger() -> logging.Logger:
    # Add SUCCESS level
    logging.SUCCESS = 25
    logging.addLevelName(logging.SUCCESS, "SUCCESS")

    logger = logging.getLogger("DatabaseService")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(PrettyFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = _setup_logger()

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

logger.info(
    f"\n{C.CYAN}═══════════════════════════════════════════════════════{C.RESET}\n"
    f"  🚀  {C.BOLD}Database Service — Démarrage{C.RESET}\n"
    f"  Port      : {C.CYAN}{config.DATABASE_PORT}{C.RESET}\n"
    f"  Redis     : {C.CYAN}{config.REDIS_HOST}:{config.REDIS_PORT}{C.RESET}\n"
    f"{C.CYAN}═══════════════════════════════════════════════════════{C.RESET}"
)

app = FastAPI(title="Database Service", version="2.1.0")

redis_client = RedisClient(logger)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/store/metrics", status_code=status.HTTP_200_OK)
async def store_metrics(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    vm_id: str | None = payload.get("vm_id")
    metrics: Any = payload.get("metrics")

    logger.info(f"📥 Réception métriques pour {C.CYAN}{vm_id}{C.RESET} (metrics count: {len(metrics) if isinstance(metrics, dict) else 0})")

    if not vm_id or not isinstance(metrics, dict):
        logger.error(f"❌ /store/metrics — payload invalide : vm_id={vm_id}, metrics={type(metrics).__name__}")
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
        logger.log(logging.SUCCESS, f"✅ Métriques persistées avec succès pour {C.GREEN}{vm_id}{C.RESET}")
        return {"status": "metrics_stored"}
    except Exception as exc:
        logger.error(f"❌ Erreur interne /store/metrics pour {C.CYAN}{vm_id}{C.RESET} : {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Redis write failure",
        )


@app.post("/store/slos", status_code=status.HTTP_200_OK)
async def store_slos(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    slos = payload.get("slos")
    logger.info(f"📥 Réception de nouveaux SLOs (count: {len(slos) if isinstance(slos, list) else 0})")

    if slos is None:
        logger.error("❌ /store/slos — champ 'slos' manquant dans le payload")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="slos is mandatory",
        )

    timestamp: str = payload.get(
        "timestamp", datetime.now(timezone.utc).isoformat()
    )

    try:
        redis_client.store_slos(slos=slos, timestamp=timestamp)
        logger.log(logging.SUCCESS, f"✅ {C.GREEN}{len(slos)}{C.RESET} SLOs mis à jour en base")
        return {"status": "slos_stored"}
    except Exception as exc:
        logger.error(f"❌ Erreur interne /store/slos : {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Redis write failure",
        )


@app.post("/store/decision", status_code=status.HTTP_200_OK)
async def store_decision(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    decision = payload.get("decision")
    from_vm = payload.get("from_vm")
    to_vm = payload.get("to_vm")
    
    logger.info(f"📥 Réception décision : {C.CYAN}{decision}{C.RESET} ({from_vm} → {to_vm})")

    if (
        not decision
        or from_vm is None
        or to_vm is None
    ):
        logger.error(f"❌ /store/decision — champs obligatoires manquants")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="decision, from_vm and to_vm are mandatory",
        )

    try:
        redis_client.store_decision(payload)
        logger.log(logging.SUCCESS, f"✅ Décision {C.GREEN}{decision}{C.RESET} enregistrée")
        return {"status": "decision_stored"}
    except Exception as exc:
        logger.error(f"❌ Erreur interne /store/decision : {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Redis write failure",
        )


@app.get("/health")
async def health() -> Dict[str, str]:
    redis_status = "connected" if redis_client.health_check() else "disconnected"
    logger.debug(f"🔍 Health check : Redis est {redis_status}")
    return {
        "status": "healthy",
        "redis": redis_status,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.DATABASE_PORT)
