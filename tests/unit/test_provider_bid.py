"""
Tests du bid unifié (lot 3) : PlacementPlan, GapGrade, ProviderBid,
slo_values_for_vm, build_gap_grade (hub/provider_arbitration.py), et
l'endpoint POST /evaluate (hub/orchestrator_core.py).

Deux familles de tests :
  • Fonctions PURES (dataclasses, conversion d'unités, action, secondaires)
    — appelées directement, sans TestClient.
  • Endpoint /evaluate — via TestClient, mêmes conventions que
    test_hub_relay_endpoint.py (TestClient SANS context manager : entrer
    dedans déclencherait le lifespan et ses appels réseau de healthcheck).
"""

import copy
import json

import pytest
from fastapi.testclient import TestClient

from hub import orchestrator_core as hub_core
from hub import provider_arbitration as arb
from hub.provider_arbitration import (
    GapGrade,
    PlacementPlan,
    ProviderBid,
    build_gap_grade,
    evaluate_provider,
    placement_action,
    signed_excess,
    slo_values_for_vm,
)
from shared import config
from shared.models import SLO

client = TestClient(hub_core.app)


# ── Helpers ───────────────────────────────────────────────────

def _slo_dict(metric="latency", operator="<", threshold=30.0, unit="ms",
              weight=1.0, is_primary=True) -> dict:
    return SLO(metric=metric, operator=operator, threshold=threshold, unit=unit,
               weight=weight, is_primary=is_primary).dict()


def _candidate(vm_id: str, latency: float = None, cpu_usage: float = 30.0,
               ram_usage: float = 40.0, cores=4, ram=8) -> dict:
    d = {
        "vm_id": vm_id, "cpu_usage": cpu_usage, "ram_usage": ram_usage,
        "total_cores": cores, "total_ram_gb": ram,
    }
    if latency is not None:
        d["latency"] = latency
    return d


def _preds(vm_id: str, **metrics) -> dict:
    return {vm_id: {m: {"predictions": [v, v, v]} for m, v in metrics.items()}}


def _prime_state(collected: list, predictions: dict) -> None:
    hub_core.state.last_collected       = collected
    hub_core.state.snapshot_predictions = predictions


