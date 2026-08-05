"""
Tests du rôle ACTIF/STANDBY (hub/orchestrator_core.py) déterminé par kubectl.

Couvre :
  • _sync_active_vm       — capture de la vérité globale (hosting_vm) et
                             dérivation du rôle (is_active) selon PROVIDER_ID.
  • _step8_decide          — garde-fou STANDBY (aucune décision, aucun appel
                             réseau) et non-régression du mode mono-processus
                             (PROVIDER_ID == "all").
  • _run_flow              — déclenchement paresseux de _sync_active_vm
                             (violation détectée OU battement de cœur).
  • _step5_check_violations — valeur de retour booléenne, corps inchangé.

Même contrainte que test_multi_provider_flow.py : pas de pytest-asyncio
installé (aucune nouvelle dépendance autorisée) → chaque test pilote sa
propre boucle via `_run()`.
"""

import asyncio

import pytest

from hub import orchestrator_core as hub_core
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

def _slo_dict(metric="latency", operator="<", threshold=30.0, unit="ms", is_primary=True) -> dict:
    # target=threshold : rend _threshold_map() prévisible (sinon target=0.0
    # par défaut écraserait le seuil primaire du registry, cf. son corps).
    return SLO(metric=metric, operator=operator, threshold=threshold, unit=unit,
               target=threshold, is_primary=is_primary).dict()


def _candidate(vm_id: str, latency: float, cores=4, ram=8) -> dict:
    return {
        "vm_id": vm_id, "latency": latency,
        "cpu_usage": 30.0, "ram_usage": 40.0,
        "total_cores": cores, "total_ram_gb": ram,
        "reliability": 1.0,
    }


def _ctx(vm_ids=("edge1", "cloud1", "edge2", "cloud2")) -> "hub_core._FlowContext":
    return hub_core._FlowContext(vm_ids=list(vm_ids), now_iso="2026-07-30T10:00:00")


def _prof() -> "hub_core.StepProfiler":
    return hub_core.StepProfiler()


