"""
Tests du module d'arbitrage inter-provider.

Le module est PUR : ces tests n'utilisent aucun mock, aucun réseau, aucun
stockage — uniquement des dicts en mémoire.

Rappel du rôle testé ici : le module PARTITIONNE (conformes vs offre de repli),
il ne choisit jamais parmi les conformes — c'est TOPSIS, en aval, qui tranche.
"""

import dataclasses
import json

import pytest

from shared import config
from shared.models import SLO
from services.decision_intelligence.topsis import TopsisSelector
from hub.provider_arbitration import (
    NEGOTIATION_DEADBAND,
    NegotiationDecision,
    NegotiationResult,
    ProviderAssessment,
    ProviderOffer,
    candidates_for_provider,
    evaluate_provider,
    evaluate_vm,
    negotiate,
)


# ── Helpers ───────────────────────────────────────────────────

def _slo(metric="latency", operator="<", threshold=30.0, unit="ms", weight=1.0) -> SLO:
    return SLO(
        metric=metric,
        operator=operator,
        threshold=threshold,
        unit=unit,
        weight=weight,
    )


def _preds(**par_vm) -> dict:
    """predictions_map à valeur constante : la moyenne pondérée vaut la valeur."""
    return {
        vm_id: {metric: {"predictions": [v, v, v]} for metric, v in metrics.items()}
        for vm_id, metrics in par_vm.items()
    }


def _eval_one(slos, candidate, predictions_map):
    return evaluate_vm(candidate["vm_id"], slos, candidate, predictions_map)


# ── Score de violation ────────────────────────────────────────

def test_vm_conforme_a_un_score_nul():
    ev = _eval_one(
        [_slo(threshold=30.0)],
        {"vm_id": "edge1"},
        _preds(edge1={"latency": 20.0}),
    )
    assert ev.is_compliant is True
    assert ev.violation_score == 0.0


def test_score_operateur_inferieur():
    """Seuil 30, valeur 32, poids 1.0 → (32−30)/30."""
    ev = _eval_one(
        [_slo(operator="<", threshold=30.0, weight=1.0)],
        {"vm_id": "edge1"},
        _preds(edge1={"latency": 32.0}),
    )
    assert ev.violation_score == pytest.approx(0.0667, abs=1e-4)
    assert ev.is_compliant is False


def test_score_operateur_superieur_ou_egal():
    """Seuil 50, valeur 40, poids 1.0 → (50−40)/50 = 0.2."""
    ev = _eval_one(
        [_slo(metric="cpu_usage", operator=">=", threshold=50.0, unit="%", weight=1.0)],
        {"vm_id": "edge1"},
        _preds(edge1={"cpu_usage": 40.0}),
    )
    assert ev.violation_score == pytest.approx(0.2)


def test_score_moyenne_ponderee_de_deux_slos():
    """(0.5 × (32−30)/30  +  0.5 × (90−80)/80) / 1.0 — somme des poids = 1.0."""
    slos = [
        _slo(metric="latency", operator="<", threshold=30.0, unit="ms", weight=0.5),
        _slo(metric="cpu_usage", operator="<", threshold=80.0, unit="%", weight=0.5),
    ]
    ev = _eval_one(
        slos,
        {"vm_id": "edge1"},
        _preds(edge1={"latency": 32.0, "cpu_usage": 90.0}),
    )
    attendu = 0.5 * (2.0 / 30.0) + 0.5 * (10.0 / 80.0)
    assert ev.violation_score == pytest.approx(attendu)


def test_poids_tous_nuls_bascule_en_poids_uniformes():
    slos = [
        _slo(metric="latency", operator="<", threshold=30.0, unit="ms", weight=0.0),
        _slo(metric="cpu_usage", operator="<", threshold=80.0, unit="%", weight=0.0),
    ]
    ev = _eval_one(
        slos,
        {"vm_id": "edge1"},
        _preds(edge1={"latency": 32.0, "cpu_usage": 90.0}),
    )
    # Poids 1.0 chacun, puis division par leur somme (2.0) → moyenne simple.
    attendu = ((2.0 / 30.0) + (10.0 / 80.0)) / 2.0
    assert ev.violation_score == pytest.approx(attendu)


