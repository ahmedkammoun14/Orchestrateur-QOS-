"""
Tests du bloc d'audit "reasoning" (hub/orchestrator_core.py, chemin
multi-provider) : la chaîne de raisonnement (évaluations par VM, VMs
conformes retenues, négociation éventuelle) qui accompagne désormais chaque
décision multi-provider dans le payload posté à observability.

Purement informatif : aucun test ici ne vérifie de changement de décision —
seulement que les données supplémentaires sont correctement assemblées,
robustes aux pannes, et absentes du mode mono-provider.

Aucun test n'ouvre de socket : `_post`, `_post_audit` et
`_execute_kubectl_migration` sont mockés. Ces fonctions sont async ; en
l'absence de pytest-asyncio (non installé, aucune nouvelle dépendance
autorisée), chaque test pilote sa propre boucle via `_run()`.
"""

import asyncio
import copy
import json

import pytest

from hub import orchestrator_core as hub_core
from hub.provider_arbitration import evaluate_provider
from shared import config
from shared.models import SLO


# ── Exécution des coroutines sans pytest-asyncio ──────────────

def _run(coro):
    """asyncio.run() + un `sleep(0)` pour laisser tourner l'audit fire-and-forget."""
    async def _wrapped():
        result = await coro
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return result
    return asyncio.run(_wrapped())


# ── Helpers de construction (repris de test_multi_provider_flow.py) ──

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
    hub_core.state.last_migration_ts    = None


def _make_post_router(responses: dict):
    """Faux `_post` routé par suffixe d'URL (ex. "/decide", "/handoff")."""
    calls = []

    async def fake_post(client, url, payload):
        calls.append({"url": url, "payload": payload})
        for suffix, handler in responses.items():
            if url.endswith(suffix):
                return handler(payload)
        return None

    return fake_post, calls


