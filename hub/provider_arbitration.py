"""
Arbitrage inter-provider — module PUR (aucune I/O, aucun réseau, aucun état).

RÔLE EXACT DE TOPSIS, ET DONC RÔLE DE CE MODULE
────────────────────────────────────────────────
TOPSIS ne sert QU'À départager des VMs qui respectent DÉJÀ les SLOs, à
l'intérieur d'un MÊME provider. Ce module ne choisit donc JAMAIS parmi les
VMs conformes : il PARTITIONNE.

  • provider avec ≥ 1 VM conforme → on rend `compliant_vms` ; c'est l'appelant
    (le hub) qui passe CE sous-ensemble à TOPSIS pour trancher.
  • provider avec 0 VM conforme   → il passe la main ; on rend une offre de
    repli (`ProviderOffer`) pour la négociation inter-provider.

POURQUOI UN SCORE DE VIOLATION ET PAS UN SCORE TOPSIS
─────────────────────────────────────────────────────
Les scores TOPSIS ne sont PAS comparables entre providers : TOPSIS normalise
en min-max À L'INTÉRIEUR de son pool, donc deux scores issus de pools
différents sont mesurés contre des solutions idéales différentes. Un 0.9 chez
provider-1 et un 0.9 chez provider-2 ne désignent pas la même qualité.

La comparaison inter-provider se fait donc sur un score de VIOLATION absolu,
relatif aux seuils SLO — lesquels sont globaux et partagés par les deux
providers, ce qui rend les offres commensurables. Plus bas = meilleur.

Ce score est une MOYENNE pondérée des excès relatifs, pas une somme :

    score = Σ (wᵢ × excèsᵢ) / Σ wᵢ      sur les seuls SLOs retenus

La division par la somme des poids effectivement utilisés rend le score
invariant au NOMBRE de métriques réellement évaluées. C'est une condition
nécessaire à la comparabilité des offres : sans elle, un SLO écarté (métrique
non valorisable, seuil ≤ 0) retirerait sa part de poids du total et abaisserait
le score, si bien qu'un provider mal instrumenté remporterait la négociation
par manque de données plutôt que par qualité de service.

NÉGOCIATION ET DEAD-BAND ANTI-PING-PONG
────────────────────────────────────────
Quand AUCUN des deux providers n'a de VM conforme, chacun propose sa VM de
violation minimale et `negotiate()` tranche entre les deux offres. TOPSIS
n'intervient pas : ses scores ne sont pas comparables entre pools.

Une migration INTER-provider est nettement plus coûteuse qu'un déplacement
interne (le service change de parc, donc d'opérateur). Un écart de score
infime, dû au bruit des prédictions ML, ne doit pas suffire à la déclencher :
sinon le service oscille d'un provider à l'autre à chaque cycle. Le provider
en place (tenant) conserve donc le service sauf si le challenger le bat d'un
écart net — le DEAD-BAND (NEGOTIATION_DEADBAND) — dans le même esprit
anti-ping-pong que _MIGRATION_MARGIN côté décision intra-provider.

Ce dead-band est ABSOLU (`challenger < tenant − deadband`) et non relatif
(`challenger < tenant × (1 − marge)`). Le score de violation étant déjà un
excès relatif au seuil, une marge en pourcentage DU SCORE n'exigerait presque
rien près du seuil et beaucoup trop loin de lui : pour un seuil à 100 ms, une
marge relative de 5 % réclame 0,05 ms d'amélioration à un tenant mesuré à
101 ms — du bruit de mesure — mais 10 ms à un tenant à 300 ms. Le dead-band
absolu réclame 5 ms dans les deux cas, ce qui correspond à ce qu'un opérateur
appelle une amélioration réelle.

Cette protection n'a de sens que s'il Y A un tenant : au déploiement initial
(`incumbent_provider_id=None`) la comparaison est stricte, sans dead-band —
il n'y a rien à protéger contre l'oscillation.

Ce module réutilise les primitives d'évaluation existantes du pipeline
(`vm_satisfies_slo`, `calculate_weighted_mean`, `_to_criterion_value`) pour
rester cohérent avec la décision intra-provider. Il n'invoque JAMAIS le
classement TOPSIS lui-même (méthode `select`) : ce n'est pas son rôle.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from shared import config
from services.decision_intelligence.topsis import TopsisSelector, vm_satisfies_slo

# Instance unique et sans état : TopsisSelector est stateless, on ne s'en sert
# ici que comme porteur des primitives d'évaluation (moyenne pondérée,
# conversion capacité). Aucun classement n'est déclenché.
_SELECTOR = TopsisSelector()

# Écart de score de violation minimal pour qu'un challenger arrache le service au
# provider en place. Exprimé en fraction du SEUIL SLO : 0.05 = « il faut gagner au
# moins 5 % du seuil » (5 ms pour un seuil à 100 ms). Un écart ABSOLU, et non
# relatif : le score étant lui-même un excès relatif au seuil, une marge en
# pourcentage du score n'exigerait presque rien près du seuil et beaucoup trop loin
# de lui. Même esprit que _TIE_THRESHOLD de topsis.py, qui neutralise déjà les
# écarts négligeables pour éviter de polariser du bruit.
NEGOTIATION_DEADBAND: float = 0.05

# Unités de SLO exprimées en ressource absolue (intention LLM mode enhanced)
# plutôt qu'en % : la valeur brute doit être convertie en disponibilité réelle
# de la VM avant toute comparaison au seuil. Même règle que _filter_candidates.
_ABSOLUTE_UNITS = ("cores", "GB")

# ── GAP GRADE v2 (lot 2) ────────────────────────────────────────
# Plancher de l'écart normalisé, appliqué UNIQUEMENT du côté MARGE.
# Les critères de COÛT (latence, opérateur <) sont déjà bornés à −1 par
# la physique : v ≥ 0 implique (v − τ)/τ ≥ −1. Les critères de BÉNÉFICE
# (disponibilité cpu/ram, opérateur ≥) ne le sont pas : une VM à 12.8
# cœurs face à un seuil de 2.5 donne δ = −4.12. Ce plancher rend aux
# critères de bénéfice la borne que les critères de coût possèdent déjà,
# et restitue aux poids du LLM leur signification.
# Le côté VIOLATION (δ > 0) reste volontairement NON borné : une
# violation catastrophique DOIT dominer.
DELTA_FLOOR: float = -1.0

# Coefficient d'augmentation de la fonction de Tchebycheff. Le terme max()
# décide (non compensatoire) ; ce petit terme additif restaure la
# discrimination entre deux VMs dont le pire critère est identique, sans
# rendre de pouvoir compensatoire. ρ trop grand ferait dériver la formule
# vers la somme pondérée, et la compensation reviendrait.
GAP_GRADE_RHO: float = 0.1


# ── Structures de résultat ────────────────────────────────────

@dataclass(frozen=True)
class VMEvaluation:
    """Évaluation d'une VM au regard des SLOs, sans aucun classement."""
    vm_id:           str
    is_compliant:    bool               # satisfait TOUS les SLOs (critère _filter_candidates)
    violation_score: float              # magnitude, pour classer les non-conformes
    evaluable:       bool               # au moins une métrique valorisée
    has_predictions: bool               # toutes les métriques SLO avaient des prédictions
    detail:          Dict[str, float]   # excès par métrique (traçabilité / logs)


