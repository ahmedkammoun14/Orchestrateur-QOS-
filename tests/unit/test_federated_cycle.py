"""
Tests du câblage fédéré du cycle (lot 6a) : _step8_decide → _decide_federated
(hub/orchestrator_core.py) — broadcast (relais) + arbitrage (placement_arbiter).

Aucun test n'ouvre de socket : `_post`, `_post_audit`, `_execute_kubectl_migration`
et `_build_local_bid` sont mockés via monkeypatch. Ces fonctions sont async ;
en l'absence de pytest-asyncio (non installé, aucune nouvelle dépendance
autorisée), chaque test pilote sa propre boucle via `_run()`.
"""

import asyncio
import copy
import time

import pytest

from hub import orchestrator_core as hub_core
from hub.provider_arbitration import GapGrade, PlacementPlan, ProviderBid
from shared import config
from shared.models import SLO


# ── Exécution des coroutines sans pytest-asyncio ──────────────

def _run(coro):
    async def _wrapped():
        result = await coro
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return result
    return asyncio.run(_wrapped())


# ── Helpers de construction ────────────────────────────────────

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
    return hub_core._FlowContext(vm_ids=list(vm_ids), now_iso="2026-07-31T10:00:00")


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
    hub_core.state.last_migration_ts    = None   # cooldown inactif par défaut


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
        timestamp="2026-07-31T10:00:00+00:00",
    )


def _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1", gap_value=-0.05):
    own = _make_bid(provider_id, vm_id, gap_value)
    async def _fake_build_local_bid(client, slos, incumbent_vm, intent_id=None):
        return own
    monkeypatch.setattr(hub_core, "_build_local_bid", _fake_build_local_bid)
    return own


def _make_post_router(responses: dict):
    """
    Faux `_post` routé par SUFFIXE d'URL (ex. "/broadcast", "/arbitrate") vers
    un handler(payload) -> dict|None. Enregistre chaque appel (url, payload),
    DANS L'ORDRE, dans la liste `calls` retournée.
    """
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
    """Sauvegarde/restaure `state` ; stub par défaut des effets de bord réels."""
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

    async def _fake_post_audit(url, payload):
        return None

    async def _fake_kubectl(client, from_vm, to_vm):
        return True

    monkeypatch.setattr(hub_core, "_post_audit", _fake_post_audit)
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


# ═══════════════════════════════════════════════════════════════
#  1. ⭐ Non-régression — MULTI_PROVIDER_ENABLED=false
# ═══════════════════════════════════════════════════════════════

def test_flag_false_appelle_mono_jamais_federe(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", False)

    mono_calls = []
    async def _fake_mono(client, ctx, prof, current_data):
        mono_calls.append(current_data)
    monkeypatch.setattr(hub_core, "_decide_mono_provider", _fake_mono)

    async def _sentinel(*args, **kwargs):
        raise AssertionError("_decide_federated appelé alors que le flag est OFF")
    monkeypatch.setattr(hub_core, "_decide_federated", _sentinel)

    calls = []
    async def _fake_post(client, url, payload):
        calls.append(url)
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_post)

    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert len(mono_calls) == 1
    assert not any(u.endswith("/broadcast") or u.endswith("/arbitrate") for u in calls)


# ═══════════════════════════════════════════════════════════════
#  2. ⭐ La gate
# ═══════════════════════════════════════════════════════════════

def test_gate_sans_violation_aucun_appel_reseau(monkeypatch):
    calls = []
    async def _fake_post(client, url, payload):
        calls.append(url)
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_post)

    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=False))

    assert calls == []
    assert hub_core.state.last_decision["decision"] == "stay"
    assert hub_core.state.last_decision["reason"] == "Aucune violation primaire"


# ═══════════════════════════════════════════════════════════════
#  3. violation_detected=True → 1 /broadcast PUIS 1 /arbitrate
# ═══════════════════════════════════════════════════════════════

def test_violation_detectee_broadcast_puis_arbitrate_dans_cet_ordre(monkeypatch):
    _mock_own_bid(monkeypatch)
    fake_post, calls = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": [], "relayed_by": "provider_relay", "timestamp": "x"},
        "/arbitrate": lambda payload: _verdict(winner_vm="edge1", winner_provider="provider-1"),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    network_calls = [c for c in calls if c["url"].endswith("/broadcast") or c["url"].endswith("/arbitrate")]
    assert len(network_calls) == 2
    assert network_calls[0]["url"].endswith("/broadcast")
    assert network_calls[1]["url"].endswith("/arbitrate")