def test_seuil_nul_ou_negatif_est_ignore_sans_division_par_zero():
    slos = [
        _slo(metric="latency", operator="<", threshold=0.0, unit="ms", weight=1.0),
        _slo(metric="cpu_usage", operator="<", threshold=80.0, unit="%", weight=1.0),
    ]
    ev = _eval_one(
        slos,
        {"vm_id": "edge1"},
        _preds(edge1={"latency": 32.0, "cpu_usage": 90.0}),
    )
    # Seul le SLO cpu contribue ; le SLO à seuil nul est écarté du calcul.
    assert ev.violation_score == pytest.approx(10.0 / 80.0)


# ── Normalisation du score (invariance au nombre de métriques) ─

def _excess_pair(exces_latency=0.10, exces_cpu=0.20, w_lat=0.25, w_cpu=0.75):
    """
    Construit un couple (slos, predictions_map) produisant exactement les excès
    demandés : latency seuil 30 « < », cpu_usage seuil 50 « < ».
    """
    slos = [
        _slo(metric="latency", operator="<", threshold=30.0, unit="ms", weight=w_lat),
        _slo(metric="cpu_usage", operator="<", threshold=50.0, unit="%", weight=w_cpu),
    ]
    preds = _preds(edge1={
        "latency":   30.0 * (1.0 + exces_latency),
        "cpu_usage": 50.0 * (1.0 + exces_cpu),
    })
    return slos, preds


def test_non_regression_scenario_de_reference():
    """SLO unique poids 1.0, seuil 30 : les scores de référence sont inchangés."""
    for valeur, attendu in ((32.0, 0.0667), (31.0, 0.0333)):
        ev = _eval_one(
            [_slo(operator="<", threshold=30.0, weight=1.0)],
            {"vm_id": "edge1"},
            _preds(edge1={"latency": valeur}),
        )
        assert ev.violation_score == pytest.approx(attendu, abs=1e-4)


def test_score_invariant_au_nombre_de_metriques_evaluees():
    """
    Cœur du correctif : même excès relatif sur latency, l'une des VMs ayant en
    plus une métrique CPU au même excès, l'autre sans donnée CPU du tout.
    Les scores doivent être ÉGAUX — sinon la VM la moins instrumentée
    l'emporterait par simple manque de données.
    """
    slos = [
        _slo(metric="latency", operator="<", threshold=30.0, unit="ms", weight=0.5),
        _slo(metric="cpu_usage", operator="<", threshold=50.0, unit="%", weight=0.5),
    ]
    preds = {
        "edge1": {
            "latency":   {"predictions": [33.0] * 3},   # excès 0.10
            "cpu_usage": {"predictions": [55.0] * 3},   # excès 0.10
        },
        "cloud1": {
            "latency":   {"predictions": [33.0] * 3},   # excès 0.10, pas de CPU
        },
    }
    bien_instrumentee = _eval_one(slos, {"vm_id": "edge1"}, preds)
    mal_instrumentee  = _eval_one(slos, {"vm_id": "cloud1"}, preds)

    assert bien_instrumentee.violation_score == pytest.approx(
        mal_instrumentee.violation_score
    )
    assert bien_instrumentee.violation_score == pytest.approx(0.10)


def test_moyenne_ponderee_poids_normalises():
    """Poids 0.25 / 0.75, excès 0.10 / 0.20 → (0.025 + 0.15) / 1.0 = 0.175."""
    slos, preds = _excess_pair(w_lat=0.25, w_cpu=0.75)
    ev = _eval_one(slos, {"vm_id": "edge1"}, preds)
    assert ev.violation_score == pytest.approx(0.175)


def test_score_invariant_a_l_echelle_des_poids():
    """
    Poids 2.0 / 6.0 (somme 8.0), mêmes excès → (0.2 + 1.2) / 8 = 0.175.
    Résultat identique au test précédent : le score ne dépend que du RATIO
    des poids, pas de leur échelle. Les poids venant du LLM ne sont pas
    normalisés, cette propriété est nécessaire.
    """
    slos, preds = _excess_pair(w_lat=2.0, w_cpu=6.0)
    ev = _eval_one(slos, {"vm_id": "edge1"}, preds)
    assert ev.violation_score == pytest.approx(0.175)


