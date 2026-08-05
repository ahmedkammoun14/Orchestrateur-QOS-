from dataclasses import dataclass, field, replace
from typing import List, Optional, Any, Dict, Tuple
from pydantic import BaseModel


# ── Dataclasses internes ──────────────────────────────────────

@dataclass
class RTTMeasurement:
    vm_id:     str
    ip:        str
    rtt_ms:    float
    raw_ms:    float
    reachable: bool
    timestamp: str


@dataclass
class SLO:
    """
    Service Level Objective.

    Champ `is_primary`:
        True  → objectif métier fixe (défini par l'utilisateur ou par
                config.METRICS_REGISTRY). Le seuil ne doit JAMAIS être
                recalculé statistiquement.
        False → SLO secondaire dérivé d'une corrélation MI. Le seuil
                est calculé dynamiquement par percentile adaptatif sur
                l'historique observé.
    """
    metric:           str
    operator:         str
    threshold:        float
    unit:             str
    weight:           float = 1.0
    target:           float = 0.0
    window:           str   = "5m"
    budget_remaining: float = 100.0
    violations:       int   = 0
    confidence:       float = 1.0
    is_primary:       bool  = False

    def dict(self) -> Dict[str, Any]:
        return {
            "metric":           self.metric,
            "operator":         self.operator,
            "threshold":        self.threshold,
            "unit":             self.unit,
            "weight":           self.weight,
            "target":           self.target,
            "window":           self.window,
            "budget_remaining": self.budget_remaining,
            "violations":       self.violations,
            "confidence":       self.confidence,
            "is_primary":       self.is_primary,
        }


# ── Pydantic models (FastAPI) ─────────────────────────────────

class RTTMeasurementModel(BaseModel):
    vm_id:     str
    ip:        str
    rtt_ms:    float
    raw_ms:    float
    reachable: bool
    timestamp: str


class LatencyPayload(BaseModel):
    timestamp:    str
    source:       str
    cycle:        int
    measurements: List[RTTMeasurementModel]


class IntentToHubPayload(BaseModel):
    intent_id:  str
    intention:  str
    slos:       List[Dict[str, Any]]
    timestamp:  str


# ── Validation du registre de providers ───────────────────────

def validate_provider_registry() -> None:
    """
    Valide la cohérence de config.PROVIDER_REGISTRY : chaque VM du parc
    GLOBAL (ALL_VM_REGISTRY) est couverte exactement une fois, aucune VM
    n'est assignée à deux providers, aucune VM inconnue n'est déclarée.
    Lève ValueError si incohérent.

    Contre ALL_VM_REGISTRY et non VM_REGISTRY : ce dernier est filtré par
    PROVIDER_ID (déploiement distribué, un processus par provider) et ne
    contient alors que 4 VMs — la validation casserait à tort si elle
    comparait PROVIDER_REGISTRY (topologie globale, 8 VMs) à ce sous-ensemble.

    NON invoquée au démarrage pour l'instant (registre purement déclaratif) —
    définie et testée, câblage éventuel dans une étape ultérieure.
    """
    from shared import config  # import différé : évite un cycle models <-> config

    seen: Dict[str, str] = {}
    for provider_id, profile in config.PROVIDER_REGISTRY.items():
        vms = profile.get("vms") or []
        if not vms:
            raise ValueError(f"{provider_id} : aucune VM déclarée")
        for vm_id in vms:
            if vm_id not in config.ALL_VM_REGISTRY:
                raise ValueError(f"{provider_id} : VM '{vm_id}' absente de ALL_VM_REGISTRY")
            if vm_id in seen:
                raise ValueError(
                    f"VM '{vm_id}' assignée à la fois à '{seen[vm_id]}' et '{provider_id}'"
                )
            seen[vm_id] = provider_id

    missing = set(config.VM_REGISTRY.keys()) - set(seen.keys())
    if missing:
        raise ValueError(f"VMs non couvertes par PROVIDER_REGISTRY : {sorted(missing)}")


# ── Intention formelle (objet relayé entre providers) ─────────

_MODES_VALIDES = ("autonomous", "enhanced")


