"""
services/placement_arbiter/arbiter.py — Logique pure de l'arbitrage de placement.

Module PUR : aucune I/O, aucun réseau, aucun état, aucun FastAPI. N'importe
QUE shared.config (pour l'ordre par défaut du registre de providers et les
valeurs de politique par défaut) — jamais hub/provider_arbitration.py, un
service ne connaît pas l'implémentation interne du hub.

PRINCIPE FONDATEUR — l'arbitre est volontairement le composant le plus
« bête » de l'architecture : il ne calcule rien, il ne normalise rien. Il
reçoit N Gap Grades déjà comparables (normalisés à la source par les seuils
SLO globaux, voir hub/provider_arbitration.py::compute_gap_grade) et
applique une politique lexicographique en 4 paliers :

    ⓪ filtre (évaluable · gap présent · vm proposée · conformité si "hard")
    ② classement (Gap Grade croissant, départage par provider_order)
    ③ dead-band anti-oscillation (protège le tenant en place)
    ④ départage déterministe (ordre du registre de providers)

⛔ INTERDIT ABSOLU : ce module ne lit JAMAIS topsis_score ni vm_scores. Ces
scores sont normalisés min-max À L'INTÉRIEUR du pool de leur provider — le
meilleur d'un pool vaut toujours ≈ 1.0 et le pire ≈ 0.0, quelles que soient
les valeurs absolues. Les comparer entre providers donnerait une réponse
FAUSSE. Seul le Gap Grade (gap_grade.value) est comparable entre providers.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from shared import config


# ── Structure de résultat ──────────────────────────────────────

@dataclass(frozen=True)
class ArbitrationVerdict:
    """Verdict de l'arbitrage. Fonction pure : décide, n'exécute rien."""
    decision:         str                   # "migrate" | "stay"
    winner_provider:  Optional[str]
    winner_vm:        Optional[str]
    gap_grade:        Optional[float]
    path:             str                   # "A" | "B" | "C" | "D" | "DEPLOY"
    reason:           str
    deadband_applied: float
    considered:       List[Dict[str, Any]]  # TOUS les bids reçus + raison
    alert:            Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        """Dict directement sérialisable par json.dumps (réponse HTTP)."""
        return {
            "decision":         self.decision,
            "winner_provider":  self.winner_provider,
            "winner_vm":        self.winner_vm,
            "gap_grade":        self.gap_grade,
            "path":             self.path,
            "reason":           self.reason,
            "deadband_applied": self.deadband_applied,
            "considered":       list(self.considered),
            "alert":            dict(self.alert) if self.alert is not None else None,
        }


# ── Extraction tolérante ───────────────────────────────────────

def _safe_get(d: Any, key: str, default: Any = None) -> Any:
    """.get() qui ne lève jamais, même si `d` n'est pas un dict."""
    return d.get(key, default) if isinstance(d, dict) else default


def _extract(bid: Dict[str, Any]) -> Dict[str, Any]:
    """
    Lecture tolérante d'un bid — jamais d'exception, quels que soient les
    champs manquants ou None. `bid` est supposé être un dict à ce stade
    (voir _classify, qui filtre les entrées non-dict en amont).
    """
    plan = _safe_get(bid, "placement_plan")
    gap  = _safe_get(bid, "gap_grade")
    return {
        "provider_id":  _safe_get(bid, "provider_id"),
        "vm_id":        _safe_get(plan, "vm_id"),
        "gap_value":    _safe_get(gap, "value"),
        "is_compliant": bool(_safe_get(gap, "is_compliant")),
        "evaluable":    bool(_safe_get(gap, "evaluable")),
    }


def _classify(bid: Any, enforcement: str) -> Tuple[Dict[str, Any], bool, str]:
    """
    Filtre (palier ⓪), ORDRE DES TESTS CRITIQUE :
        a) evaluable est True                     ← TOUJOURS testé EN PREMIER
        b) gap_value n'est pas None
        c) vm_id n'est pas None
        d) si enforcement == "hard" : is_compliant est True

    ⚠️ (a) DOIT précéder (d) : par la neutralité « ML down » de
    hub/provider_arbitration.py (evaluate_provider, ~lignes 416-425), un
    provider dont AUCUNE VM n'est évaluable renvoie is_compliant=True alors
    qu'il n'a strictement rien à proposer. Tester la conformité avant
    l'évaluabilité ferait gagner l'enchère à un provider AVEUGLE.
    """
    if not isinstance(bid, dict):
        empty = {"provider_id": None, "vm_id": None, "gap_value": None,
                  "is_compliant": False, "evaluable": False}
        return empty, False, "bid malformé"

    extracted = _extract(bid)

    if not extracted["evaluable"]:
        return extracted, False, "non évaluable"
    if extracted["gap_value"] is None:
        return extracted, False, "gap_grade absent"
    if extracted["vm_id"] is None:
        return extracted, False, "aucune VM proposée"
    if enforcement == "hard" and not extracted["is_compliant"]:
        return extracted, False, "non conforme (mode hard)"
    return extracted, True, "retenu"


# ── Arbitrage ───────────────────────────────────────────────────