def test_poids_nuls_uniformises_puis_divises_par_leur_nombre():
    """Poids 0 / 0 → uniformes (1.0 chacun), excès 0.10 / 0.20 → 0.15."""
    slos, preds = _excess_pair(w_lat=0.0, w_cpu=0.0)
    ev = _eval_one(slos, {"vm_id": "edge1"}, preds)
    assert ev.violation_score == pytest.approx(0.15)


# ── Valeur représentative ─────────────────────────────────────

def test_utilise_la_moyenne_ponderee_et_non_le_max():
    """
    Prédictions [10, 10, 100] : moyenne pondérée = 25.0, max = 100.0.
    Le seuil 30 est franchi par le max mais pas par la moyenne pondérée —
    c'est cette dernière qui fait autorité (convention du pipeline).
    """
    preds = [10.0, 10.0, 100.0]
    attendu = TopsisSelector().calculate_weighted_mean(preds)
    assert attendu == pytest.approx(25.0)

    ev = _eval_one(
        [_slo(operator="<", threshold=30.0)],
        {"vm_id": "edge1"},
        {"edge1": {"latency": {"predictions": preds}}},
    )
    assert ev.is_compliant is True          # 25 < 30 ; avec max() ce serait False
    assert ev.violation_score == 0.0


def test_predictions_none_sont_filtrees():
    preds = [None, 10.0, 10.0, 100.0, None]
    ev = _eval_one(
        [_slo(operator="<", threshold=30.0)],
        {"vm_id": "edge1"},
        {"edge1": {"latency": {"predictions": preds}}},
    )
    # Après filtrage : [10, 10, 100] → 25.0 < 30
    assert ev.is_compliant is True
    assert ev.evaluable is True


def test_sans_prediction_la_vm_n_est_pas_conforme_meme_si_la_mesure_respecte_le_seuil():
    """Règle _filter_candidates : pas de prédiction ⇒ non conforme."""
    ev = _eval_one(
        [_slo(operator="<", threshold=30.0)],
        {"vm_id": "edge1", "rtt_ms": 10.0},   # mesure très en dessous du seuil
        {},
    )
    assert ev.has_predictions is False
    assert ev.is_compliant is False


def test_sans_prediction_le_score_reste_calcule_sur_la_mesure():
    """Repli mesuré autorisé pour le SCORE : sinon ML muet ⇒ plus de négociation."""
    ev = _eval_one(
        [_slo(operator="<", threshold=30.0)],
        {"vm_id": "edge1", "rtt_ms": 45.0},
        {},
    )
    assert ev.evaluable is True
    assert ev.has_predictions is False
    assert ev.violation_score == pytest.approx(15.0 / 30.0)


def test_ni_prediction_ni_mesure_vm_non_evaluable():
    ev = _eval_one([_slo()], {"vm_id": "edge1"}, {})
    assert ev.evaluable is False
    assert ev.is_compliant is False


def test_slo_en_cores_applique_la_conversion_de_capacite():
    """
    VM à 4 cœurs chargée à 30 % → disponibilité 2.8 cœurs.
    SLO « >= 2 cores » satisfait ; SLO « >= 3 cores » violé de (3−2.8)/3.
    """
    cand = {"vm_id": "edge1", "total_cores": 4}
    preds = _preds(edge1={"cpu_usage": 30.0})

    ok = _eval_one(
        [_slo(metric="cpu_usage", operator=">=", threshold=2.0, unit="cores")],
        cand, preds,
    )
    assert ok.is_compliant is True

    ko = _eval_one(
        [_slo(metric="cpu_usage", operator=">=", threshold=3.0, unit="cores")],
        cand, preds,
    )
    assert ko.is_compliant is False
    assert ko.violation_score == pytest.approx((3.0 - 2.8) / 3.0)


# ── Cadrage par provider ──────────────────────────────────────

def test_candidates_for_provider_ne_rend_que_les_vms_du_provider():
    cands = [{"vm_id": v} for v in ("edge1", "edge2", "cloud1", "cloud2")]
    retenus = [c["vm_id"] for c in candidates_for_provider("provider-1", cands)]
    assert retenus == ["edge1", "cloud1"]


