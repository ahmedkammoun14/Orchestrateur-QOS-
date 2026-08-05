"""
Tests du Gap Grade v2 (hub/provider_arbitration.py — signed_excess,
compute_gap_grade).

Fonctions PURES, NON BRANCHÉES à ce stade (lot 2) : aucun mock, aucun
réseau. Le test 10 est un garde-fou de non-branchement — il vérifie que
evaluate_vm et negotiate produisent des résultats identiques à avant ce lot
et n'invoquent jamais les nouvelles fonctions.
"""

import pytest

from shared.models import SLO
from hub import provider_arbitration as arb
from hub.provider_arbitration import (
    GAP_GRADE_RHO,
    ProviderAssessment,
    ProviderOffer,
    compute_gap_grade,
    evaluate_provider,
    evaluate_vm,
    negotiate,
    signed_excess,
)


# ── Helpers ───────────────────────────────────────────────────

def _slo_dict(metric="latency", operator="<", threshold=40.0, weight=1.0,
              is_primary=True, unit="ms") -> dict:
    return SLO(metric=metric, operator=operator, threshold=threshold, unit=unit,
               weight=weight, is_primary=is_primary).dict()


def _slo_obj(metric="latency", operator="<", threshold=40.0, weight=1.0,
             is_primary=True, unit="ms") -> SLO:
    return SLO(metric=metric, operator=operator, threshold=threshold, unit=unit,
               weight=weight, is_primary=is_primary)


_LAT_CPU_SLOS_35 = [
    _slo_dict(metric="latency",   operator="<",  threshold=35.0, weight=0.6),
    _slo_dict(metric="cpu_usage", operator=">=", threshold=2.5,  weight=0.4),
]

_LAT_CPU_SLOS_65 = [
    _slo_dict(metric="latency",   operator="<",  threshold=65.0, weight=0.6),
    _slo_dict(metric="cpu_usage", operator=">=", threshold=2.5,  weight=0.4),
]


# ═══════════════════════════════════════════════════════════════
#  TEST 1 — Non-régression AUTONOME (le plus important)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("weight", [1.0, 0.6, 2.0])
def test_un_seul_slo_retourne_exactement_delta(weight):
    slos = [_slo_dict(metric="latency", operator="<", threshold=40.0, weight=weight)]

    assert compute_gap_grade(slos, {"latency": 46}) == pytest.approx(0.15, abs=1e-4)
    assert compute_gap_grade(slos, {"latency": 22}) == pytest.approx(-0.45, abs=1e-4)


# ═══════════════════════════════════════════════════════════════
#  TEST 2 — Cas E7, deux providers conformes
# ═══════════════════════════════════════════════════════════════

def test_e7_deux_providers_conformes_edge2_gagne():
    cloud1 = compute_gap_grade(_LAT_CPU_SLOS_65, {"latency": 60, "cpu_usage": 12.8})
    edge2  = compute_gap_grade(_LAT_CPU_SLOS_65, {"latency": 32, "cpu_usage": 3.2})

    assert cloud1 == pytest.approx(-0.0825, abs=1e-4)
    assert edge2  == pytest.approx(-0.1397, abs=1e-4)
    assert edge2 < cloud1   # edge2 gagne


# ═══════════════════════════════════════════════════════════════
#  TEST 3 — Plancher
# ═══════════════════════════════════════════════════════════════

def test_plancher_delta_floor():
    assert signed_excess(12.8, 2.5, ">=") == pytest.approx(-1.0, abs=1e-4)   # brut : -4.12
    assert signed_excess(3.2,  2.5, ">=") == pytest.approx(-0.28, abs=1e-4)  # non borné, > -1
    assert signed_excess(0.0,  40,  "<")  == pytest.approx(-1.0, abs=1e-4)   # borne naturelle


def test_cote_violation_non_borne():
    assert signed_excess(230, 40, "<") == pytest.approx(4.75, abs=1e-4)


# ═══════════════════════════════════════════════════════════════
#  TEST 4 — Non-compensation
# ═══════════════════════════════════════════════════════════════

def test_non_compensation_violation_reste_positive():
    g = compute_gap_grade(_LAT_CPU_SLOS_35, {"latency": 50, "cpu_usage": 12.8})
    assert g == pytest.approx(0.2208, abs=1e-4)
    assert g > 0   # malgré un énorme excédent CPU


# ═══════════════════════════════════════════════════════════════
#  TEST 5 — Tchebycheff vs somme pondérée
# ═══════════════════════════════════════════════════════════════

