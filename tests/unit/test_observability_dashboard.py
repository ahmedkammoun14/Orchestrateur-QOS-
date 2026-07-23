"""
Tests du dashboard observability (services/observability/app.py) — lisibilité
multi-provider : indicateur "Provider actif", colonne "Provider" du journal,
filtre "événements porteurs d'information", compteur de chemins.

Backend uniquement : le rendu visuel n'est pas exécuté ici (pas de
navigateur). On vérifie que le HTML servi contient bien les éléments requis,
et que /audit accepte/conserve/comptabilise les nouveaux champs sans casser
le mode mono-provider.

TestClient est instancié HORS context manager pour ne pas déclencher le
startup event (_poll_hub, qui interrogerait le hub réel toutes les 2 s).
"""

import pytest
from fastapi.testclient import TestClient

import services.observability.app as obs
from shared import config

client = TestClient(obs.app)


@pytest.fixture(autouse=True)
def _reset_observability_state():
    """
    `_audit_log` et `_provider_path_counts` sont des singletons module-level
    partagés par tout le process — sans ce fixture, un test polluerait les
    suivants.
    """
    obs._audit_log.clear()
    for k in obs._provider_path_counts:
        obs._provider_path_counts[k] = 0
    yield
    obs._audit_log.clear()
    for k in obs._provider_path_counts:
        obs._provider_path_counts[k] = 0


def _payload(path=None, decision="migrate", **overrides) -> dict:
    payload = {
        "decision":     decision,
        "from_vm":      "edge1",
        "to_vm":        "edge2",
        "reason":       "test",
        "topsis_score": 0.8,
        "breach_type":  "inter_provider_negotiation" if path else "reactive",
        "cycle":        1,
        "mode":         "autonomous",
    }
    if path:
        payload["provider_path"] = path
        payload["provider_used"] = "provider-2"
    payload.update(overrides)
    return payload


# ── 1. PROVIDER_OF_VM / PROVIDER_REGISTRY injectés ────────────

def test_dashboard_injecte_provider_of_vm_et_provider_registry():
    r = client.get("/")
    assert r.status_code == 200
    html = r.text

    assert "PROVIDER_OF_VM" in html
    assert "PROVIDER_REGISTRY" in html
    for vm_id in config.PROVIDER_OF_VM:
        assert vm_id in html
    assert set(config.PROVIDER_OF_VM.keys()) == {"edge1", "cloud1", "edge2", "cloud2"}


# ── 2. Tuile "Provider actif" ──────────────────────────────────

def test_dashboard_contient_la_tuile_provider_actif():
    html = client.get("/").text
    assert 'id="h-provider"' in html


# ── 3. Colonne "Provider" du journal ───────────────────────────

def test_dashboard_contient_la_colonne_provider():
    html = client.get("/").text
    assert "<th>Provider</th>" in html


# ── 4. Garde-fou anti-régression sur le filtre d'affichage ─────

def test_filtre_js_contient_le_garde_fou_chemin_d():
    html = client.get("/").text
    assert "!== 'D'" in html


# ── 5. Chemin D accepté et conservé ────────────────────────────

def test_audit_chemin_d_accepte_et_conserve():
    r = client.post("/audit", json=_payload(path="D", decision="stay"))
    assert r.status_code == 200

    stored = obs._audit_log[-1]
    assert stored["provider_path"] == "D"
    assert stored["provider_used"] == "provider-2"


# ── 6. Non-régression mono-provider ────────────────────────────

def test_audit_sans_provider_path_ne_contient_aucune_cle_provider():
    r = client.post("/audit", json=_payload())
    assert r.status_code == 200

    stored = obs._audit_log[-1]
    assert "provider_path" not in stored
    assert "provider_used" not in stored


# ── 7. Les 4 chemins acceptés et comptabilisés ─────────────────

@pytest.mark.parametrize("path,decision", [("A", "migrate"), ("B", "migrate"),
                                            ("C", "migrate"), ("D", "stay")])
def test_les_quatre_chemins_acceptes_et_comptabilises(path, decision):
    r = client.post("/audit", json=_payload(path=path, decision=decision))
    assert r.status_code == 200

    assert obs._provider_path_counts[path] == 1
    stored = obs._audit_log[-1]
    assert stored["provider_path"] == path


def test_health_toujours_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy", "service": "observability"}