def test_isolation_la_vm_active_de_l_autre_provider_est_exclue():
    """
    Aucune exception pour la « VM active » : si le service tourne sur edge2,
    provider-1 ne la voit pas — un orchestrateur distant ne la verrait pas non plus.
    """
    vm_active = "edge2"
    cands = [{"vm_id": vm_active, "active": True}, {"vm_id": "edge1"}, {"vm_id": "cloud1"}]
    retenus = [c["vm_id"] for c in candidates_for_provider("provider-1", cands)]
    assert vm_active not in retenus
    assert retenus == ["edge1", "cloud1"]


def test_provider_inconnu_leve_valueerror():
    with pytest.raises(ValueError):
        candidates_for_provider("provider-42", [{"vm_id": "edge1"}])


# ── Partition (cœur de la tâche) ──────────────────────────────

def test_deux_vms_conformes_sont_TOUTES_rendues_le_module_n_en_choisit_aucune():
    cands = [{"vm_id": "edge2"}, {"vm_id": "cloud2"}]
    a = evaluate_provider(
        "provider-2", [_slo(threshold=30.0)], cands,
        _preds(edge2={"latency": 20.0}, cloud2={"latency": 25.0}),
    )
    assert a.is_compliant is True
    assert len(a.compliant_vms) == 2                     # aucune élection ici
    assert a.compliant_vms == ("edge2", "cloud2")        # ordre d'entrée préservé


def test_une_seule_vm_conforme():
    cands = [{"vm_id": "edge1"}, {"vm_id": "cloud1"}]
    a = evaluate_provider(
        "provider-1", [_slo(threshold=30.0)], cands,
        _preds(edge1={"latency": 20.0}, cloud1={"latency": 50.0}),
    )
    assert a.is_compliant is True
    assert a.compliant_vms == ("edge1",)


def test_aucune_vm_conforme_produit_une_offre_de_repli():
    cands = [{"vm_id": "edge1"}, {"vm_id": "cloud1"}]
    a = evaluate_provider(
        "provider-1", [_slo(threshold=30.0)], cands,
        _preds(edge1={"latency": 32.0}, cloud1={"latency": 40.0}),
    )
    assert a.compliant_vms == ()
    assert a.is_compliant is False
    assert a.best_effort_vm == "edge1"                   # violation minimale
    assert a.best_effort_score == pytest.approx(2.0 / 30.0)


def test_egalite_stricte_la_premiere_dans_l_ordre_d_entree_gagne():
    cands = [{"vm_id": "cloud1"}, {"vm_id": "edge1"}]    # cloud1 en tête
    a = evaluate_provider(
        "provider-1", [_slo(threshold=30.0)], cands,
        _preds(edge1={"latency": 40.0}, cloud1={"latency": 40.0}),
    )
    assert a.best_effort_vm == "cloud1"


def test_aucune_vm_evaluable_provider_neutre():
    """ML muet et aucune mesure : ni passation ni négociation déclenchées."""
    cands = [{"vm_id": "edge1"}, {"vm_id": "cloud1"}]
    a = evaluate_provider("provider-1", [_slo()], cands, {})
    assert a.evaluable is False
    assert a.is_compliant is True
    assert a.compliant_vms == ()
    assert a.best_effort_vm is None
    assert a.best_effort_score is None
    assert a.to_offer() is None


# ── Offre ─────────────────────────────────────────────────────

def test_to_offer_d_un_provider_non_conforme():
    cands = [{"vm_id": "edge1"}, {"vm_id": "cloud1"}]
    a = evaluate_provider(
        "provider-1", [_slo(threshold=30.0)], cands,
        _preds(edge1={"latency": 32.0}, cloud1={"latency": 40.0}),
    )
    offre = a.to_offer()
    assert offre is not None
    assert offre.provider_id == "provider-1"
    assert offre.vm_id == "edge1"
    assert offre.violation_score == pytest.approx(2.0 / 30.0)


def test_offre_round_trip_et_serialisation_json():
    offre = ProviderOffer(provider_id="provider-2", vm_id="edge2", violation_score=0.0333)
    charge = json.dumps(offre.to_dict())
    reconstruite = ProviderOffer.from_dict(json.loads(charge))
    assert reconstruite == offre


# ── Entrées hétérogènes ───────────────────────────────────────

