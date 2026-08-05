"""
Tests de la vue de fédération (lot 8a, couche données) :
  • services/federation_view/replay.py — rebuild_candidates, replay_topsis
    (avec son contrôle de non-divergence contre le TOPSIS RÉEL de
    production), extract_gap_grade_steps.
  • services/federation_view/app.py — /api/cycles, /api/state, /api/cycle,
    dégradation gracieuse quand un provider est injoignable.

Ce service est LECTURE SEULE : ces tests mockent httpx.AsyncClient.get (le
seul verbe utilisé), jamais .post/.put/.delete.
"""

import json as json_module

import httpx
import pytest
from fastapi.testclient import TestClient

from shared import config
from services.decision_intelligence.topsis import TopsisSelector
from services.federation_view.replay import (
    extract_gap_grade_steps,
    rebuild_candidates,
    replay_topsis,
)
from services.federation_view import app as fv_app

client = TestClient(fv_app.app)


# ── Helpers ───────────────────────────────────────────────────

def _slo(metric, operator, threshold, weight, is_primary=True, unit="ms") -> dict:
    return {
        "metric": metric, "operator": operator, "threshold": threshold,
        "unit": unit, "weight": weight, "is_primary": is_primary,
        "target": threshold, "budget_remaining": 100.0, "violations": 0,
        "confidence": 1.0,
    }


_SLOS_LAT_CPU = [
    _slo("latency",   "<",  40.0, 0.6),
    _slo("cpu_usage", ">=", 2.0,  0.4, unit="cores"),
]


def _current_metrics_two_vms() -> dict:
    return {
        "edge1":  {"latency": 20.0, "cpu_usage": 30.0, "ram_usage": 40.0,
                   "total_cores": 4, "total_ram_gb": 8},
        "cloud1": {"latency": 25.0, "cpu_usage": 20.0, "ram_usage": 40.0,
                   "total_cores": 8, "total_ram_gb": 16},
    }


def _predictions_two_vms() -> dict:
    return {
        "edge1":  {"latency": {"predictions": [20.0, 20.0, 20.0]},
                   "cpu_usage": {"predictions": [30.0, 30.0, 30.0]}},
        "cloud1": {"latency": {"predictions": [25.0, 25.0, 25.0]},
                   "cpu_usage": {"predictions": [20.0, 20.0, 20.0]}},
    }


def _audit_entry_two_compliant(cycle=5, extra_vm=False) -> dict:
    compliant = ["edge1", "cloud1"]
    metrics = _current_metrics_two_vms()
    preds = _predictions_two_vms()
    if extra_vm:
        # VM non conforme, présente dans les mesures mais PAS dans compliant_vms.
        metrics["edge2"] = {"latency": 90.0, "cpu_usage": 5.0, "ram_usage": 5.0,
                             "total_cores": 4, "total_ram_gb": 8}
        preds["edge2"] = {"latency": {"predictions": [90.0]}}
    return {
        "cycle": cycle,
        "decision": "stay",
        "from_vm": None,
        "to_vm": "edge1",
        "provider_path": "A",
        "provider_used": "provider-1",
        "current_metrics": metrics,
        "predictions_map": preds,
        "slos_active": _SLOS_LAT_CPU,
        "reasoning": {
            "compliant_vms": compliant,
            "bids": [
                {
                    "provider_id": "provider-1",
                    "placement_plan": {"provider_id": "provider-1", "vm_id": "edge1"},
                    "gap_grade": {
                        "value": -0.11, "is_compliant": True, "evaluable": True,
                        "coverage": ["latency", "cpu_usage"],
                        "detail": {"latency": -0.16, "cpu_usage": -0.28},
                    },
                },
            ],
        },
        "received_at": "2026-08-04T10:00:00+00:00",
    }


class _FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    @property
    def text(self):
        return json_module.dumps(self._body)


