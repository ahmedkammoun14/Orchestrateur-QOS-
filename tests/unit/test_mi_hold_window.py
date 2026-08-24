"""
Retenue du score MI quand la fenetre d'historique n'a AUCUN contraste.

Une fenetre ou tous les cycles violent (ou aucun) ne permet pas d'estimer
MI(metrique ; violation) : il n'y a qu'une classe. Le code emettait alors 0.0,
ce qui signifie « aucune dependance » alors que la verite est « pas mesurable
ici ». Consequence observee le 24/08/2026 : le SLO secondaire cpu_usage
clignotait (0.27 -> 0.00 -> 0.27) sur ~25 % des cycles, sans qu'aucune
dependance reelle n'ait change -- la voiture traversait simplement une zone ou
toutes les VMs violent.

On conserve donc le dernier score REELLEMENT mesure, avec peremption apres
config.MI_HOLD_CYCLES cycles.
"""

import pytest

from shared import config
from services.metrics_manager.metrics_handler import MetricsHandler


def _hist(cpu_vals, violations):
    return [
        {"cpu_usage": c, "ram_usage": 60.0, "latency": 20.0, "is_violation": v}
        for c, v in zip(cpu_vals, violations)
    ]


# Fenetre CONTRASTEE : cpu bas -> conforme, cpu haut -> violation.
_CPU_CONTRASTE = [45.0, 47.0, 44.0, 46.0, 48.0, 85.0, 87.0, 86.0, 88.0, 84.0]
_VIOL_CONTRASTE = [False] * 5 + [True] * 5

# Meme fenetre, mais tout viole : une seule classe, MI non mesurable.
_VIOL_MONO = [True] * 10


@pytest.fixture
def handler():
    return MetricsHandler()


def test_fenetre_contrastee_produit_un_score(handler):
    scores = handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_CONTRASTE), cycle=1)
    assert scores["cpu_usage"] > config.MI_RELATIVE_THRESHOLD


def test_fenetre_mono_classe_conserve_le_score_precedent(handler):
    """⭐ Le coeur du correctif : pas de retour a 0 sur une fenetre muette."""
    mesure = handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_CONTRASTE), cycle=1)
    attendu = mesure["cpu_usage"]

    tenu = handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_MONO), cycle=2)

    assert tenu["cpu_usage"] == pytest.approx(attendu)


def test_le_score_tenu_perime(handler):
    """Au-dela de MI_HOLD_CYCLES EVALUATIONS sans mesure possible, on retombe a 0.

    L'age se compte en evaluations MI, PAS en cycles d'orchestration : la MI ne
    tourne que chez le provider ACTIF, alors que cycle_count monte aussi chez le
    STANDBY. Compter en cycles faisait perimer des scores pourtant consecutifs.
    """
    handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_CONTRASTE), cycle=1)

    for i in range(config.MI_HOLD_CYCLES):
        tenu = handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_MONO), cycle=2 + i)
        assert tenu["cpu_usage"] > 0.0, f"perime trop tot a l'evaluation {i+1}"

    perime = handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_MONO), cycle=99)
    assert perime["cpu_usage"] == 0.0


def test_un_cycle_count_qui_bondit_ne_perime_pas(handler):
    """Un saut de cycle_count (retour de STANDBY) ne doit rien perimer."""
    handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_CONTRASTE), cycle=1)

    tenu = handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_MONO), cycle=500)

    assert tenu["cpu_usage"] > 0.0


def test_sans_mesure_prealable_reste_a_zero(handler):
    """Une fenetre muette des le premier cycle ne fabrique pas de score."""
    scores = handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_MONO), cycle=1)
    assert scores["cpu_usage"] == 0.0


def test_une_nouvelle_mesure_remplace_le_score_tenu(handler):
    """La retenue est un pont, pas un verrou : la mesure suivante reprend la main."""
    handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_CONTRASTE), cycle=1)
    handler.compute_mi_scores(_hist(_CPU_CONTRASTE, _VIOL_MONO), cycle=2)

    plat = [60.0] * 10          # cpu constant -> aucune information
    rescore = handler.compute_mi_scores(_hist(plat, _VIOL_CONTRASTE), cycle=3)

    assert rescore["cpu_usage"] == 0.0