def test_slos_en_objets_ou_en_dicts_donnent_le_meme_resultat():
    cands = [{"vm_id": "edge1"}, {"vm_id": "cloud1"}]
    preds = _preds(edge1={"latency": 32.0}, cloud1={"latency": 20.0})
    slo_obj = _slo(threshold=30.0)

    via_objets = evaluate_provider("provider-1", [slo_obj], cands, preds)
    via_dicts  = evaluate_provider("provider-1", [slo_obj.dict()], cands, preds)

    assert via_objets == via_dicts


def test_slo_sur_metrique_inconnue_est_ignore():
    slos = [
        _slo(metric="disk_io", operator="<", threshold=10.0, unit="MB/s"),
        _slo(metric="latency", operator="<", threshold=30.0),
    ]
    assert "disk_io" not in config.METRICS_REGISTRY
    ev = _eval_one(slos, {"vm_id": "edge1"}, _preds(edge1={"latency": 20.0}))
    assert ev.is_compliant is True
    assert "disk_io" not in ev.detail


# ── Scénario de la spécification ──────────────────────────────

def test_cas_5_aucun_provider_conforme_offres_comparables():
    """
    Seuil latence 30 ms. provider-1 : meilleure VM à 32 ms.
    provider-2 : meilleure VM à 31 ms. Aucun des deux n'est conforme.
    On ne teste ici que la COMPARABILITÉ des offres — la règle de décision
    viendra à l'étape suivante.
    """
    slos = [_slo(threshold=30.0)]
    cands = [{"vm_id": v} for v in ("edge1", "cloud1", "edge2", "cloud2")]
    preds = _preds(
        edge1={"latency": 32.0}, cloud1={"latency": 40.0},
        edge2={"latency": 31.0}, cloud2={"latency": 45.0},
    )

    p1 = evaluate_provider("provider-1", slos, cands, preds)
    p2 = evaluate_provider("provider-2", slos, cands, preds)

    assert p1.is_compliant is False
    assert p2.is_compliant is False

    offre_p1, offre_p2 = p1.to_offer(), p2.to_offer()
    assert offre_p1.vm_id == "edge1"
    assert offre_p2.vm_id == "edge2"
    assert offre_p2.violation_score < offre_p1.violation_score


# ═══════════════════════════════════════════════════════════════
#  Négociation inter-provider (Cas 5)
# ═══════════════════════════════════════════════════════════════

def _assessment(
    provider_id="provider-2",
    compliant=(),
    best_vm=None,
    best_score=None,
    evaluable=True,
) -> ProviderAssessment:
    """
    Assemble un ProviderAssessment synthétique : la négociation se teste sur des
    scores choisis, indépendamment de la façon dont ils ont été calculés.
    `is_compliant` suit la règle du module — vrai si des VMs conformes existent,
    vrai aussi par NEUTRALITÉ quand rien n'est évaluable (piège « ML down »).
    """
    return ProviderAssessment(
        provider_id       = provider_id,
        evaluations       = (),
        compliant_vms     = tuple(compliant),
        best_effort_vm    = best_vm,
        best_effort_score = best_score,
        is_compliant      = bool(compliant) if evaluable else True,
        evaluable         = evaluable,
    )


def _offer(provider_id="provider-1", vm_id="edge1", score=0.10) -> ProviderOffer:
    return ProviderOffer(provider_id=provider_id, vm_id=vm_id, violation_score=score)


# ── Priorité de la conformité ─────────────────────────────────

def test_negociation_vms_conformes_priment():
    local = _assessment(compliant=("edge2", "cloud2"), best_vm="edge2", best_score=0.0)
    r = negotiate(local, _offer(score=0.10))

    assert r.decision is NegotiationDecision.PREND_LOCAL_CONFORME
    assert r.compliant_vms == ("edge2", "cloud2")
    assert r.winning_vm is None                  # TOPSIS tranchera
    assert r.winning_provider == "provider-2"


def test_negociation_conformite_prime_sur_une_offre_excellente():
    """
    L'offre reçue n'existe que parce que son émetteur n'avait AUCUNE VM
    conforme : même à score 0.0, elle ne peut pas battre une VM conforme.
    """
    local = _assessment(compliant=("edge2",), best_vm="edge2", best_score=0.0)
    r = negotiate(local, _offer(score=0.0))
    assert r.decision is NegotiationDecision.PREND_LOCAL_CONFORME