def test_tchebycheff_elit_edge1b_somme_ponderee_aurait_elu_cloud1():
    edge1b = compute_gap_grade(_LAT_CPU_SLOS_35, {"latency": 38, "cpu_usage": 3.8})
    cloud1 = compute_gap_grade(_LAT_CPU_SLOS_35, {"latency": 50, "cpu_usage": 12.8})

    assert edge1b == pytest.approx(0.0325, abs=1e-4)
    assert cloud1 == pytest.approx(0.2208, abs=1e-4)
    assert edge1b < cloud1   # edge1b gagne avec le Gap Grade

    # Documente le défaut corrigé : la somme pondérée SANS plancher, sur les
    # mêmes deltas non bornés, aurait élu cloud1 (score plus bas = meilleur
    # dans l'ancienne convention _excess/violation_score).
    def _unbounded_delta(value, threshold, operator):
        if operator in ("<", "<="):
            return (value - threshold) / threshold
        return (threshold - value) / threshold

    edge1b_sum = 0.6 * _unbounded_delta(38, 35, "<") + 0.4 * _unbounded_delta(3.8, 2.5, ">=")
    cloud1_sum = 0.6 * _unbounded_delta(50, 35, "<") + 0.4 * _unbounded_delta(12.8, 2.5, ">=")

    assert cloud1_sum == pytest.approx(-1.3909, abs=1e-4)
    assert edge1b_sum == pytest.approx(-0.1566, abs=1e-4)
    assert cloud1_sum < edge1b_sum   # la somme pondérée non bornée élirait (à tort) cloud1


# ═══════════════════════════════════════════════════════════════
#  TEST 6 — Le signe de G n'implique pas la conformité
# ═══════════════════════════════════════════════════════════════

def test_signe_negatif_n_implique_pas_la_conformite():
    """VM en violation de latence (35.035 > 35) mais G < 0."""
    g = compute_gap_grade(_LAT_CPU_SLOS_35, {"latency": 35.035, "cpu_usage": 4.75})

    assert g == pytest.approx(-0.0321, abs=1e-4)
    assert g < 0   # alors que la VM VIOLE la latence — is_compliant reste seul juge


# ═══════════════════════════════════════════════════════════════
#  TEST 7 — Cas dégénérés (None, aucun crash)
# ═══════════════════════════════════════════════════════════════

def test_slos_vide_retourne_none():
    assert compute_gap_grade([], {"latency": 40}) is None


def test_aucun_slo_primaire_retourne_none():
    slos = [_slo_dict(metric="latency", threshold=40.0, is_primary=False)]
    assert compute_gap_grade(slos, {"latency": 100}) is None


def test_metrique_absente_du_registry_retourne_none():
    slos = [_slo_dict(metric="metrique_inconnue", operator="<", threshold=40.0)]
    assert compute_gap_grade(slos, {"metrique_inconnue": 100}) is None


def test_threshold_non_positif_retourne_none():
    slos = [_slo_dict(metric="latency", threshold=0.0)]
    assert compute_gap_grade(slos, {"latency": 40}) is None

    slos_neg = [_slo_dict(metric="latency", threshold=-5.0)]
    assert compute_gap_grade(slos_neg, {"latency": 40}) is None


def test_values_sans_la_metrique_slo_retourne_none():
    slos = [_slo_dict(metric="latency", threshold=40.0)]
    assert compute_gap_grade(slos, {"cpu_usage": 3.0}) is None


def test_values_none_pour_seule_metrique_retourne_none():
    slos = [_slo_dict(metric="latency", threshold=40.0)]
    assert compute_gap_grade(slos, {"latency": None}) is None


# ═══════════════════════════════════════════════════════════════
#  TEST 8 — Poids
# ═══════════════════════════════════════════════════════════════

def test_poids_tous_a_zero_bascule_sur_uniforme_sans_division_par_zero():
    slos = [
        _slo_dict(metric="latency",   operator="<",  threshold=35.0, weight=0.0),
        _slo_dict(metric="cpu_usage", operator=">=", threshold=2.5,  weight=0.0),
    ]
    g = compute_gap_grade(slos, {"latency": 50, "cpu_usage": 12.8})

    # Poids uniformes (0.5 chacun) — recalcul manuel de contrôle.
    d_lat = signed_excess(50, 35, "<")
    d_cpu = signed_excess(12.8, 2.5, ">=")
    terms = [0.5 * d_lat, 0.5 * d_cpu]
    expected = (max(terms) + GAP_GRADE_RHO * sum(terms)) / (1 + GAP_GRADE_RHO)

    assert g == pytest.approx(expected, abs=1e-4)