@dataclass(frozen=True)
class ProviderOffer:
    """
    Offre de repli d'un provider — SEUL objet traversant le réseau lors de la
    négociation (Cas 5). Volontairement scalaire : le provider distant n'apprend
    rien du parc de l'émetteur, seulement « ma meilleure VM coûte X ».
    """
    provider_id:     str
    vm_id:           str
    violation_score: float

    def to_dict(self) -> Dict[str, Any]:
        """Dict directement sérialisable par json.dumps (relais HTTP futur)."""
        return {
            "provider_id":     self.provider_id,
            "vm_id":           self.vm_id,
            "violation_score": self.violation_score,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderOffer":
        """Reconstruit une offre reçue d'un provider distant."""
        return cls(
            provider_id     = data["provider_id"],
            vm_id           = data["vm_id"],
            violation_score = float(data["violation_score"]),
        )


@dataclass(frozen=True)
class ProviderAssessment:
    """
    Résultat d'évaluation d'un provider. PARTITIONNE, ne décide pas.

    - `compliant_vms` non vide → l'appelant applique TOPSIS SUR CES VMs.
    - `compliant_vms` vide     → l'appelant utilise `to_offer()` pour négocier.
    """
    provider_id:       str
    evaluations:       Tuple[VMEvaluation, ...]
    compliant_vms:     Tuple[str, ...]     # ordre d'entrée préservé (déterminisme)
    best_effort_vm:    Optional[str]       # VM de violation MINIMALE
    best_effort_score: Optional[float]
    is_compliant:      bool                # len(compliant_vms) > 0
    evaluable:         bool                # au moins une VM évaluable

    def to_offer(self) -> Optional[ProviderOffer]:
        """
        Offre de repli à soumettre à la négociation. None si le provider n'a
        rien d'évaluable : on ne négocie pas sur des données absentes.
        """
        if self.best_effort_vm is None or self.best_effort_score is None:
            return None
        return ProviderOffer(
            provider_id     = self.provider_id,
            vm_id           = self.best_effort_vm,
            violation_score = self.best_effort_score,
        )


# ── Bid unifié (lot 3) — PlacementPlan + Gap Grade ─────────────
#
# Objet d'échange entre orchestrateurs pour l'arbitrage N-providers (lot 5).
# Chaque provider élit LUI-MÊME sa meilleure VM (TOPSIS si conforme, Gap
# Grade sinon) et calcule LUI-MÊME son Gap Grade — un microservice arbitre
# compare ensuite ces bids sur la seule base du Gap Grade, jamais du score
# TOPSIS (non comparable entre pools, voir en tête de ce module).

def placement_action(vm_id: Optional[str], incumbent_vm: Optional[str]) -> str:
    """
    Règle déterministe de PlacementPlan.action :
        vm_id is None            → "none"    (rien à proposer)
        incumbent_vm is None     → "deploy"  (aucun service en place)
        vm_id == incumbent_vm    → "stay"    (déjà en place)
        sinon                    → "migrate"
    """
    if vm_id is None:
        return "none"
    if incumbent_vm is None:
        return "deploy"
    if vm_id == incumbent_vm:
        return "stay"
    return "migrate"


@dataclass(frozen=True)
class PlacementPlan:
    """VM élue par CE provider et action qui en découle. AUCUNE migration ici : c'est une proposition."""
    provider_id:  str
    vm_id:        Optional[str]          # None si aucun plan (rien d'évaluable)
    action:       str                    # "migrate" | "stay" | "deploy" | "none"
    topsis_score: Optional[float]
    vm_scores:    Dict[str, float]       # classement complet — AUDIT SEULEMENT, jamais comparé entre providers
    reason:       str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id":  self.provider_id,
            "vm_id":        self.vm_id,
            "action":       self.action,
            "topsis_score": self.topsis_score,
            "vm_scores":    dict(self.vm_scores),
            "reason":       self.reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlacementPlan":
        return cls(
            provider_id  = data["provider_id"],
            vm_id        = data.get("vm_id"),
            action       = data["action"],
            topsis_score = data.get("topsis_score"),
            vm_scores    = dict(data.get("vm_scores") or {}),
            reason       = data.get("reason", ""),
        )


@dataclass(frozen=True)
class GapGrade:
    """
    Grandeur COMPARABLE entre providers (normalisée par le seuil SLO, global
    et partagé — contrairement au score TOPSIS). Voir compute_gap_grade.

    ⚠️ `value` NE PERMET PAS de conclure à la conformité (son signe peut être
    négatif malgré une violation, voir compute_gap_grade) : `is_compliant`
    est un champ SÉPARÉ, jamais déduit de `value`.
    """
    value:        Optional[float]        # None si non évaluable
    is_compliant: bool
    evaluable:    bool
    coverage:     Tuple[str, ...]        # métriques réellement évaluées
    detail:       Dict[str, float]       # δ signé (après plancher) par métrique

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value":        self.value,
            "is_compliant": self.is_compliant,
            "evaluable":    self.evaluable,
            "coverage":     list(self.coverage),
            "detail":       dict(self.detail),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GapGrade":
        return cls(
            value        = data.get("value"),
            is_compliant = bool(data.get("is_compliant", False)),
            evaluable    = bool(data.get("evaluable", False)),
            coverage     = tuple(data.get("coverage") or ()),
            detail       = dict(data.get("detail") or {}),
        )


@dataclass(frozen=True)
class ProviderBid:
    """Offre complète d'un provider pour un jeu de SLOs donné — SEUL objet destiné à traverser le réseau (lot 4)."""
    provider_id:    str
    intent_id:      Optional[str]
    placement_plan: PlacementPlan
    gap_grade:      GapGrade
    timestamp:      str                   # ISO 8601 UTC

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id":    self.provider_id,
            "intent_id":      self.intent_id,
            "placement_plan": self.placement_plan.to_dict(),
            "gap_grade":      self.gap_grade.to_dict(),
            "timestamp":      self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderBid":
        return cls(
            provider_id    = data["provider_id"],
            intent_id      = data.get("intent_id"),
            placement_plan = PlacementPlan.from_dict(data["placement_plan"]),
            gap_grade      = GapGrade.from_dict(data["gap_grade"]),
            timestamp      = data["timestamp"],
        )


# ── Normalisation des entrées ─────────────────────────────────

def _as_slo_dict(slo: Any) -> Dict[str, Any]:
    """
    Accepte indifféremment un objet `shared.models.SLO` ou un dict : le hub
    manipule des dicts, metrics_manager des objets. `vm_satisfies_slo` attend
    un dict (accès par clé), on normalise donc en amont.
    """
    if isinstance(slo, dict):
        return slo
    return slo.dict()


def _applicable_slos(slos: List[Any]) -> List[Dict[str, Any]]:
    """
    SLOs retenus : ceux dont la métrique est connue de METRICS_REGISTRY.
    Même garde que topsis.py et violation_detector.py — un SLO portant sur une
    métrique non instrumentée est ignoré silencieusement, pas une erreur.
    """
    return [
        d for d in (_as_slo_dict(s) for s in slos)
        if d.get("metric") in config.METRICS_REGISTRY
    ]


# ── Cadrage par provider ──────────────────────────────────────

def candidates_for_provider(
    provider_id: str,
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Restreint une liste de candidats (dicts avec au moins "vm_id") aux SEULES VMs du
    provider, d'après config.PROVIDER_REGISTRY. Ordre d'entrée préservé.
    Lève ValueError si provider_id inconnu.

    ISOLATION : aucune exception pour la « VM active ». Un provider ne voit que ses
    propres VMs — si le service tourne ailleurs, cette VM n'est pas candidate ici.
    Un orchestrateur distant ne verrait littéralement pas les VMs de l'autre provider.
    """
    profile = config.PROVIDER_REGISTRY.get(provider_id)
    if profile is None:
        raise ValueError(
            f"provider '{provider_id}' inconnu de PROVIDER_REGISTRY "
            f"{sorted(config.PROVIDER_REGISTRY.keys())}"
        )
    owned = set(profile.get("vms") or ())
    return [c for c in candidates if c.get("vm_id") in owned]


# ── Valeur représentative d'une métrique, pour une VM ─────────

def _representative_value(
    vm_id: str,
    metric: str,
    slo: Dict[str, Any],
    candidate: Dict[str, Any],
    predictions_map: Dict[str, Any],
) -> Tuple[Optional[float], bool]:
    """
    Retourne (valeur, from_predictions) pour une métrique donnée sur une VM.
    Valeur None = métrique non valorisable (ni prédiction ni mesure).

    La valeur issue des prédictions est la MOYENNE PONDÉRÉE (poids n, n−1, …, 1)
    et non le max : c'est la convention du pipeline (calculate_weighted_mean),
    la reproduire à l'identique évite un arbitrage incohérent avec la décision.
    """
    preds_raw: List[Any] = (
        predictions_map
        .get(vm_id, {})
        .get(metric, {})
        .get("predictions", [])
    ) or []
    preds: List[float] = [p for p in preds_raw if p is not None]

    if preds:
        value:            Optional[float] = _SELECTOR.calculate_weighted_mean(preds)
        from_predictions: bool            = True
    else:
        # Repli sur la dernière mesure propagée par le collector. Contrairement
        # à topsis.py, on ne fabrique PAS de valeur par défaut à partir de
        # default_threshold : il faut pouvoir distinguer « pas d'information »
        # (VM non évaluable) d'une vraie mesure.
        meta        = config.METRICS_REGISTRY[metric]
        payload_key = meta.get("payload_key", metric)
        raw         = candidate.get(payload_key)
        value            = float(raw) if raw is not None else None
        from_predictions = False

    if value is None:
        return None, from_predictions

    # SLO exprimé en ressource absolue (cœurs/Go) : convertir l'usage % en
    # disponibilité réelle de CETTE VM avant comparaison au seuil — sinon on
    # compare un % à un nombre de cœurs. Même conversion que le pipeline.
    if slo.get("unit") in _ABSOLUTE_UNITS:
        value = _SELECTOR._to_criterion_value(metric, candidate, value)

    return value, from_predictions


def to_slo_unit(
    metric:    str,
    slo:       Dict[str, Any],
    candidate: Dict[str, Any],
    value:     float,
) -> float:
    """
    Convertit une valeur BRUTE (telle que prédite ou mesurée — un POURCENTAGE
    pour cpu_usage/ram_usage) vers l'unité DU SLO, pour qu'elle soit
    comparable à son seuil.

    Extrait la conversion déjà appliquée par _representative_value afin de
    l'utiliser sur une valeur ISOLÉE : LA GATE
    (orchestrator_core._step5_check_violations) doit tester CHACUNE des 7
    prédictions séparément, pas leur moyenne pondérée.

    ⚠️ UNE SEULE implémentation de la conversion dans tout le projet — deux
    conversions divergentes seraient pires que l'absence de conversion.
    Si `candidate` ne porte pas la capacité de la VM (VM injoignable, agent
    non à jour), _to_criterion_value retombe sur le % brut : même repli
    silencieux qu'ailleurs, jamais d'exception.
    """
    if slo.get("unit") in _ABSOLUTE_UNITS:
        return _SELECTOR._to_criterion_value(metric, candidate, value)
    return value


def _excess(value: float, threshold: float, operator: str) -> float:
    """
    Dépassement RELATIF du seuil, toujours ≥ 0 (0 = seuil respecté).
    Normaliser par le seuil rend les excès de métriques hétérogènes
    (ms, %, cœurs) additionnables et comparables entre providers.
    """
    if operator in ("<", "<="):
        return max(0.0, (value - threshold) / threshold)
    if operator in (">", ">="):
        return max(0.0, (threshold - value) / threshold)
    return 0.0


# ── GAP GRADE v2 — fonctions pures, NON BRANCHÉES (lot 2) ─────
#
# ⚠️ AJOUT PUR : ni signed_excess ni compute_gap_grade ne sont appelées par
# evaluate_vm, evaluate_provider, negotiate ou tout autre code de
# production. _excess et violation_score restent le chemin actif ; ce
# nouveau chemin sera branché dans un lot ultérieur. Aucun comportement
# runtime ne change avec cet ajout.

def signed_excess(value: float, threshold: float, operator: str) -> float:
    """
    Écart SIGNÉ et normalisé par le seuil, plancher côté marge (voir
    DELTA_FLOOR). δ < 0 = marge sous l'objectif · δ > 0 = violation ·
    PLUS BAS = MEILLEUR.

    Contrairement à _excess (toujours ≥ 0, utilisée par evaluate_vm), cette
    fonction conserve le signe : c'est ce qui permet à compute_gap_grade de
    départager deux VMs conformes, que _excess écrase toutes les deux à 0.0.
    """
    if threshold <= 0:
        # Normalisation relative indéfinie.
        return 0.0
    if operator in ("<", "<="):
        delta = (value - threshold) / threshold
    elif operator in (">", ">="):
        delta = (threshold - value) / threshold
    else:
        return 0.0
    return max(DELTA_FLOOR, delta)


def _retained_primary_slos(
    slos:   List[Any],
    values: Dict[str, Optional[float]],
) -> List[Tuple[Dict[str, Any], str, float, float]]:
    """
    SLOs retenus pour le Gap Grade, sous forme (slo, metric, threshold,
    value). Filtre : is_primary vrai · metric dans METRICS_REGISTRY ·
    threshold > 0 · values.get(metric) non None.

    Extrait de compute_gap_grade (lot 3) — build_gap_grade a besoin du MÊME
    filtrage pour produire `coverage`/`detail` ; dupliquer cette boucle
    créerait un risque de divergence silencieuse entre les deux.
    """
    retained: List[Tuple[Dict[str, Any], str, float, float]] = []
    for raw in slos:
        slo = _as_slo_dict(raw)
        if not slo.get("is_primary"):
            continue
        metric = slo.get("metric")
        if metric not in config.METRICS_REGISTRY:
            continue
        threshold_raw = slo.get("threshold")
        threshold     = float(threshold_raw) if threshold_raw is not None else 0.0
        if threshold <= 0:
            continue
        value = values.get(metric)
        if value is None:
            continue
        retained.append((slo, metric, threshold, value))
    return retained


def compute_gap_grade(
    slos: List[Any],
    values: Dict[str, Optional[float]],
    rho: float = GAP_GRADE_RHO,
) -> Optional[float]:
    """
    Gap Grade : agrégation NON COMPENSATOIRE des écarts signés des SLOs
    PRIMAIRES, par une fonction de Tchebycheff augmentée.

        G = ( max(wᵢ·δᵢ) + ρ · Σ(wᵢ·δᵢ) ) / (1 + ρ)

    Le terme max() est NON COMPENSATOIRE : c'est le PIRE critère pondéré qui
    décide, et un excédent sur un autre critère ne peut jamais le racheter
    (contrairement à une somme pondérée classique). Le terme ρ·Σ(...) ne sert
    qu'à DÉPARTAGER deux VMs dont le pire critère est à égalité — ρ est
    volontairement petit (GAP_GRADE_RHO) pour ne jamais rendre de pouvoir
    compensatoire au terme additif.

    ⚠️ Le SIGNE de G ne permet PAS de conclure à la conformité en multi-SLO :
    une VM peut violer un SLO secondaire dans le pire critère tout en restant
    G < 0 grâce à une large marge sur un autre critère pondéré plus lourd (le
    terme max() choisit le pire *pondéré*, pas le pire brut). Le drapeau
    is_compliant (vm_satisfies_slo) reste donc OBLIGATOIRE dans tout usage de
    ce score ; il ne doit JAMAIS être déduit du signe de G.

    Ne retient que les SLOs PRIMAIRES (is_primary vrai), de métrique connue
    de METRICS_REGISTRY, de seuil > 0, et pour lesquels `values` fournit une
    valeur non None. Retourne None si aucun SLO n'est retenu — jamais 0.0,
    qui est une valeur de Gap Grade légitime et serait indiscernable d'un
    « non évaluable ».

    Propriété de non-régression : avec un SEUL SLO primaire retenu, quel que
    soit son poids (> 0), compute_gap_grade retourne EXACTEMENT δ (après
    normalisation les poids valent 1, donc G = δ(1+ρ)/(1+ρ) = δ) — le mode
    AUTONOME (latence seule) est donc mathématiquement inchangé par rapport
    à un simple signed_excess.
    """
    retained = _retained_primary_slos(slos, values)

    if not retained:
        return None

    deltas  = [signed_excess(value, threshold, slo["operator"]) for slo, _, threshold, value in retained]
    weights = [float(slo.get("weight") or 0.0) for slo, _, _, _ in retained]

    total_weight = sum(weights)
    if total_weight <= 0:
        weights      = [1.0] * len(retained)
        total_weight = float(len(retained))

    # Normalisation OBLIGATOIRE : sans elle, un unique SLO de poids 0.6
    # renverrait 0.6·δ au lieu de δ, et la propriété de non-régression du
    # mode autonome (voir docstring ci-dessus) serait fausse.
    weights = [w / total_weight for w in weights]

    terms = [w * d for w, d in zip(weights, deltas)]
    return (max(terms) + rho * sum(terms)) / (1 + rho)


# ── Bid unifié (lot 3) — fonctions pures BRANCHÉES par /evaluate ─
#
# Contrairement à signed_excess/compute_gap_grade (lot 2, non branchées),
# ces deux fonctions SONT appelées par le nouvel endpoint /evaluate
# (hub/orchestrator_core.py). evaluate_vm/evaluate_provider/negotiate,
# eux, restent intégralement le chemin de /intent/relay — inchangés.

def slo_values_for_vm(
    vm_id:           str,
    slos:            List[Any],
    candidate:       Dict[str, Any],
    predictions_map: Dict[str, Any],
) -> Dict[str, Optional[float]]:
    """
    Construit {métrique -> valeur représentative} pour UNE VM, au format
    attendu par compute_gap_grade.

    ⚠️ Réutilise _representative_value — NE JAMAIS reconstruire les valeurs
    à la main. Un SLO peut être exprimé en ressource ABSOLUE (unit "cores"
    ou "GB") alors que le collector mesure un POURCENTAGE d'usage ;
    _representative_value applique déjà la conversion
    disponible = capacité_totale × (1 − usage/100) quand slo["unit"] est
    dans _ABSOLUTE_UNITS. Recalculer cette valeur sans passer par elle
    donnerait un Gap Grade silencieusement FAUX (aucune exception levée) —
    ex. comparer 20 (le %) à un seuil de 2.5 cœurs au lieu de 3.2 (les
    cœurs réellement disponibles).

    Ne retient que les SLOs primaires de métrique connue de METRICS_REGISTRY
    (même filtre que compute_gap_grade) ; valeur None si non valorisable.
    """
    values: Dict[str, Optional[float]] = {}
    for raw in slos:
        slo = _as_slo_dict(raw)
        if not slo.get("is_primary"):
            continue
        metric = slo.get("metric")
        if metric not in config.METRICS_REGISTRY:
            continue
        value, _from_predictions = _representative_value(
            vm_id, metric, slo, candidate, predictions_map
        )
        values[metric] = value
    return values


def build_gap_grade(
    slos:         List[Any],
    values:       Dict[str, Optional[float]],
    is_compliant: bool,
    evaluable:    bool,
) -> GapGrade:
    """
    Assemble la structure GapGrade à partir de valeurs déjà calculées
    (slo_values_for_vm). `is_compliant`/`evaluable` viennent TOUJOURS de la
    logique de conformité existante (evaluate_provider) — jamais déduits du
    signe de `value` (voir l'avertissement de compute_gap_grade).
    """
    retained = _retained_primary_slos(slos, values)
    value    = compute_gap_grade(slos, values)
    coverage = tuple(metric for _, metric, _, _ in retained)
    detail   = {
        metric: signed_excess(v, threshold, slo["operator"])
        for slo, metric, threshold, v in retained
    }
    return GapGrade(
        value        = value,
        is_compliant = is_compliant,
        evaluable    = evaluable,
        coverage     = coverage,
        detail       = detail,
    )


# ── Évaluation d'une VM ───────────────────────────────────────

def evaluate_vm(
    vm_id: str,
    slos: List[Any],
    candidate: Dict[str, Any],
    predictions_map: Dict[str, Any],
) -> VMEvaluation:
    """
    Évalue une VM : conformité (autorité = vm_satisfies_slo) et magnitude de
    violation (pour classer les non-conformes).

    `violation_score` est la MOYENNE pondérée des excès relatifs des SLOs
    retenus — Σ(wᵢ × excèsᵢ) / Σwᵢ — et non leur somme. Diviser par la somme
    des poids effectivement utilisés rend le score invariant au nombre de
    métriques évaluées : deux VMs au même excès relatif obtiennent le même
    score, que l'une ait deux métriques instrumentées et l'autre une seule.
    Sans cela, l'absence de données ferait baisser le score et avantagerait
    la VM (et le provider) la moins renseignée lors de la négociation.

    Deux notions PROCHES MAIS DISTINCTES, à ne pas confondre :
      • `is_compliant` vient de vm_satisfies_slo, jamais de `violation_score == 0`.
        Cas limite : valeur == seuil avec l'opérateur strict « < » → l'excès vaut
        0.0 alors que la VM n'est PAS conforme.
      • `violation_score` accepte la mesure en repli quand le ML est muet, alors
        que la conformité l'interdit (voir ci-dessous).
    """
    applicable = _applicable_slos(slos)

    detail:           Dict[str, float] = {}
    weights:          List[float]      = []
    excesses:         List[float]      = []
    valorised:        int              = 0
    all_predicted:    bool             = True
    satisfies_all:    bool             = True

    for slo in applicable:
        metric = slo["metric"]
        # ⚠️ La CONFORMITÉ ne porte QUE sur les SLOs PRIMAIRES — règle métier.
        # Un secondaire est issu du MI : son seuil est un percentile inféré
        # sur l'historique d'UNE VM, jamais un engagement pris envers le
        # client. Lui laisser un droit de veto écarte des VMs qui respectent
        # le contrat (ex. latence 32 ms < 40 ms recalée pour « cpu 0.46 <
        # 1.0 cœur »), et peut réduire un provider entier au silence puisque
        # sans VM conforme il ne propose plus aucune offre.
        # Les secondaires gardent TOUTE leur influence ailleurs : pondération
        # de TOPSIS (services/decision_intelligence/topsis.py, qui lit le
        # poids de tous les SLOs sans filtre) et violation_score ci-dessous.
        is_primary = bool(slo.get("is_primary"))
        value, from_predictions = _representative_value(
            vm_id, metric, slo, candidate, predictions_map
        )

        if not from_predictions:
            all_predicted = False
            # Règle _filter_candidates : absence de prédiction ⇒ NON conforme.
            # On ne promet pas le respect d'un SLO sur la foi d'une mesure
            # passée ; seule une prédiction engage sur l'état futur.
            if is_primary:
                satisfies_all = False

        if value is None:
            # Métrique non valorisable : ni prédiction ni mesure. Elle ne peut
            # ni confirmer la conformité, ni contribuer au score.
            if is_primary:
                satisfies_all = False
            continue

        valorised += 1

        if is_primary and not vm_satisfies_slo(value, slo):
            satisfies_all = False

        threshold_raw = slo.get("threshold")
        threshold     = float(threshold_raw) if threshold_raw is not None else 0.0
        if threshold <= 0:
            # Normalisation relative indéfinie : SLO écarté du score (et tracé
            # à part pour que l'exclusion reste visible dans les logs).
            detail[metric] = 0.0
            continue

        excess = _excess(value, threshold, slo["operator"])
        detail[metric] = excess
        excesses.append(excess)
        weights.append(float(slo.get("weight") or 0.0))

    # Somme des poids nulle (SLOs sans poids, ou poids tous à 0) : poids
    # uniformes plutôt qu'une division par zéro ou un score nul trompeur.
    # Appliqué AVANT la division, le dénominateur vaut alors le nombre de
    # SLOs retenus.
    if excesses and sum(weights) <= 0:
        weights = [1.0] * len(excesses)

    # Division par la somme des poids EFFECTIVEMENT utilisés : le score est une
    # moyenne pondérée, pas une somme. Sans cette normalisation, un SLO écarté
    # (métrique non valorisable, seuil ≤ 0) retirerait sa part de poids du total
    # et ferait baisser le score — une VM moins instrumentée paraîtrait
    # meilleure qu'une VM identique mieux instrumentée, et le provider le moins
    # renseigné remporterait la négociation.
    total_weight    = sum(weights)
    violation_score = (
        sum(w * e for w, e in zip(weights, excesses)) / total_weight
        if excesses else 0.0
    )
    evaluable       = valorised > 0

    return VMEvaluation(
        vm_id           = vm_id,
        # Une VM vacuellement conforme mais non évaluable (aucun SLO applicable)
        # ne doit pas être présentée comme un placement sûr.
        is_compliant    = satisfies_all and evaluable,
        violation_score = violation_score,
        evaluable       = evaluable,
        has_predictions = all_predicted,
        detail          = detail,
    )


# ── Évaluation d'un provider ──────────────────────────────────

def evaluate_provider(
    provider_id: str,
    slos: List[Any],
    candidates: List[Dict[str, Any]],
    predictions_map: Dict[str, Any],
) -> ProviderAssessment:
    """
    Évalue les VMs du provider et PARTITIONNE. Ne choisit pas parmi les conformes
    (c'est le rôle de TOPSIS, appelé par le hub).
    `best_effort_vm` = VM évaluable de score MINIMAL ; en cas d'égalité stricte, la
    première dans l'ordre d'entrée (déterminisme requis pour les tests).
    """
    owned = candidates_for_provider(provider_id, candidates)

    evaluations: Tuple[VMEvaluation, ...] = tuple(
        evaluate_vm(c["vm_id"], slos, c, predictions_map) for c in owned
    )

    evaluables = [e for e in evaluations if e.evaluable]

    # ── Neutralité « ML down » ────────────────────────────────
    # Aucune VM évaluable : on ne déclenche JAMAIS de passation ni de
    # négociation sur des données totalement absentes. L'absence
    # d'information n'est pas un échec du provider — le déclarer non
    # conforme relaierait l'intention à tort dès la moindre panne de
    # collecte. Cohérent avec le filet « ML down » du projet.
    if not evaluables:
        return ProviderAssessment(
            provider_id       = provider_id,
            evaluations       = evaluations,
            compliant_vms     = (),
            best_effort_vm    = None,
            best_effort_score = None,
            is_compliant      = True,
            evaluable         = False,
        )

    # Toutes les VMs conformes sont rendues : ce module n'en élit AUCUNE.
    # L'ordre d'entrée est préservé pour que le classement aval soit reproductible.
    compliant_vms: Tuple[str, ...] = tuple(e.vm_id for e in evaluations if e.is_compliant)

    # min() est stable : à score strictement égal, la première VM dans l'ordre
    # d'entrée l'emporte — pas d'arbitrage arbitraire dépendant du hasard.
    best = min(evaluables, key=lambda e: e.violation_score)

    return ProviderAssessment(
        provider_id       = provider_id,
        evaluations       = evaluations,
        compliant_vms     = compliant_vms,
        best_effort_vm    = best.vm_id,
        best_effort_score = best.violation_score,
        is_compliant      = len(compliant_vms) > 0,
        evaluable         = True,
    )


# ── Négociation inter-provider (Cas 5) ────────────────────────

class NegotiationDecision(str, Enum):
    """Issue de la négociation, vue du provider RECEVEUR."""
    PREND_LOCAL_CONFORME  = "prend_local_conforme"   # j'ai des VMs conformes → TOPSIS local
    PREND_LOCAL_MEILLEURE = "prend_local_meilleure"  # non conforme, mais je bats l'offre
    CEDE_A_L_OFFRE        = "cede_a_l_offre"         # l'offre reçue l'emporte
    AUCUNE_OPTION         = "aucune_option"          # ni l'un ni l'autre n'a d'option exploitable


@dataclass(frozen=True)
class NegotiationResult:
    """Verdict de la négociation. Aucune action : l'appelant l'applique."""
    decision:          NegotiationDecision
    winning_provider:  Optional[str]        # provider qui hérite du service
    winning_vm:        Optional[str]        # None si PREND_LOCAL_CONFORME (TOPSIS tranchera)
    compliant_vms:     Tuple[str, ...]      # non vide UNIQUEMENT si PREND_LOCAL_CONFORME
    local_score:       Optional[float]
    offered_score:     Optional[float]
    deadband_applied:  float                # dead-band réellement appliqué (0.0 si pas de tenant)
    reason:            str                  # phrase lisible en français, pour logs et audit

    def to_dict(self) -> Dict[str, Any]:
        """Dict directement sérialisable par json.dumps (réponse HTTP du relais)."""
        return {
            "decision":         self.decision.value,
            "winning_provider": self.winning_provider,
            "winning_vm":       self.winning_vm,
            "compliant_vms":    list(self.compliant_vms),
            "local_score":      self.local_score,
            "offered_score":    self.offered_score,
            "deadband_applied": self.deadband_applied,
            "reason":           self.reason,
        }


def negotiate(
    local: ProviderAssessment,
    received_offer: Optional[ProviderOffer],
    incumbent_provider_id: Optional[str] = None,
    deadband: float = NEGOTIATION_DEADBAND,
) -> NegotiationResult:
    """
    Tranche entre l'évaluation LOCALE et une offre reçue de l'autre provider.
    Fonction pure : elle décide, elle n'exécute rien.

    Ordre de décision :
      1. VMs conformes localement → elles l'emportent toujours ;
      2. aucune offre reçue → on se rabat sur le local s'il a quelque chose ;
      3. rien d'exploitable localement → on cède ;
      4. sinon, comparaison des deux scores de violation, dead-band compris.

    Le dead-band n'intervient QU'À l'étape 4, et seulement s'il y a un tenant :
    une VM conforme l'emporte toujours (étape 1), quel que soit son écart de
    score avec l'offre reçue.

    LIMITE CONNUE ET ASSUMÉE : les deux scores comparés peuvent avoir été
    calculés sur des JEUX DE MÉTRIQUES différents, si les prédictions manquent
    de façon inégale d'un provider à l'autre. Le score étant une moyenne
    pondérée normalisée, les deux valeurs restent comparables en ÉCHELLE, mais
    elles ne portent pas exactement sur les mêmes critères. Traitement
    volontairement reporté (il faudrait transporter dans ProviderOffer la liste
    des métriques effectivement évaluées, et n'arbitrer que sur leur
    intersection).
    """
    offered_score: Optional[float] = (
        received_offer.violation_score if received_offer is not None else None
    )
    local_score: Optional[float] = local.best_effort_score

    # ── 1. La conformité prime sur toute offre de repli ───────
    # Une VM conforme respecte TOUS les SLOs ; une offre reçue n'existe que
    # parce que son émetteur n'avait AUCUNE VM conforme. Aucun score de repli,
    # si bas soit-il, ne peut donc l'emporter sur une VM conforme.
    #
    # On teste `compliant_vms` et NON `is_compliant` : par la neutralité
    # « ML down », un provider dont aucune VM n'est évaluable porte
    # is_compliant=True alors qu'il n'a rien à proposer. Se fier à
    # is_compliant ferait prendre le service à un provider aveugle.
    if local.compliant_vms:
        return NegotiationResult(
            decision          = NegotiationDecision.PREND_LOCAL_CONFORME,
            winning_provider  = local.provider_id,
            winning_vm        = None,        # c'est TOPSIS qui élira la VM
            compliant_vms     = local.compliant_vms,
            local_score       = local_score,
            offered_score     = offered_score,
            deadband_applied  = 0.0,         # aucune comparaison : aucun dead-band
            reason            = (
                f"{local.provider_id} prend le service : "
                f"{len(local.compliant_vms)} VM(s) conforme(s) "
                f"{list(local.compliant_vms)} — TOPSIS départagera"
            ),
        )

    # ── 2. Aucune offre reçue ─────────────────────────────────
    if received_offer is None:
        if local.best_effort_vm is not None:
            return NegotiationResult(
                decision          = NegotiationDecision.PREND_LOCAL_MEILLEURE,
                winning_provider  = local.provider_id,
                winning_vm        = local.best_effort_vm,
                compliant_vms     = (),
                local_score       = local_score,
                offered_score     = None,
                deadband_applied  = 0.0,
                reason            = (
                    f"{local.provider_id} prend le service sur "
                    f"{local.best_effort_vm} ({local_score:.4f}) : "
                    f"aucune offre concurrente reçue"
                ),
            )
        return NegotiationResult(
            decision          = NegotiationDecision.AUCUNE_OPTION,
            winning_provider  = None,
            winning_vm        = None,
            compliant_vms     = (),
            local_score       = None,
            offered_score     = None,
            deadband_applied  = 0.0,
            reason            = (
                f"{local.provider_id} n'a aucune VM exploitable "
                f"et n'a reçu aucune offre"
            ),
        )

    # ── 3. Rien d'exploitable localement, mais une offre existe ─
    if local.best_effort_vm is None:
        return NegotiationResult(
            decision          = NegotiationDecision.CEDE_A_L_OFFRE,
            winning_provider  = received_offer.provider_id,
            winning_vm        = received_offer.vm_id,
            compliant_vms     = (),
            local_score       = None,
            offered_score     = offered_score,
            deadband_applied  = 0.0,
            reason            = (
                f"{local.provider_id} cède : aucune VM exploitable localement, "
                f"l'offre {received_offer.vm_id} ({offered_score:.4f}) de "
                f"{received_offer.provider_id} est la seule option"
            ),
        )

    # ── 4. Comparaison des deux offres ────────────────────────
    # Rappel : score plus bas = meilleur. Le dead-band ne joue qu'en faveur du
    # provider EN PLACE, et seulement s'il est partie prenante ici.
    if incumbent_provider_id == local.provider_id:
        # Le receveur est tenant : l'offre reçue doit le battre du dead-band.
        deadband_applied = deadband
        local_wins       = not (offered_score < local_score - deadband)
    elif incumbent_provider_id == received_offer.provider_id:
        # L'émetteur est tenant : le receveur doit le battre du dead-band.
        deadband_applied = deadband
        local_wins       = local_score < offered_score - deadband
    else:
        # Aucun tenant (déploiement initial), ou tenant étranger aux deux
        # parties : comparaison stricte, sans dead-band — il n'y a pas de
        # service en place à protéger de l'oscillation. À égalité exacte
        # l'offre l'emporte : déterminisme, et cela évite un relais inutile
        # puisque l'émetteur est le point d'entrée de l'intention.
        deadband_applied = 0.0
        local_wins       = local_score < offered_score

    deadband_txt = f"dead-band {deadband_applied:.4f}"

    if local_wins:
        return NegotiationResult(
            decision          = NegotiationDecision.PREND_LOCAL_MEILLEURE,
            winning_provider  = local.provider_id,
            winning_vm        = local.best_effort_vm,
            compliant_vms     = (),
            local_score       = local_score,
            offered_score     = offered_score,
            deadband_applied  = deadband_applied,
            reason            = (
                f"{local.provider_id} prend le service : sa meilleure VM "
                f"{local.best_effort_vm} ({local_score:.4f}) l'emporte sur "
                f"l'offre {received_offer.vm_id} ({offered_score:.4f}) de "
                f"{received_offer.provider_id} avec la {deadband_txt}"
            ),
        )

    return NegotiationResult(
        decision          = NegotiationDecision.CEDE_A_L_OFFRE,
        winning_provider  = received_offer.provider_id,
        winning_vm        = received_offer.vm_id,
        compliant_vms     = (),
        local_score       = local_score,
        offered_score     = offered_score,
        deadband_applied  = deadband_applied,
        reason            = (
            f"{local.provider_id} cède : sa meilleure VM "
            f"{local.best_effort_vm} ({local_score:.4f}) ne bat pas l'offre "
            f"{received_offer.vm_id} ({offered_score:.4f}) de "
            f"{received_offer.provider_id} avec la {deadband_txt}"
        ),
    )
