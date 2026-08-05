"""
Tests du microservice placement_arbiter (services/placement_arbiter/).

Deux familles de tests :
  • La fonction PURE arbitrate() (arbiter.py) — l'essentiel de la couverture,
    aucun réseau nécessaire.
  • L'endpoint POST /arbitrate et GET /health (app.py) — quelques tests via
    TestClient pour vérifier l'enveloppe HTTP (gardes, codes).
"""

import copy

import pytest
from fastapi.testclient import TestClient

from services.placement_arbiter.arbiter import arbitrate
from services.placement_arbiter import app as arbiter_app
from shared import config

client = TestClient(arbiter_app.app)


# ── Helpers ───────────────────────────────────────────────────

def _bid(provider_id, vm_id, gap_value, is_compliant=True, evaluable=True,
         topsis_score=None, vm_scores=None) -> dict:
    return {
        "provider_id": provider_id,
        "intent_id":   "t1",
        "placement_plan": {
            "provider_id":  provider_id,
            "vm_id":        vm_id,
            "action":       "x",
            "topsis_score": topsis_score,
            "vm_scores":    vm_scores or {},
            "reason":       "x",
        },
        "gap_grade": {
            "value":        gap_value,
            "is_compliant": is_compliant,
            "evaluable":    evaluable,
            "coverage":     ["latency"],
            "detail":       {"latency": gap_value},
        },
        "timestamp": "2026-07-31T10:00:00+00:00",
    }


# ═══════════════════════════════════════════════════════════════
#  1-2. Écart > / < dead-band
# ═══════════════════════════════════════════════════════════════

def test_ecart_superieur_au_deadband_migrate_path_b():
    bids = [_bid("provider-1", "edge1", -0.03), _bid("provider-2", "edge2", -0.09)]
    v = arbitrate(bids, incumbent_provider="provider-1", incumbent_vm="edge1")

    assert v.path == "B"
    assert v.decision == "migrate"
    assert v.winner_provider == "provider-2"
    assert v.winner_vm == "edge2"


def test_ecart_inferieur_au_deadband_le_tenant_conserve_path_a():
    bids = [_bid("provider-1", "edge1", -0.03), _bid("provider-2", "edge2", -0.06)]
    v = arbitrate(bids, incumbent_provider="provider-1", incumbent_vm="edge1", deadband=0.05)

    assert v.path == "A"
    assert v.decision == "stay"
    assert v.winner_provider == "provider-1"
    assert v.winner_vm == "edge1"


# ═══════════════════════════════════════════════════════════════
#  3. Tenant non conforme + challenger conforme
# ═══════════════════════════════════════════════════════════════

def test_tenant_non_conforme_challenger_conforme_path_b_sans_deadband():
    bids = [
        _bid("provider-1", "edge1", 0.20, is_compliant=False),   # tenant en violation
        _bid("provider-2", "edge2", -0.05, is_compliant=True),
    ]
    v = arbitrate(bids, incumbent_provider="provider-1", incumbent_vm="edge1", enforcement="hard")

    assert v.path == "B"
    assert v.deadband_applied == 0.0   # tenant non retenu : rien à protéger
    assert v.winner_provider == "provider-2"


# ═══════════════════════════════════════════════════════════════
#  4-5. Aucun bid retenu — alertes
# ═══════════════════════════════════════════════════════════════

def test_aucun_conforme_bids_evaluables_path_c_infaisable():
    bids = [
        _bid("provider-1", "edge1", 0.10, is_compliant=False),
        _bid("provider-2", "edge2", 0.20, is_compliant=False),
    ]
    v = arbitrate(bids, enforcement="hard")

    assert v.path == "C"
    assert v.decision == "stay"
    assert v.winner_provider is None
    assert v.alert is not None
    assert v.alert["kind"] == "INFAISABLE"
    assert v.alert["best_effort"] is not None
    assert v.alert["best_effort"]["provider_id"] == "provider-1"   # gap minimal


def test_aucun_bid_evaluable_path_d_sans_donnees():
    bids = [
        _bid("provider-1", None, None, evaluable=False),
        _bid("provider-2", None, None, evaluable=False),
    ]
    v = arbitrate(bids)

    assert v.path == "D"
    assert v.alert["kind"] == "SANS_DONNEES"
    assert v.alert["best_effort"] is None


# ═══════════════════════════════════════════════════════════════
#  6. ⭐ Piège du provider aveugle
# ═══════════════════════════════════════════════════════════════

