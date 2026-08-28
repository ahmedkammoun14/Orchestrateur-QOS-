"""
Régression UC4 : l'adoption d'un contrat par BROADCAST doit mettre à jour les
poids d'origine, pas seulement la liste des SLOs.

Symptôme observé en campagne : le provider STANDBY adopte le contrat de
l'initiateur (`state.current_slos`), mais `state.original_intent_weights`
restait celui de l'intention PRÉCÉDENTE. Le filtre de _step1_slos ne garde du
contrat que les métriques présentes dans original_intent_weights ; quand la
nouvelle intention porte sur d'autres métriques, il renvoie une liste VIDE.
Le provider repart alors en comportement autonome — seuil adaptatif par
percentile et promotion d'un secondaire MI en primaire — au lieu du contrat
demandé par le client.

Les deux autres chemins d'adoption (/intent et /award) mettaient déjà les
poids à jour ; seul le broadcast en était dépourvu.
"""

import asyncio

import pytest

from hub import orchestrator_core as hub_core
from shared import config


def _run(coro):
    return asyncio.run(coro)


def _slo(metric, operator, threshold, weight, primary=True):
    return {
        "metric": metric, "operator": operator, "threshold": threshold,
        "unit": "cores" if metric == "cpu_usage" else "ms",
        "weight": weight, "is_primary": primary,
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_ID", "provider-1")
    monkeypatch.setattr(hub_core.state, "is_active", False)      # STANDBY
    monkeypatch.setattr(hub_core.state, "last_collected",
                        [{"vm_id": "edge1", "latency": 20.0,
                          "cpu_usage": 30.0, "ram_usage": 40.0}])
    yield


def test_broadcast_met_a_jour_les_poids_dorigine(monkeypatch):
    """⭐ Le cas UC4 : intention 2 sur latency, puis intention 3 sur cpu/ram."""
    # État laissé par l'intention précédente
    monkeypatch.setattr(hub_core.state, "current_slos",
                        [_slo("latency", "<", 50.0, 1.0)])
    monkeypatch.setattr(hub_core.state, "original_intent_weights",
                        {"latency": 1.0})

    nouveau = [_slo("cpu_usage", ">=", 4.0, 0.6),
               _slo("ram_usage", ">=", 8.0, 0.4)]

    async def _fake_bid(client, slos, incumbent_vm, intent_id=None, prof=None):
        class _B:
            def to_dict(self):
                return {"ok": True}
        return _B()

    monkeypatch.setattr(hub_core, "_build_local_bid", _fake_bid)

    _run(hub_core.evaluate({"slos": nouveau}))

    assert hub_core.state.current_slos == nouveau
    # Le point de la régression :
    assert hub_core.state.original_intent_weights == {
        "cpu_usage": 0.6, "ram_usage": 0.4
    }
    # ...et donc le filtre de _step1_slos ne vide plus le contrat.
    garde = [s for s in hub_core.state.current_slos
             if s["metric"] in hub_core.state.original_intent_weights]
    assert len(garde) == 2


def test_broadcast_ignore_les_secondaires_dans_les_poids(monkeypatch):
    """Seuls les PRIMAIRES constituent les poids d'origine."""
    monkeypatch.setattr(hub_core.state, "current_slos", [])
    monkeypatch.setattr(hub_core.state, "original_intent_weights", {})

    recu = [_slo("latency", "<", 50.0, 0.8),
            _slo("cpu_usage", ">=", 1.0, 0.2, primary=False)]

    async def _fake_bid(client, slos, incumbent_vm, intent_id=None, prof=None):
        class _B:
            def to_dict(self):
                return {"ok": True}
        return _B()

    monkeypatch.setattr(hub_core, "_build_local_bid", _fake_bid)

    _run(hub_core.evaluate({"slos": recu}))

    assert hub_core.state.original_intent_weights == {"latency": 0.8}


def test_provider_actif_nadopte_pas(monkeypatch):
    """Le provider ACTIF est la SOURCE du contrat : il ne se le fait pas écraser."""
    monkeypatch.setattr(hub_core.state, "is_active", True)
    initial = [_slo("latency", "<", 28.0, 1.0)]
    monkeypatch.setattr(hub_core.state, "current_slos", list(initial))
    monkeypatch.setattr(hub_core.state, "original_intent_weights", {"latency": 1.0})

    async def _fake_bid(client, slos, incumbent_vm, intent_id=None, prof=None):
        class _B:
            def to_dict(self):
                return {"ok": True}
        return _B()

    monkeypatch.setattr(hub_core, "_build_local_bid", _fake_bid)

    _run(hub_core.evaluate({"slos": [_slo("cpu_usage", ">=", 4.0, 0.6)]}))

    assert hub_core.state.current_slos == initial
    assert hub_core.state.original_intent_weights == {"latency": 1.0}
