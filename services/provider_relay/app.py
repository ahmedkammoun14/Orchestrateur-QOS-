"""
services/provider_relay/app.py — Passerelle de fédération inter-provider.

Rôle : TRANSPORT UNIQUEMENT. Ce service ne calcule rien, ne décide rien, ne
connaît ni les SLOs ni les VMs.

Flux relais → relais (le hub d'un provider n'est JAMAIS joignable par un
pair, uniquement par SON propre relais en localhost) :

    hub P1 → relais P1 (/handoff) → relais P2 (/inbound) → hub P2 local

    et la réponse remonte par le même chemin.

/handoff  : reçu du hub LOCAL, route vers le relais du provider cible
            (config.PROVIDER_RELAY_URLS), sur SON endpoint /inbound.
/inbound  : reçu d'un relais PAIR, livre au hub LOCAL (config.CORE_URL)
            sur /intent/relay et renvoie sa réponse telle quelle.

En mono-processus (déploiement actuel du projet), PROVIDER_RELAY_URLS pointe
par défaut sur l'unique relais de base (:8010) pour les deux providers : la
passation boucle localement sur lui-même (/handoff → /inbound du même
relais → hub local). Passer à N orchestrateurs réels ne change rien ici —
seules les tables de routage de shared/config.py (PROVIDER_RELAY_URLS,
CORE_URL) changent.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

# ── Client HTTP partagé (essai phase 1 — retard de negociation) ─────────
# httpx.AsyncClient() ouvre une connexion neuve a chaque instanciation.
# Avant ce changement, /broadcast et /inbound/evaluate en recreaient une a
# CHAQUE negociation (74 a 165 fois par run) au lieu de reutiliser une
# connexion existante. Un seul client, cree une fois, reutilise partout.
# Pour revenir en arriere : git checkout -- services/provider_relay/app.py
_shared_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient()
    return _shared_client


app = FastAPI(title="Provider Relay", version="1.0.0")

_peer_lines = "\n".join(
    f"    {C.CYAN}{provider_id:<12}{C.RESET} → {url}/inbound"
    for provider_id, url in config.PROVIDER_RELAY_URLS.items()
)
logger.info(
    f"\n{'═'*60}\n"
    f"  🚀  {C.BOLD}Provider Relay — Démarrage{C.RESET}\n"
    f"  Table de routage vers les relais pairs (/handoff → /inbound) :\n"
    f"{_peer_lines}\n"
    f"  Hub LOCAL livré par /inbound : {C.CYAN}{config.CORE_URL}{C.RESET}/intent/relay\n"
    f"  {C.CYAN}/broadcast{C.RESET}        → diffusion N-aire vers les pairs (/inbound/evaluate)\n"
    f"  {C.CYAN}/inbound/evaluate{C.RESET} → hub LOCAL {C.CYAN}{config.CORE_URL}{C.RESET}/evaluate\n"
    f"  {C.CYAN}/award{C.RESET}            → attribution vers UN pair (/inbound/award)\n"
    f"  {C.CYAN}/inbound/award{C.RESET}    → hub LOCAL {C.CYAN}{config.CORE_URL}{C.RESET}/award\n"
    f"  Passage au distribué = changer PROVIDER_RELAY_URLS/CORE_URL, rien d'autre.\n"
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

    if target_provider not in config.PROVIDER_RELAY_URLS:
        logger.warning(f"⚠️  /handoff — target_provider inconnu : {target_provider}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"target_provider '{target_provider}' inconnu de "
                f"PROVIDER_RELAY_URLS {sorted(config.PROVIDER_RELAY_URLS.keys())}"
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

    target_relay = config.PROVIDER_RELAY_URLS[target_provider]
    relay_url    = f"{target_relay}/inbound"          # <-- relais du pair, PAS son hub
    relay_body   = {
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
        logger.error(f"❌ /handoff — relais cible injoignable : {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Relais de '{target_provider}' injoignable ({relay_url}) : {exc}",
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
            f"⚠️  /handoff — le relais cible a répondu {resp.status_code}"
        )
        raise HTTPException(status_code=resp.status_code, detail=body)

    logger.info(
        f"✅ /handoff — relayé avec succès vers {C.GREEN}{target_provider}{C.RESET}"
    )
    return body


@app.post("/inbound", status_code=status.HTTP_200_OK)
async def inbound(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Reçu d'un relais PAIR (jamais du hub local) : livre le payload tel quel
    au hub LOCAL (config.CORE_URL, toujours en localhost — jamais l'adresse
    d'un pair) sur /intent/relay, et renvoie sa réponse telle quelle.

    Symétrique de /handoff : ce service ne réinterprète jamais le contenu
    (slo_intent/offer/acting_as_provider/incumbent_*), il transporte.
    """
    local_hub = f"{config.CORE_URL}/intent/relay"

    logger.info(f"📥 /inbound — relais pair → hub local ({local_hub})")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(local_hub, json=payload, timeout=config.POST_TIMEOUT)
    except Exception as exc:
        logger.error(f"❌ /inbound — hub local injoignable : {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Hub local injoignable ({local_hub}) : {exc}",
        )

    try:
        body = resp.json()
    except ValueError:
        body = {"raw_response": resp.text}
    if not isinstance(body, dict):
        body = {"response": body}

    if resp.status_code >= 400:
        logger.warning(f"⚠️  /inbound — le hub local a répondu {resp.status_code}")
        raise HTTPException(status_code=resp.status_code, detail=body)

    logger.info("✅ /inbound — livré avec succès au hub local")
    return body