def arbitrate(
    bids: List[Dict[str, Any]],
    incumbent_provider: Optional[str] = None,
    incumbent_vm: Optional[str] = None,
    enforcement: Optional[str] = None,
    deadband: Optional[float] = None,
    provider_order: Optional[Sequence[str]] = None,
) -> ArbitrationVerdict:
    """Arbitre entre N bids déjà collectés (voir hub POST /evaluate, lot 3, et
    provider_relay POST /broadcast, lot 4). Fonction pure et sans état :
    deux appels avec les mêmes arguments produisent toujours le même verdict.
    """
    enforcement = (enforcement or config.SLO_ENFORCEMENT).lower()
    deadband    = config.ARBITER_DEADBAND if deadband is None else deadband
    order       = list(provider_order) if provider_order is not None else list(config.PROVIDER_REGISTRY.keys())
    order_index = {pid: i for i, pid in enumerate(order)}

    def _tie_break_key(entry: Dict[str, Any]) -> Tuple[float, int]:
        return (entry["gap_value"], order_index.get(entry["provider_id"], len(order)))

    considered:    List[Dict[str, Any]] = []
    all_extracted: List[Dict[str, Any]] = []
    retained:      List[Dict[str, Any]] = []

    for bid in bids:
        extracted, is_retained, why = _classify(bid, enforcement)
        considered.append({
            "provider_id":  extracted["provider_id"],
            "gap_grade":    extracted["gap_value"],
            "is_compliant": extracted["is_compliant"],
            "evaluable":    extracted["evaluable"],
            "retained":     is_retained,
            "why":          why,
        })
        all_extracted.append(extracted)
        if is_retained:
            retained.append(extracted)

    # ── ③ Aucun bid retenu ──────────────────────────────────────
    if not retained:
        any_evaluable = any(e["evaluable"] for e in all_extracted)
        evaluable_with_gap = [
            e for e in all_extracted if e["evaluable"] and e["gap_value"] is not None
        ]
        best_effort: Optional[Dict[str, Any]] = None
        if evaluable_with_gap:
            be = min(evaluable_with_gap, key=_tie_break_key)
            best_effort = {
                "provider_id": be["provider_id"],
                "vm_id":       be["vm_id"],
                "gap_grade":   be["gap_value"],
            }

        path = "C" if any_evaluable else "D"
        kind = "INFAISABLE" if path == "C" else "SANS_DONNEES"
        message = (
            f"Aucun provider conforme — meilleure offre disponible : "
            f"{best_effort['provider_id']} sur {best_effort['vm_id']} "
            f"({best_effort['gap_grade']:.4f}) — refusée en mode {enforcement}"
        ) if best_effort else (
            "Aucun provider évaluable — aucune donnée disponible pour arbitrer"
        )

        return ArbitrationVerdict(
            decision="stay", winner_provider=None, winner_vm=None, gap_grade=None,
            path=path, reason=message, deadband_applied=0.0,
            considered=considered,
            alert={
                "kind":                kind,
                "best_effort":         best_effort,
                "providers_evaluated": [e["provider_id"] for e in all_extracted],
                "message":             message,
            },
        )

    # ── ④ Classement (palier ②) ──────────────────────────────────
    ranked = sorted(retained, key=_tie_break_key)
    best   = ranked[0]

    # ── ⑤ Dead-band (palier ③) ───────────────────────────────────
    incumbent_bid = next(
        (e for e in retained if e["provider_id"] == incumbent_provider), None
    ) if incumbent_provider is not None else None

    path: Optional[str] = None
    if incumbent_provider is None:
        winner, deadband_applied, path = best, 0.0, "DEPLOY"
    elif incumbent_bid is None:
        winner, deadband_applied = best, 0.0
    elif best["provider_id"] == incumbent_provider:
        winner, deadband_applied = best, 0.0
    else:
        deadband_applied = deadband
        if best["gap_value"] < incumbent_bid["gap_value"] - deadband:
            winner = best
        else:
            winner = incumbent_bid

    # ── ⑥ Chemin et décision ─────────────────────────────────────
    if path is None:
        path = "A" if winner["provider_id"] == incumbent_provider else "B"
    decision = "stay" if winner["vm_id"] == incumbent_vm else "migrate"

    # ── ⑦ Reason ──────────────────────────────────────────────────
    if incumbent_provider is None:
        reason = (
            f"{winner['provider_id']} prend le service sur {winner['vm_id']} "
            f"({winner['gap_value']:.4f}) : premier placement, aucun tenant à comparer"
        )
    elif incumbent_bid is None:
        reason = (
            f"{winner['provider_id']} prend le service sur {winner['vm_id']} "
            f"({winner['gap_value']:.4f}) : le tenant '{incumbent_provider}' n'a "
            f"aucun bid retenu, rien à protéger"
        )
    elif best["provider_id"] == incumbent_provider:
        reason = (
            f"{incumbent_provider} conserve le service sur {winner['vm_id']} "
            f"({winner['gap_value']:.4f}) : déjà le meilleur Gap Grade"
        )
    elif winner is best:
        reason = (
            f"{best['provider_id']} prend le service sur {best['vm_id']} "
            f"({best['gap_value']:.4f}) : bat {incumbent_provider} "
            f"({incumbent_bid['gap_value']:.4f}) au-delà du dead-band {deadband_applied:.4f}"
        )
    else:
        reason = (
            f"{incumbent_provider} conserve le service : {best['provider_id']} "
            f"({best['gap_value']:.4f}) ne bat pas {incumbent_bid['gap_value']:.4f} "
            f"du dead-band {deadband_applied:.4f} requis"
        )

    return ArbitrationVerdict(
        decision=decision,
        winner_provider=winner["provider_id"],
        winner_vm=winner["vm_id"],
        gap_grade=winner["gap_value"],
        path=path,
        reason=reason,
        deadband_applied=deadband_applied,
        considered=considered,
        alert=None,
    )