def test_negociation_piege_ml_down_ne_prend_pas_le_service():
    """
    Provider aveugle : is_compliant=True par neutralité, mais compliant_vms
    vide. Se fier à is_compliant ferait prendre le service à un provider qui
    n'a rien à proposer — c'est compliant_vms qui fait foi.
    """
    local = _assessment(evaluable=False)
    assert local.is_compliant is True and local.compliant_vms == ()

    r = negotiate(local, _offer(score=0.50))
    assert r.decision is NegotiationDecision.CEDE_A_L_OFFRE
    assert r.winning_provider == "provider-1"
    assert r.winning_vm == "edge1"


# ── Absence d'offre ───────────────────────────────────────────

def test_negociation_sans_offre_receveur_conforme():
    local = _assessment(compliant=("edge2",), best_vm="edge2", best_score=0.0)
    r = negotiate(local, None)
    assert r.decision is NegotiationDecision.PREND_LOCAL_CONFORME
    assert r.offered_score is None


def test_negociation_sans_offre_receveur_non_conforme_mais_evaluable():
    local = _assessment(best_vm="edge2", best_score=0.20)
    r = negotiate(local, None)
    assert r.decision is NegotiationDecision.PREND_LOCAL_MEILLEURE
    assert r.winning_vm == "edge2"


def test_negociation_sans_offre_receveur_non_evaluable():
    r = negotiate(_assessment(evaluable=False), None)
    assert r.decision is NegotiationDecision.AUCUNE_OPTION
    assert r.winning_provider is None
    assert r.winning_vm is None


# ── Comparaison sans tenant (déploiement initial) ─────────────

def test_negociation_sans_tenant_receveur_meilleur():
    local = _assessment(best_vm="edge2", best_score=0.05)
    r = negotiate(local, _offer(score=0.20))
    assert r.decision is NegotiationDecision.PREND_LOCAL_MEILLEURE
    assert r.deadband_applied == 0.0


def test_negociation_sans_tenant_offre_meilleure():
    local = _assessment(best_vm="edge2", best_score=0.20)
    r = negotiate(local, _offer(score=0.05))
    assert r.decision is NegotiationDecision.CEDE_A_L_OFFRE
    assert r.deadband_applied == 0.0


def test_negociation_sans_tenant_egalite_exacte_cede():
    """Déterminisme : à égalité l'offre l'emporte, pas de relais inutile."""
    local = _assessment(best_vm="edge2", best_score=0.10)
    r = negotiate(local, _offer(score=0.10))
    assert r.decision is NegotiationDecision.CEDE_A_L_OFFRE
    assert r.deadband_applied == 0.0


# ── Comparaison avec tenant (migration en fonctionnement) ─────

def test_negociation_tenant_receveur_offre_dans_le_deadband_il_garde():
    """
    Tenant à 0.0600, challenger à 0.0400 : écart 0.02, sous le dead-band de
    0.05 → c'est du bruit, le tenant garde. Avec l'ancienne marge RELATIVE
    (0.06 × 0.95 = 0.057), ce même écart aurait déclenché une migration.
    """
    local = _assessment(best_vm="edge2", best_score=0.0600)
    r = negotiate(local, _offer(score=0.0400), incumbent_provider_id="provider-2")
    assert r.decision is NegotiationDecision.PREND_LOCAL_MEILLEURE
    assert r.deadband_applied == NEGOTIATION_DEADBAND


def test_negociation_tenant_receveur_gain_reel_il_cede():
    """Tenant à 0.5000, challenger à 0.2000 : écart 0.30 → migration légitime."""
    local = _assessment(best_vm="edge2", best_score=0.5000)
    r = negotiate(local, _offer(score=0.2000), incumbent_provider_id="provider-2")
    assert r.decision is NegotiationDecision.CEDE_A_L_OFFRE


def test_negociation_tenant_emetteur_dans_le_deadband_le_tenant_garde():
    """Symétrie : tenant à droite cette fois, même écart de 0.02 → il garde."""
    local = _assessment(best_vm="edge2", best_score=0.0400)
    r = negotiate(local, _offer(score=0.0600), incumbent_provider_id="provider-1")
    assert r.decision is NegotiationDecision.CEDE_A_L_OFFRE
    assert r.deadband_applied == NEGOTIATION_DEADBAND


