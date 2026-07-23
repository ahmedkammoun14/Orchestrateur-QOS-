"""
Tests de la machine à états multi-provider dans hub/orchestrator_core.py
(_step8_decide → _decide_mono_provider / _decide_multi_provider).

Aucun test n'ouvre de socket : tous les appels réseau (_post,
_execute_kubectl_migration, _post_audit) sont mockés via monkeypatch.
Ces fonctions sont async ; en l'absence de pytest-asyncio (non installé,
aucune nouvelle dépendance autorisée), chaque test pilote sa propre boucle
via `_run()` (wrapper autour de `asyncio.run`).
"""

import asyncio
import copy
import time

import pytest

from hub import orchestrator_core as hub_core
from shared import config
from shared.models import SLO


# ── Exécution des coroutines sans pytest-asyncio ──────────────

def _run(coro):
    """
    asyncio.run() + un `sleep(0)` final pour laisser les tâches
    fire-and-forget (asyncio.create_task de l'audit) s'exécuter avant que
    la boucle ne soit fermée — sinon elles seraient annulées sans avoir
    tourné, et les tests qui inspectent l'audit ne verraient rien.
    """
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
    return hub_core._FlowContext(vm_ids=list(vm_ids), now_iso="2026-07-22T10:00:00")


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


def _make_post_router(responses: dict):
    """
    Fabrique un faux `_post` qui route selon le SUFFIXE de l'URL appelée
    (ex. "/decide", "/handoff") vers un handler(payload) -> dict|None.
    Enregistre chaque appel (url, payload) dans la liste `calls` retournée.
    """
    calls = []

    async def fake_post(client, url, payload):
        calls.append({"url": url, "payload": payload})
        for suffix, handler in responses.items():
            if url.endswith(suffix):
                return handler(payload)
        return None

    return fake_post, calls


def _capture_warnings(monkeypatch) -> list:
    messages = []

    def _wrapped(msg, *a, **kw):
        messages.append(str(msg))

    monkeypatch.setattr(hub_core.logger, "warning", _wrapped)
    return messages


@pytest.fixture(autouse=True)
def _reset_state_and_stub_side_effects(monkeypatch):
    """
    Sauvegarde/restaure les champs de `state` touchés par ces tests (état
    module-level partagé), et remplace par des doublures inertes les deux
    effets de bord réels que le flag ON peut déclencher : l'audit
    fire-and-forget et la migration kubectl. Sans ça, chaque test ouvrirait
    un vrai socket vers observability/openstack_client.
    """
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
#  Non-régression (flag OFF) — LE PLUS IMPORTANT
# ═══════════════════════════════════════════════════════════════

def test_flag_off_n_appelle_jamais_decide_multi_provider(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", False)

    async def _sentinel(*args, **kwargs):
        raise AssertionError("_decide_multi_provider appelé alors que le flag est OFF")
    monkeypatch.setattr(hub_core, "_decide_multi_provider", _sentinel)

    async def _fake_post(client, url, payload):
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_post)

    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))   # ne doit pas lever


def test_flag_off_payload_decide_identique_a_l_origine():
    """
    Reconstruit le payload attendu à partir de la formule EXACTE du code
    d'origine (désormais dans _decide_mono_provider) et vérifie que
    _step8_decide, flag OFF, envoie bien ce payload — au champ près.
    """
    monkeypatch_target = config.MULTI_PROVIDER_ENABLED
    config.MULTI_PROVIDER_ENABLED = False
    try:
        captured = {}

        async def _fake_post(client, url, payload):
            if url.endswith("/decide"):
                captured["payload"] = payload
            return None
        original_post = hub_core._post
        hub_core._post = _fake_post
        try:
            candidates = [
                _candidate("edge1", 40.0), _candidate("cloud1", 45.0),
                _candidate("edge2", 50.0), _candidate("cloud2", 55.0),
            ]
            _prime("edge1", candidates, _preds(edge1=40.0, cloud1=45.0, edge2=50.0, cloud2=55.0))
            ctx = _ctx()

            _run(hub_core._step8_decide(client=None, ctx=ctx, prof=_prof()))

            expected_current_data = hub_core._build_candidates(hub_core.state.last_collected)
            collected_lookup = {lc["vm_id"]: lc for lc in hub_core.state.last_collected}
            expected = {
                "current_data":       expected_current_data,
                "predictions_map":    hub_core.state.last_predictions,
                "slos":               hub_core.state.current_slos,
                "service_vm":         hub_core.state.service_vm,
                "cooldown_active":    False,
                "reliability_scores": {
                    vid: collected_lookup.get(vid, {}).get("reliability", 1.0)
                    for vid in ctx.vm_ids
                },
                "cycle":              hub_core.state.cycle_count,
                "mi_scores":          hub_core.state.last_mi_scores,
            }
            assert captured["payload"] == expected
        finally:
            hub_core._post = original_post
    finally:
        config.MULTI_PROVIDER_ENABLED = monkeypatch_target


