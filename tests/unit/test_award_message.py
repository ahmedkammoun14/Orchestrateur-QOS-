"""
Tests du message d'ATTRIBUTION (award, lot 7) : hub/orchestrator_core.py
(endpoint POST /award, envoi depuis _decide_federated).

Couvre :
  • POST /award — garde de périmètre, mise à jour de state.
  • Non-régression avec le lot 1b : un award pose service_vm à une VM
    précise que _sync_active_vm ne doit PAS écraser au sync suivant si
    kubectl renvoie la VM canonique du même node.
  • _decide_federated — envoi best-effort de l'award après une migration
    kubectl réussie vers un pair, jamais vers soi-même, jamais sur échec
    kubectl, jamais fatal si le relais est injoignable.

Même contrainte que les autres fichiers de tests fédérés : pas de
pytest-asyncio installé → chaque test async pilote sa propre boucle via
`_run()`.
"""

import asyncio
import copy

import pytest
from fastapi.testclient import TestClient

from hub import orchestrator_core as hub_core
from hub.provider_arbitration import GapGrade, PlacementPlan, ProviderBid
from shared import config
from shared.models import SLO

client = TestClient(hub_core.app)


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
    return hub_core._FlowContext(vm_ids=list(vm_ids), now_iso="2026-08-03T10:00:00")


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


def _make_bid(provider_id: str, vm_id, gap_value, is_compliant=True, evaluable=True) -> ProviderBid:
    return ProviderBid(
        provider_id=provider_id,
        intent_id="cycle-5",
        placement_plan=PlacementPlan(
            provider_id=provider_id, vm_id=vm_id, action="stay",
            topsis_score=None, vm_scores={}, reason="x",
        ),
        gap_grade=GapGrade(
            value=gap_value, is_compliant=is_compliant, evaluable=evaluable,
            coverage=("latency",), detail={"latency": gap_value},
        ),
        timestamp="2026-08-03T10:00:00+00:00",
    )