def test_negociation_tenant_emetteur_gain_reel_le_receveur_prend():
    local = _assessment(best_vm="edge2", best_score=0.2000)
    r = negotiate(local, _offer(score=0.5000), incumbent_provider_id="provider-1")
    assert r.decision is NegotiationDecision.PREND_LOCAL_MEILLEURE


@pytest.mark.parametrize("tenant", ["provider-1", "provider-2"])
def test_negociation_deadband_applique_des_qu_un_tenant_est_fourni(tenant):
    local = _assessment(best_vm="edge2", best_score=0.10)
    r = negotiate(local, _offer(score=0.12), incumbent_provider_id=tenant)
    assert r.deadband_applied == NEGOTIATION_DEADBAND


def test_negociation_sans_tenant_comparaison_stricte_meme_sous_le_deadband():
    """
    0.0400 contre 0.0600 : écart 0.02 < dead-band, mais sans tenant il n'y a
    rien à protéger — le meilleur score l'emporte, dans les deux sens.
    """
    meilleur = negotiate(_assessment(best_vm="edge2", best_score=0.0400),
                         _offer(score=0.0600))
    assert meilleur.decision is NegotiationDecision.PREND_LOCAL_MEILLEURE
    assert meilleur.deadband_applied == 0.0

    moins_bon = negotiate(_assessment(best_vm="edge2", best_score=0.0600),
                          _offer(score=0.0400))
    assert moins_bon.decision is NegotiationDecision.CEDE_A_L_OFFRE
    assert moins_bon.deadband_applied == 0.0


@pytest.mark.parametrize("local_score, offered_score", [(0.098, 0.10), (0.10, 0.098)])
def test_negociation_deadband_nul_explicite_equivaut_a_l_absence_de_tenant(
    local_score, offered_score
):
    """
    deadband=0.0 neutralise la protection du tenant. Testé hors égalité stricte :
    à score exactement égal, le tenant conserve par définition alors que le cas
    sans tenant cède — seule frontière où les deux régimes divergent.
    """
    local = _assessment(best_vm="edge2", best_score=local_score)
    offre = _offer(score=offered_score)

    avec_tenant = negotiate(local, offre, incumbent_provider_id="provider-2", deadband=0.0)
    sans_tenant = negotiate(local, offre)

    assert avec_tenant.decision is sans_tenant.decision
    assert avec_tenant.deadband_applied == 0.0


def test_negociation_conformite_ignore_le_deadband():
    """
    Non-régression : la conformité est testée AVANT toute comparaison de
    scores. Un provider conforme l'emporte même face à une offre bien
    meilleure, tenant ou pas — le dead-band n'entre jamais en jeu.
    """
    local = _assessment(compliant=("edge2",), best_vm="edge2", best_score=0.90)
    r = negotiate(local, _offer(score=0.01), incumbent_provider_id="provider-1")
    assert r.decision is NegotiationDecision.PREND_LOCAL_CONFORME
    assert r.deadband_applied == 0.0


def test_negociation_anti_oscillation_sur_huit_cycles():
    """
    Scénario d'oscillation : 8 cycles où les deux providers se croisent de peu
    (écarts de 0.01 à 0.03, tous sous le dead-band). Le vainqueur de chaque
    cycle devient le tenant du suivant. Avec l'ancienne marge relative de 5 %,
    ce scénario produisait 7 migrations sur 8 cycles.
    """
    cycles = [
        (0.04, 0.06), (0.05, 0.04), (0.03, 0.05), (0.06, 0.04),
        (0.04, 0.05), (0.05, 0.03), (0.03, 0.04), (0.04, 0.02),
    ]
    tenant      = "provider-1"
    migrations  = 0

    for score_p1, score_p2 in cycles:
        # Le receveur est le provider qui n'est PAS tenant : c'est lui qui
        # reçoit l'offre et applique la règle de négociation.
        if tenant == "provider-1":
            local, offre = (
                _assessment("provider-2", best_vm="edge2", best_score=score_p2),
                _offer("provider-1", "edge1", score_p1),
            )
        else:
            local, offre = (
                _assessment("provider-1", best_vm="edge1", best_score=score_p1),
                _offer("provider-2", "edge2", score_p2),
            )

        r = negotiate(local, offre, incumbent_provider_id=tenant)
        if r.winning_provider != tenant:
            migrations += 1
            tenant = r.winning_provider

    assert migrations == 0
    assert tenant == "provider-1"