@dataclass(frozen=True)
class SLOIntent:
    """
    Intention utilisateur CONVERTIE UNE SEULE FOIS en contraintes formelles.

    Principe fondateur (anti « téléphone arabe ») : la conversion langage
    naturel → SLOs a lieu une unique fois, à l'entrée du système. C'est CET
    objet qui est relayé d'un provider à l'autre, jamais le texte brut.

    ⚠️ `source_text` est conservé à des fins de PROVENANCE / AFFICHAGE
    uniquement (audit, dashboard). Aucun consommateur ne doit le
    ré-interpréter ni en ré-extraire des SLOs.

    Immuabilité : le conteneur est gelé (frozen) et `slos` est un tuple — on ne
    peut ni réassigner un champ, ni ajouter/retirer un SLO.
    LIMITE CONNUE ET ASSUMÉE : les objets SLO eux-mêmes restent mutables
    (services/metrics_manager modifie leurs champs weight/threshold) ; les
    geler casserait le code existant. On gèle le conteneur, pas le contenu.
    """
    intent_id:           str
    slos:                Tuple[SLO, ...]
    mode:                str                    # "autonomous" | "enhanced"
    created_at:          str                    # horodatage de la conversion UNIQUE
    source_text:         Optional[str]   = None # provenance seule — NE PAS ré-interpréter
    service:             Optional[str]   = None # prospectif : pas encore alimenté
    attempted_providers: Tuple[str, ...] = ()   # trace de relais (anti-boucle)

    def __post_init__(self) -> None:
        # Normalisation : on accepte une liste en entrée (confort d'appel) mais
        # on stocke des tuples. `object.__setattr__` est obligatoire ici : la
        # dataclass est frozen, l'assignation normale lèverait FrozenInstanceError.
        object.__setattr__(self, "slos", tuple(self.slos))
        object.__setattr__(self, "attempted_providers", tuple(self.attempted_providers))

        if not self.intent_id or not self.intent_id.strip():
            raise ValueError("intent_id : identifiant vide ou blanc")
        if not self.slos:
            raise ValueError(f"{self.intent_id} : aucun SLO déclaré")
        if self.mode not in _MODES_VALIDES:
            raise ValueError(
                f"{self.intent_id} : mode '{self.mode}' invalide "
                f"(attendu : {' | '.join(_MODES_VALIDES)})"
            )
        if len(set(self.attempted_providers)) != len(self.attempted_providers):
            raise ValueError(
                f"{self.intent_id} : doublon dans attempted_providers "
                f"{list(self.attempted_providers)}"
            )

    # ── Trace de relais ───────────────────────────────────────

    def has_attempted(self, provider_id: str) -> bool:
        """Ce provider a-t-il déjà été tenté pour cette intention ?"""
        return provider_id in self.attempted_providers

    def with_attempt(self, provider_id: str) -> "SLOIntent":
        """
        Retourne une NOUVELLE intention marquant `provider_id` comme tenté.
        L'instance d'origine n'est jamais mutée.

        Lève ValueError si le provider a déjà été tenté : re-tenter le même
        provider est un bug de la logique de relais, pas un cas nominal.
        """
        if self.has_attempted(provider_id):
            raise ValueError(
                f"{self.intent_id} : provider '{provider_id}' déjà tenté "
                f"{list(self.attempted_providers)}"
            )
        return replace(
            self,
            attempted_providers=self.attempted_providers + (provider_id,),
        )

    # ── Sérialisation (relais HTTP futur) ─────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Dict directement sérialisable par json.dumps."""
        return {
            "intent_id":           self.intent_id,
            "slos":                [slo.dict() for slo in self.slos],
            "mode":                self.mode,
            "created_at":          self.created_at,
            "source_text":         self.source_text,
            "service":             self.service,
            "attempted_providers": list(self.attempted_providers),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SLOIntent":
        """Reconstruit une intention depuis un dict issu de to_dict()."""
        return cls(
            intent_id=          data["intent_id"],
            slos=               tuple(SLO(**d) for d in data["slos"]),
            mode=               data["mode"],
            created_at=         data["created_at"],
            source_text=        data.get("source_text"),
            service=            data.get("service"),
            attempted_providers=tuple(data.get("attempted_providers") or ()),
        )
