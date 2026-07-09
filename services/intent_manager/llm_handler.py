import json
import logging
import re
import httpx
from typing import List, Dict, Any, Optional
from shared import config
from shared.logging_utils import C
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
    Facade for natural language processing — extraction via LLM uniquement.

    Tous les SLOs générés sont marqués is_primary=True car ils représentent
    les objectifs métier explicites de l'utilisateur. Les SLOs secondaires
    adaptatifs (basés sur MI) sont ajoutés ensuite par le metrics_manager.

    Le LLM (Ollama qwen2.5) gère lui-même la cohérence avec les SLOs actifs
    via le prompt — REFINE est donc désactivé en permanence, devenu inutile
    puisqu'il n'existe plus de niveau de repli sans intelligence sémantique.
    """

    def __init__(self):
        self.rag_builder     = RAGContextBuilder()
        self.merger          = SLOMerger()
        self.history: List[Dict[str, Any]] = []
        self._history_loaded = False

    async def _ensure_history_loaded(self) -> None:
        if self._history_loaded:
            return
        self._history_loaded = True  # set before await to avoid race on concurrent calls
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{config.DATABASE_SERVICE_URL}/load/llm_history",
                    params={"size": config.HISTORY_SIZE},
                    timeout=3.0,
                )
                if resp.status_code == 200:
                    self.history = resp.json().get("history", [])
                    if self.history:
                        logger.info(
                            f"📂 Historique LLM rechargé depuis Redis — "
                            f"{len(self.history)} intention(s)"
                        )
        except Exception as exc:
            logger.warning(f"⚠️  Chargement historique LLM échoué (dégradation gracieuse) : {exc}")

    async def _persist_intent(self, entry: Dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{config.DATABASE_SERVICE_URL}/store/llm_history",
                    json=entry,
                    timeout=3.0,
                )
        except Exception as exc:
            logger.warning(f"⚠️  Persistance historique LLM échouée : {exc}")

    async def handle(self, payload: Dict[str, Any]) -> Optional[List[SLO]]:
        await self._ensure_history_loaded()

        intention = payload.get("intention", "")
        if not intention:
            return None

        context = await self.rag_builder.build()

        result = await self._level1_llm(intention, context)

        if not result:
            logger.error("❌ Le LLM n'a pas produit de résultat exploitable — intention non interprétable")
            return None

        # ── Log des seuils bruts extraits par le LLM ─────────────────
        _intent_short = f"\"{intention[:60]}{'...' if len(intention) > 60 else ''}\""
        _raw_parts = [
            f"{C.BOLD}{r.get('metric','?')}{C.RESET} "
            f"{r.get('operator','<')} {C.CYAN}{r.get('threshold','?')}{C.RESET} "
            f"{r.get('unit','') or ''}"
            for r in result
        ]
        logger.info(
            f"🔍 Seuils LLM bruts — {_intent_short}\n"
            f"   " + "   |   ".join(_raw_parts)
        )

        # Marque tous les SLOs comme PRIMAIRES (objectifs explicites utilisateur)
        for r in result:
            r["is_primary"] = True

        normalized = self._normalize_and_validate(result)

        active_slos = [SLO(**s) for s in context.get("active_slos", [])]

        # REFINE toujours désactivé — le LLM gère seul la cohérence avec
        # l'existant via le prompt (RAG context).
        final_slos = self.merger.merge(
            active_slos, normalized, intention,
            allow_refine=False
        )

        # Garantit que tous les SLOs finaux sont marqués primaires
        for s in final_slos:
            s.is_primary = True

        # ── Log des SLOs finaux validés (après normalisation + merge) ─
        _slo_lines = [
            f"   {C.BOLD}{s.metric:<12}{C.RESET} "
            f"détection {s.operator} {C.GREEN}{s.target:<8.1f}{C.RESET} {s.unit:<4}"
            f"| contrat {s.operator} {C.CYAN}{s.threshold:.1f}{C.RESET} {s.unit}"
            for s in final_slos
        ]
        logger.info(
            f"\n{'─'*58}\n"
            f"  🎯  SLOs validés — {_intent_short}\n"
            + "\n".join(_slo_lines)
            + f"\n{'─'*58}"
        )

        entry = {"intention": intention, "slos": [s.dict() for s in final_slos]}
        self.history.append(entry)
        if len(self.history) > config.HISTORY_SIZE:
            self.history.pop(0)
        await self._persist_intent(entry)

        return final_slos

    async def _level1_llm(self, text: str, context: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        rag_summary = {
            "active_slos": context.get("active_slos", []),
            "service_vm":  context.get("service_vm", "unknown"),
            "cycle":       context.get("cycle", 0),
        }

        system_prompt = (
            "Tu es un expert QoS réseau. Tu convertis des intentions utilisateur en SLOs "
            "au format JSON pour le SERVICE qui réalisera cette intention.\n\n"
            "DÉMARCHE DE RAISONNEMENT (toujours dans cet ordre) :\n"
            "1. Identifie le TYPE DE SERVICE qu'il faudrait déployer pour réaliser l'intention "
            "(ex: serveur de streaming, backend web, sonde de surveillance/détection, agent "
            "d'inférence ML). L'intention n'a PAS besoin de mentionner explicitement "
            "CPU/RAM/latence — déduis les besoins du service réalisateur : une tâche "
            "d'analyse continue implique du CPU, un état/historique à maintenir implique de "
            "la RAM, une exigence de réactivité (alerte, temps réel) implique une latence "
            "faible.\n"
            "2. Estime les besoins cpu_usage/ram_usage de ce service via le catalogue "
            "ci-dessous.\n"
            "3. Déduis la contrainte de latence du niveau de réactivité qu'implique "
            "l'intention.\n\n"
            "Métriques disponibles — UNIQUEMENT ces 3, avec exactement ces noms et ces "
            "unités (n'invente JAMAIS d'autre nom de métrique ni d'autre unité) :\n"
            "  - latency   : latence réseau en ms (operator: \"<\", unit: \"ms\").\n"
            "  - cpu_usage : BESOIN ABSOLU de calcul du service, en cœurs disponibles "
            "nécessaires (operator: \">=\", unit: \"cores\") — PAS un pourcentage de charge, "
            "une quantité de ressource que le service doit trouver libre sur la machine qui "
            "l'héberge, indépendamment de la capacité de cette machine.\n"
            "  - ram_usage : BESOIN ABSOLU de mémoire du service, en Go disponibles "
            "nécessaires (operator: \">=\", unit: \"GB\") — même logique que cpu_usage. "
            "Toujours en GB, jamais en Mo/MB (512 Mo → 0.5 GB).\n\n"
            "N'inclus une métrique QUE si le service réalisateur en a réellement besoin — "
            "n'ajoute pas une métrique juste pour compléter la liste. Renvoie un tableau "
            "vide `[]` UNIQUEMENT si aucun service réseau déployable ne peut réaliser "
            "l'intention (ex: question générale, demande de contenu sans service associé).\n\n"
            "CATALOGUE DE PROFILS DE RÉFÉRENCE (besoin du service réalisateur — jamais en "
            "fonction d'une VM précise) :\n"
            "  - service léger / API / monitoring simple        : 0.3-0.5 cœur,  0.2-0.5 Go\n"
            "  - surveillance / détection continue (sonde, IDS, inspection de trafic/TLS) : 0.3-0.8 cœur, 0.3-0.8 Go\n"
            "  - traitement web classique / backend             : 0.5-1.0 cœur,  0.5-1.0 Go\n"
            "  - streaming / transcodage vidéo                  : 1.5-3.0 cœurs, 1.0-2.0 Go\n"
            "  - inférence / entraînement ML                     : 1.0-4.0 cœurs, 2.0-8.0 Go\n\n"
            "RÈGLES :\n"
            "1. Si l'intention donne un chiffre explicite pour une métrique, utilise-le "
            "(garde l'unité adaptée : ms pour latency, cœurs/GB pour cpu/ram).\n"
            "2. Latence selon la réactivité implicite : alerte/temps réel critique ≈ 50-100 ms ; "
            "confort utilisateur ≈ 100-200 ms ; tâche d'arrière-plan tolérante ≈ 200-500 ms. "
            "Si vraiment aucun indice : 100 ms.\n"
            "3. Pour cpu_usage/ram_usage sans chiffre explicite, utilise le catalogue selon "
            "le type de service réalisateur identifié à l'étape 1.\n"
            "4. Si active_slos contient des SLOs actifs, utilise leurs valeurs comme référence.\n"
            "5. Les poids (weight) doivent sommer à 1.0 — poids dominant à la métrique qui "
            "porte la valeur métier de l'intention (ex: la latence pour une alerte, le "
            "CPU pour du calcul intensif).\n\n"
            "FORMAT DE RÉPONSE OBLIGATOIRE — tableau JSON uniquement, "
            "sans texte avant ou après, sans markdown :\n"
            '[{"metric":"latency","operator":"<","threshold":100.0,"unit":"ms","weight":0.5,"target":90.0,"window":"5m"},'
            '{"metric":"cpu_usage","operator":">=","threshold":0.5,"unit":"cores","weight":0.25,"target":0.55,"window":"5m"},'
            '{"metric":"ram_usage","operator":">=","threshold":0.5,"unit":"GB","weight":0.25,"target":0.55,"window":"5m"}]'
        )

        user_prompt = (
            f"État actuel du système : {json.dumps(rag_summary, ensure_ascii=False)}\n\n"
            f"Intention utilisateur : \"{text}\"\n\n"
            "Identifie d'abord le service qui réaliserait cette intention, puis génère le "
            "tableau JSON des SLOs de ce service — uniquement les métriques dont il a "
            "réellement besoin, tableau vide si aucun service déployable ne correspond."
        )

        # ── 1. Try LAAS vLLM (primary) ───────────────────────────────
        result = await self._call_laas(system_prompt, user_prompt, text)
        if result is not None:
            return result

        # ── 2. Fallback: Ollama local ─────────────────────────────────
        logger.warning("⚠️  LAAS indisponible — fallback vers Ollama local")
        ollama_prompt = (
            f"{system_prompt}\n\n"
            f"{user_prompt}"
        )
        return await self._call_ollama(ollama_prompt, text)

    async def _call_laas(self, system_prompt: str, user_prompt: str, text: str) -> Optional[List[Dict[str, Any]]]:
        proxies = {"https://": config.LAAS_LLM_PROXY} if config.LAAS_LLM_PROXY else None
        payload = {
            "model": config.LAAS_MODEL,
            "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
            "extra_body":           {"enable_thinking": False, "include_thought": False},
            "thinking":             {"enabled": False},
            "enable_thinking":      False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
        }
        try:
            async with httpx.AsyncClient(proxies=proxies) as client:
                logger.info(
                    f"🤖 Appel LAAS vLLM — modèle : {config.LAAS_MODEL} "
                    f"| intention : \"{text[:60]}{'...' if len(text) > 60 else ''}\""
                )
                resp = await client.post(
                    config.LAAS_LLM_URL,
                    json=payload,
                    timeout=60.0,
                )
                if resp.status_code == 200:
                    raw_content = resp.json()["choices"][0]["message"]["content"].strip()
                    logger.debug(f"LAAS raw response: {raw_content[:300]}")
                    # greedy match to capture the full array including nested objects
                    # (*.  et non +. : un tableau vide `[]` est une réponse valide et
                    # délibérée — l'intention ne concerne aucune métrique réseau/QoS).
                    match = re.search(r'\[.*\]', raw_content, re.DOTALL)
                    if match:
                        parsed = json.loads(match.group())
                        if isinstance(parsed, list):
                            if len(parsed) > 0:
                                logger.info(f"✅ LAAS LLM — {len(parsed)} SLO(s) extraits")
                            else:
                                logger.info("ℹ️  LAAS LLM — tableau vide : intention hors du domaine réseau/QoS")
                            return parsed
                    logger.warning(f"⚠️  LAAS LLM — réponse sans JSON exploitable : {raw_content[:300]}")
                else:
                    logger.error(f"❌ LAAS LLM a retourné HTTP {resp.status_code} : {resp.text[:200]}")
        except Exception as e:
            logger.error(f"❌ LAAS LLM indisponible ou erreur d'appel : {e}")
        return None

    async def _call_ollama(self, prompt: str, text: str) -> Optional[List[Dict[str, Any]]]:
        try:
            async with httpx.AsyncClient() as client:
                logger.info(
                    f"🤖 Appel LLM Ollama — modèle : {config.INTENT_MODEL} "
                    f"| intention : \"{text[:60]}{'...' if len(text) > 60 else ''}\""
                )
                resp = await client.post(
                    f"{config.OLLAMA_URL}/api/generate",
                    json={"model": config.INTENT_MODEL, "prompt": prompt, "stream": False},
                    timeout=60.0,
                )
                if resp.status_code == 200:
                    raw_content = resp.json().get("response", "")
                    match = re.search(r'\[.*?\]', raw_content, re.DOTALL)
                    if match:
                        parsed = json.loads(match.group())
                        if isinstance(parsed, list):
                            if len(parsed) > 0:
                                logger.info(f"✅ Ollama — {len(parsed)} SLO(s) extraits")
                            else:
                                logger.info("ℹ️  Ollama — tableau vide : intention hors du domaine réseau/QoS")
                            return parsed
                else:
                    logger.error(f"❌ Ollama a retourné HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"❌ Ollama indisponible ou erreur d'appel : {e}")
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

            # "Go" (FR) → "GB" : le reste du pipeline (filtre, TOPSIS) ne
            # connaît que "GB" pour identifier un besoin en ressource absolue.
            if r.get("unit") == "Go":
                r["unit"] = "GB"
            # Mo/MB → GB : le LLM peut raisonner en Mo pour un petit service
            # (ex: sonde 256 Mo). Sans conversion, l'unité inconnue ferait
            # retomber le seuil dans la branche % (clamp 1-99) — absurde.
            if r.get("unit") in ("MB", "Mo"):
                r["unit"] = "GB"
                for fld in ("threshold", "target"):
                    if r.get(fld) is not None:
                        r[fld] = float(r[fld]) / 1024.0

            # Récupération défensive du threshold — gère à la fois clé absente
            # ET clé présente avec valeur None (cas où le LLM renvoie un null)
            raw_threshold = r.get("threshold")

            if r["metric"] == "latency":
                default_thr = 200.0
                base = raw_threshold if raw_threshold is not None else default_thr
                r["threshold"] = max(config.LATENCY_MIN, min(config.LATENCY_MAX, float(base)))
            elif r["metric"] in ["cpu_usage", "ram_usage"]:
                if r.get("unit") in ("cores", "GB"):
                    # Besoin absolu du service (cœurs/Go nécessaires) — pas un
                    # pourcentage de charge, donc pas de borne 0-100. Juste un
                    # plancher de sanité pour écarter une valeur nulle/négative.
                    default_thr = 0.5
                    base = raw_threshold if raw_threshold is not None else default_thr
                    r["threshold"] = max(0.1, float(base))
                else:
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

            # target : défensif contre target=None explicite. Pour un plancher
            # (">="), viser un peu AU-DESSUS du minimum contractuel a du sens
            # (marge de sécurité) — l'inverse d'un plafond ("<") où on vise
            # légèrement EN DESSOUS.
            raw_target = r.get("target")
            if raw_target is not None:
                r["target"] = float(raw_target)
            else:
                r["target"] = r["threshold"] * (1.1 if r.get("operator") in (">", ">=") else 0.9)

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