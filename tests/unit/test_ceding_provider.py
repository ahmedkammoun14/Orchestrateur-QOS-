"""
Tests de la démission du cédant (lot 9) : hub/orchestrator_core.py,
_decide_federated — bloc inséré entre l'award (lot 7) et
`state.service_vm = winner_vm`.

Symétrique de l'award (lot 7, test_award_message.py) : l'award PROMEUT le
gagnant, ce lot DÉMET le cédant — sans lui, les deux orchestrateurs se
croient actifs pendant jusqu'à ACTIVE_VM_SYNC_EVERY_N_CYCLES cycles
(split-brain).

Même contrainte que les autres fichiers de tests fédérés : pas de
pytest-asyncio installé → chaque test async pilote sa propre boucle via
`_run()`.
"""

import asyncio
import copy

import pytest

from hub import orchestrator_core as hub_core
from hub.provider_arbitration import GapGrade, PlacementPlan, ProviderBid
from shared import config
from shared.models import SLO


def _run(coro):
    async def _wrapped():
        result = await coro
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return result
    return asyncio.run(_wrapped())


# ── Helpers (repris de test_award_message.py / test_federated_cycle.py) ──

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
    return hub_core._FlowContext(vm_ids=list(vm_ids), now_iso="2026-08-05T10:00:00")


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
        timestamp="2026-08-05T10:00:00+00:00",
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
#  1. Chemin B + kubectl_ok=True + gagnant ≠ moi → démission
# ═══════════════════════════════════════════════════════════════

def test_1_chemin_b_kubectl_ok_demission(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1")

    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(
            decision="migrate", winner_vm="edge2", winner_provider="provider-2", path="B",
        ),
        "/award": lambda payload: {"delivered": True},
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert hub_core.state.is_active is False
    assert hub_core.state.hosting_vm == "edge2"


# ═══════════════════════════════════════════════════════════════
#  2. ⭐ Après démission, _step8_decide prend la branche STANDBY
# ═══════════════════════════════════════════════════════════════

def test_2_apres_demission_step8_decide_standby_sans_decision(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
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
    assert hub_core.state.is_active is False

    calls.clear()
    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert hub_core.state.last_decision["decision"] == "stay"
    assert "STANDBY" in hub_core.state.last_decision["reason"]
    assert calls == []   # aucun appel réseau : ni broadcast, ni arbitrate, ni decide


# ═══════════════════════════════════════════════════════════════
#  3. Chemin B avec award en échec → démission quand même
# ═══════════════════════════════════════════════════════════════

def test_3_award_en_echec_demission_quand_meme(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1")

    async def _fake_post(client, url, payload):
        if url.endswith("/broadcast"):
            return {"bids": [], "errors": []}
        if url.endswith("/arbitrate"):
            return _verdict(decision="migrate", winner_vm="edge2", winner_provider="provider-2", path="B")
        if url.endswith("/award"):
            return None   # relais local injoignable
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert hub_core.state.is_active is False
    assert hub_core.state.hosting_vm == "edge2"


# ═══════════════════════════════════════════════════════════════
#  4. kubectl_ok=False → pas de démission
# ═══════════════════════════════════════════════════════════════

def test_4_kubectl_echec_pas_de_demission(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1")

    async def _fake_kubectl_fail(client, from_vm, to_vm):
        return False
    monkeypatch.setattr(hub_core, "_execute_kubectl_migration", _fake_kubectl_fail)

    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(
            decision="migrate", winner_vm="edge2", winner_provider="provider-2", path="B",
        ),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert hub_core.state.is_active is True   # inchangé


# ═══════════════════════════════════════════════════════════════
#  5. Chemin A (gagnant = moi) → pas de démission
# ═══════════════════════════════════════════════════════════════

def test_5_chemin_a_gagnant_moi_pas_de_demission(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="cloud1")

    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(
            decision="migrate", winner_vm="cloud1", winner_provider="provider-1", path="A",
        ),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert hub_core.state.is_active is True
    assert hub_core.state.service_vm == "cloud1"


# ═══════════════════════════════════════════════════════════════
#  6. state.service_vm reste winner_vm après démission
# ═══════════════════════════════════════════════════════════════

def test_6_service_vm_toujours_winner_vm_apres_demission(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1")

    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(
            decision="migrate", winner_vm="edge2", winner_provider="provider-2", path="B",
        ),
        "/award": lambda payload: {"delivered": True},
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert hub_core.state.is_active is False
    assert hub_core.state.service_vm == "edge2"   # ligne suivante non régressée


# ═══════════════════════════════════════════════════════════════
#  7. Scénario complet : P1 cède, P2 est promu par l'award
# ═══════════════════════════════════════════════════════════════

def test_7_scenario_complet_p1_demis_p2_promu_un_seul_actif(monkeypatch):
    # ── Étape P1 : cède le service à provider-2 ──────────────────────────
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1")

    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(
            decision="migrate", winner_vm="edge2", winner_provider="provider-2", path="B",
        ),
        "/award": lambda payload: {"delivered": True},
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert hub_core.state.is_active is False   # P1 : démis

    # ── Étape P2 : reçoit l'award pour edge2 ─────────────────────────────
    # Même processus, même `state` : on bascule le contexte pour simuler le
    # hub PAIR qui reçoit /award (côté serveur, `award()` ne connaît que
    # `state` et `config.PROVIDER_ID` — c'est exactement ce que ferait une
    # VRAIE instance provider-2 recevant ce message).
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-2")

    result = _run(hub_core.award({
        "vm_id": "edge2", "intent_id": "cycle-5", "from_provider": "provider-1",
    }))

    assert result["accepted"] is True
    assert hub_core.state.is_active is True    # P2 : promu

    # Exactement un seul actif à l'issue de la séquence complète.
