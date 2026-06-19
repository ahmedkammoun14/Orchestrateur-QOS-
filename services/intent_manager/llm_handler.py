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
                logger.warning(f"⚠️  Récupération contexte RAG échouée : {e}")
        return {"active_slos": [], "percentiles": {}, "history": []}


class LLMHandler:
    """
    Facade for natural language processing with a 3-level fallback cascade.

    Tous les SLOs générés sont marqués is_primary=True car ils représentent
    les objectifs métier explicites de l'utilisateur. Les SLOs secondaires
    adaptatifs (basés sur MI) sont ajoutés ensuite par le metrics_manager.

    Level 1 : Ollama LLM (qwen2.5) avec contexte RAG résumé
    Level 2 : Regex — patterns explicites (latency < 100ms)
    Level 3 : Keywords — profils sémantiques (streaming, critique, etc.)

    Si le niveau 1 (LLM) produit le résultat, il gère lui-même la cohérence
    avec les SLOs actifs (active_slos) selon le sens de l'intention — la
    stratégie REFINE du SLOMerger est alors désactivée pour ne pas écraser
    son résultat. REFINE reste actif comme filet de sécurité pour les
    niveaux 2/3, qui n'ont aucune intelligence sémantique.
    """

    def __init__(self):
        self.rag_builder = RAGContextBuilder()
        self.merger = SLOMerger()
        self.history: List[Dict[str, Any]] = []

    async def handle(self, payload: Dict[str, Any]) -> Optional[List[SLO]]:
        intention = payload.get("intention", "")
        if not intention:
            return None

        context = await self.rag_builder.build()

        result = await self._level1_llm(intention, context)
        source_level = 1

        if not result:
            result = self._level2_regex(intention)
            source_level = 2
            if result:
                logger.info(
                    f"🔁 Niveau 2 activé — extraction par regex "
                    f"| {len(result)} SLO(s) trouvé(s)"
                )

        if not result:
            result = self._level3_keywords(intention)
            source_level = 3
            if result:
                logger.info(
                    f"🔁 Niveau 3 activé — extraction par mots-clés "
                    f"| {len(result)} SLO(s) trouvé(s)"
                )

        if not result:
            logger.error("❌ Tous les niveaux d'extraction ont échoué — intention non interprétable")
            return None

        # Marque tous les SLOs comme PRIMAIRES (objectifs explicites utilisateur)
        for r in result:
            r["is_primary"] = True

        normalized = self._normalize_and_validate(result)

        active_slos = [SLO(**s) for s in context.get("active_slos", [])]

        # REFINE désactivé si le LLM (niveau 1) a produit le résultat —
        # il a déjà géré la cohérence avec l'existant lui-même via le prompt.
        final_slos = self.merger.merge(
            active_slos, normalized, intention,
            allow_refine=(source_level != 1)
        )

        # Garantit que tous les SLOs finaux sont marqués primaires
        for s in final_slos:
            s.is_primary = True

        self.history.append({"intention": intention, "slos": [s.dict() for s in final_slos]})
        if len(self.history) > config.HISTORY_SIZE:
            self.history.pop(0)

        logger.info(
            f"✅ SLOs primaires extraits et validés — {len(final_slos)} SLO(s) "
            f"| niveau source : {source_level} "
            f"| métriques : {[s.metric for s in final_slos]}"
        )
        return final_slos

    async def _level1_llm(self, text: str, context: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        rag_summary = {
            "active_slos": context.get("active_slos", []),
            "service_vm":  context.get("service_vm", "unknown"),
            "cycle":       context.get("cycle", 0),
        }

        prompt = f"""Tu es un expert QoS réseau.
État actuel du système : {json.dumps(rag_summary, ensure_ascii=False)}
Convertis cette intention utilisateur en une liste JSON de SLOs réseau.
Métriques disponibles : latency (ms), cpu_usage (%), ram_usage (%).

RÈGLE IMPORTANTE — cohérence avec l'existant :
Si l'intention fait référence à une amélioration ou un changement relatif
par rapport à l'état actuel (ex: "plus rapide", "encore mieux", "moins de
charge CPU", "améliore le streaming") SANS préciser de chiffre exact,
utilise les SLOs listés dans "active_slos" comme référence pour la ou les
métriques concernées, et propose un nouveau seuil cohérent avec le sens de
l'intention (plus strict si on demande mieux, plus permissif si on demande
moins strict).
Si l'intention donne un chiffre explicite, utilise-le tel quel.
Si l'intention ne fait pas référence à l'existant pour une métrique donnée,
ignore active_slos pour cette métrique et utilise ton jugement d'expert QoS.

Intention : "{text}"
Réponds UNIQUEMENT avec un tableau JSON valide sans texte autour, exemple :
[{{"metric": "latency", "operator": "<", "threshold": 100.0, "unit": "ms", "weight": 1.0, "target": 80.0, "window": "5m"}}]
"""

        try:
            async with httpx.AsyncClient() as client:
                logger.info(
                    f"🤖 Appel LLM Ollama — modèle : {config.INTENT_MODEL} "
                    f"| intention : \"{text[:60]}{'...' if len(text) > 60 else ''}\""
                )
                resp = await client.post(
                    f"{config.OLLAMA_URL}/api/generate",
                    json={"model": config.INTENT_MODEL, "prompt": prompt, "stream": False},
                    timeout=60.0
                )
                if resp.status_code == 200:
                    raw_content = resp.json().get("response", "")
                    match = re.search(r'\[.*?\]', raw_content, re.DOTALL)
                    if match:
                        parsed = json.loads(match.group())
                        if isinstance(parsed, list) and len(parsed) > 0:
                            logger.info(
                                f"✅ LLM — réponse valide reçue "
                                f"| {len(parsed)} SLO(s) extraits"
                            )
                            return parsed
        except Exception as e:
            logger.warning(f"⚠️  Ollama indisponible : {e} — passage au niveau 2")
        return None

    def _level2_regex(self, text: str) -> Optional[List[Dict[str, Any]]]:
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
        t = text.lower()

        if any(k in t for k in [
            "fluide", "coupure", "streaming", "video", "vidéo",
            "flux", "continu", "interruption", "stable", "qualité"
        ]):
            logger.info("📺 Profil détecté : Streaming / Fluidité")
            return [
                {"metric": "latency",   "operator": "<", "threshold": 100.0,
                 "unit": "ms", "weight": 0.6, "target": 80.0,  "window": "5m"},
                {"metric": "cpu_usage", "operator": "<", "threshold": 70.0,
                 "unit": "%",  "weight": 0.2, "target": 60.0, "window": "5m"},
                {"metric": "ram_usage", "operator": "<", "threshold": 70.0,
                 "unit": "%",  "weight": 0.2, "target": 60.0, "window": "5m"},
            ]

        if any(k in t for k in [
            "critique", "edge", "temps réel", "realtime", "urgent", "prioritaire"
        ]):
            logger.info("🔴 Profil détecté : Temps réel / Critique")
            return [
                {"metric": "latency", "operator": "<", "threshold": 50.0,
                 "unit": "ms", "weight": 1.0, "target": 40.0, "window": "1m"}
            ]

        if any(k in t for k in [
            "sensible", "ux", "utilisateur", "expérience", "confort", "réactif"
        ]):
            logger.info("👤 Profil détecté : UX / Expérience utilisateur")
            return [
                {"metric": "latency", "operator": "<", "threshold": 150.0,
                 "unit": "ms", "weight": 0.7, "target": 120.0, "window": "2m"}
            ]

        if any(k in t for k in [
            "lourd", "ressources", "calcul", "intensif", "traitement"
        ]):
            logger.info("⚙️  Profil détecté : Ressources intensives")
            return [
                {"metric": "cpu_usage", "operator": "<", "threshold": 70.0,
                 "unit": "%", "weight": 0.5, "target": 60.0, "window": "5m"},
                {"metric": "ram_usage", "operator": "<", "threshold": 75.0,
                 "unit": "%", "weight": 0.5, "target": 65.0, "window": "5m"},
            ]

        return None

    def _normalize_and_validate(self, raw_list: List[Dict[str, Any]]) -> List[SLO]:
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
            m = r.get("metric", "").lower()
            r["metric"] = norm_map.get(m, m)

            # Récupération défensive du threshold — gère à la fois clé absente
            # ET clé présente avec valeur None (cas où le LLM renvoie un null)
            raw_threshold = r.get("threshold")

            if r["metric"] == "latency":
                default_thr = 200.0
                base = raw_threshold if raw_threshold is not None else default_thr
                r["threshold"] = max(config.LATENCY_MIN, min(config.LATENCY_MAX, float(base)))
            elif r["metric"] in ["cpu_usage", "ram_usage"]:
                default_thr = 80.0
                base = raw_threshold if raw_threshold is not None else default_thr
                r["threshold"] = max(config.USAGE_MIN, min(config.USAGE_MAX, float(base)))
            else:
                # Métrique inconnue — pas de bornes définies, on garde la valeur
                # ou on écarte le SLO si threshold est totalement absent/None
                if raw_threshold is None:
                    logger.warning(f"⚠️  SLO ignoré — métrique '{r['metric']}' sans threshold valide")
                    continue
                r["threshold"] = float(raw_threshold)

            # target : défensif contre target=None explicite
            raw_target = r.get("target")
            r["target"] = float(raw_target) if raw_target is not None else r["threshold"] * 0.9

            r.setdefault("window", "5m")

            raw_weight = r.get("weight")
            r["weight"] = float(raw_weight) if raw_weight is not None else 1.0 / len(raw_list)

            r.setdefault("budget_remaining", 100.0)
            r.setdefault("violations", 0)

            raw_confidence = r.get("confidence")
            r["confidence"] = float(raw_confidence) if raw_confidence is not None else 0.8

            r.setdefault("is_primary", True)  # objectif explicite utilisateur

            try:
                valid_slos.append(SLO(**r))
            except Exception as e:
                logger.warning(f"⚠️  SLO ignoré — validation échouée : {e}")
                continue

        return valid_slos