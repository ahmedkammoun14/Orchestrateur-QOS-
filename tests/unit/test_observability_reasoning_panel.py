"""
Tests du panneau "Raisonnement du cycle" (services/observability/app.py).

Backend uniquement : le rendu visuel n'est pas exécuté ici (pas de
navigateur). On vérifie que le HTML servi contient bien le nouveau panneau,
plus les deux blocs redondants supprimés, et que /audit accepte, conserve et
rediffuse intégralement les blocs `reasoning` (y compris `topsis` et
`negotiation`) sans casser le mode mono-provider.

TestClient est instancié HORS context manager pour ne pas déclencher le
startup event (_poll_hub, qui interrogerait le hub réel toutes les 2 s).
"""

import pytest
from fastapi.testclient import TestClient

import services.observability.app as obs

client = TestClient(obs.app)


@pytest.fixture(autouse=True)
def _reset_observability_state():
    """`_audit_log`/`_provider_path_counts` sont des singletons module-level."""
    obs._audit_log.clear()
    for k in obs._provider_path_counts:
        obs._provider_path_counts[k] = 0
    yield
    obs._audit_log.clear()
    for k in obs._provider_path_counts:
        obs._provider_path_counts[k] = 0


def _reasoning_chemin_a() -> dict:
    return {
        "provider_courant": "provider-2",
        "evaluations": [
            {"vm_id": "edge2", "violation_score": 0.0, "is_compliant": True,
             "evaluable": True, "detail": {"latency": 0.0}},
            {"vm_id": "cloud2", "violation_score": 0.0, "is_compliant": True,
             "evaluable": True, "detail": {"latency": 0.0}},
        ],
        "compliant_vms": ["edge2", "cloud2"],
        "negotiation": None,
        "topsis": {
            "classement": {"cloud2": 0.85, "edge2": 0.62},
            "retenue": "cloud2",
            "score": 0.85,
        },
    }


def _reasoning_chemin_c() -> dict:
    return {
        "provider_courant": "provider-2",
        "evaluations": [
            {"vm_id": "cloud2", "violation_score": 0.021, "is_compliant": False,
             "evaluable": True, "detail": {"ram_usage": 0.021}},
            {"vm_id": "edge2", "violation_score": 0.187, "is_compliant": False,
             "evaluable": True, "detail": {"ram_usage": 0.187}},
        ],
        "compliant_vms": [],
        "negotiation": {
            "offre_locale":   {"provider_id": "provider-2", "vm_id": "cloud2", "violation_score": 0.0215},
            "offre_recue":    {"provider_id": "provider-1", "vm_id": "cloud1", "violation_score": 0.3230},
            "deadband":       0.05,
            "decision":       "cede_a_l_offre",
            "provider_cible": "provider-1",
        },
        "topsis": {"classement": {}, "retenue": None, "score": None},
    }


def _audit_payload_a() -> dict:
    return {
        "decision": "migrate", "from_vm": "edge2", "to_vm": "cloud2",
        "reason": "proactive violation on latency — TOPSIS selected 'cloud2' (score=1.0)",
        "topsis_score": 0.85, "breach_type": "proactive",
        "violated_metrics": [{"metric": "latency", "weight": 0.62}],
        "cycle": 27, "mode": "autonomous",
        "slos_active": [
            {"metric": "latency", "operator": "<", "threshold": 100.0, "unit": "ms",
             "weight": 0.62, "is_primary": True},
            {"metric": "cpu_usage", "operator": "<", "threshold": 55.0, "unit": "%",
             "weight": 0.38, "is_primary": False},
        ],
        "mi_scores": {"cpu_usage": 0.31},
        "provider_path": "A", "provider_used": "provider-2",
        "reasoning": _reasoning_chemin_a(),
    }


def _audit_payload_c() -> dict:
    return {
        "decision": "stay", "from_vm": "cloud2", "to_vm": None,
        "reason": "provider-1 cède : sa meilleure VM cloud1 ne bat pas l'offre",
        "topsis_score": None, "breach_type": "inter_provider_negotiation",
        "violated_metrics": [],
        "cycle": 29, "mode": "autonomous",
        "slos_active": [
            {"metric": "ram_usage", "operator": "<", "threshold": 80.0, "unit": "%",
             "weight": 1.0, "is_primary": False},
        ],
        "mi_scores": {},
        "provider_path": "C", "provider_used": "provider-2",
        "reasoning": _reasoning_chemin_c(),
    }


# ── 1. Titre du nouveau panneau ────────────────────────────────

def test_dashboard_contient_le_panneau_de_raisonnement():
    r = client.get("/")
    assert r.status_code == 200
    assert "Raisonnement du cycle" in r.text


# ── 2. Blocs redondants supprimés ───────────────────────────────

def test_dashboard_ne_contient_plus_les_blocs_redondants():
    html = client.get("/").text
    assert "Latence — historique & prédictions" not in html
    assert "Détail des SLOs actifs" not in html


# ── 3. Poids SLOs actifs (TOPSIS) conservé ──────────────────────

def test_dashboard_conserve_poids_slos_actifs():
    html = client.get("/").text
    assert "Poids SLOs actifs (TOPSIS)" in html


# ── 4. Audit chemin A complet, conservé intégralement ──────────

def test_audit_chemin_a_avec_topsis_conserve_integralement():
    r = client.post("/audit", json=_audit_payload_a())
    assert r.status_code == 200

    stored = obs._audit_log[-1]
    assert stored["reasoning"]["topsis"]["classement"] == {"cloud2": 0.85, "edge2": 0.62}
    assert stored["reasoning"]["topsis"]["retenue"] == "cloud2"
    assert stored["reasoning"]["compliant_vms"] == ["edge2", "cloud2"]
    assert stored["reasoning"]["negotiation"] is None


# ── 5. Audit chemin C complet, conservé intégralement ──────────

def test_audit_chemin_c_avec_negotiation_conserve_integralement():
    r = client.post("/audit", json=_audit_payload_c())
    assert r.status_code == 200

    stored = obs._audit_log[-1]
    neg = stored["reasoning"]["negotiation"]
    assert neg["offre_locale"]["vm_id"] == "cloud2"
    assert neg["offre_recue"]["vm_id"] == "cloud1"
    assert neg["deadband"] == pytest.approx(0.05)
    assert neg["decision"] == "cede_a_l_offre"
    assert neg["provider_cible"] == "provider-1"


# ── 6. Non-régression mono-provider ─────────────────────────────

def test_audit_sans_reasoning_ne_gagne_aucune_cle_parasite():
    payload = {
        "decision": "stay", "from_vm": None, "to_vm": None,
        "reason": "No SLO violation detected", "topsis_score": None,
        "breach_type": "none", "cycle": 3, "mode": "autonomous",
    }
    r = client.post("/audit", json=payload)
    assert r.status_code == 200

    stored = obs._audit_log[-1]
    assert "reasoning" not in stored
    assert "provider_path" not in stored
    assert "provider_used" not in stored


# ── 7. Gardes JS pour topsis/negotiation absents ────────────────

def test_js_gere_topsis_et_negotiation_absents_sans_planter():
    """
    Vérifie la présence des gardes défensives dans le JS servi : le code doit
    tester l'existence de reasoning.topsis / reasoning.negotiation (et non les
    déréférencer aveuglément), et retomber sur un texte explicite plutôt
    qu'une valeur muette quand l'information manque.
    """
    html = client.get("/").text
    assert "information indisponible" in html
    # Gardes explicites sur les deux sous-blocs optionnels du reasoning.
    assert "r.topsis && r.topsis.classement" in html
    assert "if (!neg)" in html