@app.post("/broadcast", status_code=status.HTTP_200_OK)
async def broadcast(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Diffuse un jeu de SLOs vers TOUS les pairs EN PARALLÈLE et agrège leurs
    bids (scatter-gather N-aire — complément de /handoff, qui ne route que
    vers UN seul pair).

    Transport pur, comme /handoff et /inbound : `slos` est du JSON OPAQUE,
    jamais désérialisé ni interprété sur le fond — ce service n'importe pas
    hub/provider_arbitration.py, il ne connaît ni les SLOs ni les VMs.

    Dégradation gracieuse : un pair injoignable, en timeout ou en erreur
    alimente `errors` sans jamais faire échouer l'appel globalement — sinon
    la panne d'UN pair paralyserait tous les autres. Toujours HTTP 200, sauf
    payload invalide (slos/from_provider absents, seuls refus possibles).
    """
    slos:          Any           = payload.get("slos")
    intent_id:     Optional[str] = payload.get("intent_id")
    incumbent_vm:  Optional[str] = payload.get("incumbent_vm")
    from_provider: Optional[str] = payload.get("from_provider")

    if not slos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'slos' absent ou vide",
        )
    if not from_provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from_provider' absent",
        )

    # L'initiateur s'exclut de sa propre diffusion : il évalue ses propres
    # VMs en local, sans passer par le réseau — même convention que /handoff
    # (target_provider != from_provider).
    targets: Dict[str, str] = {
        pid: url for pid, url in config.PROVIDER_RELAY_URLS.items()
        if pid != from_provider
    }

    if not targets:
        # Fédération à un seul membre : configuration VALIDE, pas une erreur.
        logger.info(
            f"📡 /broadcast — {C.CYAN}{from_provider}{C.RESET} : aucune cible "
            "(fédération à un seul provider)"
        )
        return {
            "bids":       [],
            "errors":     [],
            "relayed_by": "provider_relay",
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        }

    logger.info(
        f"📡 /broadcast — {C.CYAN}{from_provider}{C.RESET} → "
        f"{C.CYAN}{len(targets)}{C.RESET} cible(s) : {list(targets.keys())}"
    )

    # from_provider n'est PAS transmis : le pair receveur n'en a pas l'usage
    # (il sait déjà qui il est).
    relay_body = {"slos": slos, "intent_id": intent_id, "incumbent_vm": incumbent_vm}
    target_items = list(targets.items())   # ordre figé, réutilisé pour l'agrégation déterministe

    async def _call(url: str, http_client: httpx.AsyncClient):
        return await http_client.post(
            f"{url}/inbound/evaluate", json=relay_body, timeout=config.POST_TIMEOUT
        )

    # ⚠️ PARALLÈLE, PAS séquentiel : un seul client HTTP partagé, un seul
    # gather. Une boucle "for pid in targets: await client.post(...)" serait
    # fonctionnellement identique mais ferait grandir le temps de cycle
    # linéairement avec le nombre de providers (3 pairs à 800 ms : ~800 ms en
    # parallèle contre ~2400 ms en séquentiel).
    client  = _get_client()
    tasks   = [_call(url, client) for _pid, url in target_items]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    bids:   List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for (pid, _url), result in zip(target_items, results):
        if isinstance(result, BaseException):
            errors.append({"provider_id": pid, "error": str(result)})
            continue

        resp = result
        try:
            resp_body = resp.json()
        except ValueError:
            resp_body = None

        if resp.status_code >= 400:
            errors.append({
                "provider_id": pid,
                "error":       f"HTTP {resp.status_code}",
                "detail":      resp_body,
            })
        elif isinstance(resp_body, dict):
            bids.append(resp_body)
        else:
            errors.append({
                "provider_id": pid,
                "error":       "réponse malformée (corps non JSON-objet)",
                "detail":      resp_body,
            })

    logger.info(
        f"📬 /broadcast — {C.GREEN}{len(bids)}{C.RESET} bid(s) reçu(s), "
        f"{C.YELLOW}{len(errors)}{C.RESET} erreur(s)"
    )

    return {
        "bids":       bids,
        "errors":     errors,
        "relayed_by": "provider_relay",
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    }


@app.post("/inbound/evaluate", status_code=status.HTTP_200_OK)
async def inbound_evaluate(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Symétrique de /inbound pour le chemin bid unifié (/broadcast) : reçu
    d'un relais PAIR, livre le payload tel quel au hub LOCAL (config.CORE_URL,
    toujours en localhost — jamais l'adresse d'un pair) sur /evaluate, et
    renvoie sa réponse telle quelle.

    ⚠️ POINT TERMINAL : ne rediffuse JAMAIS, n'appelle JAMAIS /broadcast ni un
    autre relais. C'est cette propriété — et elle seule — qui rend toute
    boucle impossible par construction sur ce chemin, et qui explique
    pourquoi la garde anti-boucle (attempted_providers/409) de /handoff est
    inutile ici : il n'y a personne à qui retransmettre.
    """
    local_hub = f"{config.CORE_URL}/evaluate"

    logger.info(f"📥 /inbound/evaluate — relais pair → hub local ({local_hub})")

    try:
        client = _get_client()
        resp   = await client.post(local_hub, json=payload, timeout=config.POST_TIMEOUT)
    except Exception as exc:
        logger.error(f"❌ /inbound/evaluate — hub local injoignable : {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Hub local injoignable ({local_hub}) : {exc}",
        )

    try:
        body = resp.json()
    except ValueError:
        body = {"raw_response": resp.text}
    if not isinstance(body, dict):
        body = {"response": body}

    if resp.status_code >= 400:
        logger.warning(f"⚠️  /inbound/evaluate — le hub local a répondu {resp.status_code}")
        raise HTTPException(status_code=resp.status_code, detail=body)

    logger.info("✅ /inbound/evaluate — livré avec succès au hub local")
    return body


@app.post("/award", status_code=status.HTTP_200_OK)
async def award(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Relaie un message d'ATTRIBUTION (award, lot 7) vers le relais du provider
    gagnant : notifie la VM RETENUE pour que le gagnant n'ait pas à la
    redécouvrir via kubectl — qui résout au NODE, pas à la VM (voir
    shared/config.py::VM_NODE_GROUP, lot 1b) et renverrait la VM canonique du
    node plutôt que la VM précise choisie par l'arbitre.

    Transport pur, comme /handoff et /broadcast : le corps (`vm_id`,
    `intent_id`, ...) est du JSON opaque, jamais interprété ici.

    ⚠️ OPTIMISATION, jamais une dépendance dure : timeout COURT
    (config.AWARD_TIMEOUT_S, pas config.POST_TIMEOUT), et TOUT échec
    (provider inconnu, timeout, exception réseau) répond HTTP 200 avec
    {"delivered": false, ...} — jamais un 4xx/5xx que l'appelant devrait
    intercepter. L'appelant (_decide_federated) ne fait déjà qu'un
    best-effort : le pair retombera sur la découverte kubectl si l'award
    n'arrive pas, c'est-à-dire le comportement antérieur au lot 7.
    """
    target_provider: Optional[str] = payload.get("target_provider")

    if target_provider not in config.PROVIDER_RELAY_URLS:
        logger.warning(f"⚠️  /award — target_provider inconnu : {target_provider}")
        return {"delivered": False, "error": f"target_provider '{target_provider}' inconnu"}

    target_relay = config.PROVIDER_RELAY_URLS[target_provider]
    relay_url    = f"{target_relay}/inbound/award"

    logger.info(
        f"📨 /award — {C.CYAN}{payload.get('from_provider')}{C.RESET} → "
        f"{C.CYAN}{target_provider}{C.RESET} ({relay_url}) : "
        f"vm={C.CYAN}{payload.get('vm_id')}{C.RESET}"
    )

    try:
        async with httpx.AsyncClient() as client:
            await client.post(relay_url, json=payload, timeout=config.AWARD_TIMEOUT_S)
    except Exception as exc:
        logger.warning(f"⚠️  /award — relais cible injoignable : {exc}")
        return {"delivered": False, "error": str(exc)}

    logger.info(f"✅ /award — livré au relais de {C.GREEN}{target_provider}{C.RESET}")
    return {"delivered": True}


@app.post("/inbound/award", status_code=status.HTTP_200_OK)
async def inbound_award(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Symétrique de /inbound/evaluate pour le message d'attribution (award) :
    reçu d'un relais PAIR, livre le payload tel quel au hub LOCAL
    (config.CORE_URL, toujours en localhost — jamais l'adresse d'un pair)
    sur /award, et renvoie sa réponse telle quelle.

    ⚠️ POINT TERMINAL, comme /inbound/evaluate : ne rediffuse JAMAIS, n'appelle
    JAMAIS /award ni un autre relais.
    """
    local_hub = f"{config.CORE_URL}/award"

    logger.info(f"📥 /inbound/award — relais pair → hub local ({local_hub})")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(local_hub, json=payload, timeout=config.POST_TIMEOUT)
    except Exception as exc:
        logger.error(f"❌ /inbound/award — hub local injoignable : {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Hub local injoignable ({local_hub}) : {exc}",
        )

    try:
        body = resp.json()
    except ValueError:
        body = {"raw_response": resp.text}
    if not isinstance(body, dict):
        body = {"response": body}

    if resp.status_code >= 400:
        logger.warning(f"⚠️  /inbound/award — le hub local a répondu {resp.status_code}")
        raise HTTPException(status_code=resp.status_code, detail=body)

    logger.info("✅ /inbound/award — livré avec succès au hub local")
    return body


@app.post("/intent/propagate", status_code=status.HTTP_200_OK)
async def intent_propagate(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Diffuse une INTENTION (ses SLOs) vers TOUS les pairs en parallèle, pour
    que la fédération entière partage le même contrat.

    Même contrat que /broadcast : transport pur (le corps est du JSON
    opaque, jamais interprété ici), diffusion parallèle, dégradation
    gracieuse — un pair injoignable alimente `errors` sans faire échouer
    l'appel. Contrairement à /broadcast, aucune réponse n'est agrégée :
    c'est une notification, pas une collecte de bids.
    """
    from_provider: Optional[str] = payload.get("from_provider")

    if not from_provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from_provider' absent",
        )

    targets: Dict[str, str] = {
        pid: url for pid, url in config.PROVIDER_RELAY_URLS.items()
        if pid != from_provider
    }

    if not targets:
        logger.info(
            f"📨 /intent/propagate — {C.CYAN}{from_provider}{C.RESET} : aucune cible "
            "(fédération à un seul provider)"
        )
        return {"delivered": [], "errors": []}

    logger.info(
        f"📨 /intent/propagate — {C.CYAN}{from_provider}{C.RESET} → "
        f"{C.CYAN}{len(targets)}{C.RESET} pair(s) : {list(targets.keys())}"
    )

    # from_provider retiré : le pair n'en a pas l'usage (même convention
    # que /broadcast).
    relay_body   = {k: v for k, v in payload.items() if k != "from_provider"}
    target_items = list(targets.items())

    async def _call(url: str, http_client: httpx.AsyncClient):
        return await http_client.post(
            f"{url}/inbound/intent", json=relay_body, timeout=config.POST_TIMEOUT
        )

    async with httpx.AsyncClient() as client:
        tasks   = [_call(url, client) for _pid, url in target_items]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    delivered: List[str] = []
    errors:    List[Dict[str, Any]] = []

    for (pid, _url), result in zip(target_items, results):
        if isinstance(result, BaseException):
            errors.append({"provider_id": pid, "error": str(result)})
        elif result.status_code >= 400:
            errors.append({"provider_id": pid, "error": f"HTTP {result.status_code}"})
        else:
            delivered.append(pid)

    logger.info(
        f"📬 /intent/propagate — {C.GREEN}{len(delivered)}{C.RESET} livré(s), "
        f"{C.YELLOW}{len(errors)}{C.RESET} erreur(s)"
    )

    return {"delivered": delivered, "errors": errors}


@app.post("/inbound/intent", status_code=status.HTTP_200_OK)
async def inbound_intent(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Symétrique de /inbound/award pour l'intention : reçu d'un relais PAIR,
    livre le payload au hub LOCAL (config.CORE_URL, toujours localhost) sur
    /intent.

    ⚠️ POINT TERMINAL : `propagate: False` est forcé ici, ce qui empêche le
    hub local de rediffuser — sans cette garde, deux providers se
    repropageraient l'intention indéfiniment.
    """
    local_hub = f"{config.CORE_URL}/intent"
    body      = {**payload, "propagate": False}

    logger.info(f"📥 /inbound/intent — relais pair → hub local ({local_hub})")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(local_hub, json=body, timeout=config.POST_TIMEOUT)
    except Exception as exc:
        logger.error(f"❌ /inbound/intent — hub local injoignable : {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Hub local injoignable ({local_hub}) : {exc}",
        )

    if resp.status_code >= 400:
        logger.warning(f"⚠️  /inbound/intent — le hub local a répondu {resp.status_code}")
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    logger.info("✅ /inbound/intent — livré avec succès au hub local")
    try:
        return resp.json()
    except ValueError:
        return {"raw_response": resp.text}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status":            "healthy",
        "service":           "provider_relay",
        "peer_relays":       dict(config.PROVIDER_RELAY_URLS),   # /handoff → relais pairs
        "local_hub":         config.CORE_URL,                    # /inbound → hub local
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.PROVIDER_RELAY_PORT)