# ── Symétrie ──────────────────────────────────────────────────

@pytest.mark.parametrize("tenant", [None, "provider-1", "provider-2"])
def test_negociation_symetrique_meme_vainqueur_vu_des_deux_cotes(tenant):
    """
    Propriété essentielle : deux orchestrateurs réels (ou un seul simulant les
    deux rôles) doivent aboutir au MÊME provider vainqueur.
    """
    a1 = _assessment("provider-1", best_vm="edge1", best_score=0.20)
    a2 = _assessment("provider-2", best_vm="edge2", best_score=0.05)

    vu_de_p1 = negotiate(a1, a2.to_offer(), incumbent_provider_id=tenant)
    vu_de_p2 = negotiate(a2, a1.to_offer(), incumbent_provider_id=tenant)

    assert vu_de_p1.winning_provider == vu_de_p2.winning_provider == "provider-2"
    assert vu_de_p1.winning_vm == vu_de_p2.winning_vm == "edge2"


# ── Scénario de la spécification ──────────────────────────────

def test_cas_5_negociation_provider_2_l_emporte():
    """
    Seuil 30 ms. provider-1 propose 32 ms, provider-2 propose 31 ms, aucun
    conforme, pas de tenant → provider-2 l'emporte, vu des deux côtés.
    """
    slos = [_slo(threshold=30.0)]
    cands = [{"vm_id": v} for v in ("edge1", "cloud1", "edge2", "cloud2")]
    preds = _preds(
        edge1={"latency": 32.0}, cloud1={"latency": 40.0},
        edge2={"latency": 31.0}, cloud2={"latency": 45.0},
    )
    a1 = evaluate_provider("provider-1", slos, cands, preds)
    a2 = evaluate_provider("provider-2", slos, cands, preds)

    vu_de_p2 = negotiate(a2, a1.to_offer())
    assert vu_de_p2.decision is NegotiationDecision.PREND_LOCAL_MEILLEURE
    assert vu_de_p2.winning_provider == "provider-2"
    assert vu_de_p2.winning_vm == "edge2"

    vu_de_p1 = negotiate(a1, a2.to_offer())
    assert vu_de_p1.decision is NegotiationDecision.CEDE_A_L_OFFRE
    assert vu_de_p1.winning_provider == "provider-2"


def test_cas_5_avec_tenant_le_deadband_protege_provider_1():
    """
    Même scénario, provider-1 tenant. L'écart de latence 32 → 31 ms vaut 1 ms
    sur un seuil de 30, soit 0.0333 en score — sous le dead-band de 0.05, donc
    provider-1 garde le service.

    C'est exactement le cas que la marge RELATIVE traitait à l'envers : elle
    n'exigeait que 0.0667 × 5 % = 0.0033 d'amélioration, et déclenchait la
    migration pour 1 ms de mieux.
    """
    slos = [_slo(threshold=30.0)]
    cands = [{"vm_id": v} for v in ("edge1", "cloud1", "edge2", "cloud2")]
    preds = _preds(
        edge1={"latency": 32.0}, cloud1={"latency": 40.0},
        edge2={"latency": 31.0}, cloud2={"latency": 45.0},
    )
    a1 = evaluate_provider("provider-1", slos, cands, preds)
    a2 = evaluate_provider("provider-2", slos, cands, preds)

    assert a1.best_effort_score == pytest.approx(2.0 / 30.0)
    assert a2.best_effort_score == pytest.approx(1.0 / 30.0)

    r = negotiate(a2, a1.to_offer(), incumbent_provider_id="provider-1")
    assert r.decision is NegotiationDecision.CEDE_A_L_OFFRE
    assert r.winning_provider == "provider-1"
    assert r.deadband_applied == NEGOTIATION_DEADBAND


# ── Divers ────────────────────────────────────────────────────

def test_negociation_reason_contient_les_deux_scores():
    local = _assessment(best_vm="edge2", best_score=0.074)
    r = negotiate(local, _offer(score=0.070))
    assert isinstance(r.reason, str) and r.reason
    assert "0.0740" in r.reason
    assert "0.0700" in r.reason


def test_negotiation_result_est_immuable():
    r = negotiate(_assessment(best_vm="edge2", best_score=0.10), _offer())
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.decision = NegotiationDecision.AUCUNE_OPTION