# ═══════════════════════════════════════════════════════════════
#  4. Le bid local figure en tête de la liste envoyée à /arbitrate
# ═══════════════════════════════════════════════════════════════

def test_bid_local_en_tete_de_la_liste_envoyee_a_arbitrate(monkeypatch):
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1", gap_value=-0.05)
    peer_bid_dict = _make_bid("provider-2", "edge2", -0.09).to_dict()

    fake_post, calls = _make_post_router({
        "/broadcast": lambda payload: {"bids": [peer_bid_dict], "errors": []},
        "/arbitrate": lambda payload: _verdict(winner_vm="edge2", winner_provider="provider-2", path="B"),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    arbitrate_call = next(c for c in calls if c["url"].endswith("/arbitrate"))
    bids_sent = arbitrate_call["payload"]["bids"]
    assert len(bids_sent) == 2
    assert bids_sent[0]["provider_id"] == "provider-1"   # notre bid TOUJOURS en tête
    assert bids_sent[1]["provider_id"] == "provider-2"


# ═══════════════════════════════════════════════════════════════
#  5. Verdict "migrate" → kubectl + state mis à jour
# ═══════════════════════════════════════════════════════════════

def test_verdict_migrate_execute_kubectl_et_met_a_jour_state(monkeypatch):
    _mock_own_bid(monkeypatch)
    kubectl_calls = []
    async def _fake_kubectl(client, from_vm, to_vm):
        kubectl_calls.append((from_vm, to_vm))
        return True
    monkeypatch.setattr(hub_core, "_execute_kubectl_migration", _fake_kubectl)

    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(
            decision="migrate", winner_vm="edge2", winner_provider="provider-2", path="B",
        ),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert kubectl_calls == [("edge1", "edge2")]
    assert hub_core.state.service_vm == "edge2"
    assert hub_core.state.last_migration_ts is not None


# ═══════════════════════════════════════════════════════════════
#  6. Verdict "stay" → aucun kubectl
# ═══════════════════════════════════════════════════════════════

def test_verdict_stay_aucun_appel_kubectl(monkeypatch):
    _mock_own_bid(monkeypatch)
    kubectl_calls = []
    async def _fake_kubectl(client, from_vm, to_vm):
        kubectl_calls.append((from_vm, to_vm))
        return True
    monkeypatch.setattr(hub_core, "_execute_kubectl_migration", _fake_kubectl)

    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(winner_vm="edge1", winner_provider="provider-1"),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert kubectl_calls == []
    assert hub_core.state.service_vm == "edge1"


# ═══════════════════════════════════════════════════════════════
#  7. Verdict chemin "C"/"D" → aucun kubectl, alerte dans l'audit
# ═══════════════════════════════════════════════════════════════

def test_verdict_chemin_c_aucun_kubectl_alerte_dans_audit(monkeypatch):
    _mock_own_bid(monkeypatch)
    audits = []
    async def _capture_audit(url, payload):
        audits.append(payload)
    monkeypatch.setattr(hub_core, "_post_audit", _capture_audit)

    kubectl_calls = []
    async def _fake_kubectl(client, from_vm, to_vm):
        kubectl_calls.append((from_vm, to_vm))
        return True
    monkeypatch.setattr(hub_core, "_execute_kubectl_migration", _fake_kubectl)

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
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert kubectl_calls == []
    assert len(audits) == 1
    assert audits[0]["provider_path"] == "C"
    assert audits[0]["reasoning"]["alert"] == alert


# ═══════════════════════════════════════════════════════════════
#  8. Relais injoignable → le cycle continue, /arbitrate avec le seul bid local
# ═══════════════════════════════════════════════════════════════

def test_relais_injoignable_arbitrate_appele_avec_seul_bid_local(monkeypatch):
    _mock_own_bid(monkeypatch, provider_id="provider-1", vm_id="edge1")

    calls = []
    async def _fake_post(client, url, payload):
        calls.append({"url": url, "payload": payload})
        if url.endswith("/broadcast"):
            return None   # relais injoignable
        if url.endswith("/arbitrate"):
            return _verdict(winner_vm="edge1", winner_provider="provider-1")
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_post)

    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))   # ne doit pas lever

    arbitrate_call = next(c for c in calls if c["url"].endswith("/arbitrate"))
    assert len(arbitrate_call["payload"]["bids"]) == 1
    assert arbitrate_call["payload"]["bids"][0]["provider_id"] == "provider-1"