def test_provider_aveugle_evaluable_false_is_compliant_true_ne_gagne_jamais():
    """
    Neutralité 'ML down' de evaluate_provider : is_compliant=True alors
    qu'evaluable=False (rien n'est réellement proposable). Même seul face
    à un bid évaluable mais non conforme, ce bid aveugle NE DOIT PAS gagner.
    """
    blind      = _bid("provider-1", None, None, is_compliant=True, evaluable=False)
    noncompl   = _bid("provider-2", "edge2", 0.15, is_compliant=False, evaluable=True)

    v = arbitrate([blind, noncompl], enforcement="hard")

    assert v.winner_provider is None   # aucun des deux n'est retenu en mode hard
    assert v.path == "C"
    entry_blind = next(c for c in v.considered if c["provider_id"] == "provider-1")
    assert entry_blind["retained"] is False
    assert entry_blind["why"] == "non évaluable"
    # Le best-effort de l'alerte doit être le bid ÉVALUABLE, jamais l'aveugle.
    assert v.alert["best_effort"]["provider_id"] == "provider-2"


def test_provider_aveugle_seul_ne_gagne_pas_meme_en_mode_soft():
    """
    Même en mode "soft" (conformité non exigée), evaluable=False reste
    éliminatoire — (a) est testé indépendamment de (d).
    """
    blind = _bid("provider-1", None, None, is_compliant=True, evaluable=False)

    v = arbitrate([blind], enforcement="soft")

    assert v.winner_provider is None
    assert v.path == "D"   # aucun bid evaluable du tout


# ═══════════════════════════════════════════════════════════════
#  7. winner_vm == incumbent_vm → stay
# ═══════════════════════════════════════════════════════════════

def test_winner_vm_egal_incumbent_vm_decision_stay():
    bids = [_bid("provider-1", "edge1", -0.05)]
    v = arbitrate(bids, incumbent_provider="provider-1", incumbent_vm="edge1")

    assert v.winner_vm == "edge1"
    assert v.decision == "stay"


# ═══════════════════════════════════════════════════════════════
#  8. incumbent_provider is None → DEPLOY
# ═══════════════════════════════════════════════════════════════

def test_incumbent_provider_none_deploy_deadband_nul():
    bids = [_bid("provider-1", "edge1", -0.05), _bid("provider-2", "edge2", -0.02)]
    v = arbitrate(bids, incumbent_provider=None)

    assert v.path == "DEPLOY"
    assert v.deadband_applied == 0.0
    assert v.winner_provider == "provider-1"   # gap le plus bas


# ═══════════════════════════════════════════════════════════════
#  9. Égalité stricte — départage reproductible
# ═══════════════════════════════════════════════════════════════

def test_egalite_stricte_departage_par_provider_order_reproductible():
    bids = [_bid("provider-2", "edge2", -0.05), _bid("provider-1", "edge1", -0.05)]
    order = ["provider-1", "provider-2"]

    results = [arbitrate(bids, provider_order=order).winner_provider for _ in range(100)]

    assert all(r == "provider-1" for r in results)   # provider-1 en tête de order


# ═══════════════════════════════════════════════════════════════
#  10. ⭐ Extensibilité (preuve R3)
# ═══════════════════════════════════════════════════════════════

def test_extensibilite_ajout_d_un_4e_bid_ne_change_pas_les_3_premiers():
    order = ["provider-1", "provider-2", "provider-3", "provider-4"]
    bids_3 = [
        _bid("provider-1", "edge1", -0.03),
        _bid("provider-2", "edge2", -0.09),
        _bid("provider-3", "edge3", -0.01),
    ]
    v3 = arbitrate(bids_3, provider_order=order)
    gaps_3 = {c["provider_id"]: c["gap_grade"] for c in v3.considered}

    bids_4 = bids_3 + [_bid("provider-4", "edge4", -0.50)]   # nouveau meilleur
    v4 = arbitrate(bids_4, provider_order=order)
    gaps_4 = {c["provider_id"]: c["gap_grade"] for c in v4.considered}

    for pid in ("provider-1", "provider-2", "provider-3"):
        assert gaps_4[pid] == gaps_3[pid]   # inchangés au chiffre près

    assert v3.winner_provider == "provider-2"   # meilleur des 3
    assert v4.winner_provider == "provider-4"   # le classement s'étend, rien d'autre


# ═══════════════════════════════════════════════════════════════
#  11. ⭐ TOPSIS ignoré
# ═══════════════════════════════════════════════════════════════

def test_topsis_score_ignore_le_gagnant_est_celui_du_gap_grade():
    winner_by_gap = _bid("provider-1", "edge1", -0.50, topsis_score=0.0,
                          vm_scores={"edge1": 0.0})
    loser_by_gap  = _bid("provider-2", "edge2", -0.01, topsis_score=1.0,
                          vm_scores={"edge2": 1.0})

    v = arbitrate([winner_by_gap, loser_by_gap])

    assert v.winner_provider == "provider-1"   # gap_grade décide, pas topsis_score