def _install_fake_get(monkeypatch, responses: dict = None, raises: dict = None):
    """
    Faux httpx.AsyncClient.get, routé PAR SOUS-CHAÎNE d'URL vers une
    _FakeResponse (dict `responses`) ou une exception à lever (dict
    `raises`). Défaut : 404 vide si rien ne correspond.
    """
    responses = responses or {}
    raises    = raises or {}

    async def fake_get(self, url, timeout=None):
        for needle, exc in raises.items():
            if needle in url:
                raise exc
        for needle, resp in responses.items():
            if needle in url:
                return resp
        return _FakeResponse(404, {})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


@pytest.fixture(autouse=True)
def _patch_targets(monkeypatch):
    """Table de cibles déterministe, indépendante de l'environnement réel."""
    monkeypatch.setattr(config, "FEDERATION_VIEW_TARGETS", {
        "provider-1": {"hub": "http://hub-p1", "observability": "http://obs-p1"},
        "provider-2": {"hub": "http://hub-p2", "observability": "http://obs-p2"},
    })


# ═══════════════════════════════════════════════════════════════
#  1. rebuild_candidates
# ═══════════════════════════════════════════════════════════════

def test_1_rebuild_candidates_ne_garde_que_les_conformes():
    entry = _audit_entry_two_compliant(extra_vm=True)

    candidates = rebuild_candidates(entry)

    vm_ids = {c["vm_id"] for c in candidates}
    assert vm_ids == {"edge1", "cloud1"}   # edge2 (non conforme) exclue
    for c in candidates:
        assert set(c.keys()) == {"vm_id", "latency", "cpu_usage", "ram_usage",
                                  "total_cores", "total_ram_gb"}


# ═══════════════════════════════════════════════════════════════
#  2-3. replay_topsis — 4 phases, consistent, ⭐ scores identiques à select()
# ═══════════════════════════════════════════════════════════════

def test_2_replay_topsis_quatre_phases_consistent_true():
    entry = _audit_entry_two_compliant()

    result = replay_topsis(entry)

    assert len(result["phases"]) == 4
    names = [p["name"] for p in result["phases"]]
    assert names == ["matrice", "normalisation", "ponderation", "distances_et_score"]
    assert result["consistent"] is True


def test_3_scores_rejoues_identiques_a_select_production():
    entry = _audit_entry_two_compliant()

    result = replay_topsis(entry)

    candidates = rebuild_candidates(entry)
    _, _, prod_vm_scores = TopsisSelector().select(
        candidates, entry["predictions_map"], entry["slos_active"], {},
    )

    assert result["vm_scores"].keys() == prod_vm_scores.keys()
    for vm_id, replayed in result["vm_scores"].items():
        assert replayed == pytest.approx(prod_vm_scores[vm_id], abs=1e-6)


# ═══════════════════════════════════════════════════════════════
#  4-6. Cas limites
# ═══════════════════════════════════════════════════════════════

def test_4_zero_candidat_phases_vides_sans_exception():
    entry = _audit_entry_two_compliant()
    entry["reasoning"]["compliant_vms"] = []

    result = replay_topsis(entry)   # ne doit pas lever

    assert result["phases"] == []
    assert result["reason"] == "aucune VM conforme"


def test_5_un_seul_candidat_reason_explicite():
    entry = _audit_entry_two_compliant()
    entry["reasoning"]["compliant_vms"] = ["edge1"]

    result = replay_topsis(entry)

    assert result["phases"] == []
    assert result["reason"] == "candidat unique — score 1.0 par défaut"


def test_6_predictions_map_absent_reason_explicite_sans_exception():
    entry = _audit_entry_two_compliant()
    del entry["predictions_map"]

    result = replay_topsis(entry)   # ne doit pas lever

    assert result["phases"] == []
    assert result["reason"] == "prédictions non archivées pour ce cycle"


def test_6b_predictions_map_none_meme_comportement():
    entry = _audit_entry_two_compliant()
    entry["predictions_map"] = None

    result = replay_topsis(entry)

    assert result["reason"] == "prédictions non archivées pour ce cycle"


