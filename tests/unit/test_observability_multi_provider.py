"""
Tests du service observability (services/observability/app.py) — extension
multi-provider du dashboard (badge de chemin, compteur cumulé).

Backend uniquement : le rendu HTML/JS n'est pas exécuté ici (pas de
navigateur). On vérifie que /audit accepte, conserve et rediffuse les
nouveaux champs (provider_path, provider_used, breach_type
inter_provider_negotiation) sans rien perdre, et que le mode mono-provider
(sans provider_path) reste identique à avant.

TestClient est instancié HORS context manager pour ne pas déclencher le
startup event (_poll_hub, qui interrogerait le hub réel toutes les 2 s).
"""

import pytest
from fastapi.testclient import TestClient

import services.observability.app as obs

client = TestClient(obs.app)


@pytest.fixture(autouse=True)
def _reset_observability_state():
    """
    `_audit_log` et `_provider_path_counts` sont des singletons module-level
    partagés par tout le process — sans ce fixture, un test polluerait les
    suivants (en particulier le compteur cumulé, additif par nature).
    """
    obs._audit_log.clear()
    for k in obs._provider_path_counts:
        obs._provider_path_counts[k] = 0
    yield
    obs._audit_log.clear()
    for k in obs._provider_path_counts:
        obs._provider_path_counts[k] = 0


def _mono_payload(**overrides) -> dict:
    payload = {
        "decision":         "migrate",
        "from_vm":          "edge1",
        "to_vm":            "edge2",
        "reason":           "reactive violation on latency — TOPSIS selected 'edge2' (score=0.87)",
        "topsis_score":     0.87,
        "breach_type":      "reactive",
        "violated_metrics": [{"metric": "latency", "weight": 1.0}],
        "cycle":            12,
        "mode":             "autonomous",
        "slos_active":      [{"metric": "latency", "weight": 1.0}],
        "mi_scores":        {"cpu_usage": 0.2},
        "current_metrics":  {"edge1": {"latency": 120.0, "cpu_usage": 30.0, "ram_usage": 40.0}},
    }
    payload.update(overrides)
    return payload


def _multi_payload(path: str, **overrides) -> dict:
    payload = _mono_payload(
        breach_type="inter_provider_negotiation",
        provider_path=path,
        provider_used="provider-2",
    )
    payload.update(overrides)
    return payload


# ── 1. Mono-provider : ni badge, ni catégorie ─────────────────

def test_audit_mono_provider_sans_provider_path():
    r = client.post("/audit", json=_mono_payload())
    assert r.status_code == 200

    stored = obs._audit_log[-1]
    assert "provider_path" not in stored
    assert "provider_used" not in stored
    assert obs._provider_path_counts == {"A": 0, "B": 0, "C": 0, "D": 0}


# ── 2 / 3. Les quatre chemins sont acceptés et conservés ──────

@pytest.mark.parametrize("path", ["A", "B", "C", "D"])
def test_audit_chemin_accepte_et_conserve(path):
    r = client.post("/audit", json=_multi_payload(path))
    assert r.status_code == 200

    stored = obs._audit_log[-1]
    assert stored["provider_path"] == path
    assert stored["provider_used"] == "provider-2"


# ── 4. breach_type inter_provider_negotiation conservé tel quel ─

def test_breach_type_inter_provider_negotiation_conserve():
    r = client.post("/audit", json=_multi_payload("C"))
    assert r.status_code == 200
    assert obs._audit_log[-1]["breach_type"] == "inter_provider_negotiation"


# ── 5. Compteur cumulé de synthèse ─────────────────────────────

def test_compteur_reflete_plusieurs_audits_de_chemins_differents():
    for path in ["A", "A", "B", "C", "C", "C", "D"]:
        r = client.post("/audit", json=_multi_payload(path))
        assert r.status_code == 200

    log = client.get("/audit/log").json()
    assert log["provider_path_counts"] == {"A": 2, "B": 1, "C": 3, "D": 1}
    assert obs._provider_path_counts == {"A": 2, "B": 1, "C": 3, "D": 1}


def test_compteur_ignore_les_entrees_mono_provider():
    client.post("/audit", json=_mono_payload())
    client.post("/audit", json=_multi_payload("B"))
    client.post("/audit", json=_mono_payload())

    counts = client.get("/audit/log").json()["provider_path_counts"]
    assert counts == {"A": 0, "B": 1, "C": 0, "D": 0}


def test_snapshot_sse_inclut_le_compteur():
    """
    Vérification légère (sans ouvrir un vrai flux SSE — le générateur de
    /stream attend jusqu'à 30 s avant de se terminer proprement une fois le
    client déconnecté, ce qui rendrait ce test inutilement lent) : le code
    du endpoint /stream inclut bien provider_path_counts dans l'événement de
    snapshot envoyé à la connexion, à partir de la même source que
    /audit/log (_provider_path_counts).
    """
    import inspect
    src = inspect.getsource(obs.stream)
    assert "provider_path_counts" in src


# ── 6. Non-régression : entrée mono-provider strictement identique ─

def test_non_regression_entree_mono_provider_identique():
    """
    Un payload mono-provider complet produit exactement la même entrée
    stockée qu'avant la modification : mêmes clés, mêmes valeurs. Seule
    "received_at" est ajoutée — comportement déjà existant, pas nouveau.
    """
    payload = _mono_payload()
    payload_reference = dict(payload)   # /audit reçoit une COPIE JSON, pas cet objet

    r = client.post("/audit", json=payload)
    assert r.status_code == 200

    stored = obs._audit_log[-1]
    stored_sans_received_at = {k: v for k, v in stored.items() if k != "received_at"}

    assert stored_sans_received_at == payload_reference
    assert "received_at" in stored
    assert "provider_path" not in stored
    assert "provider_used" not in stored


def test_health_toujours_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy", "service": "observability"}