def _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1", gap_value=-0.05):
    own = _make_bid(provider_id, vm_id, gap_value)
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
             reason="x", gap_grade=-0.05, considered=None, alert=None) -> dict:
    return {
        "decision": decision, "winner_vm": winner_vm, "winner_provider": winner_provider,
        "path": path, "reason": reason, "gap_grade": gap_grade,
        "deadband_applied": 0.05, "considered": considered or [], "alert": alert,
    }


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Double minimal de httpx.AsyncClient — seul `.get()` est utilisé par _sync_active_vm."""

    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    async def get(self, url, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._response


@pytest.fixture(autouse=True)
def _reset_state_and_stub_side_effects(monkeypatch):
    saved = {
        "last_collected":       copy.deepcopy(hub_core.state.last_collected),
        "last_predictions":     copy.deepcopy(hub_core.state.last_predictions),
        "snapshot_predictions": copy.deepcopy(hub_core.state.snapshot_predictions),
        "current_slos":         copy.deepcopy(hub_core.state.current_slos),
        "service_vm":           hub_core.state.service_vm,
        "hosting_vm":           hub_core.state.hosting_vm,
        "is_active":            hub_core.state.is_active,
        "cycle_count":          hub_core.state.cycle_count,
        "last_decision":        copy.deepcopy(hub_core.state.last_decision),
        "last_migration_ts":    hub_core.state.last_migration_ts,
        "_mode":                hub_core.state._mode,
    }

    async def _fake_post_audit(url, payload):
        return None

    async def _fake_kubectl_ok(client, from_vm, to_vm):
        return True

    monkeypatch.setattr(hub_core, "_post_audit", _fake_post_audit)
    monkeypatch.setattr(hub_core, "_execute_kubectl_migration", _fake_kubectl_ok)

    yield

    hub_core.state.last_collected       = saved["last_collected"]
    hub_core.state.last_predictions     = saved["last_predictions"]
    hub_core.state.snapshot_predictions = saved["snapshot_predictions"]
    hub_core.state.current_slos         = saved["current_slos"]
    hub_core.state.service_vm           = saved["service_vm"]
    hub_core.state.hosting_vm           = saved["hosting_vm"]
    hub_core.state.is_active            = saved["is_active"]
    hub_core.state.cycle_count          = saved["cycle_count"]
    hub_core.state.last_decision        = saved["last_decision"]
    hub_core.state.last_migration_ts    = saved["last_migration_ts"]
    hub_core.state._mode                = saved["_mode"]


# ═══════════════════════════════════════════════════════════════
#  1-3. Endpoint POST /award
# ═══════════════════════════════════════════════════════════════

def test_1_award_vm_de_mon_provider_accepted(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "hosting_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "is_active", False)

    r = client.post("/award", json={"vm_id": "edge1b", "intent_id": "cycle-5", "from_provider": "provider-1"})

    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["vm_id"] == "edge1b"
    assert hub_core.state.service_vm == "edge1b"
    assert hub_core.state.hosting_vm == "edge1b"
    assert hub_core.state.is_active is True


def test_2_award_vm_d_un_autre_provider_refuse_sans_mutation(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "hosting_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "is_active", True)

    r = client.post("/award", json={"vm_id": "edge2", "intent_id": "cycle-5", "from_provider": "provider-2"})

    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is False
    assert hub_core.state.service_vm == "cloud1"
    assert hub_core.state.hosting_vm == "cloud1"
    assert hub_core.state.is_active is True


def test_3_award_vm_inconnue_refuse_sans_mutation(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "hosting_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "is_active", True)

    r = client.post("/award", json={"vm_id": "vm-fantome", "intent_id": "cycle-5", "from_provider": "provider-2"})

    assert r.status_code == 200
    assert r.json()["accepted"] is False
    assert hub_core.state.service_vm == "cloud1"
    assert hub_core.state.hosting_vm == "cloud1"


# ═══════════════════════════════════════════════════════════════
#  4. ⭐ Non-régression lot 1b : award puis sync même node
# ═══════════════════════════════════════════════════════════════

def test_4_award_puis_sync_meme_node_conserve(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-2")
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud2")
    monkeypatch.setattr(hub_core.state, "hosting_vm", "cloud2")
    monkeypatch.setattr(hub_core.state, "is_active", False)

    r = client.post("/award", json={"vm_id": "edge2b", "intent_id": "cycle-5", "from_provider": "provider-1"})
    assert r.json()["accepted"] is True
    assert hub_core.state.service_vm == "edge2b"
    assert hub_core.state.is_active is True

    # kubectl renvoie la VM CANONIQUE du même node (pop1-worker-2) : edge2.
    fake_client = _FakeClient(response=_FakeResponse(200, {"active_vm": "edge2", "cluster": "x"}))
    _run(hub_core._sync_active_vm(fake_client))

    assert hub_core.state.service_vm == "edge2b"   # PAS écrasé par la VM canonique
    assert hub_core.state.hosting_vm == "edge2"
    assert hub_core.state.is_active is True


# ═══════════════════════════════════════════════════════════════
#  5-8. Envoi de l'award depuis _decide_federated
# ═══════════════════════════════════════════════════════════════

def test_5_chemin_b_migration_reussie_envoie_award(monkeypatch):
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1")
    fake_post, calls = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(
            decision="migrate", winner_vm="edge2", winner_provider="provider-2", path="B",
        ),
        "/award": lambda payload: {"delivered": True},
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    award_calls = [c for c in calls if c["url"].endswith("/award")]
    assert len(award_calls) == 1
    assert award_calls[0]["payload"]["target_provider"] == "provider-2"
    assert award_calls[0]["payload"]["vm_id"] == "edge2"
    assert hub_core.state.service_vm == "edge2"   # la migration a bien eu lieu


def test_6_kubectl_echec_aucun_award(monkeypatch):
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1")

    async def _fake_kubectl_fail(client, from_vm, to_vm):
        return False
    monkeypatch.setattr(hub_core, "_execute_kubectl_migration", _fake_kubectl_fail)

    fake_post, calls = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(
            decision="migrate", winner_vm="edge2", winner_provider="provider-2", path="B",
        ),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert [c for c in calls if c["url"].endswith("/award")] == []


def test_7_chemin_a_gagnant_moi_aucun_award(monkeypatch):
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="cloud1")
    fake_post, calls = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(
            decision="migrate", winner_vm="cloud1", winner_provider="provider-1", path="A",
        ),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert [c for c in calls if c["url"].endswith("/award")] == []
    assert hub_core.state.service_vm == "cloud1"


def test_8_relais_injoignable_pour_award_cycle_termine_normalement(monkeypatch):
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1")
    audits = []
    async def _capture_audit(url, payload):
        audits.append(payload)
    monkeypatch.setattr(hub_core, "_post_audit", _capture_audit)

    async def _fake_post(client, url, payload):
        if url.endswith("/broadcast"):
            return {"bids": [], "errors": []}
        if url.endswith("/arbitrate"):
            return _verdict(decision="migrate", winner_vm="edge2", winner_provider="provider-2", path="B")
        if url.endswith("/award"):
            return None   # relais local injoignable — _post renvoie None
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))   # ne doit pas lever

    assert hub_core.state.last_decision["decision"] == "migrate"
    assert hub_core.state.last_decision["to_vm"]    == "edge2"
    assert hub_core.state.service_vm == "edge2"     # la migration elle-même a réussi
    assert len(audits) == 1                         # l'audit est bien posté malgré l'échec de l'award