# ═══════════════════════════════════════════════════════════════
#  7. extract_gap_grade_steps
# ═══════════════════════════════════════════════════════════════

def test_7_extract_gap_grade_steps_seulement_primaires_valeurs_identiques():
    entry = _audit_entry_two_compliant()
    bid = entry["reasoning"]["bids"][0]
    slos_mixed = _SLOS_LAT_CPU + [_slo("ram_usage", "<", 80.0, 0.0, is_primary=False, unit="%")]

    result = extract_gap_grade_steps(bid, slos_mixed)

    metrics = {s["metric"] for s in result["steps"]}
    assert metrics == {"latency", "cpu_usage"}   # ram_usage (secondaire) exclue
    assert result["value"] == bid["gap_grade"]["value"]
    assert result["is_compliant"] == bid["gap_grade"]["is_compliant"]
    for step in result["steps"]:
        assert step["delta"] == bid["gap_grade"]["detail"][step["metric"]]   # aucun recalcul


# ═══════════════════════════════════════════════════════════════
#  8. /api/cycles — fusion + tri
# ═══════════════════════════════════════════════════════════════

def test_8_api_cycles_fusionne_et_trie(monkeypatch):
    _install_fake_get(monkeypatch, responses={
        "obs-p1/audit/log": _FakeResponse(200, {"log": [
            {"cycle": 5, "provider_path": "A", "decision": "stay",
             "from_vm": None, "to_vm": "edge1", "received_at": "t5"},
            {"cycle": 3, "provider_path": "A", "decision": "stay",
             "from_vm": None, "to_vm": "edge1", "received_at": "t3"},
        ]}),
        "obs-p2/audit/log": _FakeResponse(200, {"log": [
            {"cycle": 7, "provider_path": "B", "decision": "migrate",
             "from_vm": "edge2", "to_vm": "cloud1", "received_at": "t7"},
        ]}),
    })

    r = client.get("/api/cycles")

    assert r.status_code == 200
    cycles = r.json()["cycles"]
    assert [(c["provider_id"], c["cycle"]) for c in cycles] == [
        ("provider-2", 7), ("provider-1", 5), ("provider-1", 3),
    ]


# ═══════════════════════════════════════════════════════════════
#  9. Un provider injoignable → HTTP 200, l'autre exploitable
# ═══════════════════════════════════════════════════════════════

def test_9_provider_injoignable_http_200_autre_exploitable(monkeypatch):
    _install_fake_get(
        monkeypatch,
        responses={"hub-p1/status": _FakeResponse(200, {"service_vm": "edge1"}),
                   "hub-p1/data":   _FakeResponse(200, {"vms": {}})},
        raises={"hub-p2": httpx.ConnectError("connection refused")},
    )

    r = client.get("/api/state")

    assert r.status_code == 200
    body = r.json()["providers"]
    assert body["provider-1"]["reachable"] is True
    assert body["provider-1"]["status"]["service_vm"] == "edge1"
    assert body["provider-2"]["reachable"] is False
    assert "error" in body["provider-2"]


def test_9b_api_cycles_provider_injoignable_http_200_autre_exploitable(monkeypatch):
    _install_fake_get(
        monkeypatch,
        responses={"obs-p1/audit/log": _FakeResponse(200, {"log": [
            {"cycle": 1, "provider_path": "A", "decision": "stay",
             "from_vm": None, "to_vm": "edge1", "received_at": "t1"},
        ]})},
        raises={"obs-p2": httpx.ConnectError("connection refused")},
    )

    r = client.get("/api/cycles")

    assert r.status_code == 200
    body = r.json()
    assert len(body["cycles"]) == 1
    assert body["cycles"][0]["provider_id"] == "provider-1"
    assert any(e["provider_id"] == "provider-2" for e in body["errors"])


# ═══════════════════════════════════════════════════════════════
#  10. /api/cycle/... — TROIS causes d'échec distinctes (lot 8b, §1.1)
# ═══════════════════════════════════════════════════════════════

