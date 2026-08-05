"""
Tests de la diffusion N-aire (lot 4) : POST /broadcast et POST
/inbound/evaluate (services/provider_relay/app.py).

Mêmes conventions que tests/unit/test_provider_relay.py : le relais ne
calcule rien, ces tests vérifient le ROUTAGE et l'AGRÉGATION (parallélisme,
dégradation gracieuse) en mockant httpx.AsyncClient.post, jamais une
logique métier — ce service n'importe pas hub/provider_arbitration.py.
"""

import asyncio
import json as json_module

import httpx
import pytest
from fastapi.testclient import TestClient

from shared import config
from services.provider_relay import app as relay_app

client = TestClient(relay_app.app)


# ── Helpers ───────────────────────────────────────────────────

def _payload(slos=None, intent_id="t1", incumbent_vm=None, from_provider="provider-1"):
    return {
        "slos":          slos if slos is not None else [{"metric": "latency", "threshold": 30}],
        "intent_id":     intent_id,
        "incumbent_vm":  incumbent_vm,
        "from_provider": from_provider,
    }


def _bid(provider_id: str) -> dict:
    return {
        "provider_id":    provider_id,
        "intent_id":      "t1",
        "placement_plan": {"provider_id": provider_id, "vm_id": f"vm-{provider_id}",
                            "action": "deploy", "topsis_score": None, "vm_scores": {}, "reason": "x"},
        "gap_grade":      {"value": -0.1, "is_compliant": True, "evaluable": True,
                            "coverage": ["latency"], "detail": {"latency": -0.1}},
        "timestamp":      "2026-07-30T10:00:00+00:00",
    }


class _FakeResponse:
    """Simule httpx.Response : seuls .status_code, .json() et .text sont utilisés."""
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body        = body if body is not None else {}

    def json(self):
        return self._body

    @property
    def text(self):
        return json_module.dumps(self._body)