def test_poids_absent_meme_comportement_que_zero():
    """Clé 'weight' manquante du dict → traitée comme 0.0 (poids uniformes)."""
    slo_sans_poids = _slo_dict(metric="latency", operator="<", threshold=40.0)
    del slo_sans_poids["weight"]

    g = compute_gap_grade([slo_sans_poids], {"latency": 46})

    assert g == pytest.approx(0.15, abs=1e-4)   # 1 seul SLO → poids uniforme = 1 de toute façon


# ═══════════════════════════════════════════════════════════════
#  TEST 9 — Accepte dicts ET objets SLO
# ═══════════════════════════════════════════════════════════════

def test_accepte_dicts_et_objets_slo():
    slos_dict = [_slo_dict(metric="latency", operator="<", threshold=40.0, weight=1.0)]
    slos_obj  = [_slo_obj(metric="latency", operator="<", threshold=40.0, weight=1.0)]

    g_dict = compute_gap_grade(slos_dict, {"latency": 46})
    g_obj  = compute_gap_grade(slos_obj,  {"latency": 46})

    assert g_dict == pytest.approx(0.15, abs=1e-4)
    assert g_obj  == pytest.approx(0.15, abs=1e-4)
    assert g_dict == g_obj


def test_accepte_melange_dicts_et_objets_slo():
    slos = [
        _slo_dict(metric="latency",   operator="<",  threshold=35.0, weight=0.6),
        _slo_obj(metric="cpu_usage",  operator=">=", threshold=2.5,  weight=0.4),
    ]
    g = compute_gap_grade(slos, {"latency": 50, "cpu_usage": 12.8})
    assert g == pytest.approx(0.2208, abs=1e-4)


# ═══════════════════════════════════════════════════════════════
#  TEST 10 — Garde-fou de non-branchement
# ═══════════════════════════════════════════════════════════════

def test_evaluate_vm_resultats_identiques_a_avant_ce_lot():
    """
    Jeu de référence repris de test_provider_arbitration.py
    (test_score_operateur_inferieur) : seuil 30, valeur 32, poids 1.0
    → violation_score == (32-30)/30 == 0.0667, is_compliant False.
    Ce résultat ne doit pas bouger d'un iota avec l'ajout de ce lot.
    """
    slos = [_slo_obj(metric="latency", operator="<", threshold=30.0, weight=1.0)]
    predictions_map = {"edge1": {"latency": {"predictions": [32.0, 32.0, 32.0]}}}

    ev = evaluate_vm("edge1", slos, {"vm_id": "edge1"}, predictions_map)

    assert ev.violation_score == pytest.approx(0.0667, abs=1e-4)
    assert ev.is_compliant is False


def test_negotiate_resultat_identique_a_avant_ce_lot():
    local = ProviderAssessment(
        provider_id       = "provider-1",
        evaluations       = (),
        compliant_vms     = (),
        best_effort_vm    = "edge1",
        best_effort_score = 0.20,
        is_compliant      = False,
        evaluable         = True,
    )
    offer = ProviderOffer(provider_id="provider-2", vm_id="edge2", violation_score=0.05)

    result = negotiate(local, offer, incumbent_provider_id=None)

    assert result.decision.value == "cede_a_l_offre"
    assert result.winning_provider == "provider-2"
    assert result.winning_vm == "edge2"


def test_evaluate_vm_n_appelle_jamais_signed_excess_ni_compute_gap_grade(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("signed_excess/compute_gap_grade appelée par du code de production")

    monkeypatch.setattr(arb, "signed_excess", _boom)
    monkeypatch.setattr(arb, "compute_gap_grade", _boom)

    slos = [_slo_obj(metric="latency", operator="<", threshold=30.0, weight=1.0)]
    predictions_map = {"edge1": {"latency": {"predictions": [32.0, 32.0, 32.0]}}}

    ev = evaluate_vm("edge1", slos, {"vm_id": "edge1"}, predictions_map)   # ne doit pas lever

    assert ev.violation_score == pytest.approx(0.0667, abs=1e-4)


def test_negotiate_n_appelle_jamais_signed_excess_ni_compute_gap_grade(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("signed_excess/compute_gap_grade appelée par du code de production")

    monkeypatch.setattr(arb, "signed_excess", _boom)
    monkeypatch.setattr(arb, "compute_gap_grade", _boom)

    local = ProviderAssessment(
        provider_id       = "provider-1",
        evaluations       = (),
        compliant_vms     = (),
        best_effort_vm    = "edge1",
        best_effort_score = 0.20,
        is_compliant      = False,
        evaluable         = True,
    )
    offer = ProviderOffer(provider_id="provider-2", vm_id="edge2", violation_score=0.05)

    result = negotiate(local, offer, incumbent_provider_id=None)   # ne doit pas lever

    assert result.decision.value == "cede_a_l_offre"