def test_10_cycle_inexistant_404_message_distinct(monkeypatch):
    _install_fake_get(monkeypatch, responses={
        "obs-p1/audit/log": _FakeResponse(200, {"log": [
            {"cycle": 1, "provider_path": "A", "decision": "stay"},
        ]}),
    })

    r = client.get("/api/cycle/provider-1/999")

    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "cycle absent du journal"
    assert body["cycle"] == 999


def test_10b_provider_inconnu_404_message_distinct():
    r = client.get("/api/cycle/provider-42/1")

    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "provider inconnu"
    assert body["provider_id"] == "provider-42"


def test_10c_observability_injoignable_503_pas_404(monkeypatch):
    """
    ⭐ lot 8b : distinct du cas "cycle absent" — un pair simplement tombé ne
    doit JAMAIS s'afficher comme "cycle inexistant".
    """
    _install_fake_get(monkeypatch, raises={
        "obs-p1": httpx.ConnectError("connection refused"),
    })

    r = client.get("/api/cycle/provider-1/1")

    assert r.status_code == 503   # jamais 404, jamais 500
    assert r.json()["error"] == "observability injoignable"


def test_10d_cycle_existant_reussi_contient_replay_et_gap_grades(monkeypatch):
    entry = _audit_entry_two_compliant(cycle=42)
    _install_fake_get(monkeypatch, responses={
        "obs-p1/audit/log": _FakeResponse(200, {"log": [entry]}),
    })

    r = client.get("/api/cycle/provider-1/42")

    assert r.status_code == 200
    body = r.json()
    assert body["entry"]["cycle"] == 42
    assert len(body["replay"]["phases"]) == 4
    assert body["replay"]["consistent"] is True
    assert len(body["gap_grades"]) == 1
    assert body["gap_grades"][0]["provider_id"] == "provider-1"


# ═══════════════════════════════════════════════════════════════
#  Santé
# ═══════════════════════════════════════════════════════════════

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy", "service": "federation_view"}


# ═══════════════════════════════════════════════════════════════
#  Page HTML (lot 8b, §3)
# ═══════════════════════════════════════════════════════════════

def test_11_index_200_html():
    r = client.get("/")

    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_12_index_contient_les_7_sections():
    r = client.get("/")
    body = r.text

    for anchor in [
        "section-header",
        "section-timebar",
        "section-pipeline",
        "section-gate",
        "section-topsis",
        "section-gapgrade",
        "section-arbitrage",
    ]:
        assert f'id="{anchor}"' in body, f"ancre manquante : {anchor}"


# ═══════════════════════════════════════════════════════════════
#  Page HTML — LIVE suit le dernier cycle, GATE explicative,
#  Gap Grade 5 étapes (lot 10)
# ═══════════════════════════════════════════════════════════════

def test_13_js_suivi_automatique_live_present():
    r = client.get("/")
    body = r.text

    # Marqueurs de la logique de suivi automatique du dernier cycle en LIVE.
    assert "followLastCycle" in body
    assert "liveCycleKey" in body
    assert "dernier cycle, suivi en direct" in body


def test_14_texte_attente_premier_cycle_present():
    r = client.get("/")
    assert "en attente du premier cycle de décision" in r.text


def test_15_texte_signal_proactif_gate_present():
    r = client.get("/")
    body = r.text
    assert "Gate ouverte par un signal proactif" in body
    assert "LA GATE est volontairement plus sensible que le test" in body


def test_16_cinq_libelles_etapes_gap_grade_presents():
    r = client.get("/")
    body = r.text

    for label in [
        "Filtrer les primaires",
        "Écarts signés",
        "Plancher δ ≥ −1",
        "Normalisation des poids",
        "Tchebycheff",
    ]:
        assert label in body, f"libellé d'étape manquant : {label}"


def test_17_mention_poids_normalise_presente():
    r = client.get("/")
    assert "poids normalisé" in r.text