def _install_fake_post_by_url(monkeypatch, calls, responses: dict = None, raises: dict = None):
    """
    Faux httpx.AsyncClient.post routé PAR URL : `responses`/`raises` sont des
    dicts {url_suffix: FakeResponse|Exception}. Enregistre chaque appel dans
    `calls` (liste partagée). Défaut : réponse 200 générique si aucune règle
    ne correspond.
    """
    responses = responses or {}
    raises    = raises or {}

    async def fake_post(self, url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        for suffix, exc in raises.items():
            if url.endswith(suffix) or suffix in url:
                raise exc
        for suffix, resp in responses.items():
            if suffix in url:
                return resp
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


@pytest.fixture
def calls():
    return []


# ═══════════════════════════════════════════════════════════════
#  1. Deux pairs joignables
# ═══════════════════════════════════════════════════════════════

def test_deux_pairs_joignables_deux_bids_aucune_erreur(monkeypatch, calls):
    monkeypatch.setattr(config, "PROVIDER_RELAY_URLS", {
        "provider-1": "http://relay-p1",
        "provider-2": "http://relay-p2",
        "provider-3": "http://relay-p3",
    })
    _install_fake_post_by_url(monkeypatch, calls, responses={
        "relay-p2": _FakeResponse(200, _bid("provider-2")),
        "relay-p3": _FakeResponse(200, _bid("provider-3")),
    })

    r = client.post("/broadcast", json=_payload(from_provider="provider-1"))

    assert r.status_code == 200
    body = r.json()
    assert len(body["bids"])   == 2
    assert body["errors"]      == []


# ═══════════════════════════════════════════════════════════════
#  2. ⭐ 1 pair joignable + 1 injoignable → HTTP 200 quand même
# ═══════════════════════════════════════════════════════════════

def test_un_pair_injoignable_un_bid_une_erreur_http_200(monkeypatch, calls):
    monkeypatch.setattr(config, "PROVIDER_RELAY_URLS", {
        "provider-1": "http://relay-p1",
        "provider-2": "http://relay-p2",
        "provider-3": "http://relay-p3",
    })
    _install_fake_post_by_url(
        monkeypatch, calls,
        responses={"relay-p2": _FakeResponse(200, _bid("provider-2"))},
        raises={"relay-p3": httpx.ConnectError("connection refused")},
    )

    r = client.post("/broadcast", json=_payload(from_provider="provider-1"))

    assert r.status_code == 200   # jamais 502, malgré le pair injoignable
    body = r.json()
    assert len(body["bids"])   == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["provider_id"] == "provider-3"


# ═══════════════════════════════════════════════════════════════
#  3. TOUS les pairs injoignables
# ═══════════════════════════════════════════════════════════════

def test_tous_les_pairs_injoignables_bids_vide_http_200(monkeypatch, calls):
    monkeypatch.setattr(config, "PROVIDER_RELAY_URLS", {
        "provider-1": "http://relay-p1",
        "provider-2": "http://relay-p2",
        "provider-3": "http://relay-p3",
    })
    _install_fake_post_by_url(
        monkeypatch, calls,
        raises={
            "relay-p2": httpx.ConnectError("connection refused"),
            "relay-p3": httpx.TimeoutException("timeout"),
        },
    )

    r = client.post("/broadcast", json=_payload(from_provider="provider-1"))

    assert r.status_code == 200
    body = r.json()
    assert body["bids"]         == []
    assert len(body["errors"])  == 2


# ═══════════════════════════════════════════════════════════════
#  4. from_provider exclu des cibles
# ═══════════════════════════════════════════════════════════════

def test_from_provider_exclu_des_cibles(monkeypatch, calls):
    monkeypatch.setattr(config, "PROVIDER_RELAY_URLS", {
        "provider-1": "http://relay-p1",
        "provider-2": "http://relay-p2",
    })
    _install_fake_post_by_url(monkeypatch, calls, responses={
        "relay-p2": _FakeResponse(200, _bid("provider-2")),
    })

    r = client.post("/broadcast", json=_payload(from_provider="provider-1"))

    assert r.status_code == 200
    urls_called = {c["url"] for c in calls}
    assert not any("relay-p1" in u for u in urls_called)   # jamais soi-même
    assert any("relay-p2" in u for u in urls_called)


# ═══════════════════════════════════════════════════════════════
#  5. ⭐ Parallélisme — test déterministe, pas un chronomètre
# ═══════════════════════════════════════════════════════════════

def test_diffusion_reellement_parallele(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_RELAY_URLS", {
        "provider-1": "http://relay-p1",
        "provider-2": "http://relay-p2",
        "provider-3": "http://relay-p3",
    })

    concurrency = {"current": 0, "max": 0}

    async def fake_post(self, url, json=None, timeout=None):
        concurrency["current"] += 1
        concurrency["max"] = max(concurrency["max"], concurrency["current"])
        await asyncio.sleep(0.05)   # laisse une chance aux autres tâches de démarrer
        concurrency["current"] -= 1
        return _FakeResponse(200, _bid("x"))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    r = client.post("/broadcast", json=_payload(from_provider="provider-1"))

    assert r.status_code == 200
    # Une implémentation séquentielle ("for pair: await client.post(...)")
    # ne dépasserait JAMAIS 1 appel simultané.
    assert concurrency["max"] >= 2


# ═══════════════════════════════════════════════════════════════
#  6. slos jamais désérialisé — retransmis identique, opaque
# ═══════════════════════════════════════════════════════════════

def test_slos_opaque_retransmis_identique_sans_erreur(monkeypatch, calls):
    monkeypatch.setattr(config, "PROVIDER_RELAY_URLS", {
        "provider-1": "http://relay-p1",
        "provider-2": "http://relay-p2",
    })
    _install_fake_post_by_url(monkeypatch, calls, responses={
        "relay-p2": _FakeResponse(200, _bid("provider-2")),
    })

    weird_slos = [{"n_importe_quoi": 42, "nested": {"a": [1, 2, 3]}}]
    r = client.post("/broadcast", json=_payload(slos=weird_slos, from_provider="provider-1"))

    assert r.status_code == 200
    sent = next(c for c in calls if "relay-p2" in c["url"])
    assert sent["json"]["slos"] == weird_slos


# ═══════════════════════════════════════════════════════════════
#  7. /inbound/evaluate livre bien sur CORE_URL/evaluate
# ═══════════════════════════════════════════════════════════════