@pytest.fixture(autouse=True)
def _reset_hub_state(monkeypatch):
    """Sauvegarde/restaure l'état module-level touché, mocke _post par défaut."""
    saved = {
        "last_collected":       copy.deepcopy(hub_core.state.last_collected),
        "snapshot_predictions": copy.deepcopy(hub_core.state.snapshot_predictions),
        "service_vm":           hub_core.state.service_vm,
        "cycle_count":          hub_core.state.cycle_count,
        "last_decision":        copy.deepcopy(hub_core.state.last_decision),
        "last_mi_scores":       copy.deepcopy(hub_core.state.last_mi_scores),
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
    hub_core.state.last_mi_scores       = saved["last_mi_scores"]


# ═══════════════════════════════════════════════════════════════
#  Fonctions pures — placement_action (test 7)
# ═══════════════════════════════════════════════════════════════

def test_placement_action_les_quatre_cas():
    assert placement_action(None, "edge1")   == "none"
    assert placement_action("edge1", None)   == "deploy"
    assert placement_action("edge1", "edge1") == "stay"
    assert placement_action("cloud1", "edge1") == "migrate"


# ═══════════════════════════════════════════════════════════════
#  ⭐ Conversion d'unités (test 4) — le test le plus important
# ═══════════════════════════════════════════════════════════════

def test_slo_values_for_vm_convertit_pourcentage_en_coeurs_disponibles():
    """4 cœurs, 20 % d'usage → 3.2 cœurs disponibles, PAS 20."""
    slo = _slo_dict(metric="cpu_usage", operator=">=", threshold=2.5, unit="cores")
    candidate = _candidate("edge1", cpu_usage=20.0, cores=4)

    values = slo_values_for_vm("edge1", [slo], candidate, predictions_map={})

    assert values["cpu_usage"] == pytest.approx(3.2, abs=1e-6)
    assert values["cpu_usage"] != 20.0


def test_delta_resultant_moins_0_28_pas_moins_7():
    slo = _slo_dict(metric="cpu_usage", operator=">=", threshold=2.5, unit="cores")
    candidate = _candidate("edge1", cpu_usage=20.0, cores=4)
    values = slo_values_for_vm("edge1", [slo], candidate, predictions_map={})

    delta = signed_excess(values["cpu_usage"], 2.5, ">=")

    assert delta == pytest.approx(-0.28, abs=1e-4)
    assert delta != pytest.approx(-7.0, abs=1e-4)

    gap = build_gap_grade([slo], values, is_compliant=True, evaluable=True)
    assert gap.detail["cpu_usage"] == pytest.approx(-0.28, abs=1e-4)
    assert gap.value == pytest.approx(-0.28, abs=1e-4)


# ═══════════════════════════════════════════════════════════════
#  Sérialisation round-trip (test 5)
# ═══════════════════════════════════════════════════════════════

def _round_trip(cls, obj):
    return cls.from_dict(json.loads(json.dumps(obj.to_dict())))


def test_placement_plan_round_trip():
    plan = PlacementPlan(
        provider_id="provider-1", vm_id="edge1", action="migrate",
        topsis_score=0.87, vm_scores={"edge1": 0.87, "cloud1": 0.5},
        reason="topsis local",
    )
    assert _round_trip(PlacementPlan, plan) == plan


def test_gap_grade_round_trip():
    gap = GapGrade(
        value=-0.28, is_compliant=True, evaluable=True,
        coverage=("cpu_usage", "latency"), detail={"cpu_usage": -0.28, "latency": 0.1},
    )
    assert _round_trip(GapGrade, gap) == gap


def test_provider_bid_round_trip():
    plan = PlacementPlan(
        provider_id="provider-1", vm_id="edge1", action="stay",
        topsis_score=None, vm_scores={}, reason="repli",
    )
    gap = GapGrade(value=None, is_compliant=False, evaluable=False, coverage=(), detail={})
    bid = ProviderBid(
        provider_id="provider-1", intent_id="intent-42",
        placement_plan=plan, gap_grade=gap,
        timestamp="2026-07-30T10:00:00+00:00",
    )
    assert _round_trip(ProviderBid, bid) == bid


def test_to_dict_est_directement_serialisable_par_json_dumps():
    """Aucun tuple, aucun objet — round-trip json.dumps SANS erreur."""
    gap = GapGrade(value=0.1, is_compliant=False, evaluable=True,
                    coverage=("latency",), detail={"latency": 0.1})
    raw = json.dumps(gap.to_dict())
    assert json.loads(raw)["coverage"] == ["latency"]   # tuple → liste


# ═══════════════════════════════════════════════════════════════
#  SLOs secondaires — absents de coverage/detail, sans effet (test 6)
# ═══════════════════════════════════════════════════════════════

def test_slo_secondaire_absent_de_coverage_et_detail_sans_effet_sur_value():
    slos = [
        _slo_dict(metric="latency",   operator="<", threshold=30.0, weight=1.0, is_primary=True),
        _slo_dict(metric="cpu_usage", operator="<", threshold=80.0, weight=1.0, is_primary=False),
    ]
    values = {"latency": 40.0, "cpu_usage": 90.0}

    gap = build_gap_grade(slos, values, is_compliant=False, evaluable=True)

    assert gap.coverage == ("latency",)
    assert "cpu_usage" not in gap.detail
    assert gap.value == pytest.approx((40.0 - 30.0) / 30.0, abs=1e-6)   # == 0.3333, comme si seule


# ═══════════════════════════════════════════════════════════════
#  Endpoint /evaluate — Cas A : provider avec VMs conformes (test 1)
# ═══════════════════════════════════════════════════════════════

def test_provider_avec_vms_conformes_topsis_champion_action_migrate(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")

    async def _fake_decide(client, url, payload):
        return {
            "decision": "migrate", "to_vm": "cloud1", "topsis_score": 0.87,
            "reason": "meilleur score TOPSIS local",
            "vm_scores": {"edge1": 0.5, "cloud1": 0.87},
        }
    monkeypatch.setattr(hub_core, "_post", _fake_decide)

    _prime_state(
        [_candidate("edge1", latency=20.0), _candidate("cloud1", latency=25.0)],
        {**_preds("edge1", latency=20.0), **_preds("cloud1", latency=25.0)},
    )

    r = client.post("/evaluate", json={
        "slos": [_slo_dict(threshold=30.0)],
        "incumbent_vm": "edge1",
    })

    assert r.status_code == 200
    body = r.json()
    assert body["gap_grade"]["is_compliant"] is True
    assert body["placement_plan"]["vm_id"] == "cloud1"
    assert body["placement_plan"]["action"] == "migrate"
    assert body["placement_plan"]["topsis_score"] == pytest.approx(0.87)


# ═══════════════════════════════════════════════════════════════
#  Endpoint /evaluate — Cas B : aucune VM conforme (test 2)
# ═══════════════════════════════════════════════════════════════

def test_provider_sans_vm_conforme_ne_propose_aucune_vm(monkeypatch):
    """
    edge1 (34 ms, marge quasi nulle) et cloud1 (10 ms, grosse marge) sont
    tous deux NON conformes (aucune prédiction — règle _filter_candidates).

    Règle métier (revue) : un provider sans VM conforme ne propose plus
    AUCUNE VM à l'arbitrage — jamais une offre "moins pire" via un repli
    Gap Grade minimal. champion reste None, même si des VMs étaient
    évaluables (evaluable=True) : seule la conformité ouvre le droit de
    proposer une VM.
    """
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")

    collected = [_candidate("edge1", latency=34.0), _candidate("cloud1", latency=10.0)]
    _prime_state(collected, {})   # AUCUNE prédiction pour personne

    slos = [_slo_dict(metric="latency", operator="<", threshold=35.0, weight=1.0)]

    candidates = hub_core._build_candidates(collected)
    assessment = evaluate_provider("provider-1", slos, candidates, {})
    assert assessment.compliant_vms == ()
    assert assessment.evaluable is True   # données présentes, juste non conformes

    r = client.post("/evaluate", json={"slos": slos})

    assert r.status_code == 200
    body = r.json()
    assert body["placement_plan"]["vm_id"] is None
    assert body["placement_plan"]["action"] == "none"
    assert body["gap_grade"]["is_compliant"] is False
    assert body["gap_grade"]["evaluable"] is True
    assert body["gap_grade"]["value"] is None


# ═══════════════════════════════════════════════════════════════
#  Endpoint /evaluate — Cas C : non évaluable (test 3)
# ═══════════════════════════════════════════════════════════════

def test_provider_non_evaluable_action_none(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")

    # Aucune métrique valorisable : ni prédiction, ni mesure brute.
    _prime_state([{"vm_id": "edge1"}, {"vm_id": "cloud1"}], {})

    r = client.post("/evaluate", json={"slos": [_slo_dict(threshold=30.0)]})

    assert r.status_code == 200
    body = r.json()
    assert body["gap_grade"]["evaluable"] is False
    assert body["gap_grade"]["value"] is None
    assert body["placement_plan"]["vm_id"] is None
    assert body["placement_plan"]["action"] == "none"


# ═══════════════════════════════════════════════════════════════
#  decision_intelligence injoignable → repli (test 8)
# ═══════════════════════════════════════════════════════════════

def test_decide_injoignable_repli_sur_premiere_vm_conforme(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")

    async def _fake_decide_fails(client, url, payload):
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_decide_fails)

    _prime_state(
        [_candidate("edge1", latency=20.0), _candidate("cloud1", latency=25.0)],
        {**_preds("edge1", latency=20.0), **_preds("cloud1", latency=25.0)},
    )

    r = client.post("/evaluate", json={"slos": [_slo_dict(threshold=30.0)]})

    assert r.status_code == 200
    body = r.json()
    assert body["placement_plan"]["vm_id"] == "edge1"   # première VM conforme (ordre d'entrée)
    assert "repli" in body["placement_plan"]["reason"]
    assert body["placement_plan"]["topsis_score"] is None


# ═══════════════════════════════════════════════════════════════
#  incumbent_vm injectée dans le payload /decide (test 9)
# ═══════════════════════════════════════════════════════════════

def test_incumbent_vm_injecte_dans_decide_current_data_et_declaree_service_vm(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    captured = {}

    async def _fake_decide(client, url, payload):
        captured["payload"] = payload
        return {"decision": "stay", "to_vm": None, "topsis_score": None, "reason": "x"}
    monkeypatch.setattr(hub_core, "_post", _fake_decide)

    _prime_state(
        [_candidate("edge1", latency=20.0), _candidate("cloud1", latency=25.0),
         _candidate("edge2", latency=45.0)],   # edge2 : VM active de l'AUTRE provider, en violation
        {**_preds("edge1", latency=20.0), **_preds("cloud1", latency=25.0)},
    )

    r = client.post("/evaluate", json={
        "slos": [_slo_dict(threshold=30.0)],
        "incumbent_vm": "edge2",
    })

    assert r.status_code == 200
    assert captured["payload"]["service_vm"] == "edge2"
    vm_ids = {c["vm_id"] for c in captured["payload"]["current_data"]}
    assert vm_ids == {"edge1", "cloud1", "edge2"}   # conformes + VM active injectée


# ═══════════════════════════════════════════════════════════════
#  Non-mutation de state (test 10)
# ═══════════════════════════════════════════════════════════════

def test_evaluate_ne_modifie_aucun_champ_de_state(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")

    _prime_state(
        [_candidate("edge1", latency=20.0), _candidate("cloud1", latency=25.0)],
        {**_preds("edge1", latency=20.0), **_preds("cloud1", latency=25.0)},
    )
    before_vm       = hub_core.state.service_vm
    before_cycle    = hub_core.state.cycle_count
    before_decision = copy.deepcopy(hub_core.state.last_decision)
    before_collected = copy.deepcopy(hub_core.state.last_collected)
    before_preds     = copy.deepcopy(hub_core.state.snapshot_predictions)

    r = client.post("/evaluate", json={
        "slos": [_slo_dict(threshold=30.0)],
        "incumbent_vm": "edge1",
    })

    assert r.status_code == 200
    assert hub_core.state.service_vm           == before_vm
    assert hub_core.state.cycle_count          == before_cycle
    assert hub_core.state.last_decision        == before_decision
    assert hub_core.state.last_collected       == before_collected
    assert hub_core.state.snapshot_predictions == before_preds


# ═══════════════════════════════════════════════════════════════
#  Gardes d'entrée (test 11)
# ═══════════════════════════════════════════════════════════════

def test_slos_absent_400():
    _prime_state([_candidate("edge1", latency=20.0)], _preds("edge1", latency=20.0))
    r = client.post("/evaluate", json={})
    assert r.status_code == 400


def test_slos_vide_400():
    _prime_state([_candidate("edge1", latency=20.0)], _preds("edge1", latency=20.0))
    r = client.post("/evaluate", json={"slos": []})
    assert r.status_code == 400


def test_last_collected_vide_503():
    hub_core.state.last_collected = []
    r = client.post("/evaluate", json={"slos": [_slo_dict(threshold=30.0)]})
    assert r.status_code == 503
