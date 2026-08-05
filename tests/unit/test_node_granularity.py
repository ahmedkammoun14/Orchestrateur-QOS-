"""
Tests du lot 1b : granularité kubectl (node, pas VM) dans _sync_active_vm,
et correctifs de LA GATE (_step5_check_violations) — hub/orchestrator_core.py.

Groupe A : _sync_active_vm ne doit adopter la VM canonique renvoyée par
kubectl que lorsque c'est RÉELLEMENT plus sûr que notre suivi local (VMs
d'un même node physique indiscernables pour kubectl, voir config.VM_NODE_GROUP).

Groupe B : _step5_check_violations (LA GATE du tour fédéré, lot 6a) ne doit
s'ouvrir que sur une métrique PRIMAIRE et respecter le sens de l'opérateur du
SLO — jamais sur un signal cpu/ram secondaire (règle métier verrouillée).

Groupe C : robustesse du clamp de ACTIVE_VM_SYNC_EVERY_N_CYCLES.

Même contrainte que test_active_standby_role.py : pas de pytest-asyncio
installé → chaque test async pilote sa propre boucle via `_run()`.
"""

import asyncio
import importlib

import pytest

from hub import orchestrator_core as hub_core
from shared import config
from shared.models import SLO


def _run(coro):
    async def _wrapped():
        result = await coro
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return result
    return asyncio.run(_wrapped())


# ── Helpers ───────────────────────────────────────────────────

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
    return hub_core._FlowContext(vm_ids=list(vm_ids), now_iso="2026-08-01T10:00:00")


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
    """Évite tout socket réel pendant ces tests."""
    async def _fake_post_audit(url, payload):
        return None
    monkeypatch.setattr(hub_core, "_post_audit", _fake_post_audit)
    yield


# ═══════════════════════════════════════════════════════════════
#  GROUPE A — granularité kubectl (_sync_active_vm)
# ═══════════════════════════════════════════════════════════════

