import json
import logging
import re
import httpx
from typing import List, Dict, Any, Optional
from shared import config
from shared.models import SLO
from services.intent_manager.slo_merger import SLOMerger

logger = logging.getLogger("LLMHandler")


class RAGContextBuilder:
    """Fetches real-time system state from the Hub to enrich LLM prompts."""

    async def build(self) -> Dict[str, Any]:
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(config.HUB_STATS_URL, timeout=config.RAG_TIMEOUT)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as e:
                logger.warning(f"RAG fetch failed: {str(e)}", extra={"event": "rag_failed"})
        return {"active_slos": [], "percentiles": {}, "history": []}


class LLMHandler:
    """
    Facade for natural language processing with a 3-level fallback cascade.
    Level 1 : Ollama LLM (qwen2.5) avec contexte RAG résumé
    Level 2 : Regex — patterns explicites (latency < 100ms)
    Level 3 : Keywords — profils sémantiques (streaming, critique, etc.)
    """

    def __init__(self):
        self.rag_builder = RAGContextBuilder()
        self.merger = SLOMerger()
        self.history: List[Dict[str, Any]] = []

    async def handle(self, payload: Dict[str, Any]) -> Optional[List[SLO]]:
        intention = payload.get("intention", "")
        if not intention:
            return None

        # 0. Context Gathering (RAG)
        context = await self.rag_builder.build()

        # 1. Cascade Level 1: LLM (Ollama)
        result = await self._level1_llm(intention, context)

        # 2. Cascade Level 2: Regex Fallback
        if not result:
            result = self._level2_regex(intention)
            if result:
                logger.info("Regex fallback triggered", extra={"event": "regex_fallback"})

        # 3. Cascade Level 3: Keywords Fallback
        if not result:
            result = self._level3_keywords(intention)
            if result:
                logger.info("Keywords fallback triggered", extra={"event": "keywords_fallback"})

        if not result:
            logger.error("All extraction levels failed", extra={"event": "llm_failed"})
            return None

        # 4. Normalization & Physical Validation
        normalized = self._normalize_and_validate(result)

        # 5. Merging with History/Active state
        active_slos = [SLO(**s) for s in context.get("active_slos", [])]
        final_slos = self.merger.merge(active_slos, normalized, intention)

        # 6. Update History (FIFO 10)
        self.history.append({"intention": intention, "slos": [s.dict() for s in final_slos]})
        if len(self.history) > config.HISTORY_SIZE:
            self.history.pop(0)

        logger.info("SLOs successfully processed", extra={"event": "slos_validated"})
        return final_slos

    async def _level1_llm(self, text: str, context: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """
        Ollama/Qwen2.5 avec contexte RAG résumé.
        On ne passe que les informations essentielles au LLM pour éviter
        de surcharger le prompt avec des données brutes volumineuses.
        """
        # Résumé RAG — uniquement les infos utiles pour le LLM
        rag_summary = {
            "active_slos":  context.get("active_slos", []),
            "service_vm":   context.get("service_vm", "unknown"),
            "cycle":        context.get("cycle", 0),
        }

        prompt = f"""Tu es un expert QoS réseau.
État actuel du système : {json.dumps(rag_summary, ensure_ascii=False)}
Convertis cette intention utilisateur en une liste JSON de SLOs réseau.
Métriques disponibles : latency (ms), cpu_usage (%), ram_usage (%).
Intention : "{text}"
Réponds UNIQUEMENT avec un tableau JSON valide sans texte autour, exemple :
[{{"metric": "latency", "operator": "<", "threshold": 100.0, "unit": "ms", "weight": 1.0, "target": 80.0, "window": "5m"}}]
"""

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{config.OLLAMA_URL}/api/generate",
                    json={"model": config.INTENT_MODEL, "prompt": prompt, "stream": False},
                    timeout=60.0
                )
                if resp.status_code == 200:
                    raw_content = resp.json().get("response", "")
                    # Extraire le JSON du contenu (peut contenir du markdown)
                    match = re.search(r'\[.*?\]', raw_content, re.DOTALL)
                    if match:
                        parsed = json.loads(match.group())
                        if isinstance(parsed, list) and len(parsed) > 0:
                            logger.info("LLM success", extra={"event": "llm_success"})
                            return parsed
        except Exception as e:
            logger.warning(f"Ollama error: {str(e)}", extra={"event": "llm_failed"})
        return None

    def _level2_regex(self, text: str) -> Optional[List[Dict[str, Any]]]:
        """
        Extraction par patterns regex explicites.
        Ex : "latency < 100ms", "latence <= 50"
        """
        pattern = r"(latency|latence|ping|cpu|ram|mémoire)\s*(<|<=|>|>=)\s*(\d+)"
        matches = re.findall(pattern, text.lower())
        if not matches:
            return None

        metric_map = {
            "latency": "latency", "latence": "latency", "ping": "latency",
            "cpu": "cpu_usage", "ram": "ram_usage", "mémoire": "ram_usage"
        }
        unit_map = {
            "latency": "ms", "cpu_usage": "%", "ram_usage": "%"
        }

        result = []
        for m in matches:
            metric = metric_map.get(m[0], m[0])
            unit   = unit_map.get(metric, "ms")
            thr    = float(m[2])
            result.append({
                "metric":    metric,
                "operator":  m[1],
                "threshold": thr,
                "unit":      unit,
                "weight":    1.0,
                "target":    thr * 0.8,
                "window":    "5m"
            })
        return result if result else None

    def _level3_keywords(self, text: str) -> Optional[List[Dict[str, Any]]]:
        """
        Profils sémantiques basés sur mots-clés.
        Couvre les cas courants : streaming, critique, ux, ressources.
        """
        t = text.lower()

        # Profil streaming / fluidité
        if any(k in t for k in [
            "fluide", "coupure", "streaming", "video", "vidéo",
            "flux", "continu", "interruption", "stable", "qualité"
        ]):
            return [
                {"metric": "latency",   "operator": "<", "threshold": 100.0,
                 "unit": "ms", "weight": 0.6, "target": 80.0,  "window": "5m"},
                {"metric": "cpu_usage", "operator": "<", "threshold": 70.0,
                 "unit": "%",  "weight": 0.2, "target": 60.0, "window": "5m"},
                {"metric": "ram_usage", "operator": "<", "threshold": 70.0,
                 "unit": "%",  "weight": 0.2, "target": 60.0, "window": "5m"},
            ]

        # Profil temps réel / critique
        if any(k in t for k in [
            "critique", "edge", "temps réel", "realtime", "urgent", "prioritaire"
        ]):
            return [
                {"metric": "latency", "operator": "<", "threshold": 50.0,
                 "unit": "ms", "weight": 1.0, "target": 40.0, "window": "1m"}
            ]

        # Profil UX / utilisateur
        if any(k in t for k in [
            "sensible", "ux", "utilisateur", "expérience", "confort", "réactif"
        ]):
            return [
                {"metric": "latency", "operator": "<", "threshold": 150.0,
                 "unit": "ms", "weight": 0.7, "target": 120.0, "window": "2m"}
            ]

        # Profil ressources lourdes
        if any(k in t for k in [
            "lourd", "ressources", "calcul", "intensif", "traitement"
        ]):
            return [
                {"metric": "cpu_usage", "operator": "<", "threshold": 70.0,
                 "unit": "%", "weight": 0.5, "target": 60.0, "window": "5m"},
                {"metric": "ram_usage", "operator": "<", "threshold": 75.0,
                 "unit": "%", "weight": 0.5, "target": 65.0, "window": "5m"},
            ]

        return None

    def _normalize_and_validate(self, raw_list: List[Dict[str, Any]]) -> List[SLO]:
        """
        Normalisation multi-langue + validation physique des valeurs.
        """
        norm_map = {
            "cpu":        "cpu_usage",
            "processeur": "cpu_usage",
            "ram":        "ram_usage",
            "mémoire":    "ram_usage",
            "memory":     "ram_usage",
            "latence":    "latency",
            "ping":       "latency",
            "delay":      "latency",
            "rtt":        "latency",
        }

        valid_slos = []
        for r in raw_list:
            # Normalisation du nom de métrique
            m = r.get("metric", "").lower()
            r["metric"] = norm_map.get(m, m)

            # Validation physique des seuils
            if r["metric"] == "latency":
                r["threshold"] = max(
                    config.LATENCY_MIN,
                    min(config.LATENCY_MAX, r.get("threshold", 200.0))
                )
            elif r["metric"] in ["cpu_usage", "ram_usage"]:
                r["threshold"] = max(
                    config.USAGE_MIN,
                    min(config.USAGE_MAX, r.get("threshold", 80.0))
                )

            # Enrichissement des champs optionnels
            r.setdefault("target",           r["threshold"] * 0.9)
            r.setdefault("window",           "5m")
            r.setdefault("weight",           1.0 / len(raw_list))
            r.setdefault("budget_remaining", 100.0)
            r.setdefault("violations",       0)
            r.setdefault("confidence",       0.8)

            try:
                valid_slos.append(SLO(**r))
            except Exception as e:
                logger.warning(f"SLO validation failed: {e}", extra={"event": "slo_invalid"})
                continue

        return valid_slos