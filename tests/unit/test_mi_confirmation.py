"""
Confirmation par persistance avant promotion en SLO secondaire.

Un vrai lien causal dure plusieurs evaluations MI ; une correlation
fallacieuse entre deux marches aleatoires autocorrelees (Granger & Newbold,
1974) est typiquement un eclair isole. Mesure le 25/08/2026 sur un run reel
de 86 evaluations : 50 % des episodes ram_usage (sans lien causal, coude
memoire jamais atteint) ne duraient qu'UNE evaluation, contre 23 % pour
cpu_usage (couplage reel, verifie a <0,08 ms pres contre la formule M/M/1
sur les 8 VM). is_confirmed() exige MI_CONFIRM_CYCLES evaluations
consecutives au-dessus du seuil, ce qui divise par deux le taux de faux
positifs RAM sans casser les episodes CPU soutenus.

compute_mi_scores() lui-meme est INCHANGE : il retourne toujours le score
brut (ou tenu, cf. test_mi_hold_window.py). Seule la GATE de promotion
(select_dynamic_slos, validate_and_enrich_slos) doit utiliser is_confirmed()
au lieu d'un seuil brut.
"""

import pytest

from shared import config
from services.metrics_manager.metrics_handler import MetricsHandler
from shared.models import SLO


def _hist(cpu_vals, violations):
    return [
        {"cpu_usage": c, "ram_usage": 60.0, "latency": 20.0, "is_violation": v}
        for c, v in zip(cpu_vals, violations)
    ]


_CPU_CONTRASTE  = [45.0, 47.0, 44.0, 46.0, 48.0, 85.0, 87.0, 86.0, 88.0, 84.0]
_VIOL_CONTRASTE = [False] * 5 + [True] * 5


@pytest.fixture
def handler():
    return MetricsHandler()


def test_un_seul_cycle_au_dessus_du_seuil_ne_suffit_pas(handler):
    """Une evaluation isolee, meme forte, ne confirme pas — c'est le coeur du correctif."""
    scores = handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_CONTRASTE), cycle=1)
    assert scores["cpu_usage"] > config.MI_RELATIVE_THRESHOLD   # le score brut EST fort
    assert not handler.is_confirmed("cpu_usage")                # mais pas encore confirme


def test_deux_evaluations_consecutives_confirment(handler):
    handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_CONTRASTE), cycle=1)
    handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_CONTRASTE), cycle=2)

    assert handler.is_confirmed("cpu_usage")


def test_une_chute_sous_le_seuil_reinitialise_le_compteur(handler):
    """Un eclair isole (2 hits puis 1 miss puis 1 hit) ne doit jamais confirmer."""
    handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_CONTRASTE), cycle=1)
    assert not handler.is_confirmed("cpu_usage")

    plat = [60.0] * 10   # cpu constant -> aucune information -> score bas
    handler.compute_mi_scores(_hist(plat, _VIOL_CONTRASTE), cycle=2)
    assert not handler.is_confirmed("cpu_usage")

    handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_CONTRASTE), cycle=3)
    assert not handler.is_confirmed("cpu_usage")   # 1 seul hit consecutif, pas 2


def test_select_dynamic_slos_exige_la_confirmation(handler):
    """Reproduit le faux positif RAM : un score fort mais isole ne doit PAS
    produire de SLO secondaire en mode AUTONOMOUS."""
    history = _hist(_CPU_CONTRASTE, _VIOL_CONTRASTE)
    mi_scores = handler.compute_mi_scores(history, cycle=1, include_primaries=False)
    assert mi_scores["cpu_usage"] > config.MI_RELATIVE_THRESHOLD

    slos, active = handler.select_dynamic_slos(mi_scores, {}, history)

    assert "cpu_usage" not in active   # score fort, mais 1 seule evaluation


def test_select_dynamic_slos_promeut_apres_confirmation(handler):
    history = _hist(_CPU_CONTRASTE, _VIOL_CONTRASTE)
    handler.compute_mi_scores(history, cycle=1, include_primaries=False)
    mi_scores = handler.compute_mi_scores(history, cycle=2, include_primaries=False)

    slos, active = handler.select_dynamic_slos(mi_scores, {}, history)

    assert "cpu_usage" in active
    cpu_slo = next(s for s in slos if s.metric == "cpu_usage")
    assert not cpu_slo.is_primary


def test_validate_and_enrich_slos_exige_aussi_la_confirmation(handler):
    """Meme garde-fou cote mode ENHANCED (LLM)."""
    history = _hist(_CPU_CONTRASTE, _VIOL_CONTRASTE)
    llm_slo = SLO(metric="latency", operator="<", threshold=28.0, unit="ms",
                  target=25.0, weight=1.0, window="5m", is_primary=True)

    mi_scores = handler.compute_mi_scores(history, cycle=1, skip_metrics={"latency"})
    _, active_1 = handler.validate_and_enrich_slos([llm_slo], mi_scores, history)
    assert "cpu_usage" not in active_1

    mi_scores = handler.compute_mi_scores(history, cycle=2, skip_metrics={"latency"})
    _, active_2 = handler.validate_and_enrich_slos([llm_slo], mi_scores, history)
    assert "cpu_usage" in active_2
