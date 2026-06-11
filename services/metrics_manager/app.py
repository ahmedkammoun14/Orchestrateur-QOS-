import logging
import uvicorn
from fastapi import FastAPI, HTTPException, status, Body
from datetime import datetime, timezone
from typing import Dict, Any

from shared import config
from shared.models import SLO
from services.metrics_manager.metrics_handler import MetricsHandler


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


class PrettyFormatter(logging.Formatter):
    LEVEL_STYLES = {
        "INFO":     f"{C.BLUE}[INFO]{C.RESET}",
        "WARNING":  f"{C.YELLOW}[WARNING]{C.RESET}",
        "ERROR":    f"{C.RED}[ERROR]{C.RESET}",
        "CRITICAL": f"{C.RED}{C.BOLD}[CRITICAL]{C.RESET}",
        "DEBUG":    f"{C.CYAN}[DEBUG]{C.RESET}",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts    = datetime.now(timezone.utc).strftime("%H:%M:%S")
        level = self.LEVEL_STYLES.get(record.levelname, f"[{record.levelname}]")
        msg   = record.getMessage()
        return f"{C.CYAN}{ts}{C.RESET}  {level}  {msg}"


def setup_logger():
    logger = logging.getLogger("MetricsManager")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(PrettyFormatter())
        logger.addHandler(h)
    # Propagate formatter to MetricsHandler sub-logger
    sub = logging.getLogger("MetricsHandler")
    sub.setLevel(logging.DEBUG)
    if not sub.handlers:
        h2 = logging.StreamHandler()
        h2.setFormatter(PrettyFormatter())
        sub.addHandler(h2)
    sub.propagate = False
    return logger


logger = setup_logger()

# ─────────────────────────────────────────────
#  Application
# ─────────────────────────────────────────────

logger.info(
    f"\n{'═'*60}\n"
    f"  🚀  {C.BOLD}Metrics Manager — Démarrage{C.RESET}\n"
    f"  {'MI threshold':<18}: {C.CYAN}{config.MI_RELATIVE_THRESHOLD}{C.RESET}\n"
    f"  {'History window':<18}: {C.CYAN}{config.HISTORY_WINDOW} points{C.RESET}\n"
    f"  {'Métriques':<18}: {C.CYAN}{list(config.METRICS_REGISTRY.keys())}{C.RESET}\n"
    f"{'═'*60}"
)

app     = FastAPI(title="Metrics Manager", version="2.2.0")
handler = MetricsHandler()

logger.info(f"✅ Metrics Manager prêt — port {C.CYAN}{config.METRICS_MANAGER_PORT}{C.RESET}")


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.post("/compute", status_code=status.HTTP_200_OK)
async def compute(payload: Dict[str, Any] = Body(...)):
    """Autonomous Mode — sélection dynamique des SLOs via MI."""
    history = payload.get("history", [])
    if len(history) < 5:
        logger.warning(
            f"⚠️  /compute — historique insuffisant "
            f"| points reçus : {len(history)} (minimum : 5)"
        )
        raise HTTPException(status_code=400, detail="At least 5 history points required")

    logger.info(
        f"⚙️  /compute — Mode autonome "
        f"| historique : {C.CYAN}{len(history)}{C.RESET} points"
    )

    try:
        mi_scores = handler.compute_mi_scores(history)
        all_vals  = payload.get("all_vals", {})
        final_slos, active_metrics = handler.select_dynamic_slos(mi_scores, all_vals, history)

        return {
            "slos":           [s.dict() for s in final_slos],
            "active_metrics": active_metrics,
            "mi_scores":      mi_scores,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"❌ /compute — erreur interne : {e}")
        raise HTTPException(status_code=500, detail="Internal metrics computation error")


@app.post("/validate", status_code=status.HTTP_200_OK)
async def validate(payload: Dict[str, Any] = Body(...)):
    """Enhanced Mode — validation et enrichissement des SLOs existants."""
    slos_raw = payload.get("slos", [])
    history  = payload.get("history", [])

    if not slos_raw or not history:
        logger.warning(
            f"⚠️  /validate — payload incomplet "
            f"| slos={len(slos_raw)} history={len(history)}"
        )
        raise HTTPException(status_code=400, detail="SLOs and History are required")

    logger.info(
        f"🔎 /validate — Mode enhanced "
        f"| {C.CYAN}{len(slos_raw)}{C.RESET} SLO(s) à valider "
        f"| historique : {C.CYAN}{len(history)}{C.RESET} points"
    )

    try:
        mi_scores  = handler.compute_mi_scores(history)
        slos       = [SLO(**s) for s in slos_raw]
        final_slos, active_metrics = handler.validate_and_enrich_slos(slos, mi_scores)

        return {
            "slos":           [s.dict() for s in final_slos],
            "active_metrics": active_metrics,
            "mi_scores":      mi_scores,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"❌ /validate — erreur interne : {e}")
        raise HTTPException(status_code=500, detail="Internal metrics validation error")


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "metrics_manager"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.METRICS_MANAGER_PORT)