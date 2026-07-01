import logging
import time
import httpx
import uvicorn
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, status
from typing import Dict, Any

from shared import config
from shared.logging_utils import C, PrettyFormatter
from services.intent_manager.llm_handler import LLMHandler


def setup_logger():
    logger = logging.getLogger("IntentManager")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(PrettyFormatter())
        logger.addHandler(handler)
    # Propagate formatter to sub-loggers
    for name in ("LLMHandler", "SLOMerger"):
        sub = logging.getLogger(name)
        sub.setLevel(logging.DEBUG)
        if not sub.handlers:
            h = logging.StreamHandler()
            h.setFormatter(PrettyFormatter())
            sub.addHandler(h)
        sub.propagate = False
    return logger


logger = setup_logger()

# ─────────────────────────────────────────────
#  Application
# ─────────────────────────────────────────────

logger.info(
    f"\n{'═'*60}\n"
    f"  🚀  {C.BOLD}Intent Manager — Démarrage{C.RESET}\n"
    f"  {'Ollama URL':<16}: {C.CYAN}{config.OLLAMA_URL}{C.RESET}\n"
    f"  {'Modèle LLM':<16}: {C.CYAN}{config.INTENT_MODEL}{C.RESET}\n"
    f"  {'Hub Intent URL':<16}: {C.CYAN}{config.HUB_INTENT_URL}{C.RESET}\n"
    f"{'═'*60}"
)

app = FastAPI(title="Intent Manager", version="2.0.0")
handler = LLMHandler()

logger.info(f"✅ Intent Manager prêt — port {C.CYAN}{config.INTENT_MANAGER_PORT}{C.RESET}")


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.post("/intent", status_code=status.HTTP_202_ACCEPTED)
async def process_intent(payload: Dict[str, Any]):
    intention = payload.get("intention")
    if not intention or not intention.strip():
        logger.warning("⚠️  /intent — intention vide reçue, requête rejetée")
        raise HTTPException(status_code=400, detail="Intention field is required and cannot be empty")

    intent_id = payload.get("intent_id", "gen-" + datetime.now().strftime("%H%M%S"))
    logger.info(
        f"\n{'═'*60}\n"
        f"  📥 Intention reçue\n"
        f"  {'ID':<16}: {C.CYAN}{intent_id}{C.RESET}\n"
        f"  {'Intention':<16}: {C.WHITE}\"{intention[:80]}{'...' if len(intention) > 80 else ''}\"{C.RESET}\n"
        f"{'═'*60}"
    )

    try:
        # 1. Processing (LLM + Cascade + Merge) — chronométré (réception intention)
        _t_start    = time.perf_counter()
        _start_iso  = datetime.now(timezone.utc).isoformat()
        slos        = await handler.handle(payload)
        _llm_ms     = (time.perf_counter() - _t_start) * 1000.0

        if not slos:
            raise HTTPException(status_code=422, detail="Could not extract valid SLOs from intention")

        # 2. Forward to Hub (avec le timing de réception pour les mesures de perf)
        _reception_ms = (time.perf_counter() - _t_start) * 1000.0
        logger.info(
            f"⏱  Réception intention {C.CYAN}{intent_id}{C.RESET} — "
            f"durée {C.GREEN}{_reception_ms:.2f} ms{C.RESET} (dont LLM {_llm_ms:.2f} ms)"
        )
        hub_payload = {
            "intent_id": intent_id,
            "intention": intention,
            "slos": [s.dict() for s in slos],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "timing": {
                "reception_ms": round(_reception_ms, 3),
                "llm_ms":       round(_llm_ms, 3),
                "started_at":   _start_iso,
            },
        }

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    config.HUB_INTENT_URL,
                    json=hub_payload,
                    timeout=config.POST_TIMEOUT
                )
                if resp.status_code in (200, 201, 202):
                    _slo_summary = "  ".join(
                        f"{s.metric} {s.operator} {C.CYAN}{s.threshold}{C.RESET} {s.unit}"
                        for s in slos
                    )
                    logger.info(
                        f"✅ Intent transmis au Hub — {C.GREEN}{len(slos)}{C.RESET} SLO(s) "
                        f"| {_slo_summary}"
                    )
                    return {"status": "accepted", "slos_count": len(slos)}
                else:
                    logger.error(
                        f"❌ Hub a rejeté l'intent — HTTP {resp.status_code} "
                        f"| intent_id : {intent_id}"
                    )
                    raise HTTPException(status_code=502, detail="Hub rejected the processed intent")
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"❌ Connexion au Hub échouée : {e}")
                raise HTTPException(status_code=502, detail="Could not connect to Hub")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Erreur inattendue lors du traitement : {e}")
        raise HTTPException(status_code=500, detail="Internal server error during processing")


@app.get("/health")
async def health():
    health_status = {"service": "healthy", "ollama": "unreachable"}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{config.OLLAMA_URL}/api/tags", timeout=2.0)
            if resp.status_code == 200:
                health_status["ollama"] = "healthy"
    except:
        pass

    ollama_ok = health_status["ollama"] == "healthy"
    if not ollama_ok:
        logger.warning("⚠️  /health — Ollama injoignable (fallbacks regex/keywords disponibles)")

    return health_status


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.INTENT_MANAGER_PORT)