@pytest.fixture(autouse=True)
def _reset_state_and_stub_side_effects(monkeypatch):
    """
    Sauvegarde/restaure `state` (singleton module-level). Stub par défaut de
    `_post_audit`/`_execute_kubectl_migration` — chaque test qui inspecte
    l'audit remplace `_post_audit` par sa propre doublure capturante.
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


def _capture_audits(monkeypatch) -> list:
    audits = []

    async def _capture(url, payload):
        audits.append(payload)
    monkeypatch.setattr(hub_core, "_post_audit", _capture)
    return audits


# ═══════════════════════════════════════════════════════════════
#  1 — Chemin A
# ═══════════════════════════════════════════════════════════════

def test_chemin_a_reasoning_present_compliant_vms_et_negotiation_none(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    fake_post, _ = _make_post_router({
        "/decide": lambda payload: {
            "decision": "stay", "to_vm": None, "topsis_score": 0.9, "reason": "conforme",
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
    reasoning = audits[0]["reasoning"]
    assert reasoning is not None
    assert reasoning["provider_courant"] == "provider-1"
    vm_ids = {e["vm_id"] for e in reasoning["evaluations"]}
    assert vm_ids == {"edge1", "cloud1"}       # les 2 VMs DU PROVIDER COURANT uniquement
    assert reasoning["compliant_vms"]          # non vide
    assert reasoning["negotiation"] is None


def test_vm_active_renseignee_meme_sur_un_maintien(monkeypatch):
    """
    Sur un MAINTIEN, le hub laisse from_vm/to_vm à None (ils ne sont renseignés
    que sur une migration). Le dashboard n'avait alors aucun moyen d'identifier
    la VM active : il affichait « Aucune violation détectée » à l'étape 2 —
    juste au-dessus d'une VM montrée non conforme à l'étape 3 — et
    « MAINTIEN sur — » à l'étape 5. reasoning["vm_active"] comble ce trou.
    """
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    fake_post, _ = _make_post_router({
        "/decide": lambda payload: {
            "decision": "stay", "to_vm": None, "topsis_score": 0.9, "reason": "conforme",
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

    audit = audits[0]
    assert audit["decision"] == "stay"
    # from_vm/to_vm n'identifient pas la VM sur un maintien : selon le chemin
    # elles sont nulles ou carrément absentes du payload. C'est précisément ce
    # qui privait le dashboard de l'information.
    assert audit.get("from_vm") is None
    assert audit.get("to_vm") is None

    reasoning = audit["reasoning"]
    assert reasoning["vm_active"] == "edge1"
    # ... et elle doit être présente dans les évaluations, sinon l'étape 2 du
    # dashboard ne retrouve toujours pas son détail par métrique.
    assert reasoning["vm_active"] in {e["vm_id"] for e in reasoning["evaluations"]}


# ═══════════════════════════════════════════════════════════════
#  2 — evaluations fidèles à evaluate_provider
# ═══════════════════════════════════════════════════════════════

def test_evaluations_fideles_a_evaluate_provider(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    fake_post, _ = _make_post_router({
        "/decide": lambda payload: {
            "decision": "stay", "to_vm": None, "topsis_score": None, "reason": "x",
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)

    candidates = [
        _candidate("edge1", 20.0), _candidate("cloud1", 40.0),
        _candidate("edge2", 50.0), _candidate("cloud2", 55.0),
    ]
    preds = _preds(edge1=20.0, cloud1=40.0, edge2=50.0, cloud2=55.0)
    _prime("edge1", candidates, preds)

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    # Référence : même appel que celui fait en interne par _decide_multi_provider
    # (current_data passe par _build_candidates, PAS les candidats bruts —
    # convention payload_key : latency → rtt_ms).
    current_data = hub_core._build_candidates(candidates)
    expected = evaluate_provider("provider-1", hub_core.state.current_slos, current_data, preds)

    reasoning = audits[0]["reasoning"]
    by_vm = {e["vm_id"]: e for e in reasoning["evaluations"]}
    for ev in expected.evaluations:
        assert by_vm[ev.vm_id]["violation_score"] == pytest.approx(ev.violation_score)
        assert by_vm[ev.vm_id]["is_compliant"] == ev.is_compliant


# ═══════════════════════════════════════════════════════════════
#  3 — Chemin B
# ═══════════════════════════════════════════════════════════════

def test_chemin_b_negotiation_renseignee_avec_provider_cible_et_decision(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    fake_post, _ = _make_post_router({
        "/handoff": lambda payload: {
            "negotiation": {
                "decision": "prend_local_conforme", "winning_provider": "provider-2",
                "winning_vm": None, "compliant_vms": ["edge2", "cloud2"],
                "local_score": 0.0, "offered_score": None, "deadband_applied": 0.05,
                "reason": "provider-2 prend le service",
            },
            "local_offer": None,
            "local_topsis": {"to_vm": "edge2", "topsis_score": 0.9, "reason": "meilleur TOPSIS"},
        },
    })
    monkeypatch.setattr(hub_core, "_post", fake_post)

    _prime(
        "edge1",
        [_candidate("edge1", 40.0), _candidate("cloud1", 45.0),
         _candidate("edge2", 20.0), _candidate("cloud2", 25.0)],
        _preds(edge1=40.0, cloud1=45.0, edge2=20.0, cloud2=25.0),
    )

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    reasoning   = audits[0]["reasoning"]
    negotiation = reasoning["negotiation"]
    assert negotiation is not None
    assert negotiation["provider_cible"] == "provider-2"
    assert negotiation["decision"] == "prend_local_conforme"


# ═══════════════════════════════════════════════════════════════
#  4 — Chemin C
# ═══════════════════════════════════════════════════════════════

def test_chemin_c_offre_locale_et_recue_et_deadband(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    fake_post, _ = _make_post_router({
        "/handoff": lambda payload: {
            "negotiation": {
                "decision": "cede_a_l_offre", "winning_provider": "provider-1",
                "winning_vm": None, "compliant_vms": [], "local_score": 0.0667,
                "offered_score": 0.20, "deadband_applied": 0.05,
                "reason": "notre offre gagne",
            },
            "local_offer":  {"provider_id": "provider-2", "vm_id": "edge2", "violation_score": 0.20},
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

    reasoning   = audits[0]["reasoning"]
    negotiation = reasoning["negotiation"]
    assert negotiation["offre_locale"] is not None
    assert negotiation["offre_recue"] is not None
    assert negotiation["deadband"] == pytest.approx(0.05)


# ═══════════════════════════════════════════════════════════════
#  5 — Chemin D / passation échouée
# ═══════════════════════════════════════════════════════════════

def test_chemin_d_passation_echouee_reasoning_present_negotiation_degradee(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    async def _fake_post_handoff_refuse(client, url, payload):
        return None   # relais indisponible ou refus (409/502) — _post renvoie None
    monkeypatch.setattr(hub_core, "_post", _fake_post_handoff_refuse)

    _prime(
        "edge1",
        [_candidate("edge1", 40.0), _candidate("cloud1", 45.0),
         _candidate("edge2", 50.0), _candidate("cloud2", 55.0)],
        _preds(edge1=40.0, cloud1=45.0, edge2=50.0, cloud2=55.0),
    )

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))   # ne doit pas lever

    assert len(audits) == 1
    reasoning = audits[0]["reasoning"]
    assert reasoning is not None
    assert reasoning["negotiation"] is None   # aucune donnée de passation disponible
    assert audits[0]["decision"] == "stay"


# ═══════════════════════════════════════════════════════════════
#  6 — Relais qui lève une exception
# ═══════════════════════════════════════════════════════════════

def test_relais_leve_exception_audit_quand_meme_poste(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    async def _fake_post_raises(client, url, payload):
        raise RuntimeError("le relais explose de façon inattendue")
    monkeypatch.setattr(hub_core, "_post", _fake_post_raises)

    _prime(
        "edge1",
        [_candidate("edge1", 40.0), _candidate("cloud1", 45.0),
         _candidate("edge2", 50.0), _candidate("cloud2", 55.0)],
        _preds(edge1=40.0, cloud1=45.0, edge2=50.0, cloud2=55.0),
    )

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))   # ne doit pas lever

    assert len(audits) == 1
    reasoning = audits[0]["reasoning"]
    assert reasoning is not None
    assert reasoning["negotiation"] is None
    assert audits[0]["decision"] == "stay"


# ═══════════════════════════════════════════════════════════════
#  7 — Non-régression mono-provider
# ═══════════════════════════════════════════════════════════════

def test_mono_provider_sans_reasoning_ni_provider_path(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", False)
    audits = _capture_audits(monkeypatch)

    async def _fake_post(client, url, payload):
        return {
            "decision": "stay", "from_vm": None, "to_vm": None,
            "reason": "No SLO violation detected",
            "topsis_score": None, "breach_type": None,
        }
    monkeypatch.setattr(hub_core, "_post", _fake_post)

    _prime("edge1", [_candidate("edge1", 20.0)], _preds(edge1=20.0))

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert len(audits) == 1
    assert "reasoning" not in audits[0]
    assert "provider_path" not in audits[0]
    assert "provider_used" not in audits[0]


# ═══════════════════════════════════════════════════════════════
#  8 — Sérialisabilité JSON
# ═══════════════════════════════════════════════════════════════

def test_serialisabilite_json_chemin_a(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    fake_post, _ = _make_post_router({
        "/decide": lambda payload: {
            "decision": "stay", "to_vm": None, "topsis_score": 0.9, "reason": "conforme",
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

    json.dumps(audits[0])   # ne doit pas lever


def test_serialisabilite_json_chemin_c(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    fake_post, _ = _make_post_router({
        "/handoff": lambda payload: {
            "negotiation": {
                "decision": "cede_a_l_offre", "winning_provider": "provider-1",
                "winning_vm": None, "compliant_vms": [], "local_score": 0.0667,
                "offered_score": 0.20, "deadband_applied": 0.05,
                "reason": "notre offre gagne",
            },
            "local_offer":  {"provider_id": "provider-2", "vm_id": "edge2", "violation_score": 0.20},
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

    payload_json = json.dumps(audits[0], indent=2, ensure_ascii=False)   # ne doit pas lever

    # ── Preuve de lisibilité (jointe au livrable) ──────────────────────
    print("\n" + "=" * 70)
    print("Exemple de payload d'audit — chemin C (négociation)")
    print("=" * 70)
    print(payload_json)


# ═══════════════════════════════════════════════════════════════
#  9 (Volet 2) — reasoning.topsis reflète vm_scores
# ═══════════════════════════════════════════════════════════════

def test_reasoning_topsis_reflete_vm_scores_chemin_a(monkeypatch):
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    fake_post, _ = _make_post_router({
        "/decide": lambda payload: {
            "decision": "migrate", "from_vm": "edge1", "to_vm": "cloud1",
            "topsis_score": 0.87, "reason": "TOPSIS selected 'cloud1'",
            "vm_scores": {"edge1": 0.12, "cloud1": 0.87},
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

    topsis = audits[0]["reasoning"]["topsis"]
    assert topsis["classement"] == {"edge1": 0.12, "cloud1": 0.87}
    assert topsis["retenue"] == "cloud1"
    assert topsis["score"] == pytest.approx(0.87)


def test_reasoning_topsis_vaut_classement_vide_quand_vm_scores_absent(monkeypatch):
    """
    /decide répond sans "vm_scores" (ancien format, ou repli côté
    decision_intelligence) : reasoning.topsis reste un dict valide avec un
    classement vide — jamais une exception, jamais une clé manquante.
    """
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    fake_post, _ = _make_post_router({
        "/decide": lambda payload: {
            "decision": "stay", "to_vm": None, "topsis_score": None, "reason": "conforme",
            # pas de "vm_scores"
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

    topsis = audits[0]["reasoning"]["topsis"]
    assert topsis == {"classement": {}, "retenue": None, "score": None}


def test_reasoning_topsis_vide_sur_chemin_c(monkeypatch):
    """Chemins C/D : TOPSIS ne tourne jamais — topsis reste au repli neutre."""
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", True)
    audits = _capture_audits(monkeypatch)

    fake_post, _ = _make_post_router({
        "/handoff": lambda payload: {
            "negotiation": {
                "decision": "cede_a_l_offre", "winning_provider": "provider-1",
                "winning_vm": None, "compliant_vms": [], "local_score": 0.0667,
                "offered_score": 0.20, "deadband_applied": 0.05,
                "reason": "notre offre gagne",
            },
            "local_offer":  {"provider_id": "provider-2", "vm_id": "edge2", "violation_score": 0.20},
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

    assert audits[0]["reasoning"]["topsis"] == {"classement": {}, "retenue": None, "score": None}
