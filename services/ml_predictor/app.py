import logging
import uvicorn
import httpx
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, status, Body
from typing import Any, AsyncGenerator, Dict

from shared import config
from shared.logging_utils import C, PrettyFormatter
from services.ml_predictor.predictor import PredictorHandler


def _setup_logger() -> logging.Logger:
    for name in ("MLPredictor", "PredictorHandler"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        if not lg.handlers:
            h = logging.StreamHandler()
            h.setFormatter(PrettyFormatter())
            lg.addHandler(h)
        lg.propagate = False
    return logging.getLogger("MLPredictor")


logger = _setup_logger()

# ─────────────────────────────────────────────
#  Application
# ─────────────────────────────────────────────

logger.info(
    f"\n{'═'*60}\n"
    f"  🚀  {C.BOLD}ML Predictor — Démarrage{C.RESET}\n"
    f"  {'Latency API':<16}: {C.CYAN}{config.ML_RTT_URL}{C.RESET}\n"
    f"  {'CPU API':<16}: {C.CYAN}{config.ML_CPU_URL}{C.RESET}\n"
    f"  {'RAM API':<16}: {C.CYAN}{config.ML_RAM_URL}{C.RESET}\n"
    f"  {'Horizon':<16}: {C.CYAN}7 pas{C.RESET}\n"
    # Tracé au démarrage : sans cette ligne, impossible de savoir a posteriori
    # sur quel prédicteur une exécution a tourné — et donc si elle est
    # comparable à la campagne de référence.
    f"  {'Extrapolation':<16}: {C.CYAN}"
    f"{'ACTIVE (GRU + linéaire, meilleure erreur récente)' if config.ML_LINEAR_EXTRAPOLATION else 'désactivée — GRU seul (référence campagne)'}"
    f"{C.RESET}\n"
    f"{'═'*60}"
)

predictor = PredictorHandler()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("🔧 Chargement des hyperparamètres depuis les APIs ML...")
    await predictor.fetch_window_sizes()
    logger.info(
        f"✅ ML Predictor prêt — port {C.CYAN}{config.ML_PREDICTOR_PORT}{C.RESET} "
        f"| fenêtres : { {m: predictor.window_sizes[m] for m in predictor.window_sizes} }"
    )
    yield
    logger.info("🛑 Fermeture du client HTTP ML Predictor...")
    await predictor.close()


app = FastAPI(title="ML Predictor", version="2.2.0", lifespan=lifespan)


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.post("/predict")
async def predict(payload: Dict[str, Any] = Body(...)):
    if "latency_history" not in payload:
        logger.warning("⚠️  /predict — champ 'latency_history' manquant dans le payload")
        raise HTTPException(status_code=400, detail="latency_history is mandatory")

    lat_len = len(payload.get("latency_history", []))
    cpu_len = len(payload.get("cpu_history", []))
    ram_len = len(payload.get("ram_history", []))
    logger.info(
        f"📡 /predict — requête reçue "
        f"| latency : {C.CYAN}{lat_len}{C.RESET} pts  "
        f"cpu : {C.CYAN}{cpu_len}{C.RESET} pts  "
        f"ram : {C.CYAN}{ram_len}{C.RESET} pts"
    )

    try:
        result = await predictor.handle(payload)

        if result.get("all_apis_down", False):
            logger.error(
                "❌ Toutes les APIs ML sont injoignables — "
                "prédictions non disponibles"
            )
            raise HTTPException(status_code=502, detail="All ML APIs are unreachable")

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ /predict — erreur interne : {e}")
        raise HTTPException(status_code=500, detail="Internal predictor error")


@app.get("/health")
async def health():
    checks = {
        "service":     "healthy",
        "latency_api": "down",
        "cpu_api":     "down",
        "ram_api":     "down",
    }

    async def check_url(name, url):
        try:
            url_h = url.replace("/predict", "/health")
            async with httpx.AsyncClient() as client:
                r = await client.get(url_h, timeout=1.0)
                if r.status_code == 200:
                    checks[name] = "healthy"
        except Exception:
            pass

    await asyncio.gather(
        check_url("latency_api", config.ML_RTT_URL),
        check_url("cpu_api",     config.ML_CPU_URL),
        check_url("ram_api",     config.ML_RAM_URL),
    )

    down = [k for k, v in checks.items() if v == "down" and k != "service"]
    if down:
        logger.warning(f"⚠️  /health — APIs ML indisponibles : {down}")

    return checks


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.ML_PREDICTOR_PORT)