def test_1_meme_node_service_vm_plus_fin_conserve(monkeypatch):
    """⭐ Le cas qui bloquait la démo : edge2b ne doit JAMAIS être écrasé par edge2."""
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-2")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    monkeypatch.setattr(hub_core.state, "service_vm", "edge2b")
    client = _FakeClient(response=_FakeResponse(200, {"active_vm": "edge2", "cluster": "x"}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.service_vm == "edge2b"
    assert hub_core.state.hosting_vm == "edge2"
    assert hub_core.state.is_active is True


def test_2_node_different_meme_provider_adopte_kubectl(monkeypatch):
    """⭐ Contre-exemple qui invalide une règle par provider : cloud1 → edge1 (provider-1 des deux côtés)."""
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    client = _FakeClient(response=_FakeResponse(200, {"active_vm": "edge1", "cluster": "x"}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.service_vm == "edge1"
    assert hub_core.state.hosting_vm == "edge1"
    assert hub_core.state.is_active is True


def test_3_transition_standby_vers_actif_adopte_kubectl(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-2")
    monkeypatch.setattr(hub_core.state, "is_active", False)
    monkeypatch.setattr(hub_core.state, "service_vm", "edge2c")
    client = _FakeClient(response=_FakeResponse(200, {"active_vm": "edge2", "cluster": "x"}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.is_active is True
    assert hub_core.state.service_vm == "edge2"   # adoption : on vient de devenir ACTIF


def test_4_standby_service_vm_inchange(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    # Hors fenêtre de grâce award (voir hub_core._sync_active_vm) : sans ce
    # reset explicite, un test antérieur ayant appelé /award (ex.
    # test_award_message.py) laisserait state.last_award_ts récent sur ce
    # singleton de module, et ce test entrerait à tort dans la fenêtre de
    # grâce — is_active resterait True au lieu du False attendu ici.
    monkeypatch.setattr(hub_core.state, "last_award_ts", None)
    client = _FakeClient(response=_FakeResponse(200, {"active_vm": "edge2", "cluster": "x"}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.is_active is False
    assert hub_core.state.service_vm == "cloud1"   # inchangé — on est STANDBY
    assert hub_core.state.hosting_vm == "edge2"


def test_5_provider_all_meme_node_conserve(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "all")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    monkeypatch.setattr(hub_core.state, "service_vm", "edge1b")
    client = _FakeClient(response=_FakeResponse(200, {"active_vm": "edge1", "cluster": "x"}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.is_active is True
    assert hub_core.state.service_vm == "edge1b"   # même node : conservé


def test_6_provider_all_node_different_adopte(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "all")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud2")
    client = _FakeClient(response=_FakeResponse(200, {"active_vm": "edge1", "cluster": "x"}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.service_vm == "edge1"


def test_7_vm_absente_de_node_group_adopte_sans_erreur(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "all")
    monkeypatch.setattr(config, "VM_NODE_GROUP", {})   # table vide : rien à comparer
    monkeypatch.setattr(hub_core.state, "is_active", True)
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    client = _FakeClient(response=_FakeResponse(200, {"active_vm": "edge1", "cluster": "x"}))

    _run(hub_core._sync_active_vm(client))   # ne doit pas lever

    assert hub_core.state.service_vm == "edge1"


# ── 8. Non-régression des branches d'erreur ───────────────────

def test_8a_exception_conserve_role_et_service_vm(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", True)
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "hosting_vm", "cloud1")
    client = _FakeClient(exc=RuntimeError("kubectl injoignable"))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.is_active is True
    assert hub_core.state.service_vm == "cloud1"
    assert hub_core.state.hosting_vm == "cloud1"


def test_8b_http_500_conserve_role_et_service_vm(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", False)
    monkeypatch.setattr(hub_core.state, "service_vm", "cloud1")
    monkeypatch.setattr(hub_core.state, "hosting_vm", "edge2")
    client = _FakeClient(response=_FakeResponse(500, {}))

    _run(hub_core._sync_active_vm(client))

    assert hub_core.state.is_active is False
    assert hub_core.state.service_vm == "cloud1"
    assert hub_core.state.hosting_vm == "edge2"


def test_8c_active_vm_none_conserve_role_et_service_vm(monkeypatch):
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
#  GROUPE B — LA GATE (_step5_check_violations)
# ═══════════════════════════════════════════════════════════════

def test_9_prediction_latence_primaire_depasse_seuil_true(monkeypatch):
    monkeypatch.setattr(hub_core.state, "service_vm", "edge1")
    monkeypatch.setattr(hub_core.state, "last_collected", [_candidate("edge1", 10.0)])
    monkeypatch.setattr(hub_core.state, "last_predictions",
                         {"edge1": {"latency": {"predictions": [50.0]}}})
    monkeypatch.setattr(hub_core.state, "current_slos",
                         [_slo_dict(metric="latency", operator="<", threshold=30.0, is_primary=True)])

    assert hub_core._step5_check_violations(_ctx()) is True


def test_10_cpu_secondaire_hors_seuil_ne_declenche_pas_mais_logue(monkeypatch):
    monkeypatch.setattr(hub_core.state, "service_vm", "edge1")
    monkeypatch.setattr(hub_core.state, "last_collected", [_candidate("edge1", 10.0)])
    monkeypatch.setattr(hub_core.state, "last_predictions", {
        "edge1": {
            "latency":   {"predictions": [10.0]},    # conforme
            "cpu_usage": {"predictions": [90.0]},     # hors seuil, mais SECONDAIRE
        },
    })
    monkeypatch.setattr(hub_core.state, "current_slos", [
        _slo_dict(metric="latency",   operator="<", threshold=30.0, is_primary=True),
        _slo_dict(metric="cpu_usage", operator="<", threshold=80.0, is_primary=False),
    ])

    warnings = []
    monkeypatch.setattr(hub_core.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg)))

    result = hub_core._step5_check_violations(_ctx())

    assert result is False
    assert any("cpu_usage" in w for w in warnings)   # l'info n'est pas perdue du log


def test_11_cas_reel_cpu_cores_vs_pourcent_false(monkeypatch):
    """
    Le cas observé en production : SLO secondaire cpu_usage seuil 1.0 CŒURS,
    prédictions ≈65 (pourcents). AVANT ce lot : True à chaque cycle (65 > 1.0).
    APRÈS : False (métrique secondaire, exclue de la gate).
    """
    monkeypatch.setattr(hub_core.state, "service_vm", "edge1")
    monkeypatch.setattr(hub_core.state, "last_collected", [_candidate("edge1", 10.0)])
    monkeypatch.setattr(hub_core.state, "last_predictions", {
        "edge1": {
            "latency":   {"predictions": [10.0]},
            "cpu_usage": {"predictions": [65.0, 66.0, 64.0]},
        },
    })
    monkeypatch.setattr(hub_core.state, "current_slos", [
        _slo_dict(metric="latency",   operator="<", threshold=30.0, is_primary=True),
        _slo_dict(metric="cpu_usage", operator="<", threshold=1.0, unit="cores", is_primary=False),
    ])

    assert hub_core._step5_check_violations(_ctx()) is False


def test_12_violation_reactive_true_inchange(monkeypatch):
    monkeypatch.setattr(hub_core.state, "service_vm", "edge1")
    monkeypatch.setattr(hub_core.state, "last_collected", [_candidate("edge1", 999.0)])
    monkeypatch.setattr(hub_core.state, "last_predictions", {})
    monkeypatch.setattr(hub_core.state, "current_slos",
                         [_slo_dict(metric="latency", operator="<", threshold=30.0, is_primary=True)])

    assert hub_core._step5_check_violations(_ctx()) is True


def test_13_operateur_gte_primaire_respecte_le_sens(monkeypatch):
    """
    Isole le signal PROACTIF : la MESURE (cpu_usage = 5.0) respecte le SLO
    « cpu_usage >= 2.0 », donc le chemin RÉACTIF (_is_violation) reste muet et
    seule la PRÉDICTION décide du résultat.

    La mesure valait 1.0 avant le correctif de _is_violation : elle violait
    déjà le SLO, mais passait inaperçue car l'ancien code appliquait
    l'opérateur "<" du METRICS_REGISTRY au lieu du ">=" du SLO. Maintenant que
    l'opérateur du SLO fait foi, cette mesure serait — correctement — détectée
    comme une violation et masquerait le signal proactif sous test.
    """
    monkeypatch.setattr(hub_core.state, "service_vm", "edge1")
    candidate = {
        "vm_id": "edge1", "latency": 10.0, "cpu_usage": 5.0, "ram_usage": 40.0,
        "total_cores": 4, "total_ram_gb": 8, "reliability": 1.0,
    }
    monkeypatch.setattr(hub_core.state, "last_collected", [candidate])
    monkeypatch.setattr(hub_core.state, "current_slos",
                         [_slo_dict(metric="cpu_usage", operator=">=", threshold=2.0, is_primary=True)])

    # Prédiction SOUS le seuil (bénéfice insuffisant) → violation → True
    monkeypatch.setattr(hub_core.state, "last_predictions",
                         {"edge1": {"cpu_usage": {"predictions": [1.0]}}})
    assert hub_core._step5_check_violations(_ctx()) is True

    # Prédiction AU-DESSUS du seuil (bénéfice suffisant) → conforme → False
    monkeypatch.setattr(hub_core.state, "last_predictions",
                         {"edge1": {"cpu_usage": {"predictions": [5.0]}}})
    assert hub_core._step5_check_violations(_ctx()) is False


def test_14_aucun_slo_aucune_prediction_false_sans_exception(monkeypatch):
    monkeypatch.setattr(hub_core.state, "service_vm", "edge1")
    monkeypatch.setattr(hub_core.state, "last_collected", [_candidate("edge1", 10.0)])
    monkeypatch.setattr(hub_core.state, "last_predictions", {})
    monkeypatch.setattr(hub_core.state, "current_slos", [])

    assert hub_core._step5_check_violations(_ctx()) is False


# ═══════════════════════════════════════════════════════════════
#  GROUPE C — robustesse
# ═══════════════════════════════════════════════════════════════

def test_15_active_vm_sync_every_n_cycles_clampe_a_1_depuis_env(monkeypatch):
    """
    ACTIVE_VM_SYNC_EVERY_N_CYCLES=0 depuis l'environnement doit être clampé à
    1 par shared/config.py — sinon le modulo de _run_flow
    (state.cycle_count % ACTIVE_VM_SYNC_EVERY_N_CYCLES) lèverait
    ZeroDivisionError à chaque cycle.
    """
    monkeypatch.setenv("ACTIVE_VM_SYNC_EVERY_N_CYCLES", "0")
    importlib.reload(config)
    try:
        assert config.ACTIVE_VM_SYNC_EVERY_N_CYCLES == 1
        # Le modulo réellement utilisé par _run_flow ne doit jamais lever.
        for cycle in range(5):
            cycle % config.ACTIVE_VM_SYNC_EVERY_N_CYCLES
    finally:
        monkeypatch.delenv("ACTIVE_VM_SYNC_EVERY_N_CYCLES", raising=False)
        importlib.reload(config)
        assert config.ACTIVE_VM_SYNC_EVERY_N_CYCLES == 10   # bien restauré (défaut)
