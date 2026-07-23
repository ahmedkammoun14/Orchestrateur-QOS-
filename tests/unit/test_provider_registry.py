"""
Tests du registre de providers (partition transversale).

Le registre est purement DÉCLARATIF à ce stade : on vérifie sa cohérence
et le lookup inverse, sans aucun impact runtime.
"""

import pytest

from shared import config
from shared.models import validate_provider_registry


# ── Couverture du parc ────────────────────────────────────────

def test_registry_couvre_exactement_les_vms_du_vm_registry():
    """Chaque VM de VM_REGISTRY est couverte, aucune VM étrangère déclarée."""
    declarees = [
        vm_id
        for profile in config.PROVIDER_REGISTRY.values()
        for vm_id in profile["vms"]
    ]
    assert set(declarees) == set(config.VM_REGISTRY.keys())


def test_registry_sans_doublon():
    """Aucune VM n'apparaît deux fois (dans un même provider ou entre providers)."""
    declarees = [
        vm_id
        for profile in config.PROVIDER_REGISTRY.values()
        for vm_id in profile["vms"]
    ]
    assert len(declarees) == len(set(declarees))


def test_provider_est_transversal_edge_et_cloud():
    """
    Axe provider ORTHOGONAL à l'axe cluster : chaque provider possède
    au moins une VM edge-cluster et une VM cloud-cluster.
    """
    for provider_id, profile in config.PROVIDER_REGISTRY.items():
        clusters = {config.VM_CLUSTER_MAP[vm_id] for vm_id in profile["vms"]}
        assert clusters == {"edge-cluster", "cloud-cluster"}, provider_id


# ── Lookup inverse ────────────────────────────────────────────

@pytest.mark.parametrize(
    "vm_id, provider_id",
    [
        ("edge1",  "provider-1"),
        ("cloud1", "provider-1"),
        ("edge2",  "provider-2"),
        ("cloud2", "provider-2"),
    ],
)
def test_provider_of_vm(vm_id, provider_id):
    assert config.PROVIDER_OF_VM[vm_id] == provider_id


def test_provider_of_vm_couvre_tout_le_parc():
    assert set(config.PROVIDER_OF_VM.keys()) == set(config.VM_REGISTRY.keys())


# ── Validation ────────────────────────────────────────────────

def test_validation_ok_sur_le_registre_reel():
    """Le registre réel est cohérent : aucune exception."""
    validate_provider_registry()


def test_validation_rejette_vm_inconnue(monkeypatch):
    monkeypatch.setattr(
        config,
        "PROVIDER_REGISTRY",
        {
            "provider-1": {"vms": ["edge1", "cloud1"]},
            "provider-2": {"vms": ["edge2", "cloud2", "edge99"]},
        },
    )
    with pytest.raises(ValueError):
        validate_provider_registry()


def test_validation_rejette_vm_en_double(monkeypatch):
    monkeypatch.setattr(
        config,
        "PROVIDER_REGISTRY",
        {
            "provider-1": {"vms": ["edge1", "cloud1"]},
            "provider-2": {"vms": ["edge1", "edge2", "cloud2"]},
        },
    )
    with pytest.raises(ValueError):
        validate_provider_registry()


def test_validation_rejette_vm_manquante(monkeypatch):
    monkeypatch.setattr(
        config,
        "PROVIDER_REGISTRY",
        {
            "provider-1": {"vms": ["edge1", "cloud1"]},
            "provider-2": {"vms": ["edge2"]},          # cloud2 non couverte
        },
    )
    with pytest.raises(ValueError):
        validate_provider_registry()


def test_validation_rejette_provider_vide(monkeypatch):
    monkeypatch.setattr(
        config,
        "PROVIDER_REGISTRY",
        {
            "provider-1": {"vms": ["edge1", "cloud1", "edge2", "cloud2"]},
            "provider-2": {"vms": []},
        },
    )
    with pytest.raises(ValueError):
        validate_provider_registry()


def test_registre_restaure_apres_monkeypatch():
    """Garde-fou : les patches précédents n'ont pas fui hors de leur test."""
    validate_provider_registry()
    assert set(config.PROVIDER_REGISTRY.keys()) == {"provider-1", "provider-2"}
