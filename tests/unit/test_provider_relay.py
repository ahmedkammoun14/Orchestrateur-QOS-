"""
Tests du microservice de transport provider_relay (services/provider_relay/app.py).

Ce service ne calcule rien : ces tests vérifient le ROUTAGE (URL cible
appelée, codes HTTP, garde-fous) en mockant `httpx.AsyncClient.post`, jamais
la logique de négociation — qui appartient à hub/provider_arbitration.py et
est testée ailleurs.
"""

import json as json_module

import httpx
import pytest
from fastapi.testclient import TestClient

from shared import config
from services.provider_relay import app as relay_app

client = TestClient(relay_app.app)


# ── Helpers ───────────────────────────────────────────────────

def _payload(target="provider-2", frm="provider-1", attempted=(), offer=None,
             incumbent=None, incumbent_vm=None):
    return {
        "slo_intent": {
            "intent_id":           "t1",
            "slos":                [],
            "mode":                "enhanced",
            "created_at":          "2026-07-22T10:00:00",
            "attempted_providers": list(attempted),
        },
        "offer":              offer,
        "target_provider":    target,
        "from_provider":      frm,
        "incumbent_provider": incumbent,
        "incumbent_vm":       incumbent_vm,
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


def _install_fake_post(monkeypatch, calls, response=None, raise_exc=None):
    """
    Remplace httpx.AsyncClient.post par un faux, qui enregistre l'appel dans
    `calls` (liste partagée) au lieu d'émettre une vraie requête réseau.
    """
    async def fake_post(self, url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        if raise_exc is not None:
            raise raise_exc
        return response if response is not None else _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


@pytest.fixture
def calls():
    return []


# ── Passation nominale ────────────────────────────────────────

def test_passation_nominale_appelle_la_bonne_url(monkeypatch, calls):
    """
    Routage relais → relais (pas relais → hub) : /handoff appelle désormais
    /inbound du relais du provider cible (config.PROVIDER_RELAY_URLS), qui
    livrera lui-même au hub local. Avant ce correctif, /handoff appelait
    directement PROVIDER_ORCHESTRATOR_URL[target]/intent/relay — le hub d'un
    pair n'est plus jamais joignable directement.
    """
    _install_fake_post(
        monkeypatch, calls,
        response=_FakeResponse(200, {"negotiation": {"decision": "prend_local_conforme"}}),
    )

    r = client.post("/handoff", json=_payload(target="provider-2", frm="provider-1"))

    assert r.status_code == 200
    assert len(calls) == 1
    expected_url = f"{config.PROVIDER_RELAY_URLS['provider-2']}/inbound"
    assert calls[0]["url"] == expected_url
    assert calls[0]["json"]["acting_as_provider"] == "provider-2"


# ── incumbent_vm : transport pur, transmis tel quel ───────────

def test_incumbent_vm_transmis_inchange_a_intent_relay(monkeypatch, calls):
    """
    Correctif TOPSIS : la VM active de l'émetteur doit atteindre /intent/relay
    sans transformation — provider_relay ne l'interprète pas, il la relaie.
    """
    _install_fake_post(monkeypatch, calls, response=_FakeResponse(200, {"negotiation": {}}))

    r = client.post("/handoff", json=_payload(target="provider-2", incumbent_vm="edge1"))

    assert r.status_code == 200
    assert calls[0]["json"]["incumbent_vm"] == "edge1"


def test_incumbent_vm_absent_ne_leve_pas_champ_none(monkeypatch, calls):
    """Sans incumbent_vm : aucune erreur, champ absent ou None — jamais planté."""
    _install_fake_post(monkeypatch, calls, response=_FakeResponse(200, {"negotiation": {}}))

    r = client.post("/handoff", json=_payload(target="provider-2"))

    assert r.status_code == 200
    assert calls[0]["json"].get("incumbent_vm") is None


# ── Garde-fous — aucun appel HTTP émis ────────────────────────

def test_target_provider_inconnu_400_sans_appel_http(monkeypatch, calls):
    _install_fake_post(monkeypatch, calls)

    r = client.post("/handoff", json=_payload(target="provider-42"))

    assert r.status_code == 400
    assert calls == []


def test_cible_egale_emetteur_409_sans_appel_http(monkeypatch, calls):
    _install_fake_post(monkeypatch, calls)

    r = client.post("/handoff", json=_payload(target="provider-1", frm="provider-1"))

    assert r.status_code == 409
    assert calls == []


def test_garde_anti_boucle_409_sans_appel_http(monkeypatch, calls):
    """
    slo_intent.attempted_providers contient déjà la cible : c'est la garde qui
    rend le passage à N orchestrateurs réels sûr contre les boucles infinies.
    """
    _install_fake_post(monkeypatch, calls)

    r = client.post("/handoff", json=_payload(target="provider-2", attempted=("provider-2",)))

    assert r.status_code == 409
    assert calls == []


# ── Panne réseau ──────────────────────────────────────────────

def test_orchestrateur_cible_injoignable_502(monkeypatch, calls):
    _install_fake_post(monkeypatch, calls, raise_exc=httpx.ConnectError("connection refused"))

    r = client.post("/handoff", json=_payload())

    assert r.status_code == 502
    assert len(calls) == 1   # l'appel a bien été TENTÉ avant d'échouer


# ── Traçabilité de la réponse relayée ──────────────────────────

def test_reponse_relayee_contient_relayed_by_et_target_orchestrator(monkeypatch, calls):
    _install_fake_post(
        monkeypatch, calls,
        response=_FakeResponse(200, {"negotiation": {"decision": "cede_a_l_offre"}}),
    )

    r = client.post("/handoff", json=_payload(target="provider-2"))

    body = r.json()
    assert body["relayed_by"] == "provider_relay"
    assert body["target_orchestrator"] == f"{config.PROVIDER_RELAY_URLS['provider-2']}/inbound"
    # La réponse d'origine de l'orchestrateur cible n'est pas perdue au passage.
    assert body["negotiation"]["decision"] == "cede_a_l_offre"


# ── /inbound : reçu d'un relais pair, livré au hub local ───────

def test_inbound_livre_au_hub_local_et_renvoie_sa_reponse(monkeypatch, calls):
    """
    /inbound est le pendant symétrique de /handoff côté réception : reçu
    d'un relais pair, il livre tel quel au hub LOCAL (config.CORE_URL), et
    ne réinterprète jamais le contenu.
    """
    _install_fake_post(
        monkeypatch, calls,
        response=_FakeResponse(200, {"negotiation": {"decision": "prend_local_conforme"}}),
    )

    body_in = {
        "slo_intent": {"intent_id": "t1", "slos": [], "mode": "enhanced",
                        "created_at": "2026-07-22T10:00:00", "attempted_providers": []},
        "offer": None,
        "acting_as_provider": "provider-2",
        "incumbent_provider": "provider-1",
        "incumbent_vm": "edge1",
    }
    r = client.post("/inbound", json=body_in)

    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0]["url"] == f"{config.CORE_URL}/intent/relay"
    assert calls[0]["json"] == body_in
    assert r.json()["negotiation"]["decision"] == "prend_local_conforme"


def test_inbound_hub_local_injoignable_502(monkeypatch, calls):
    _install_fake_post(monkeypatch, calls, raise_exc=httpx.ConnectError("connection refused"))

    r = client.post("/inbound", json={"acting_as_provider": "provider-2"})

    assert r.status_code == 502
    assert len(calls) == 1


# ── Santé / topologie ─────────────────────────────────────────

def test_health_expose_la_table_de_routage_complete():
    r = client.get("/health")

    assert r.status_code == 200
    body = r.json()
    assert body["status"]      == "healthy"
    assert body["service"]     == "provider_relay"
    assert body["peer_relays"] == dict(config.PROVIDER_RELAY_URLS)
    assert body["local_hub"]   == config.CORE_URL
