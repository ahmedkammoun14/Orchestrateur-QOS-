"""
Tests du lot 6b-express : bloc "reasoning" compatible dashboard dans l'audit
du cycle fédéré (_post_federated_audit, hub/orchestrator_core.py).

Le panneau RAISONNEMENT du dashboard attend les clés historiques
(provider_courant/vm_active/evaluations/compliant_vms/negotiation/topsis) —
ce lot les ajoute EN PLUS des clés fédérées du lot 6a (federated/bids/
considered/alert/peer_errors), sans en perdre aucune.

Même contrainte que test_federated_cycle.py : pas de pytest-asyncio installé
→ chaque test pilote sa propre boucle via `_run()`.
"""

import asyncio
import copy
import json

import pytest

from hub import orchestrator_core as hub_core
from hub.provider_arbitration import GapGrade, PlacementPlan, ProviderBid
from shared.models import SLO


def _run(coro):
    async def _wrapped():
        result = await coro
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return result
    return asyncio.run(_wrapped())


# ── Helpers ───────────────────────────────────────────────────

def _slo_dict(metric="latency", operator="<", threshold=30.0, unit="ms", weight=1.0) -> dict:
    return SLO(metric=metric, operator=operator, threshold=threshold, unit=unit, weight=weight).dict()


def _candidate(vm_id: str, latency: float, cores=4, ram=8) -> dict:
    return {
        "vm_id": vm_id, "latency": latency,
        "cpu_usage": 30.0, "ram_usage": 40.0,
        "total_cores": cores, "total_ram_gb": ram,
        "reliability": 1.0,
    }


def _preds(**par_vm) -> dict:
    return {vm_id: {"latency": {"predictions": [v, v, v]}} for vm_id, v in par_vm.items()}


def _ctx(vm_ids=("edge1", "cloud1", "edge2", "cloud2")) -> "hub_core._FlowContext":
    return hub_core._FlowContext(vm_ids=list(vm_ids), now_iso="2026-08-02T10:00:00")


def _prof() -> "hub_core.StepProfiler":
    return hub_core.StepProfiler()


def _prime(service_vm: str, candidates: list, predictions: dict, slos=None, cycle=5) -> None:
    hub_core.state.service_vm           = service_vm
    hub_core.state.last_collected       = candidates
    hub_core.state.last_predictions     = predictions
    hub_core.state.snapshot_predictions = predictions
    hub_core.state.current_slos         = slos if slos is not None else [_slo_dict(threshold=30.0)]
    hub_core.state._mode                = "enhanced"
    hub_core.state.cycle_count          = cycle
    hub_core.state.last_migration_ts    = None


def _make_bid(provider_id: str, vm_id, gap_value, is_compliant=True, evaluable=True,
              topsis_score=None, vm_scores=None) -> ProviderBid:
    return ProviderBid(
        provider_id=provider_id,
        intent_id="cycle-5",
        placement_plan=PlacementPlan(
            provider_id=provider_id, vm_id=vm_id, action="stay",
            topsis_score=topsis_score, vm_scores=vm_scores or {}, reason="x",
        ),
        gap_grade=GapGrade(
            value=gap_value, is_compliant=is_compliant, evaluable=evaluable,
            coverage=("latency",), detail={"latency": gap_value},
        ),
        timestamp="2026-08-02T10:00:00+00:00",
    )


def _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1", gap_value=-0.05,
                   topsis_score=0.8, vm_scores=None):
    own = _make_bid(provider_id, vm_id, gap_value, topsis_score=topsis_score,
                     vm_scores=vm_scores or {"edge1": 0.8, "cloud1": 0.5})
    async def _fake_build_local_bid(client, slos, incumbent_vm, intent_id=None):
        return own
    monkeypatch.setattr(hub_core, "_build_local_bid", _fake_build_local_bid)
    return own


def _make_post_router(responses: dict):
    calls = []

    async def fake_post(client, url, payload):
        calls.append({"url": url, "payload": payload})
        for suffix, handler in responses.items():
            if url.endswith(suffix):
                return handler(payload)
        return None

    return fake_post, calls