# ═══════════════════════════════════════════════════════════════
#  12. enforcement="soft"
# ═══════════════════════════════════════════════════════════════

def test_enforcement_soft_rend_les_non_conformes_eligibles():
    bids = [_bid("provider-1", "edge1", 0.10, is_compliant=False)]
    v = arbitrate(bids, enforcement="soft")

    assert v.winner_provider == "provider-1"
    assert v.path == "DEPLOY"   # aucun tenant ici


# ═══════════════════════════════════════════════════════════════
#  13. bids vide
# ═══════════════════════════════════════════════════════════════

def test_bids_vide_path_d_sans_crash():
    v = arbitrate([])
    assert v.path == "D"
    assert v.decision == "stay"
    assert v.considered == []


# ═══════════════════════════════════════════════════════════════
#  14. Bids malformés
# ═══════════════════════════════════════════════════════════════

def test_bids_malformes_aucune_exception_raisons_explicites():
    malformed = [
        {},                                              # tout absent
        {"provider_id": "provider-1"},                   # placement_plan/gap_grade absents
        {"provider_id": "provider-2", "gap_grade": None,
         "placement_plan": None},
        "pas un dict du tout",
        None,
        {"provider_id": "provider-3",
         "placement_plan": {"vm_id": "edge3"},
         "gap_grade": {"value": None, "is_compliant": True, "evaluable": True}},
    ]

    v = arbitrate(malformed)   # ne doit lever aucune exception

    assert v.winner_provider is None
    assert len(v.considered) == len(malformed)
    for entry in v.considered:
        assert entry["retained"] is False
        assert entry["why"]   # raison non vide


# ═══════════════════════════════════════════════════════════════
#  15. considered contient TOUS les bids
# ═══════════════════════════════════════════════════════════════

def test_considered_contient_tous_les_bids_avec_raison_non_vide():
    bids = [
        _bid("provider-1", "edge1", -0.03),
        _bid("provider-2", None, None, evaluable=False),
        _bid("provider-3", "edge3", 0.10, is_compliant=False),
    ]
    v = arbitrate(bids, enforcement="hard")

    assert len(v.considered) == 3
    provider_ids = {c["provider_id"] for c in v.considered}
    assert provider_ids == {"provider-1", "provider-2", "provider-3"}
    for c in v.considered:
        assert c["why"]
    assert next(c for c in v.considered if c["provider_id"] == "provider-1")["retained"] is True
    assert next(c for c in v.considered if c["provider_id"] == "provider-2")["retained"] is False
    assert next(c for c in v.considered if c["provider_id"] == "provider-3")["retained"] is False


# ═══════════════════════════════════════════════════════════════
#  16. Endpoint HTTP
# ═══════════════════════════════════════════════════════════════

def test_endpoint_bids_absent_400():
    r = client.post("/arbitrate", json={})
    assert r.status_code == 400


def test_endpoint_bids_vide_200_path_d():
    r = client.post("/arbitrate", json={"bids": []})
    assert r.status_code == 200
    assert r.json()["path"] == "D"


def test_endpoint_health_200():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"]  == "healthy"
    assert body["service"] == "placement_arbiter"
    assert body["enforcement"] == config.SLO_ENFORCEMENT
    assert body["deadband"]    == config.ARBITER_DEADBAND


# ═══════════════════════════════════════════════════════════════
#  17. Déterminisme
# ═══════════════════════════════════════════════════════════════

def test_determinisme_deux_appels_identiques_verdicts_identiques():
    bids = [
        _bid("provider-1", "edge1", -0.03),
        _bid("provider-2", "edge2", -0.09),
    ]
    v1 = arbitrate(copy.deepcopy(bids), incumbent_provider="provider-1", incumbent_vm="edge1")
    v2 = arbitrate(copy.deepcopy(bids), incumbent_provider="provider-1", incumbent_vm="edge1")

    assert v1.to_dict() == v2.to_dict()


def test_determinisme_via_endpoint_http():
    payload = {
        "bids": [
            {"provider_id": "provider-1",
             "placement_plan": {"vm_id": "edge1"},
             "gap_grade": {"value": -0.03, "is_compliant": True, "evaluable": True}},
            {"provider_id": "provider-2",
             "placement_plan": {"vm_id": "edge2"},
             "gap_grade": {"value": -0.09, "is_compliant": True, "evaluable": True}},
        ],
        "incumbent_provider": "provider-1",
        "incumbent_vm": "edge1",
    }
    r1 = client.post("/arbitrate", json=payload)
    r2 = client.post("/arbitrate", json=payload)

    assert r1.json() == r2.json()