# ═══════════════════════════════════════════════════════════════
#  Chemin A — TOPSIS intra-provider sur les conformes
# ═══════════════════════════════════════════════════════════════

def test_chemin_a_decide_recoit_exactement_les_vms_conformes(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    fake_post, calls = _make_post_router({
        "/decide": lambda payload: {
            "decision": "stay", "from_vm": None, "to_vm": None,
            "reason": "conforme", "topsis_score": 0.5, "breach_type": None,
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)

    _prime(
        "edge1",
        [_candidate("edge1", 20.0), _candidate("cloud1", 25.0),
         _candidate("edge2", 50.0), _candidate("cloud2", 55.0)],
        _preds(edge1=20.0, cloud1=25.0, edge2=50.0, cloud2=55.0),
    )

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    decide_calls = [c for c in calls if c["url"].endswith("/decide")]
    assert len(decide_calls) == 1
    vm_ids_sent = {c["vm_id"] for c in decide_calls[0]["payload"]["current_data"]}
    assert vm_ids_sent == {"edge1", "cloud1"}   # jamais les 4


def test_chemin_a_n_appelle_pas_le_relais(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    fake_post, calls = _make_post_router({
        "/decide": lambda payload: {
            "decision": "stay", "from_vm": None, "to_vm": None,
            "reason": "conforme", "topsis_score": 0.5, "breach_type": None,
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)

    _prime(
        "edge1",
        [_candidate("edge1", 20.0), _candidate("cloud1", 25.0),
         _candidate("edge2", 50.0), _candidate("cloud2", 55.0)],
        _preds(edge1=20.0, cloud1=25.0, edge2=50.0, cloud2=55.0),
    )

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert [c for c in calls if c["url"].endswith("/handoff")] == []


# ═══════════════════════════════════════════════════════════════
#  Chemins B / C — passation inter-provider
# ═══════════════════════════════════════════════════════════════

def _prime_provider_1_sans_conforme():
    """Les 2 VMs de provider-1 violent le seuil : aucune conforme chez lui."""
    _prime(
        "edge1",
        [_candidate("edge1", 40.0), _candidate("cloud1", 45.0),
         _candidate("edge2", 50.0), _candidate("cloud2", 55.0)],
        _preds(edge1=40.0, cloud1=45.0, edge2=50.0, cloud2=55.0),
    )


def test_chemin_bc_appelle_handoff_avec_bon_target_et_attempted(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    fake_post, calls = _make_post_router({
        "/handoff": lambda payload: {
            "negotiation": {
                "decision": "aucune_option", "winning_provider": None, "winning_vm": None,
                "compliant_vms": [], "local_score": None, "offered_score": None,
                "deadband_applied": 0.0, "reason": "rien d'exploitable",
            },
            "local_topsis": None,
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_provider_1_sans_conforme()

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    handoff_calls = [c for c in calls if c["url"].endswith("/handoff")]
    assert len(handoff_calls) == 1
    payload = handoff_calls[0]["payload"]
    assert payload["target_provider"] == "provider-2"
    assert payload["from_provider"]   == "provider-1"
    assert "provider-1" in payload["slo_intent"]["attempted_providers"]


def test_prend_local_conforme_migre_vers_local_topsis_to_vm(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    fake_post, _ = _make_post_router({
        "/handoff": lambda payload: {
            "negotiation": {
                "decision": "prend_local_conforme", "winning_provider": "provider-2",
                "winning_vm": None, "compliant_vms": ["edge2", "cloud2"],
                "local_score": 0.0, "offered_score": None, "deadband_applied": 0.0,
                "reason": "provider-2 prend le service",
            },
            "local_topsis": {"to_vm": "cloud2", "topsis_score": 0.9, "reason": "meilleur TOPSIS"},
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_provider_1_sans_conforme()

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert hub_core.state.service_vm == "cloud2"
    assert hub_core.state.last_decision["decision"] == "migrate"
    assert hub_core.state.last_decision["to_vm"]    == "cloud2"


def test_prend_local_meilleure_migre_vers_negotiation_winning_vm(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    fake_post, _ = _make_post_router({
        "/handoff": lambda payload: {
            "negotiation": {
                "decision": "prend_local_meilleure", "winning_provider": "provider-2",
                "winning_vm": "edge2", "compliant_vms": [], "local_score": 0.20,
                "offered_score": 0.05, "deadband_applied": 0.0, "reason": "offre meilleure",
            },
            "local_topsis": None,
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_provider_1_sans_conforme()

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert hub_core.state.service_vm == "edge2"
    assert hub_core.state.last_decision["decision"] == "migrate"


def test_cede_a_l_offre_migre_vers_notre_best_effort_vm(monkeypatch):
    """cloud1 (excès 0.0667) est moins mauvaise qu'edge1 (excès 0.333, actif)."""
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    fake_post, _ = _make_post_router({
        "/handoff": lambda payload: {
            "negotiation": {
                "decision": "cede_a_l_offre", "winning_provider": "provider-1",
                "winning_vm": None, "compliant_vms": [], "local_score": 0.0667,
                "offered_score": 0.20, "deadband_applied": 0.0, "reason": "notre offre gagne",
            },
            "local_topsis": None,
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime(
        "edge1",
        [_candidate("edge1", 40.0), _candidate("cloud1", 32.0),
         _candidate("edge2", 50.0), _candidate("cloud2", 55.0)],
        _preds(edge1=40.0, cloud1=32.0, edge2=50.0, cloud2=55.0),
    )

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert hub_core.state.service_vm == "cloud1"
    assert hub_core.state.last_decision["decision"] == "migrate"


def test_cede_a_l_offre_stay_si_best_effort_vm_deja_actif(monkeypatch):
    """edge1 (actif, excès 0.0667) est déjà la meilleure VM du provider."""
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    fake_post, _ = _make_post_router({
        "/handoff": lambda payload: {
            "negotiation": {
                "decision": "cede_a_l_offre", "winning_provider": "provider-1",
                "winning_vm": None, "compliant_vms": [], "local_score": 0.0667,
                "offered_score": 0.20, "deadband_applied": 0.0, "reason": "notre offre gagne",
            },
            "local_topsis": None,
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime(
        "edge1",
        [_candidate("edge1", 32.0), _candidate("cloud1", 40.0),
         _candidate("edge2", 50.0), _candidate("cloud2", 55.0)],
        _preds(edge1=32.0, cloud1=40.0, edge2=50.0, cloud2=55.0),
    )

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert hub_core.state.service_vm == "edge1"
    assert hub_core.state.last_decision["decision"] == "stay"


def test_aucune_option_reste_stay_avec_placement_impossible(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    fake_post, _ = _make_post_router({
        "/handoff": lambda payload: {
            "negotiation": {
                "decision": "aucune_option", "winning_provider": None, "winning_vm": None,
                "compliant_vms": [], "local_score": None, "offered_score": None,
                "deadband_applied": 0.0, "reason": "personne n'a de VM exploitable",
            },
            "local_topsis": None,
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)
    _prime_provider_1_sans_conforme()

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert hub_core.state.last_decision["decision"] == "stay"
    assert "PLACEMENT_IMPOSSIBLE" in hub_core.state.last_decision["reason"]


# ═══════════════════════════════════════════════════════════════
#  Robustesse
# ═══════════════════════════════════════════════════════════════

def test_relais_injoignable_stay_sans_exception_warning_journalise(monkeypatch):
    """
    _post capture déjà toute exception réseau et renvoie None (voir son
    implémentation) — c'est ce None que _decide_multi_provider doit
    absorber en STAY, jamais en exception remontée.
    """
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    warnings = _capture_warnings(monkeypatch)

    async def _fake_post_unreachable(client, url, payload):
        return None   # simule le relais injoignable (timeout, connexion refusée...)
    monkeypatch.setattr(hub_core, "_post", _fake_post_unreachable)
    _prime_provider_1_sans_conforme()

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))   # ne doit pas lever

    assert hub_core.state.last_decision["decision"] == "stay"
    assert any("passation" in m.lower() or "relais" in m.lower() or "relay" in m.lower()
               for m in warnings)


def test_relais_409_anti_boucle_stay_sans_exception(monkeypatch):
    """
    Un 409 (garde anti-boucle de provider_relay) est traduit par le _post
    réel en None (statut hors 200/201/202) — même trajectoire que
    l'injoignabilité du point de vue de _decide_multi_provider.
    """
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    warnings = _capture_warnings(monkeypatch)

    async def _fake_post_409(client, url, payload):
        return None   # _post renvoie None sur tout statut hors 200/201/202
    monkeypatch.setattr(hub_core, "_post", _fake_post_409)
    _prime_provider_1_sans_conforme()

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))   # ne doit pas lever

    assert hub_core.state.last_decision["decision"] == "stay"
    assert len(warnings) > 0


def test_vm_active_hors_registre_replie_sur_mono_provider(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    warnings = _capture_warnings(monkeypatch)

    mono_calls = []

    async def _fake_mono(client, ctx, prof, current_data):
        mono_calls.append(current_data)
    monkeypatch.setattr(hub_core, "_decide_mono_provider", _fake_mono)

    _prime("vm-fantome", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert len(mono_calls) == 1
    assert any("PROVIDER_OF_VM" in m for m in warnings)


def test_cooldown_actif_stay_immediat_sans_evaluation_ni_passation(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)

    post_calls = []

    async def _fake_post(client, url, payload):
        post_calls.append(url)
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_post)

    eval_calls = []
    original_evaluate = hub_core.evaluate_provider

    def _tracking_evaluate(*args, **kwargs):
        eval_calls.append(args)
        return original_evaluate(*args, **kwargs)
    monkeypatch.setattr(hub_core, "evaluate_provider", _tracking_evaluate)

    _prime_provider_1_sans_conforme()
    hub_core.state.last_migration_ts = time.monotonic()   # cooldown actif

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert post_calls == []
    assert eval_calls == []
    assert hub_core.state.last_decision["decision"] == "stay"
    assert hub_core.state.last_decision["reason"]   == "Cooldown active"


# ═══════════════════════════════════════════════════════════════
#  Audit
# ═══════════════════════════════════════════════════════════════

def test_audit_contient_provider_path_et_provider_used_chemin_a(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = []

    async def _capture_audit(url, payload):
        audits.append(payload)
    monkeypatch.setattr(hub_core, "_post_audit", _capture_audit)

    fake_post, _ = _make_post_router({
        "/decide": lambda payload: {
            "decision": "stay", "from_vm": None, "to_vm": None,
            "reason": "conforme, stay", "topsis_score": 0.9, "breach_type": None,
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)

    _prime(
        "edge1",
        [_candidate("edge1", 20.0), _candidate("cloud1", 25.0),
         _candidate("edge2", 50.0), _candidate("cloud2", 55.0)],
        _preds(edge1=20.0, cloud1=25.0, edge2=50.0, cloud2=55.0),
    )

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert len(audits) == 1
    assert audits[0]["provider_path"] == "A"
    assert audits[0]["provider_used"] == "provider-1"


def test_audit_contient_provider_path_et_provider_used_chemin_c(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = []

    async def _capture_audit(url, payload):
        audits.append(payload)
    monkeypatch.setattr(hub_core, "_post_audit", _capture_audit)

    fake_post, _ = _make_post_router({
        "/handoff": lambda payload: {
            "negotiation": {
                "decision": "cede_a_l_offre", "winning_provider": "provider-1",
                "winning_vm": None, "compliant_vms": [], "local_score": 0.0667,
                "offered_score": 0.20, "deadband_applied": 0.0, "reason": "notre offre gagne",
            },
            "local_topsis": None,
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)

    _prime(
        "edge1",
        [_candidate("edge1", 40.0), _candidate("cloud1", 32.0),
         _candidate("edge2", 50.0), _candidate("cloud2", 55.0)],
        _preds(edge1=40.0, cloud1=32.0, edge2=50.0, cloud2=55.0),
    )

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert len(audits) == 1
    assert audits[0]["provider_path"] == "C"
    assert audits[0]["provider_used"] == "provider-1"