def _verdict(decision="stay", winner_vm=None, winner_provider=None, path="A",
             reason="x", gap_grade=-0.05, considered=None, alert=None,
             deadband_applied=0.05) -> dict:
    return {
        "decision": decision, "winner_vm": winner_vm, "winner_provider": winner_provider,
        "path": path, "reason": reason, "gap_grade": gap_grade,
        "deadband_applied": deadband_applied, "considered": considered or [], "alert": alert,
    }


@pytest.fixture(autouse=True)
def _reset_state_and_stub_side_effects(monkeypatch):
    saved = {
        "last_collected":       copy.deepcopy(hub_core.state.last_collected),
        "last_predictions":     copy.deepcopy(hub_core.state.last_predictions),
        "snapshot_predictions": copy.deepcopy(hub_core.state.snapshot_predictions),
        "current_slos":         copy.deepcopy(hub_core.state.current_slos),
        "service_vm":           hub_core.state.service_vm,
        "cycle_count":          hub_core.state.cycle_count,
        "last_decision":        copy.deepcopy(hub_core.state.last_decision),
        "last_migration_ts":    hub_core.state.last_migration_ts,
        "_mode":                hub_core.state._mode,
    }

    async def _fake_kubectl(client, from_vm, to_vm):
        return True
    monkeypatch.setattr(hub_core, "_execute_kubectl_migration", _fake_kubectl)

    yield

    hub_core.state.last_collected       = saved["last_collected"]
    hub_core.state.last_predictions     = saved["last_predictions"]
    hub_core.state.snapshot_predictions = saved["snapshot_predictions"]
    hub_core.state.current_slos         = saved["current_slos"]
    hub_core.state.service_vm           = saved["service_vm"]
    hub_core.state.cycle_count          = saved["cycle_count"]
    hub_core.state.last_decision        = saved["last_decision"]
    hub_core.state.last_migration_ts    = saved["last_migration_ts"]
    hub_core.state._mode                = saved["_mode"]


def _capture_audit(monkeypatch) -> list:
    audits = []
    async def _capture(url, payload):
        audits.append(payload)
    monkeypatch.setattr(hub_core, "_post_audit", _capture)
    return audits


def _prime_two_compliant_vms():
    _prime(
        "edge1",
        [_candidate("edge1", 20.0), _candidate("cloud1", 25.0)],
        {**_preds(edge1=20.0), **_preds(cloud1=25.0)},
    )


# ═══════════════════════════════════════════════════════════════
#  1-2. Les six clés dashboard, evaluations non vide
# ═══════════════════════════════════════════════════════════════

def test_1_reasoning_contient_les_six_cles_dashboard(monkeypatch):
    audits = _capture_audit(monkeypatch)
    _mock_own_bid(monkeypatch)
    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(winner_vm="edge1", winner_provider="provider-1"),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_two_compliant_vms()

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert len(audits) == 1
    reasoning = audits[0]["reasoning"]
    for key in ("provider_courant", "vm_active", "evaluations",
                "compliant_vms", "negotiation", "topsis"):
        assert key in reasoning, f"clé manquante : {key}"


def test_2_evaluations_non_vide_avec_champs_attendus(monkeypatch):
    audits = _capture_audit(monkeypatch)
    _mock_own_bid(monkeypatch)
    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(winner_vm="edge1", winner_provider="provider-1"),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_two_compliant_vms()

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    evaluations = audits[0]["reasoning"]["evaluations"]
    assert len(evaluations) > 0
    for ev in evaluations:
        assert "vm_id" in ev and "is_compliant" in ev and "evaluable" in ev
    vm_ids = {ev["vm_id"] for ev in evaluations}
    assert vm_ids == {"edge1", "cloud1"}   # VMs DU PROVIDER COURANT uniquement


# ═══════════════════════════════════════════════════════════════
#  3. Clés fédérées toujours présentes
# ═══════════════════════════════════════════════════════════════

def test_3_cles_federees_toujours_presentes(monkeypatch):
    audits = _capture_audit(monkeypatch)
    own = _mock_own_bid(monkeypatch)
    peer_bid_dict = _make_bid("provider-2", "edge2", -0.09).to_dict()
    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [peer_bid_dict], "errors": []},
        "/arbitrate": lambda payload: _verdict(winner_vm="edge2", winner_provider="provider-2", path="B"),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_two_compliant_vms()

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    reasoning = audits[0]["reasoning"]
    assert reasoning["federated"] is True
    assert reasoning["bids"] == [own.to_dict(), peer_bid_dict]
    assert "considered" in reasoning
    assert "alert" in reasoning
    assert "peer_errors" in reasoning


