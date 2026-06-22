import asyncio
import logging
import uvicorn
from datetime import datetime, timezone
from fastapi import FastAPI, Body, HTTPException, status
from typing import Any, Dict, List

from shared import config
from shared.logging_utils import C, PrettyFormatter
from shared.excel_writer import ExcelWriter
from services.database.redis_client import RedisClient


def _setup_logger() -> logging.Logger:
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

app          = FastAPI(title="Database Service", version="2.2.0")
redis_client = RedisClient(logger)
excel        = ExcelWriter(config.EXCEL_PATH, config.EXCEL_MAX_MB * 1024 * 1024)


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
        ts          = payload.get("timestamp", datetime.now(timezone.utc).isoformat())
        reliability = payload.get("reliability")
        redis_client.store_metrics(
            vm_id=vm_id,
            metrics=metrics,
            timestamp=ts,
            reliability=reliability,
        )
        asyncio.create_task(excel.write_metrics(vm_id, metrics, ts, reliability))
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
        asyncio.create_task(excel.write_slos(slos, timestamp))
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
        asyncio.create_task(excel.write_decision(payload))
        logger.log(logging.SUCCESS, f"✅ Décision {C.GREEN}{decision}{C.RESET} enregistrée")
        return {"status": "decision_stored"}
    except Exception as exc:
        logger.error(f"❌ Erreur interne /store/decision : {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Redis write failure",
        )


@app.post("/store/llm_history", status_code=status.HTTP_200_OK)
async def store_llm_history(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    intention = payload.get("intention")
    if not intention:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="intention is mandatory",
        )
    try:
        redis_client.store_llm_history(payload)
        asyncio.create_task(excel.write_intent(intention, len(payload.get("slos", []))))
        logger.debug(f"🔍 Historique LLM persisté : \"{intention[:60]}\"")
        return {"status": "llm_history_stored"}
    except Exception as exc:
        logger.error(f"❌ Erreur interne /store/llm_history : {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Redis write failure",
        )


@app.get("/load/llm_history", status_code=status.HTTP_200_OK)
async def load_llm_history(size: int = 10) -> Dict[str, Any]:
    history: List[Dict[str, Any]] = redis_client.load_llm_history(size)
    logger.debug(f"🔍 Historique LLM chargé : {len(history)} entrée(s)")
    return {"history": history, "count": len(history)}


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
