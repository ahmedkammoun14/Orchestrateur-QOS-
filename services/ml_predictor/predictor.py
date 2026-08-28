import asyncio
import re
import json
import logging
import statistics
import httpx
from collections import deque
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
from shared import config

logger = logging.getLogger("PredictorHandler")


class PredictorHandler:
    def __init__(self):
        self.endpoints = {
            "latency": config.ML_RTT_URL,
            "cpu":     config.ML_CPU_URL,
            "ram":     config.ML_RAM_URL,
        }
        self.window_sizes = {
            "latency": config.HISTORY_WINDOW,
            "cpu":     config.HISTORY_WINDOW,
            "ram":     config.HISTORY_WINDOW,
        }
        self.client = httpx.AsyncClient(timeout=config.POST_TIMEOUT)

        # Comparaison GRU vs extrapolation linéaire : erreur récente à t+1,
        # mesurée d'un appel au suivant (le pas d'appel = le pas d'horizon
        # réel de l'orchestrateur, ~6 s). maxlen=5 : fenêtre glissante courte
        # pour rester réactif si un modèle décroche.
        self.last_predictions: Dict[Tuple[str, str], Dict[str, Optional[float]]] = {}
        self.recent_errors: Dict[Tuple[str, str], Dict[str, deque]] = {}

    async def fetch_window_sizes(self):
        tasks   = [self._get_hyperparams(metric, url) for metric, url in self.endpoints.items()]
        results = await asyncio.gather(*tasks)
        for metric, size in results:
            if size:
                self.window_sizes[metric] = size
                logger.info(
                    f"✅ Hyperparamètres chargés — {metric} "
                    f"| window_size = {size}"
                )
            else:
                logger.warning(
                    f"⚠️  Hyperparamètres indisponibles pour {metric} "
                    f"— fenêtre par défaut : {self.window_sizes[metric]}"
                )

    async def _get_hyperparams(self, metric: str, base_url: str) -> Tuple[str, Optional[int]]:
        try:
            url  = base_url.replace("/predict", "/hyperparameters")
            resp = await self.client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                return metric, data.get("window_size")
        except Exception:
            pass
        return metric, None

    async def handle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # vm_id distingue les VM dans le suivi d'erreur recente GRU/lineaire
        # (self.recent_errors) : sans lui, des appels concurrents pour des VM
        # differentes mais le meme service ml_predictor melangent leurs
        # historiques dans le meme compteur, et l'"erreur recente" comparee
        # n'a plus de sens (prediction de la VM A comparee a la valeur reelle
        # de la VM B). Vu en pratique : MAE ~60-70ms au lieu de quelques ms.
        vm_id = payload.get("vm_id", "") or "unknown"
        tasks = [
            self._predict_metric("latency", payload.get("latency_history", []), vm_id),
            self._predict_metric("cpu",     payload.get("cpu_history", []), vm_id),
            self._predict_metric("ram",     payload.get("ram_history", []), vm_id),
        ]
        results = await asyncio.gather(*tasks)

        api_failures   = 0
        prediction_map = {}
        for metric, result_dict, api_failed in results:
            prediction_map[metric] = result_dict
            if api_failed:
                api_failures += 1

        metrics_requested = sum(
            1 for k in ["latency", "cpu", "ram"]
            if prediction_map.get(k) is not None
        )
        all_apis_down = (api_failures == metrics_requested) and metrics_requested > 0

        # Log récapitulatif des prédictions
        for metric, pred in prediction_map.items():
            if pred:
                preds  = pred.get("predictions", [])
                model  = pred.get("model", "?")
                conf   = pred.get("confidence", "?")
                first  = f"{preds[0]:.2f}" if preds else "N/A"
                logger.debug(
                    f"🔍 {metric:<10} modèle : {model:<22} "
                    f"conf : {conf}  prédiction[0] : {first}"
                )

        logger.info(
            f"🤖 Prédictions générées — "
            f"APIs OK : {metrics_requested - api_failures}/{metrics_requested} "
            f"| all_apis_down : {all_apis_down}"
        )

        return {
            "predicted_latency": prediction_map.get("latency"),
            "predicted_cpu":     prediction_map.get("cpu"),
            "predicted_ram":     prediction_map.get("ram"),
            "all_apis_down":     all_apis_down,
            "timestamp":         datetime.now(timezone.utc).isoformat(),
        }

    async def _predict_metric(
        self, metric: str, history: List[Dict[str, Any]], vm_id: str
    ) -> Tuple[str, Optional[Dict[str, Any]], bool]:

        if not history:
            if metric != "latency":
                return metric, None, False
            return metric, {
                "predictions": [0.0] * 7,
                "confidence":  0.5,
                "uncertainty": 1.0,
                "model":       "no_data",
            }, True

        last_val    = history[-1]["value"] if history else 0.0
        window_size = self.window_sizes.get(metric, 10)

        # ── Niveau 1 : predict_sequence ─────────────────────────────
        if len(history) >= window_size:
            try:
                sequence = [h["value"] / 100.0 for h in history[-window_size:]]
                url      = self.endpoints[metric].replace("/predict", "/predict_sequence")
                resp     = await self.client.post(url, json={"sequence": sequence, "horizon": 7})
                if resp.status_code == 200:
                    result       = resp.json()
                    parsed_preds = self._parse_api_list(result.get("predictions", []))
                    if parsed_preds:
                        logger.info(
                            f"✅ Niveau 1 (sequence) — {metric} "
                            f"| {len(parsed_preds)} valeurs prédites"
                        )
                        gru_response = self._build_response(metric, parsed_preds, result, "sequence_model")
                        if not config.ML_LINEAR_EXTRAPOLATION:
                            # Comportement de la campagne de référence : GRU seul.
                            return metric, gru_response, False
                        chosen = self._select_prediction(metric, vm_id, last_val, gru_response, history)
                        return metric, chosen, False
            except Exception as e:
                logger.warning(
                    f"⚠️  Niveau 1 échoué pour {metric} : {e} "
                    "— passage au niveau 2"
                )

        # ── Niveau 2 : GET /predict?input_data=X ────────────────────
        try:
            input_val = last_val / 100.0
            url       = f"{self.endpoints[metric]}?input_data={input_val}"
            resp      = await self.client.get(url)
            if resp.status_code == 200:
                result   = resp.json()
                raw_pred = result.get("prediction")
                if raw_pred is not None:
                    parsed = self._parse_api_list(raw_pred)
                    if parsed:
                        logger.info(
                            f"✅ Niveau 2 (point unique) — {metric} "
                            f"| valeur d'entrée : {last_val:.2f}"
                        )
                        return metric, self._build_response(metric, parsed, {}, "point_model"), False
        except Exception as e:
            logger.warning(
                f"⚠️  Niveau 2 échoué pour {metric} : {e} "
                "— passage au niveau 3"
            )

        # ── Niveau 3 : Last Known Value (fallback final) ─────────────
        logger.error(
            f"❌ Niveaux 1 et 2 épuisés pour {metric} "
            f"— fallback last_value ({last_val:.2f})"
        )
        return metric, {
            "predictions": [last_val] * 7,
            "confidence":  0.5,
            "uncertainty": 1.0,
            "model":       "last_value_fallback",
        }, True

    def _build_response(
        self, metric: str, preds: List[float], raw_result: Dict[str, Any], model_name: str
    ) -> Dict[str, Any]:
        denorm_preds = self._clamp(metric, self._denormalize(metric, preds))
        return {
            "predictions": denorm_preds[:7],
            "confidence":  raw_result.get("confidence", 0.8),
            "uncertainty": self._calc_uncertainty(raw_result, denorm_preds),
            "model":       raw_result.get("model", model_name),
        }

    def _select_prediction(
        self, metric: str, vm_id: str, actual_last_val: float,
        gru_response: Dict[str, Any], history: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Compare l'erreur récente à t+1 du GRU et d'une extrapolation linéaire
        simple, et retient le meilleur des deux. Le GRU reste actif et mesuré
        à chaque appel — rien n'est désactivé, seul le choix de sortie change.

        Suivi par (vm_id, metric) : ce service reçoit des appels concurrents
        pour PLUSIEURS VM par cycle (le hub interroge toutes les VM candidates
        en parallèle). Une clé par metric seul mélangerait la prédiction d'une
        VM avec la valeur réelle d'une autre au tour suivant.
        """
        key = (vm_id, metric)
        linear_preds = self._clamp(metric, self._linear_extrapolate(history))
        linear_response = {
            "predictions": linear_preds,
            "confidence":  0.6,
            "uncertainty": 1.0,
            "model":       "linear_extrapolation",
        }

        errors = self.recent_errors.setdefault(
            key, {"gru": deque(maxlen=5), "linear": deque(maxlen=5)}
        )
        prev = self.last_predictions.get(key)
        if prev is not None:
            if prev.get("gru") is not None:
                errors["gru"].append(abs(prev["gru"] - actual_last_val))
            if prev.get("linear") is not None:
                errors["linear"].append(abs(prev["linear"] - actual_last_val))

        self.last_predictions[key] = {
            "gru":    gru_response["predictions"][0] if gru_response["predictions"] else None,
            "linear": linear_preds[0] if linear_preds else None,
        }

        mae_gru    = statistics.mean(errors["gru"]) if errors["gru"] else None
        mae_linear = statistics.mean(errors["linear"]) if errors["linear"] else None

        if mae_gru is not None and mae_linear is not None and mae_linear < mae_gru:
            logger.info(
                f"📈 {vm_id}/{metric} — extrapolation linéaire retenue "
                f"(MAE récent lin={mae_linear:.2f} < GRU={mae_gru:.2f}, n={len(errors['linear'])})"
            )
            return linear_response

        logger.debug(
            f"🤖 {vm_id}/{metric} — GRU retenu "
            f"(MAE récent GRU={mae_gru}, lin={mae_linear})"
        )
        return gru_response

    def _linear_extrapolate(self, history: List[Dict[str, Any]], horizon: int = 7) -> List[float]:
        """
        Pente au sens des moindres carrés sur les 3 derniers points, projetée
        sur l'horizon. Suppose un pas d'échantillonnage uniforme (celui du
        cycle de l'orchestrateur, ~6 s), ce qui est le cas de `history`.
        """
        n = min(3, len(history))
        if n < 2:
            last = history[-1]["value"] if history else 0.0
            return [last] * horizon

        ys = [h["value"] for h in history[-n:]]
        xs = list(range(n))
        x_mean = sum(xs) / n
        y_mean = sum(ys) / n
        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        den = sum((x - x_mean) ** 2 for x in xs)
        slope = num / den if den else 0.0
        last_y = ys[-1]
        return [last_y + slope * (i + 1) for i in range(horizon)]

    def _clamp(self, metric: str, preds: List[float]) -> List[float]:
        """
        Borne les prédictions aux limites physiques pour éviter qu'un modèle
        instable (ex. ESN qui diverge) ne produise des valeurs absurdes
        — latence négative, % > 100 — qui fausseraient TOPSIS et la détection
        de violations. Log un avertissement quand une correction est appliquée
        (le modèle reste à surveiller).
        """
        if metric == "latency":
            lo, hi = 0.0, config.LATENCY_MAX
        else:  # cpu, ram
            lo, hi = 0.0, 100.0
        clamped = [max(lo, min(hi, float(p))) for p in preds]
        n_fixed = sum(1 for p, c in zip(preds, clamped) if p != c)
        if n_fixed:
            logger.warning(
                f"⚠️  {metric} — {n_fixed} prédiction(s) hors bornes "
                f"[{lo:.0f}, {hi:.0f}] corrigée(s) (modèle instable ?)"
            )
        return clamped

    def _denormalize(self, metric: str, preds: List[float]) -> List[float]:
        if metric in ["cpu", "ram"]:
            if preds and max(preds) > 1.0:
                return preds
            return [p * 100.0 for p in preds]
        if metric == "latency":
            # predictor.py envoie latency_ms/100 → les prédictions reviennent dans
            # la même échelle (ex: 3.93 pour 393ms) → multiplier par 100 pour obtenir ms.
            # Seuil à 100 : si valeur > 100 elle est déjà en ms brut.
            if preds and max(preds) < 100.0:
                return [p * 100.0 for p in preds]
        return preds

    def _parse_api_list(self, raw: Any) -> List[float]:
        if isinstance(raw, list):
            return [float(x) for x in raw]
        if isinstance(raw, str):
            try:
                return [float(x) for x in json.loads(raw)]
            except Exception:
                return [float(x) for x in re.findall(r"[\d.]+", raw)]
        return []

    def _calc_uncertainty(self, result: Dict[str, Any], preds: List[float]) -> float:
        c_high = result.get("confidence_high")
        c_low  = result.get("confidence_low")
        if c_high is not None and c_low is not None and len(preds) > 0:
            high_list = self._parse_api_list(c_high)
            low_list  = self._parse_api_list(c_low)
            if high_list and low_list:
                mean_h = statistics.mean(high_list)
                mean_l = statistics.mean(low_list)
                mean_p = statistics.mean(preds)
                if mean_p != 0:
                    return abs(mean_h - mean_l) / mean_p
        return 1.0

    async def close(self):
        await self.client.aclose()