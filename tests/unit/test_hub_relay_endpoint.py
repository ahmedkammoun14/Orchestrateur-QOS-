"""
Tests de l'endpoint POST /intent/relay du hub (hub/orchestrator_core.py).

Vérifie que ce point de réception de la passation inter-provider est un pur
point d'observation : il évalue et répond, mais ne modifie JAMAIS l'état du
hub ni ne déclenche de migration.

TestClient est utilisé SANS context manager (`with TestClient(...) as c`) :
entrer dans le context manager déclencherait le `lifespan` de l'application,
qui fait des appels réseau de healthcheck vers les 8 autres spokes au
démarrage (voir hub/orchestrator_core.py::lifespan) — inutile et lent ici,
et absent par défaut avec TestClient(app) utilisé directement.
"""

import copy

import pytest
from fastapi.testclient import TestClient

from hub import orchestrator_core as hub_core
from shared.models import SLO, SLOIntent

client = TestClient(hub_core.app)


# ── Helpers ───────────────────────────────────────────────────

def _slo_dict(metric="latency", operator="<", threshold=30.0, unit="ms", weight=1.0,
              is_primary=True) -> dict:
    # is_primary=True par défaut : ces tests portent sur le CONTRAT — le SLO
    # doit pouvoir disqualifier une VM. Depuis que la conformité ne retient que
    # les primaires (hub/provider_arbitration.py::evaluate_vm), un SLO laissé au
    # défaut False de shared.models.SLO ne disqualifierait plus rien.
    return SLO(metric=metric, operator=operator, threshold=threshold, unit=unit,
               weight=weight, is_primary=is_primary).dict()


def _intent_payload(slos: list, attempted=(), intent_id="t1") -> dict:
    return SLOIntent(
        intent_id=intent_id,
        slos=[SLO(**s) for s in slos],
        mode="enhanced",
        created_at="2026-07-22T10:00:00",
        attempted_providers=tuple(attempted),
    ).to_dict()


def _candidate(vm_id: str, latency: float, cores=4, ram=8) -> dict:
    return {
        "vm_id": vm_id, "latency": latency,
        "cpu_usage": 30.0, "ram_usage": 40.0,
        "total_cores": cores, "total_ram_gb": ram,
    }


def _preds(vm_id: str, value: float) -> dict:
    return {vm_id: {"latency": {"predictions": [value, value, value]}}}


def _prime_state(candidates: list, predictions: dict) -> None:
    hub_core.state.last_collected       = candidates
    hub_core.state.snapshot_predictions = predictions


@pytest.fixture(autouse=True)
def _reset_hub_state(monkeypatch):
    """
    Sauvegarde/restaure les champs de `state` touchés par ces tests. `state`
    est un singleton module-level partagé par tout le process — sans ce
    fixture, un test polluerait les suivants. C'est aussi ce qui permet au
    test de non-mutation (ci-dessous) de comparer avant/après en confiance.

    Mocke aussi `_post` : quand `compliant_vms` est non vide, l'endpoint
    appelle maintenant decision_intelligence pour son `local_topsis` (voir
    Partie 1). Sans ce mock, ces tests ouvriraient un vrai socket vers
    localhost:8008 — ils retombent alors sur le repli déterministe (première
    VM conforme), ce qui suffit : aucun de ces tests n'affirme sur le contenu
    de `local_topsis`.
    """
    saved = {
        "last_collected":       copy.deepcopy(hub_core.state.last_collected),
        "snapshot_predictions": copy.deepcopy(hub_core.state.snapshot_predictions),
        "service_vm":           hub_core.state.service_vm,
        "cycle_count":          hub_core.state.cycle_count,
        "last_decision":        copy.deepcopy(hub_core.state.last_decision),
    }

    async def _fake_post(client, url, payload):
        return None

    monkeypatch.setattr(hub_core, "_post", _fake_post)

    yield
    hub_core.state.last_collected       = saved["last_collected"]
    hub_core.state.snapshot_predictions = saved["snapshot_predictions"]
    hub_core.state.service_vm           = saved["service_vm"]
    hub_core.state.cycle_count          = saved["cycle_count"]
    hub_core.state.last_decision        = saved["last_decision"]


# ── Scénarios de négociation ──────────────────────────────────