# ═══════════════════════════════════════════════════════════════
#  4. Chemin C → alert + negotiation présents
# ═══════════════════════════════════════════════════════════════

def test_4_chemin_c_alert_et_negotiation_presents(monkeypatch):
    audits = _capture_audit(monkeypatch)
    _mock_own_bid(monkeypatch)
    alert = {
        "kind": "INFAISABLE",
        "best_effort": {"provider_id": "provider-1", "vm_id": "edge1", "gap_grade": 0.10},
        "providers_evaluated": ["provider-1"],
        "message": "Aucun provider conforme",
    }
    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(
            decision="stay", winner_vm=None, winner_provider=None, path="C",
            gap_grade=None, alert=alert,
        ),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_two_compliant_vms()

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    reasoning = audits[0]["reasoning"]
    assert reasoning["alert"] == alert
    assert reasoning["negotiation"] is not None
    assert reasoning["negotiation"]["decision"] == "stay"


# ═══════════════════════════════════════════════════════════════
#  5. ⭐ Arbitre indisponible — audit posté quand même
# ═══════════════════════════════════════════════════════════════

def test_5_arbitre_indisponible_audit_poste_reasoning_partiel(monkeypatch):
    audits = _capture_audit(monkeypatch)
    _mock_own_bid(monkeypatch)
    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        # pas de handler pour /arbitrate → None (injoignable)
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_two_compliant_vms()

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))   # ne doit pas lever

    assert len(audits) == 1
    reasoning = audits[0]["reasoning"]
    assert reasoning is not None
    assert reasoning["provider_courant"] == "provider-1"
    assert len(reasoning["evaluations"]) > 0
    assert reasoning["topsis"] is None
    assert reasoning["negotiation"] is None


# ═══════════════════════════════════════════════════════════════
#  6. ⭐ Exception dans la construction du reasoning → cycle continue
# ═══════════════════════════════════════════════════════════════

def test_6_exception_construction_reasoning_audit_poste_reasoning_none(monkeypatch):
    audits = _capture_audit(monkeypatch)
    _mock_own_bid(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("evaluate_provider a explosé")
    monkeypatch.setattr(hub_core, "evaluate_provider", _boom)

    warnings = []
    monkeypatch.setattr(hub_core.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg)))

    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(winner_vm="edge1", winner_provider="provider-1"),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_two_compliant_vms()

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))   # ne doit pas lever

    assert len(audits) == 1
    assert audits[0]["reasoning"] is None
    assert any("reasoning" in w.lower() for w in warnings)
    # Le cycle a bien continué : provider_path/provider_used restent posés.
    assert audits[0]["provider_path"] == "A"


# ═══════════════════════════════════════════════════════════════
#  7. Sérialisable json.dumps tel quel
# ═══════════════════════════════════════════════════════════════

def test_7_payload_json_dumps_able(monkeypatch):
    audits = _capture_audit(monkeypatch)
    _mock_own_bid(monkeypatch)
    peer_bid_dict = _make_bid("provider-2", "edge2", -0.09).to_dict()
    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [peer_bid_dict], "errors": []},
        "/arbitrate": lambda payload: _verdict(winner_vm="edge2", winner_provider="provider-2", path="B"),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_two_compliant_vms()

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    json.dumps(audits[0])   # ne doit pas lever


# ═══════════════════════════════════════════════════════════════
#  8. provider_path / provider_used toujours présents (non-régression 6a)
# ═══════════════════════════════════════════════════════════════

def test_8_provider_path_et_provider_used_presents(monkeypatch):
    audits = _capture_audit(monkeypatch)
    _mock_own_bid(monkeypatch)
    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(winner_vm="edge1", winner_provider="provider-1", path="A"),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_two_compliant_vms()

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert audits[0]["provider_path"] == "A"
    assert audits[0]["provider_used"] == "provider-1"