# ═══════════════════════════════════════════════════════════════
#  9. ⭐ Arbitre injoignable → STAY par sécurité
# ═══════════════════════════════════════════════════════════════

def test_arbitre_injoignable_stay_par_securite(monkeypatch):
    _mock_own_bid(monkeypatch)
    kubectl_calls = []
    async def _fake_kubectl(client, from_vm, to_vm):
        kubectl_calls.append((from_vm, to_vm))
        return True
    monkeypatch.setattr(hub_core, "_execute_kubectl_migration", _fake_kubectl)

    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        # pas de handler pour /arbitrate → fake_post renvoie None (injoignable)
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert hub_core.state.last_decision["decision"] == "stay"
    assert kubectl_calls == []
    assert "Arbitre indisponible" in hub_core.state.last_decision["reason"]


# ═══════════════════════════════════════════════════════════════
#  10. Arbitre répond un corps invalide → même comportement sûr
# ═══════════════════════════════════════════════════════════════

def test_arbitre_reponse_invalide_stay_par_securite(monkeypatch):
    _mock_own_bid(monkeypatch)
    kubectl_calls = []
    async def _fake_kubectl(client, from_vm, to_vm):
        kubectl_calls.append((from_vm, to_vm))
        return True
    monkeypatch.setattr(hub_core, "_execute_kubectl_migration", _fake_kubectl)

    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: {"foo": "bar"},   # dict SANS la clé "decision"
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert hub_core.state.last_decision["decision"] == "stay"
    assert kubectl_calls == []
    assert "Arbitre indisponible" in hub_core.state.last_decision["reason"]


def test_arbitre_reponse_non_dict_stay_par_securite(monkeypatch):
    _mock_own_bid(monkeypatch)
    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: "reponse inattendue, pas un dict",
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert hub_core.state.last_decision["decision"] == "stay"


# ═══════════════════════════════════════════════════════════════
#  11. Cooldown actif → aucun appel réseau, "stay"
# ═══════════════════════════════════════════════════════════════

def test_cooldown_actif_aucun_appel_reseau(monkeypatch):
    calls = []
    async def _fake_post(client, url, payload):
        calls.append(url)
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_post)

    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))
    hub_core.state.last_migration_ts = time.monotonic()   # cooldown actif

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert calls == []
    assert hub_core.state.last_decision["decision"] == "stay"
    assert hub_core.state.last_decision["reason"]   == "Cooldown active"


# ═══════════════════════════════════════════════════════════════
#  12. incumbent_provider introuvable → repli sur _decide_mono_provider
# ═══════════════════════════════════════════════════════════════

def test_incumbent_provider_introuvable_replie_sur_mono(monkeypatch):
    mono_calls = []
    async def _fake_mono(client, ctx, prof, current_data):
        mono_calls.append(current_data)
    monkeypatch.setattr(hub_core, "_decide_mono_provider", _fake_mono)

    calls = []
    async def _fake_post(client, url, payload):
        calls.append(url)
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_post)

    _prime("vm-fantome", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert len(mono_calls) == 1
    assert calls == []   # aucun broadcast/arbitrate tenté


# ═══════════════════════════════════════════════════════════════
#  13. L'audit posté contient provider_path, provider_used, reasoning
# ═══════════════════════════════════════════════════════════════

def test_audit_contient_provider_path_provider_used_et_reasoning(monkeypatch):
    _mock_own_bid(monkeypatch)
    audits = []
    async def _capture_audit(url, payload):
        audits.append(payload)
    monkeypatch.setattr(hub_core, "_post_audit", _capture_audit)

    considered = [{"provider_id": "provider-1", "gap_grade": -0.05, "is_compliant": True,
                   "evaluable": True, "retained": True, "why": "retenu"}]
    fake_post, _ = _make_post_router({
        "/broadcast": lambda payload: {"bids": [], "errors": []},
        "/arbitrate": lambda payload: _verdict(
            winner_vm="edge1", winner_provider="provider-1", considered=considered,
        ),
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._decide_federated(client=None, ctx=_ctx(), prof=_prof(), violation_detected=True))

    assert len(audits) == 1
    assert audits[0]["provider_path"] == "A"
    assert audits[0]["provider_used"] == "provider-1"
    assert audits[0]["reasoning"]["federated"] is True
    assert audits[0]["reasoning"]["considered"] == considered
    assert audits[0]["reasoning"]["bids"]
