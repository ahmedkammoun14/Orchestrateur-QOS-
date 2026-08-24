"""
Chemin MONO-PROVIDER quand aucune VM ne satisfait les SLOs : STAY.

Historiquement, config.SLO_ENFORCEMENT n'etait consulte QUE par l'arbitre
federe : le chemin mono retombait toujours sur l'ensemble des candidats et
elisait « la moins mauvaise ». Les deux chemins n'avaient donc pas la meme
politique de dernier recours -- asymetrie reelle, et non documentee.

Le repli « best_effort » a ete RETIRE le 24/08/2026, avec la variable
config.MONO_INFEASIBLE_POLICY qui le pilotait. Migrer vers une VM qui viole
elle aussi le contrat deplace le service sans retablir le SLO, au prix d'une
migration reelle. Les deux chemins appliquent desormais la meme regle : aucune
VM conforme -> aucun candidat -> STAY + infaisabilite signalee.

⚠️ Les donnees de la campagne UC2 ont ete produites avec l'ancien repli.
Elles ne sont plus reproductibles avec ce code.
"""

import pytest

from shared import config
from services.decision_intelligence.decision import DecisionHandler


def _slo(metric="latency", operator="<", threshold=28.0, unit="ms"):
    return {"metric": metric, "operator": operator, "threshold": threshold,
            "unit": unit, "weight": 1.0, "is_primary": True}


def _cand(vm_id):
    return {"vm_id": vm_id, "latency": 200.0, "cpu_usage": 90.0,
            "ram_usage": 90.0, "total_cores": 2.0, "total_ram_gb": 2.0}


def _preds(vm_ids, value=200.0):
    """Tous les candidats violent largement le seuil de 28 ms."""
    return {
        vm: {"latency": {"predictions": [value] * 7, "uncertainty": 0.0}}
        for vm in vm_ids
    }


@pytest.fixture
def handler():
    return DecisionHandler()


def test_politique_best_effort_bien_retiree():
    """Le repli historique ne doit pas revenir par inadvertance."""
    assert not hasattr(config, "MONO_INFEASIBLE_POLICY")


def test_aucune_vm_conforme_ne_propose_aucun_candidat(handler):
    """⭐ Symetrie avec le chemin federe : aucune offre quand rien n'est conforme."""
    vms = ["edge1", "edge1b", "cloud1"]
    cands = [_cand(v) for v in vms]

    out = handler._filter_candidates(cands, _preds(vms), [_slo()])

    assert out == []


def test_le_cas_conforme_nest_pas_affecte(handler):
    """Une VM conforme reste proposee : seul le dernier recours a change."""
    vms = ["edge1", "edge1b"]
    cands = [_cand(v) for v in vms]
    preds = _preds(vms)
    preds["edge1b"]["latency"]["predictions"] = [10.0] * 7   # conforme

    out = handler._filter_candidates(cands, preds, [_slo()])

    assert [c["vm_id"] for c in out] == ["edge1b"]


def test_vm_sans_prediction_nest_pas_conforme(handler):
    """Sans prediction, une VM ne peut pas etre declaree conforme -> STAY."""
    vms = ["edge1", "edge1b"]
    cands = [_cand(v) for v in vms]

    out = handler._filter_candidates(cands, {}, [_slo()])

    assert out == []
