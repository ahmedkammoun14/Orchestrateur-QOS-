"""
Tests du Volet 1 — exposition du classement TOPSIS complet (vm_scores) par
DecisionHandler.decide (services/decision_intelligence/decision.py).

Ajout strictement additif : `vm_scores` doit apparaître sur les décisions où
TOPSIS a réellement tourné (migrate, stay d'hystérésis), et rester ABSENT
partout où TOPSIS n'est jamais atteint (cooldown, absence de violation,
absence de candidat) — non-régression de la forme des retours existants.
"""

from services.decision_intelligence.decision import DecisionHandler


def _slo(metric="latency", operator="<", threshold=100.0, weight=1.0, is_primary=True):
    return {"metric": metric, "operator": operator, "threshold": threshold,
            "unit": "ms" if metric == "latency" else "%", "weight": weight,
            "is_primary": is_primary}


def _preds(*values):
    return {"predictions": list(values)}


def _payload(service_vm, current_data, predictions_map, slos, **overrides):
    payload = {
        "current_data":    current_data,
        "predictions_map": predictions_map,
        "slos":            slos,
        "service_vm":      service_vm,
    }
    payload.update(overrides)
    return payload


# ── 8. vm_scores sur une décision migrate ──────────────────────

def test_decide_renvoie_vm_scores_sur_migrate():
    handler = DecisionHandler()
    slos = [_slo(threshold=100.0)]
    current_data = [
        {"vm_id": "edge1", "total_cores": 4, "total_ram_gb": 8},
        {"vm_id": "cloud1", "total_cores": 8, "total_ram_gb": 16},
    ]
    predictions_map = {
        "edge1":  {"latency": _preds(150.0, 150.0, 150.0)},   # viole le seuil
        "cloud1": {"latency": _preds(40.0, 40.0, 40.0)},      # nettement meilleure
    }

    result = handler.decide(_payload("edge1", current_data, predictions_map, slos))

    assert result["decision"] == "migrate"
    assert "vm_scores" in result
    # cloud1 (seule VM conforme) fait autorité pour le classement : le
    # filtrage préalable (_filter_candidates) retire edge1, non conforme,
    # dès qu'au moins une VM satisfait les SLOs — vm_scores ne porte donc
    # que sur le pool réellement soumis à TOPSIS.
    assert "cloud1" in result["vm_scores"]


# ── 9. vm_scores sur un stay d'hystérésis ──────────────────────

def test_decide_renvoie_vm_scores_sur_stay_hysteresis():
    """
    edge1 (actif) et cloud1 sont assez proches pour que l'hystérésis
    (_MIGRATION_MARGIN) bloque la migration — TOPSIS a bien tourné
    (vm_scores existe) mais la décision reste STAY.

    ⚠️ Cette branche exige DEUX conditions simultanées, d'où les prédictions
    ci-dessous. Le détecteur déclare une violation dès qu'UN point de
    l'horizon dépasse le seuil (130 > 100), tandis que le filtre de candidats
    compare la MOYENNE PONDÉRÉE (96,7 < 100) et les garde donc conformes.
    Depuis le retrait du repli « best_effort » (24/08/2026), un parc
    entièrement non conforme ne produit plus aucun candidat : on part
    directement sur un STAY « contrat infaisable », sans TOPSIS ni vm_scores.
    """
    handler = DecisionHandler()
    slos = [_slo(threshold=100.0)]
    current_data = [
        {"vm_id": "edge1", "total_cores": 4, "total_ram_gb": 8},
        {"vm_id": "cloud1", "total_cores": 4, "total_ram_gb": 8},
    ]
    predictions_map = {
        # moyenne pondérée (poids 3,2,1) = 96,7 → conforme ; pic à 130 → violation
        "edge1":  {"latency": _preds(90.0, 90.0, 130.0)},
        "cloud1": {"latency": _preds(89.5, 89.5, 129.5)},     # écart sous le seuil
                                                               # de tie-break de TOPSIS
                                                               # (_TIE_THRESHOLD) → scores
                                                               # neutralisés, quasi identiques
    }

    result = handler.decide(_payload("edge1", current_data, predictions_map, slos))

    assert result["decision"] == "stay"
    assert "vm_scores" in result
    assert result["vm_scores"] is not None


# ── 10. Non-régression : vm_scores absent quand TOPSIS n'a pas tourné ──

def test_build_stay_sans_vm_scores_cooldown():
    handler = DecisionHandler()
    slos = [_slo(threshold=100.0)]
    result = handler.decide(_payload(
        "edge1", [{"vm_id": "edge1"}], {}, slos, cooldown_active=True,
    ))
    assert result["decision"] == "stay"
    assert "vm_scores" not in result


def test_build_stay_sans_vm_scores_aucune_violation():
    handler = DecisionHandler()
    slos = [_slo(threshold=100.0)]
    current_data = [{"vm_id": "edge1", "total_cores": 4, "total_ram_gb": 8}]
    predictions_map = {"edge1": {"latency": _preds(20.0, 20.0, 20.0)}}   # conforme

    result = handler.decide(_payload("edge1", current_data, predictions_map, slos))

    assert result["decision"] == "stay"
    assert "vm_scores" not in result


def test_build_stay_direct_sans_argument_vm_scores_forme_inchangee():
    """
    Appel direct de _build_stay sans le nouveau paramètre optionnel : la
    forme du dict renvoyé doit être EXACTEMENT celle d'avant l'ajout (mêmes
    clés, rien de plus).
    """
    result = DecisionHandler._build_stay(
        "No SLO violation detected", None, None, "2026-01-01T00:00:00",
    )
    assert set(result.keys()) == {
        "decision", "from_vm", "to_vm", "reason", "topsis_score",
        "breach_type", "violated_metrics", "timestamp",
    }
    assert "vm_scores" not in result
