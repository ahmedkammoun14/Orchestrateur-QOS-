"""
services/placement_arbiter/app.py — Microservice d'arbitrage de placement.

Rôle : trancher entre les ProviderBid collectés par le hub (lot 3) via le
relais (/broadcast, lot 4), sur la seule base du Gap Grade — la seule
grandeur comparable entre providers. Toute la logique de décision vit dans
arbiter.py (module PUR) ; ce fichier n'est qu'une enveloppe HTTP sans état.

POST /arbitrate : payload → ArbitrationVerdict.to_dict()
GET  /health    : santé + politique active (enforcement, deadband)
"""

import logging
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI, Body, HTTPException, status

from shared import config
from shared.logging_utils import C, PrettyFormatter
from services.placement_arbiter.arbiter import arbitrate


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("PlacementArbiter")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(PrettyFormatter())
        logger.addHandler(h)
    logger.propagate = False
    return logger


logger = _setup_logger()

app = FastAPI(title="Placement Arbiter", version="1.0.0")

logger.info(
    f"\n{'═'*60}\n"
    f"  🚀  {C.BOLD}Placement Arbiter — Démarrage{C.RESET}\n"
    f"  Politique : enforcement={C.CYAN}{config.SLO_ENFORCEMENT}{C.RESET} "
    f"| deadband={C.CYAN}{config.ARBITER_DEADBAND}{C.RESET}\n"
    f"  Sans état, sans réseau sortant, sans import de hub/provider_arbitration.py.\n"
    f"  Compare UNIQUEMENT les Gap Grades — jamais topsis_score ni vm_scores.\n"
    f"{'═'*60}"
)
logger.info(
    f"✅ Placement Arbiter prêt — port {C.CYAN}{config.PLACEMENT_ARBITER_PORT}{C.RESET}"
)


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.post("/arbitrate", status_code=status.HTTP_200_OK)
async def arbitrate_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Arbitre entre les bids fournis et renvoie le verdict. Sans état : deux
    appels identiques produisent toujours deux verdicts identiques.
    """
    bids = payload.get("bids")
    if not isinstance(bids, list):
        logger.warning("⚠️  /arbitrate — 'bids' absent ou n'est pas une liste")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'bids' est obligatoire et doit être une liste (peut être vide)",
        )

    verdict = arbitrate(
        bids=bids,
        incumbent_provider=payload.get("incumbent_provider"),
        incumbent_vm=payload.get("incumbent_vm"),
        enforcement=payload.get("enforcement"),
        deadband=payload.get("deadband"),
    )

    logger.info(
        f"⚖️  /arbitrate — chemin {C.CYAN}{verdict.path}{C.RESET} "
        f"| gagnant : {C.GREEN}{verdict.winner_provider}{C.RESET}/{C.GREEN}{verdict.winner_vm}{C.RESET} "
        f"| gap_grade={C.CYAN}{verdict.gap_grade}{C.RESET} "
        f"| {C.YELLOW}{sum(1 for c in verdict.considered if c['retained'])}{C.RESET}/"
        f"{len(verdict.considered)} bid(s) retenu(s)"
    )

    return verdict.to_dict()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status":      "healthy",
        "service":     "placement_arbiter",
        "enforcement": config.SLO_ENFORCEMENT,
        "deadband":    config.ARBITER_DEADBAND,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.PLACEMENT_ARBITER_PORT)