def _provider1_vm_registry() -> dict:
    return {
        vm_id: config.ALL_VM_REGISTRY[vm_id]
        for vm_id in config.PROVIDER_REGISTRY["provider-1"]["vms"]
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
        self.calls = 0

    async def get(self, url, timeout=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._response


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    """Évite tout socket réel vers observability/openstack_client pendant ces tests."""
    async def _fake_post_audit(url, payload):
        return None

    async def _fake_kubectl(client, from_vm, to_vm):
        return True

    monkeypatch.setattr(hub_core, "_post_audit", _fake_post_audit)
    monkeypatch.setattr(hub_core, "_execute_kubectl_migration", _fake_kubectl)
    yield


# ═══════════════════════════════════════════════════════════════
#  _sync_active_vm — vérité globale (hosting_vm) + dérivation du rôle
# ═══════════════════════════════════════════════════════════════

def test_provider_all_force_toujours_actif(monkeypatch):
    """PROVIDER_ID == 'all' : is_active repasse toujours à True (mono-processus)."""
    monkeypatch.setattr(config, "PROVIDER_ID", "all")
    monkeypatch.setattr(hub_core.state, "is_active", False)   # valeur volontairement incohérente
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    client = _FakeClient(response=_FakeResponse(200, {"active_vm": "edge1", "cluster": "edge-cluster"}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.is_active is True
    assert hub_core.state.service_vm == "edge1"
    assert hub_core.state.hosting_vm == "edge1"


def test_provider1_kubectl_renvoie_vm_du_meme_provider(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(config, "VM_REGISTRY", _provider1_vm_registry())
    monkeypatch.setattr(hub_core.state, "is_active", False)
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    client = _FakeClient(response=_FakeResponse(200, {"active_vm": "edge1", "cluster": "edge-cluster"}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.is_active is True
    assert hub_core.state.service_vm == "edge1"
    assert hub_core.state.hosting_vm == "edge1"


def test_provider1_kubectl_renvoie_vm_d_un_autre_provider(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(config, "VM_REGISTRY", _provider1_vm_registry())
    monkeypatch.setattr(hub_core.state, "is_active", True)
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")   # VM de provider-1
    client = _FakeClient(response=_FakeResponse(200, {"active_vm": "edge2", "cluster": "edge-cluster"}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.is_active is False
    assert hub_core.state.hosting_vm == "edge2"
    assert hub_core.state.service_vm == "cloud1"   # inchangé (règle de sûreté)


def test_kubectl_exception_conserve_role_et_service_vm(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "hosting_vm", "cloud1")
    client = _FakeClient(exc=RuntimeError("kubectl injoignable"))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.is_active is True
    assert hub_core.state.service_vm == "cloud1"
    assert hub_core.state.hosting_vm == "cloud1"


def test_kubectl_http_non_200_conserve_role_et_service_vm(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", False)
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "hosting_vm", "edge2")
    client = _FakeClient(response=_FakeResponse(500, {}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.is_active is False
    assert hub_core.state.service_vm == "cloud1"
    assert hub_core.state.hosting_vm == "edge2"


def test_kubectl_aucun_pod_actif_conserve_role_et_service_vm(monkeypatch):
    """active_vm absent/inconnu du parc complet (ALL_VM_REGISTRY)."""
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "hosting_vm", "cloud1")
    client = _FakeClient(response=_FakeResponse(200, {"active_vm": None}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.is_active is True
    assert hub_core.state.service_vm == "cloud1"
    assert hub_core.state.hosting_vm == "cloud1"


# ═══════════════════════════════════════════════════════════════
#  Garde-fou ACTIF/STANDBY dans _step8_decide
# ═══════════════════════════════════════════════════════════════

def test_standby_step8_decide_court_circuite_sans_appel_reseau(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", False)
    monkeypatch.setattr(hub_core.state, "hosting_vm", "edge2")
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "last_collected", [_candidate("cloud1", 20.0)])
    monkeypatch.setattr(hub_core.state, "last_decision", {})
    monkeypatch.setattr(hub_core.state, "cycle_count", 12)

    di_calls = []
    async def _fake_post(client, url, payload):
        di_calls.append(url)
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_post)

    kubectl_calls = []
    async def _fake_kubectl(client, from_vm, to_vm):
        kubectl_calls.append((from_vm, to_vm))
        return True
    monkeypatch.setattr(hub_core, "_execute_kubectl_migration", _fake_kubectl)

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert di_calls == []        # aucun appel HTTP à decision_intelligence
    assert kubectl_calls == []   # aucune migration kubectl
    assert hub_core.state.last_decision["decision"] == "stay"
    assert "STANDBY" in hub_core.state.last_decision["reason"]


def test_provider_all_step8_decide_jamais_court_circuite(monkeypatch):
    """
    Non-régression mono-processus : PROVIDER_ID == 'all' ignore is_active
    (garde-fou neutre) — la décision est toujours évaluée normalement.
    """
    monkeypatch.setattr(config, "PROVIDER_ID", "all")
    monkeypatch.setattr(config, "MULTI_PROVIDER_ENABLED", False)
    monkeypatch.setattr(hub_core.state, "is_active", False)   # valeur volontairement incohérente
    monkeypatch.setattr(hub_core.state, "service_vm", "edge1")
    monkeypatch.setattr(hub_core.state, "last_collected", [_candidate("edge1", 20.0)])
    monkeypatch.setattr(hub_core.state, "last_predictions", {})
    monkeypatch.setattr(hub_core.state, "current_slos", [_slo_dict()])
    monkeypatch.setattr(hub_core.state, "last_migration_ts", None)
    monkeypatch.setattr(hub_core.state, "cycle_count", 12)

    calls = []
    async def _fake_post(client, url, payload):
        calls.append(url)
        return None
    monkeypatch.setattr(hub_core, "_post", _fake_post)

    _run(hub_core._step8_decide(client=None, ctx=_ctx(), prof=_prof()))

    assert any(u.endswith("/decide") for u in calls)   # la décision a bien été évaluée


# ═══════════════════════════════════════════════════════════════
#  Déclenchement paresseux de _sync_active_vm dans _run_flow
# ═══════════════════════════════════════════════════════════════

def _stub_run_flow_steps(monkeypatch, violation_detected: bool) -> list:
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(hub_core, "_step1_slos", _noop)
    monkeypatch.setattr(hub_core, "_step2_persist_slos", _noop)
    monkeypatch.setattr(hub_core, "_step3_collect", _noop)
    monkeypatch.setattr(hub_core, "_step4_persist_metrics", _noop)
    monkeypatch.setattr(hub_core, "_step6_load_histories", _noop)
    monkeypatch.setattr(hub_core, "_step7_predict", _noop)
    monkeypatch.setattr(hub_core, "_step8_decide", _noop)
    monkeypatch.setattr(hub_core, "_persist_timing", lambda *a, **k: None)

    def _fake_check_violations(ctx):
        return violation_detected
    monkeypatch.setattr(hub_core, "_step5_check_violations", _fake_check_violations)

    sync_calls = []
    async def _fake_sync(client):
        sync_calls.append(client)
    monkeypatch.setattr(hub_core, "_sync_active_vm", _fake_sync)
    return sync_calls


def test_sync_declenchee_si_violation_detectee(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(config, "ACTIVE_VM_SYNC_EVERY_N_CYCLES", 10)
    monkeypatch.setattr(hub_core.state, "cycle_count", 3)   # pas un multiple de 10
    sync_calls = _stub_run_flow_steps(monkeypatch, violation_detected=True)

    _run(hub_core._run_flow([], "autonomous"))

    assert len(sync_calls) == 1


def test_sync_declenchee_sur_battement_de_coeur(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(config, "ACTIVE_VM_SYNC_EVERY_N_CYCLES", 10)
    monkeypatch.setattr(hub_core.state, "cycle_count", 20)   # multiple de 10
    sync_calls = _stub_run_flow_steps(monkeypatch, violation_detected=False)

    _run(hub_core._run_flow([], "autonomous"))

    assert len(sync_calls) == 1


def test_sync_non_declenchee_sans_violation_ni_battement(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(config, "ACTIVE_VM_SYNC_EVERY_N_CYCLES", 10)
    monkeypatch.setattr(hub_core.state, "cycle_count", 7)   # ni violation ni multiple de 10
    sync_calls = _stub_run_flow_steps(monkeypatch, violation_detected=False)

    _run(hub_core._run_flow([], "autonomous"))

    assert sync_calls == []


def test_sync_jamais_declenchee_en_mono_processus(monkeypatch):
    """PROVIDER_ID == 'all' : le battement de cœur/violation est neutre (rôle non pertinent)."""
    monkeypatch.setattr(config, "PROVIDER_ID", "all")
    monkeypatch.setattr(hub_core.state, "cycle_count", 20)   # aurait déclenché le battement
    sync_calls = _stub_run_flow_steps(monkeypatch, violation_detected=True)   # aurait déclenché la violation

    _run(hub_core._run_flow([], "autonomous"))

    assert sync_calls == []


# ═══════════════════════════════════════════════════════════════
#  _step5_check_violations — valeur de retour (corps inchangé)
# ═══════════════════════════════════════════════════════════════

def test_step5_retourne_true_sur_violation_reactive(monkeypatch):
    monkeypatch.setattr(hub_core.state, "service_vm", "edge1")
    monkeypatch.setattr(hub_core.state, "last_collected", [_candidate("edge1", 999.0)])
    monkeypatch.setattr(hub_core.state, "last_predictions", {})
    monkeypatch.setattr(hub_core.state, "current_slos", [_slo_dict(metric="latency", threshold=30.0)])

    warnings = []
    monkeypatch.setattr(hub_core.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg)))

    result = hub_core._step5_check_violations(_ctx())

    assert result is True
    assert any("Violation SLO réactive" in w for w in warnings)


def test_step5_retourne_false_sans_violation_ni_signal_proactif(monkeypatch):
    monkeypatch.setattr(hub_core.state, "service_vm", "edge1")
    monkeypatch.setattr(hub_core.state, "last_collected", [_candidate("edge1", 10.0)])
    monkeypatch.setattr(hub_core.state, "last_predictions",
                         {"edge1": {"latency": {"predictions": [10.0, 10.0]}}})
    monkeypatch.setattr(hub_core.state, "current_slos", [_slo_dict(metric="latency", threshold=30.0)])

    infos = []
    monkeypatch.setattr(hub_core.logger, "info", lambda msg, *a, **k: infos.append(str(msg)))

    result = hub_core._step5_check_violations(_ctx())

    assert result is False
    assert any("SLOs respectés" in i for i in infos)


def test_step5_retourne_true_sur_signal_proactif_sans_violation_reactive(monkeypatch):
    monkeypatch.setattr(hub_core.state, "service_vm", "edge1")
    monkeypatch.setattr(hub_core.state, "last_collected", [_candidate("edge1", 10.0)])   # pas de violation réactive
    monkeypatch.setattr(hub_core.state, "last_predictions",
                         {"edge1": {"latency": {"predictions": [50.0]}}})   # dépasse le seuil
    monkeypatch.setattr(hub_core.state, "current_slos", [_slo_dict(metric="latency", threshold=30.0)])

    warnings = []
    monkeypatch.setattr(hub_core.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg)))

    result = hub_core._step5_check_violations(_ctx())

    assert result is True
    assert any("Signal proactif ML" in w for w in warnings)
