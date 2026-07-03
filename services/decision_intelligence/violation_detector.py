from typing import Any, Dict, List, Tuple
import logging

from shared import config
from services.decision_intelligence.topsis import TopsisSelector

logger = logging.getLogger("DecisionIntelligence.handler")

# Unités signalant un besoin en ressource ABSOLUE (cœurs/Go, intention LLM
# mode enhanced) plutôt qu'un % de charge (mode autonomous). Pour ces SLOs,
# operator est ">=" (plancher à ne pas franchir vers le bas) au lieu de "<"
# (plafond à ne pas dépasser) — la direction de violation s'inverse.
_ABSOLUTE_UNITS = ("cores", "GB")

_HIGH_UNCERTAINTY_THRESHOLD: float = 0.50
_HIGH_UNCERTAINTY_FACTOR:    float = 0.90


class ViolationDetector:
    """
    Unified SLO violation analyser for a single VM.
    Stateless — no I/O, no side effects.
    """

    def detect(
        self,
        current_data: List[Dict[str, Any]],
        predictions_map: Dict[str, Dict[str, Any]],
        slos: List[Dict[str, Any]],
        service_vm: str,
    ) -> List[Dict[str, Any]]:
        vm_data:  Dict[str, Any] = next(
            (v for v in current_data if v["vm_id"] == service_vm), {}
        )
        vm_preds: Dict[str, Any] = predictions_map.get(service_vm, {})

        violations: List[Dict[str, Any]] = []

        for slo in slos:
            metric: str = slo["metric"]
            if metric not in config.METRICS_REGISTRY:
                continue

            operator:     str           = slo.get("operator", "<")
            current_val:  float         = self._get_current_val(vm_data, metric)
            metric_entry: Dict[str, Any] = vm_preds.get(metric, {})
            preds:        List[float]   = metric_entry.get("predictions", [])
            uncertainty:  float         = float(metric_entry.get("uncertainty", 0.0))

            # SLO exprimé en ressource absolue (cœurs/Go) : la valeur brute
            # (%) et les prédictions doivent être converties en disponibilité
            # réelle de CETTE VM avant comparaison — même conversion que
            # celle utilisée côté filtre/scoring TOPSIS (_to_criterion_value).
            # Sinon on compare un % à un nombre de cœurs, non-sens.
            if slo.get("unit") in _ABSOLUTE_UNITS:
                current_val = TopsisSelector._to_criterion_value(metric, vm_data, current_val)
                preds = [
                    TopsisSelector._to_criterion_value(metric, vm_data, p)
                    for p in preds
                ]

            # Seuil contractuel — les prédictions ML sont comparées directement
            # à ce seuil sans facteur α : si l'API prédit une valeur future
            # qui viole le contrat (dépasse un plafond "<", ou tombe sous un
            # plancher ">="), c'est une violation proactive réelle.
            contract_threshold: float = float(slo["threshold"])
            threshold: float = contract_threshold
            is_floor: bool = operator in (">", ">=")

            breach_type, slope, time_to_breach = self._analyze(
                preds, threshold, current_val, operator
            )

            if breach_type != "none":
                # Pour une violation proactive, la sévérité se base sur le pire
                # cas prédit — le pic pour un plafond ("<"), le creux pour un
                # plancher (">=").
                peak_val = (min(preds) if is_floor else max(preds)) if preds else current_val
                ref_val  = peak_val if breach_type == "proactive" else current_val
                sev = self._severity(ref_val, threshold, slope, operator)
                violations.append({
                    "metric":              metric,
                    "breach_type":         breach_type,
                    "severity":            sev,
                    "slope":               slope,
                    "time_to_breach":      time_to_breach,
                    "current_val":         current_val,
                    "predicted_peak":      peak_val,
                    "threshold":           threshold,           # cible = seuil détection
                    "contract_threshold":  contract_threshold,  # seuil SLO contractuel
                })
                logger.debug(
                    f"⚠️  Violation — {metric:<12} "
                    f"type : {breach_type:<10} "
                    f"val : {current_val:.2f} / cible : {threshold:.2f} "
                    f"(contrat : {contract_threshold:.2f}) "
                    f"sévérité : {sev:.3f}  TTB : {time_to_breach}"
                )

        return violations

    def _analyze(
        self,
        preds: List[float],
        threshold: float,
        current_val: float,
        operator: str = "<",
    ) -> Tuple[str, float, int]:
        # Slope sur les prédictions (dérivée moyenne par pas)
        if len(preds) >= 2:
            slope = (preds[-1] - preds[0]) / (len(preds) - 1)
        else:
            slope = 0.0

        # Plancher (">=", ">") : violation si la valeur tombe SOUS le seuil.
        # Plafond ("<", "<=", défaut) : violation si elle le DÉPASSE.
        is_floor: bool = operator in (">", ">=")

        def _breaches(v: float) -> bool:
            return v < threshold if is_floor else v > threshold

        # Index du 1er pred qui viole le seuil réel
        time_to_breach: int = len(preds) + 1
        for idx, p in enumerate(preds):
            if _breaches(p):
                time_to_breach = idx
                break

        # Une prédiction ML (n'importe où dans l'horizon) viole le seuil SLO
        pred_breach:     bool = bool(preds) and any(_breaches(p) for p in preds)
        breach_reactive: bool = _breaches(current_val)

        logger.debug(
            f"🔍 _analyze — val={current_val:.2f} seuil={threshold:.2f} "
            f"slope={slope:.3f}/pas  TTB={time_to_breach}  "
            f"has_preds={bool(preds)}  pred_breach={pred_breach}  "
            f"reactive={breach_reactive}"
        )

        # ── Décision pilotée par la PRÉDICTION pour TOUTES les métriques ───
        # (primaire ET secondaires). Tant qu'une prévision ML existe, on
        # tranche dessus : violation PROACTIVE si le futur dépasse le seuil,
        # sinon aucune — on fait confiance au modèle et on ignore un pic
        # transitoire de la mesure courante. Cela supprime le « flapping »
        # réactif des seuils adaptatifs secondaires (ex. ram_usage) qui
        # collent à la valeur mesurée.
        # Le mode RÉACTIF ne subsiste plus que comme filet de sécurité quand
        # le modèle ML est indisponible (aucune prédiction).
        if preds:
            if pred_breach:
                return "proactive", slope, time_to_breach
            return "none", slope, time_to_breach

        if breach_reactive:
            return "reactive", slope, time_to_breach
        return "none", slope, time_to_breach

    @staticmethod
    def _severity(current_val: float, threshold: float, slope: float, operator: str = "<") -> float:
        if threshold <= 0:
            return 0.0
        if operator in (">", ">="):
            # Plancher : la sévérité grandit avec le déficit sous le seuil ;
            # une pente négative (dispo qui diminue) aggrave la situation.
            excess:      float = max(0.0, threshold - current_val) / threshold
            slope_bonus: float = max(0.0, -slope) / threshold
        else:
            excess      = max(0.0, current_val - threshold) / threshold
            slope_bonus = max(0.0, slope) / threshold
        return excess + 0.3 * slope_bonus

    def _get_current_val(self, vm_data: Dict[str, Any], metric: str) -> float:
        payload_key: str = config.METRICS_REGISTRY[metric].get("payload_key", metric)
        val = vm_data.get(payload_key)
        return float(val) if val is not None else 0.0