"""
services/provider_relay/app.py — Passerelle de fédération inter-provider.

Rôle : TRANSPORT UNIQUEMENT. Ce service ne calcule rien, ne décide rien, ne
connaît ni les SLOs ni les VMs. Il sait seulement à quelle adresse joindre
l'orchestrateur responsable de chaque provider (config.PROVIDER_ORCHESTRATOR_URL)
et relaie la passation vers `/intent/relay` de cet orchestrateur.

En mono-processus (déploiement actuel du projet), les deux providers pointent
vers le MÊME hub : la passation boucle localement sur lui-même. Passer à N
orchestrateurs réels ne change rien ici — seule la table de routage de
shared/config.py (PROVIDER_ORCHESTRATOR_URL) change.
"""

import logging
from typing import Any, Dict, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Body, HTTPException, status

from shared import config
from shared.logging_utils import C, PrettyFormatter


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("ProviderRelay")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(PrettyFormatter())
        logger.addHandler(h)
    logger.propagate = False
    return logger


logger = _setup_logger()

app = FastAPI(title="Provider Relay", version="1.0.0")

_routing_lines = "\n".join(
    f"    {C.CYAN}{provider_id:<12}{C.RESET} → {url}"
    for provider_id, url in config.PROVIDER_ORCHESTRATOR_URL.items()
)
logger.info(
    f"\n{'═'*60}\n"
    f"  🚀  {C.BOLD}Provider Relay — Démarrage{C.RESET}\n"
    f"  Table de routage inter-orchestrateurs :\n"
    f"{_routing_lines}\n"
    f"  Passage au distribué = changer ces URLs, rien d'autre dans le projet.\n"
    f"{'═'*60}"
)
logger.info(
    f"✅ Provider Relay prêt — port {C.CYAN}{config.PROVIDER_RELAY_PORT}{C.RESET}"
)


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.post("/handoff", status_code=status.HTTP_200_OK)
async def handoff(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Relaie une passation inter-provider vers l'orchestrateur responsable de
    `target_provider`.

    Transport pur : `slo_intent` et `offer` sont transmis tels quels, en JSON
    opaque — la seule lecture faite ici porte sur `attempted_providers`, pour
    la garde anti-boucle ci-dessous. Aucune désérialisation en objets
    SLOIntent/ProviderOffer : ce service n'importe pas hub/provider_arbitration.py.
    """
    slo_intent:         Dict[str, Any] = payload.get("slo_intent") or {}
    offer:              Optional[Dict[str, Any]] = payload.get("offer")
    target_provider:    Optional[str] = payload.get("target_provider")
    from_provider:      Optional[str] = payload.get("from_provider")
    incumbent_provider: Optional[str] = payload.get("incumbent_provider")
    # VM active de l'émetteur (en violation) — transport pur, transmise telle
    # quelle sans interprétation. Le hub receveur en a besoin pour que TOPSIS
    # s'exécute réellement (voir le commentaire détaillé dans /intent/relay
    # côté hub/orchestrator_core.py).
    incumbent_vm:       Optional[str] = payload.get("incumbent_vm")

    if target_provider not in config.PROVIDER_ORCHESTRATOR_URL:
        logger.warning(f"⚠️  /handoff — target_provider inconnu : {target_provider}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"target_provider '{target_provider}' inconnu de "
                f"PROVIDER_ORCHESTRATOR_URL {sorted(config.PROVIDER_ORCHESTRATOR_URL.keys())}"
            ),
        )

    if target_provider == from_provider:
        logger.warning(
            f"⚠️  /handoff — passation vers soi-même refusée : {target_provider}"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"target_provider == from_provider ('{target_provider}') : bug d'appel",
        )

    attempted = slo_intent.get("attempted_providers") or []
    if target_provider in attempted:
        # Garde anti-boucle : sans elle, deux providers non conformes se
        # renverraient indéfiniment la même intention. C'est cette garde,
        # et elle seule, qui rend le passage à N orchestrateurs réels sûr.
        logger.warning(
            f"🔁 /handoff — GARDE ANTI-BOUCLE déclenchée : '{target_provider}' "
            f"a déjà tenté l'intention "
            f"'{slo_intent.get('intent_id', '?')}' {attempted} — passation refusée"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{target_provider}' figure déjà dans attempted_providers {attempted}",
        )

    target_url = config.PROVIDER_ORCHESTRATOR_URL[target_provider]
    relay_url  = f"{target_url}/intent/relay"
    relay_body = {
        "slo_intent":         slo_intent,
        "offer":              offer,
        "acting_as_provider": target_provider,
        "incumbent_provider": incumbent_provider,
        "incumbent_vm":       incumbent_vm,
    }

    logger.info(
        f"📤 /handoff — {C.CYAN}{from_provider}{C.RESET} → "
        f"{C.CYAN}{target_provider}{C.RESET} ({relay_url})"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(relay_url, json=relay_body, timeout=config.POST_TIMEOUT)
    except Exception as exc:
        logger.error(f"❌ /handoff — orchestrateur cible injoignable : {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Orchestrateur de '{target_provider}' injoignable ({relay_url}) : {exc}",
        )

    try:
        body = resp.json()
    except ValueError:
        body = {"raw_response": resp.text}
    if not isinstance(body, dict):
        body = {"response": body}

    # Traçabilité : d'où vient cette réponse, par quel relais.
    body["relayed_by"]          = "provider_relay"
    body["target_orchestrator"] = relay_url

    if resp.status_code >= 400:
        logger.warning(
            f"⚠️  /handoff — l'orchestrateur cible a répondu {resp.status_code}"
        )
        raise HTTPException(status_code=resp.status_code, detail=body)

    logger.info(
        f"✅ /handoff — relayé avec succès vers {C.GREEN}{target_provider}{C.RESET}"
    )
    return body


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status":  "healthy",
        "service": "provider_relay",
        "routes":  dict(config.PROVIDER_ORCHESTRATOR_URL),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.PROVIDER_RELAY_PORT)
