"""
Tests de l'objet formel SLOIntent.

SLOIntent matérialise le principe anti « téléphone arabe » : l'intention est
convertie une seule fois en SLOs, puis relayée telle quelle d'un provider à
l'autre. Ces tests verrouillent l'immuabilité, la trace de relais et la
fidélité du round-trip JSON.
"""

import dataclasses
import json

import pytest

from shared.models import SLO, SLOIntent


# ── Fixtures ──────────────────────────────────────────────────

def _slo_latence() -> SLO:
    return SLO(
        metric="latency",
        operator="<",
        threshold=100.0,
        unit="ms",
        weight=1.0,
        is_primary=True,
    )


def _slo_cpu() -> SLO:
    return SLO(
        metric="cpu_usage",
        operator="<",
        threshold=80.0,
        unit="%",
        weight=0.4,
        confidence=0.75,
    )


def _intent(**overrides) -> SLOIntent:
    params = {
        "intent_id":   "intent-001",
        "slos":        (_slo_latence(), _slo_cpu()),
        "mode":        "enhanced",
        "created_at":  "2026-07-21T10:00:00",
        "source_text": "je veux un service web rapide",
    }
    params.update(overrides)
    return SLOIntent(**params)


# ── Construction et validation ────────────────────────────────

def test_construction_valide():
    intent = _intent()
    assert intent.intent_id == "intent-001"
    assert len(intent.slos) == 2
    assert intent.mode == "enhanced"
    assert intent.attempted_providers == ()
    assert intent.service is None


def test_intent_id_vide_leve_valueerror():
    with pytest.raises(ValueError):
        _intent(intent_id="")


def test_intent_id_blanc_leve_valueerror():
    with pytest.raises(ValueError):
        _intent(intent_id="   ")


def test_slos_vide_leve_valueerror():
    with pytest.raises(ValueError):
        _intent(slos=())


def test_mode_invalide_leve_valueerror():
    with pytest.raises(ValueError):
        _intent(mode="turbo")


@pytest.mark.parametrize("mode", ["autonomous", "enhanced"])
def test_modes_valides_acceptes(mode):
    assert _intent(mode=mode).mode == mode


def test_attempted_providers_avec_doublon_leve_valueerror():
    with pytest.raises(ValueError):
        _intent(attempted_providers=("provider-1", "provider-1"))


# ── Normalisation et immuabilité ──────────────────────────────

def test_liste_de_slos_convertie_en_tuple():
    intent = _intent(slos=[_slo_latence()])
    assert isinstance(intent.slos, tuple)


def test_liste_de_providers_convertie_en_tuple():
    intent = _intent(attempted_providers=["provider-1"])
    assert isinstance(intent.attempted_providers, tuple)
    assert intent.attempted_providers == ("provider-1",)


def test_reassignation_de_champ_interdite():
    intent = _intent()
    with pytest.raises(dataclasses.FrozenInstanceError):
        intent.intent_id = "autre"


def test_slos_non_extensible():
    """`slos` est un tuple : impossible d'ajouter ou retirer un SLO."""
    intent = _intent()
    assert not hasattr(intent.slos, "append")


# ── Trace de relais ───────────────────────────────────────────

def test_has_attempted():
    intent = _intent(attempted_providers=("provider-1",))
    assert intent.has_attempted("provider-1") is True
    assert intent.has_attempted("provider-2") is False


def test_with_attempt_retourne_une_nouvelle_instance_sans_muter_l_original():
    original = _intent()
    assert original.attempted_providers == ()

    releve = original.with_attempt("provider-1")

    assert releve is not original
    assert releve.attempted_providers == ("provider-1",)
    # L'original doit être strictement inchangé.
    assert original.attempted_providers == ()


def test_with_attempt_chainable_et_ordonne():
    intent = _intent().with_attempt("provider-1").with_attempt("provider-2")
    assert intent.attempted_providers == ("provider-1", "provider-2")


def test_with_attempt_preserve_les_autres_champs():
    original = _intent()
    releve = original.with_attempt("provider-1")
    assert releve.intent_id == original.intent_id
    assert releve.mode == original.mode
    assert releve.created_at == original.created_at
    assert releve.source_text == original.source_text
    assert releve.slos == original.slos


def test_with_attempt_sur_provider_deja_tente_leve_valueerror():
    intent = _intent(attempted_providers=("provider-1",))
    with pytest.raises(ValueError):
        intent.with_attempt("provider-1")


# ── Sérialisation ─────────────────────────────────────────────

def test_to_dict_est_serialisable_json():
    payload = json.dumps(_intent(attempted_providers=("provider-1",)).to_dict())
    assert isinstance(payload, str)


def test_round_trip_fidele_sur_tous_les_champs():
    original = _intent(
        service="web-app",
        attempted_providers=("provider-1",),
    )
    reconstruit = SLOIntent.from_dict(json.loads(json.dumps(original.to_dict())))

    assert reconstruit.intent_id == original.intent_id
    assert reconstruit.mode == original.mode
    assert reconstruit.created_at == original.created_at
    assert reconstruit.source_text == original.source_text
    assert reconstruit.service == original.service
    assert reconstruit.attempted_providers == original.attempted_providers
    assert reconstruit == original


def test_round_trip_preserve_le_detail_des_slos():
    original = _intent()
    reconstruit = SLOIntent.from_dict(original.to_dict())

    assert len(reconstruit.slos) == len(original.slos)
    for avant, apres in zip(original.slos, reconstruit.slos):
        assert apres.dict() == avant.dict()

    latence = reconstruit.slos[0]
    assert latence.metric == "latency"
    assert latence.threshold == 100.0
    assert latence.weight == 1.0
    assert latence.is_primary is True


def test_from_dict_sans_champs_optionnels():
    """Un payload minimal (sans source_text/service/attempted) reste valide."""
    reconstruit = SLOIntent.from_dict(
        {
            "intent_id":  "intent-002",
            "slos":       [_slo_latence().dict()],
            "mode":       "autonomous",
            "created_at": "2026-07-21T11:00:00",
        }
    )
    assert reconstruit.source_text is None
    assert reconstruit.service is None
    assert reconstruit.attempted_providers == ()