def test_inbound_evaluate_livre_sur_core_url_evaluate(monkeypatch, calls):
    _install_fake_post_by_url(monkeypatch, calls, responses={
        "/evaluate": _FakeResponse(200, _bid("provider-2")),
    })

    body_in = {"slos": [{"metric": "latency", "threshold": 30}], "intent_id": "t1", "incumbent_vm": None}
    r = client.post("/inbound/evaluate", json=body_in)

    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0]["url"] == f"{config.CORE_URL}/evaluate"
    assert calls[0]["json"] == body_in


def test_inbound_evaluate_hub_local_injoignable_502(monkeypatch, calls):
    _install_fake_post_by_url(
        monkeypatch, calls,
        raises={"/evaluate": httpx.ConnectError("connection refused")},
    )

    r = client.post("/inbound/evaluate", json={"slos": []})

    assert r.status_code == 502
    assert len(calls) == 1


# ═══════════════════════════════════════════════════════════════
#  8. /inbound/evaluate ne rediffuse jamais (point terminal)
# ═══════════════════════════════════════════════════════════════

def test_inbound_evaluate_aucun_appel_sortant_vers_un_relais(monkeypatch, calls):
    """Un seul appel HTTP sortant (le hub local) — jamais un relais pair."""
    _install_fake_post_by_url(monkeypatch, calls, responses={
        "/evaluate": _FakeResponse(200, _bid("provider-2")),
    })

    r = client.post("/inbound/evaluate", json={"slos": [{"metric": "latency"}]})

    assert r.status_code == 200
    assert len(calls) == 1   # UN SEUL appel sortant : le hub local, rien d'autre
    assert calls[0]["url"] == f"{config.CORE_URL}/evaluate"


# ═══════════════════════════════════════════════════════════════
#  9. Pair renvoyant HTTP 500 → errors, pas bids
# ═══════════════════════════════════════════════════════════════

def test_pair_http_500_va_dans_errors_pas_bids(monkeypatch, calls):
    monkeypatch.setattr(config, "PROVIDER_RELAY_URLS", {
        "provider-1": "http://relay-p1",
        "provider-2": "http://relay-p2",
    })
    _install_fake_post_by_url(monkeypatch, calls, responses={
        "relay-p2": _FakeResponse(500, {"detail": "erreur interne du hub"}),
    })

    r = client.post("/broadcast", json=_payload(from_provider="provider-1"))

    assert r.status_code == 200
    body = r.json()
    assert body["bids"] == []
    assert len(body["errors"]) == 1
    assert body["errors"][0]["provider_id"] == "provider-2"
    assert body["errors"][0]["error"] == "HTTP 500"


# ═══════════════════════════════════════════════════════════════
#  10. ⭐ Extensibilité — 3 providers, aucune modification de code
# ═══════════════════════════════════════════════════════════════

def test_trois_providers_deux_appels_sans_modification_de_code(monkeypatch, calls):
    monkeypatch.setattr(config, "PROVIDER_RELAY_URLS", {
        "provider-1": "http://relay-p1",
        "provider-2": "http://relay-p2",
        "provider-3": "http://relay-p3",
    })
    _install_fake_post_by_url(monkeypatch, calls, responses={
        "relay-p2": _FakeResponse(200, _bid("provider-2")),
        "relay-p3": _FakeResponse(200, _bid("provider-3")),
    })

    r = client.post("/broadcast", json=_payload(from_provider="provider-1"))

    assert r.status_code == 200
    assert len(calls) == 2   # 3 - 1 (soi-même)
    body = r.json()
    assert len(body["bids"]) == 2


# ═══════════════════════════════════════════════════════════════
#  11. Gardes d'entrée
# ═══════════════════════════════════════════════════════════════

def test_slos_absent_400():
    r = client.post("/broadcast", json={"from_provider": "provider-1"})
    assert r.status_code == 400


def test_slos_vide_400():
    r = client.post("/broadcast", json={"slos": [], "from_provider": "provider-1"})
    assert r.status_code == 400


def test_from_provider_absent_400():
    r = client.post("/broadcast", json={"slos": [{"metric": "latency"}]})
    assert r.status_code == 400


def test_federation_a_un_seul_provider_200_bids_vide(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_RELAY_URLS", {"provider-1": "http://relay-p1"})

    r = client.post("/broadcast", json=_payload(from_provider="provider-1"))

    assert r.status_code == 200
    body = r.json()
    assert body["bids"]   == []
    assert body["errors"] == []