def test_provider_avec_vms_conformes_donne_prend_local_conforme():
    _prime_state(
        [_candidate("edge1", 20.0), _candidate("cloud1", 25.0)],
        {**_preds("edge1", 20.0), **_preds("cloud1", 25.0)},
    )
    intent = _intent_payload([_slo_dict(threshold=30.0)])

    r = client.post(
        "/intent/relay",
        json={"acting_as_provider": "provider-1", "slo_intent": intent, "offer": None},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["negotiation"]["decision"] == "prend_local_conforme"
    assert body["negotiation"]["compliant_vms"]


def test_local_topsis_replie_sur_la_premiere_vm_conforme_si_decide_indisponible():
    """
    Le mock global de `_post` (voir _reset_hub_state) renvoie None : simule
    decision_intelligence indisponible. L'endpoint doit alors retomber sur la
    première VM de `compliant_vms`, jamais planter ni renvoyer local_topsis=None.
    """
    _prime_state(
        [_candidate("edge1", 20.0), _candidate("cloud1", 25.0)],
        {**_preds("edge1", 20.0), **_preds("cloud1", 25.0)},
    )
    intent = _intent_payload([_slo_dict(threshold=30.0)])

    r = client.post(
        "/intent/relay",
        json={"acting_as_provider": "provider-1", "slo_intent": intent, "offer": None},
    )

    assert r.status_code == 200
    local_topsis = r.json()["local_topsis"]
    assert local_topsis is not None
    assert local_topsis["to_vm"] == r.json()["negotiation"]["compliant_vms"][0]


def test_local_topsis_reflete_la_reponse_de_decision_intelligence(monkeypatch):
    """Quand /decide répond, local_topsis porte SON to_vm, pas le repli."""
    async def _fake_decide(client, url, payload):
        assert payload["service_vm"] == payload["current_data"][0]["vm_id"]
        return {"decision": "migrate", "to_vm": "cloud1", "topsis_score": 0.87,
                "reason": "meilleur score TOPSIS local"}

    monkeypatch.setattr(hub_core, "_post", _fake_decide)

    _prime_state(
        [_candidate("edge1", 20.0), _candidate("cloud1", 25.0)],
        {**_preds("edge1", 20.0), **_preds("cloud1", 25.0)},
    )
    intent = _intent_payload([_slo_dict(threshold=30.0)])

    r = client.post(
        "/intent/relay",
        json={"acting_as_provider": "provider-1", "slo_intent": intent, "offer": None},
    )

    assert r.status_code == 200
    local_topsis = r.json()["local_topsis"]
    assert local_topsis["to_vm"] == "cloud1"
    assert local_topsis["topsis_score"] == 0.87


def test_aucun_conforme_offre_recue_moins_bonne_donne_prend_local_meilleure():
    _prime_state(
        [_candidate("edge1", 32.0), _candidate("cloud1", 40.0)],
        {**_preds("edge1", 32.0), **_preds("cloud1", 40.0)},
    )
    intent = _intent_payload([_slo_dict(threshold=30.0)], attempted=("provider-2",))
    offer  = {"provider_id": "provider-2", "vm_id": "edge2", "violation_score": 0.5}

    r = client.post(
        "/intent/relay",
        json={"acting_as_provider": "provider-1", "slo_intent": intent, "offer": offer},
    )

    assert r.status_code == 200
    assert r.json()["negotiation"]["decision"] == "prend_local_meilleure"


def test_offre_recue_meilleure_donne_cede_a_l_offre():
    _prime_state(
        [_candidate("edge1", 32.0), _candidate("cloud1", 40.0)],
        {**_preds("edge1", 32.0), **_preds("cloud1", 40.0)},
    )
    intent = _intent_payload([_slo_dict(threshold=30.0)], attempted=("provider-2",))
    offer  = {"provider_id": "provider-2", "vm_id": "edge2", "violation_score": 0.01}

    r = client.post(
        "/intent/relay",
        json={"acting_as_provider": "provider-1", "slo_intent": intent, "offer": offer},
    )

    assert r.status_code == 200
    assert r.json()["negotiation"]["decision"] == "cede_a_l_offre"


# ── Erreurs ───────────────────────────────────────────────────

def test_acting_as_provider_inconnu_400():
    _prime_state([_candidate("edge1", 20.0)], _preds("edge1", 20.0))
    intent = _intent_payload([_slo_dict()])

    r = client.post(
        "/intent/relay",
        json={"acting_as_provider": "provider-42", "slo_intent": intent},
    )
    assert r.status_code == 400


def test_hub_pas_encore_chaud_503():
    hub_core.state.last_collected = []
    intent = _intent_payload([_slo_dict()])

    r = client.post(
        "/intent/relay",
        json={"acting_as_provider": "provider-1", "slo_intent": intent},
    )
    assert r.status_code == 503


# ── Trace de relais ───────────────────────────────────────────

def test_reponse_ajoute_acting_as_provider_aux_attempted_providers():
    _prime_state([_candidate("edge1", 20.0)], _preds("edge1", 20.0))
    intent = _intent_payload([_slo_dict(threshold=30.0)], attempted=("provider-2",))

    r = client.post(
        "/intent/relay",
        json={"acting_as_provider": "provider-1", "slo_intent": intent},
    )

    assert r.status_code == 200
    attempted = r.json()["attempted_providers"]
    assert "provider-1" in attempted
    assert "provider-2" in attempted   # préservé, pas écrasé


def test_pas_de_doublon_si_deja_tente():
    _prime_state([_candidate("edge1", 20.0)], _preds("edge1", 20.0))
    intent = _intent_payload([_slo_dict(threshold=30.0)], attempted=("provider-1",))

    r = client.post(
        "/intent/relay",
        json={"acting_as_provider": "provider-1", "slo_intent": intent},
    )

    assert r.json()["attempted_providers"] == ["provider-1"]


# ── Non-mutation de l'état du hub ──────────────────────────────

def test_endpoint_ne_modifie_aucun_champ_de_state():
    _prime_state(
        [_candidate("edge1", 32.0), _candidate("cloud1", 40.0)],
        {**_preds("edge1", 32.0), **_preds("cloud1", 40.0)},
    )
    before_vm       = hub_core.state.service_vm
    before_cycle    = hub_core.state.cycle_count
    before_decision = copy.deepcopy(hub_core.state.last_decision)

    intent = _intent_payload([_slo_dict(threshold=30.0)])
    offer  = {"provider_id": "provider-2", "vm_id": "edge2", "violation_score": 0.001}

    r = client.post(
        "/intent/relay",
        json={
            "acting_as_provider": "provider-1",
            "slo_intent":         intent,
            "offer":              offer,
            "incumbent_provider": "provider-1",
        },
    )

    assert r.status_code == 200
    assert hub_core.state.service_vm    == before_vm
    assert hub_core.state.cycle_count   == before_cycle
    assert hub_core.state.last_decision == before_decision


# ── Round-trip de l'intention à travers l'endpoint ────────────

def test_round_trip_slo_intent_metrique_seuil_et_poids_preserves():
    """
    Deux SLOs de poids DIFFÉRENTS (0.25 / 0.75) et d'excès DIFFÉRENTS
    (0.10 / 0.20) : si le seuil OU le poids d'un des deux SLOs se perdait
    pendant le passage JSON → SLOIntent.from_dict, le score de violation
    calculé par l'endpoint ne vaudrait plus 0.175. C'est cette valeur qui
    prouve la fidélité du round-trip, pas juste l'absence de crash.
    """
    _prime_state(
        [_candidate("edge1", 33.0)],   # sera écrasé par les prédictions ci-dessous
        {"edge1": {
            "latency":   {"predictions": [33.0, 33.0, 33.0]},   # seuil 30 → excès 0.10
            "cpu_usage": {"predictions": [55.0, 55.0, 55.0]},   # seuil 50 → excès 0.10... voir poids
        }},
    )
    # cpu_usage à 60 (excès 0.20) pour rendre le poids observable dans le score.
    hub_core.state.snapshot_predictions["edge1"]["cpu_usage"]["predictions"] = [60.0] * 3

    slos = [
        _slo_dict(metric="latency",   operator="<", threshold=30.0, unit="ms", weight=0.25),
        _slo_dict(metric="cpu_usage", operator="<", threshold=50.0, unit="%",  weight=0.75),
    ]
    intent = _intent_payload(slos)

    r = client.post(
        "/intent/relay",
        json={"acting_as_provider": "provider-1", "slo_intent": intent, "offer": None},
    )

    assert r.status_code == 200
    body = r.json()
    # Pas conforme (les deux SLOs violés) : c'est local_offer qui porte le score.
    assert body["local_offer"] is not None
    assert body["local_offer"]["violation_score"] == pytest.approx(0.175, abs=1e-6)


# ── Correctif : incumbent_vm — TOPSIS doit réellement s'exécuter ─
#
# Défaut corrigé : /intent/relay désignait une VM CONFORME comme
# "service_vm" auprès de decision_intelligence. decision.py cherche
# d'abord une violation SUR cette VM ; n'en trouvant aucune (elle est
# conforme), il retourne {"decision": "stay", "to_vm": None} SANS jamais
# atteindre TOPSIS — le repli "première VM conforme" s'appliquait alors
# systématiquement, même avec plusieurs VMs conformes à départager.

def test_correctif_incumbent_vm_permet_a_topsis_de_s_executer(monkeypatch):
    """
    Le défaut est corrigé : avec 2 VMs conformes ET incumbent_vm renseignée
    (VM de l'émetteur, en violation), /decide est appelé et SON résultat
    utilisé — jamais le repli "première VM conforme".

    Le faux /decide simule ICI le vrai comportement barrière de decision.py :
    si "service_vm" ne viole rien (c'est le cas d'edge2/cloud2, conformes),
    il renvoie stay/to_vm=None SANS jamais atteindre TOPSIS — c'est
    EXACTEMENT le défaut d'origine (service_vm = compliant_vms[0], toujours
    conforme). Ce test échouerait avec l'ancien code, qui envoyait
    précisément une VM conforme comme service_vm : voir la vérification
    manuelle documentée dans le livrable de cette tâche.
    """
    async def _fake_decide(client, url, payload):
        service_vm = payload["service_vm"]
        conformes  = {"edge2", "cloud2"}
        if service_vm in conformes:
            # Barrière "aucune violation" de decision.py — TOPSIS jamais atteint.
            return {"decision": "stay", "to_vm": None, "topsis_score": None,
                    "reason": "No SLO violation detected"}
        # service_vm en violation (la vraie VM active) : TOPSIS s'exécute
        # réellement et départage les candidats conformes du receveur.
        return {
            "decision": "migrate", "to_vm": "cloud2", "topsis_score": 0.93,
            "reason": "meilleur score TOPSIS local",
        }
    monkeypatch.setattr(hub_core, "_post", _fake_decide)

    _prime_state(
        [_candidate("edge1", 45.0), _candidate("edge2", 20.0), _candidate("cloud2", 25.0)],
        {**_preds("edge2", 20.0), **_preds("cloud2", 25.0)},
    )
    intent = _intent_payload([_slo_dict(threshold=30.0)])
    offer  = {"provider_id": "provider-1", "vm_id": "edge1", "violation_score": 0.5}

    r = client.post(
        "/intent/relay",
        json={
            "acting_as_provider": "provider-2",
            "slo_intent":         intent,
            "offer":              offer,
            "incumbent_provider": "provider-1",
            "incumbent_vm":       "edge1",
        },
    )

    assert r.status_code == 200
    local_topsis = r.json()["local_topsis"]
    assert local_topsis["to_vm"] == "cloud2"
    assert "repli" not in (local_topsis["reason"] or "")


def test_service_vm_envoye_a_decide_vaut_incumbent_vm_pas_compliant_vms_0(monkeypatch):
    captured = {}

    async def _fake_decide(client, url, payload):
        captured["payload"] = payload
        return {"decision": "stay", "to_vm": None, "topsis_score": None, "reason": "x"}
    monkeypatch.setattr(hub_core, "_post", _fake_decide)

    _prime_state(
        [_candidate("edge1", 45.0), _candidate("edge2", 20.0), _candidate("cloud2", 25.0)],
        {**_preds("edge2", 20.0), **_preds("cloud2", 25.0)},
    )
    intent = _intent_payload([_slo_dict(threshold=30.0)])

    r = client.post(
        "/intent/relay",
        json={
            "acting_as_provider": "provider-2",
            "slo_intent":         intent,
            "incumbent_provider": "provider-1",
            "incumbent_vm":       "edge1",
        },
    )

    assert r.status_code == 200
    compliant_vms = r.json()["negotiation"]["compliant_vms"]
    assert captured["payload"]["service_vm"] == "edge1"
    assert captured["payload"]["service_vm"] != compliant_vms[0]


def test_current_data_contient_la_vm_active_en_plus_des_conformes(monkeypatch):
    captured = {}

    async def _fake_decide(client, url, payload):
        captured["payload"] = payload
        return {"decision": "stay", "to_vm": None, "topsis_score": None, "reason": "x"}
    monkeypatch.setattr(hub_core, "_post", _fake_decide)

    _prime_state(
        [_candidate("edge1", 45.0), _candidate("edge2", 20.0), _candidate("cloud2", 25.0)],
        {**_preds("edge2", 20.0), **_preds("cloud2", 25.0)},
    )
    intent = _intent_payload([_slo_dict(threshold=30.0)])

    r = client.post(
        "/intent/relay",
        json={
            "acting_as_provider": "provider-2",
            "slo_intent":         intent,
            "incumbent_provider": "provider-1",
            "incumbent_vm":       "edge1",
        },
    )

    assert r.status_code == 200
    vm_ids = {c["vm_id"] for c in captured["payload"]["current_data"]}
    assert vm_ids == {"edge1", "edge2", "cloud2"}   # conformes + VM active de l'émetteur


def test_repli_preserve_sans_incumbent_vm():
    """
    Sans incumbent_vm (champ absent) : comportement IDENTIQUE à avant le
    correctif — service_vm = compliant_vms[0].
    """
    _prime_state(
        [_candidate("edge1", 20.0), _candidate("cloud1", 25.0)],
        {**_preds("edge1", 20.0), **_preds("cloud1", 25.0)},
    )
    intent = _intent_payload([_slo_dict(threshold=30.0)])

    r = client.post(
        "/intent/relay",
        json={"acting_as_provider": "provider-1", "slo_intent": intent, "offer": None},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["local_topsis"]["to_vm"] == body["negotiation"]["compliant_vms"][0]


def test_repli_si_incumbent_vm_introuvable_dans_last_collected(monkeypatch):
    """
    incumbent_vm fournie mais absente de state.last_collected (VM inconnue,
    hub pas synchronisé...) : repli sûr sur la première VM conforme, jamais
    d'exception. La VM introuvable n'est PAS injectée dans current_data.
    """
    captured = {}

    async def _fake_decide(client, url, payload):
        captured["payload"] = payload
        return {"decision": "stay", "to_vm": None, "topsis_score": None, "reason": "x"}
    monkeypatch.setattr(hub_core, "_post", _fake_decide)

    _prime_state(
        [_candidate("edge2", 20.0), _candidate("cloud2", 25.0)],   # edge1 absent des mesures
        {**_preds("edge2", 20.0), **_preds("cloud2", 25.0)},
    )
    intent = _intent_payload([_slo_dict(threshold=30.0)])

    r = client.post(
        "/intent/relay",
        json={
            "acting_as_provider": "provider-2",
            "slo_intent":         intent,
            "incumbent_provider": "provider-1",
            "incumbent_vm":       "edge1",   # introuvable dans last_collected
        },
    )

    assert r.status_code == 200
    compliant_vms = r.json()["negotiation"]["compliant_vms"]
    assert captured["payload"]["service_vm"] == compliant_vms[0]
    vm_ids = {c["vm_id"] for c in captured["payload"]["current_data"]}
    assert "edge1" not in vm_ids


def test_repli_si_decide_echoue_meme_avec_incumbent_vm(monkeypatch):
    """
    /decide indisponible (le mock renvoie None, comme _post le fait
    réellement sur exception ou statut non-2xx) MÊME avec incumbent_vm
    fournie : repli sur la première VM conforme, aucune exception propagée.
    """
    async def _fake_decide_fails(client, url, payload):
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_decide_fails)

    _prime_state(
        [_candidate("edge1", 45.0), _candidate("edge2", 20.0), _candidate("cloud2", 25.0)],
        {**_preds("edge2", 20.0), **_preds("cloud2", 25.0)},
    )
    intent = _intent_payload([_slo_dict(threshold=30.0)])

    r = client.post(
        "/intent/relay",
        json={
            "acting_as_provider": "provider-2",
            "slo_intent":         intent,
            "incumbent_provider": "provider-1",
            "incumbent_vm":       "edge1",
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["local_topsis"]["to_vm"] == body["negotiation"]["compliant_vms"][0]
    assert "repli" in body["local_topsis"]["reason"]


# ── Non-régression de _build_candidates ───────────────────────

def test_build_candidates_reproduit_la_structure_du_code_original():
    collected = [{
        "vm_id":        "edge1",
        "latency":      42.0,
        "cpu_usage":    55.0,
        "ram_usage":    60.0,
        "total_cores":  4,
        "total_ram_gb": 8,
        "reliability":  0.9,   # champ hors METRICS_REGISTRY : ignoré, comme avant
    }]
    result = hub_core._build_candidates(collected)
    assert result == [{
        "vm_id":        "edge1",
        "rtt_ms":       42.0,
        "cpu_usage":    55.0,
        "ram_usage":    60.0,
        "total_cores":  4,
        "total_ram_gb": 8,
    }]
