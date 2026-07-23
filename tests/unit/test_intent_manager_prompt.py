"""
Test structurel du system_prompt de l'intent_manager
(services/intent_manager/llm_handler.py).

Le LLM lui-même n'est PAS testable en unitaire déterministe (appel réseau
externe, réponse non déterministe) — ce test vérifie uniquement le PROMPT
envoyé :
  1. plusieurs exemples à nombre de SLOs variable (le biais corrigé : un seul
     exemple à 3 SLOs entraînait systématiquement une sortie à 3 SLOs) ;
  2. l'absence de toute référence à la Mutual Information ou à la découverte
     automatique de SLOs secondaires — mécanisme du metrics_manager,
     totalement indépendant du LLM, qui ne doit jamais lui être mentionné.

Aucun appel réseau : `_call_laas`/`_call_ollama` sont remplacés par des
doublures qui capturent le `system_prompt` réellement construit à
l'exécution, sans jamais ouvrir de socket.
"""

import asyncio
import re

from services.intent_manager.llm_handler import LLMHandler


def _capture_system_prompt() -> str:
    """
    Exécute _level1_llm avec _call_laas mocké (aucun réseau) et retourne le
    system_prompt réellement construit — pas une copie figée dans le test,
    le texte source de vérité reste le code de llm_handler.py.
    """
    handler = LLMHandler()
    captured: dict = {}

    async def _fake_call_laas(system_prompt, user_prompt, text):
        captured["system_prompt"] = system_prompt
        # Retourne un résultat exploitable : évite de déclencher le repli
        # Ollama (qui ouvrirait, lui aussi, un appel réseau si non mocké).
        return (None, [])

    handler._call_laas = _fake_call_laas

    asyncio.run(handler._level1_llm("intention de test", {"active_slos": []}))
    assert "system_prompt" in captured, "_call_laas n'a pas été invoqué"
    return captured["system_prompt"]


def test_system_prompt_contient_plusieurs_exemples_a_nombre_variable():
    prompt = _capture_system_prompt()

    # Au moins 3 exemples distincts (chacun introduit son propre bloc "slos":[).
    assert prompt.count('"slos":[') >= 3, (
        "un seul exemple (ou moins) trouvé — le biais few-shot n'est pas corrigé"
    )

    # Un exemple à EXACTEMENT 1 SLO doit exister (bloc slos avec un seul "metric").
    one_slo_blocks = re.findall(r'"slos":\[\{(?:(?!\{).)*?\}\]', prompt, re.DOTALL)
    assert any(block.count('"metric"') == 1 for block in one_slo_blocks), (
        "aucun exemple à 1 SLO trouvé dans le system_prompt"
    )

    # Un exemple à EXACTEMENT 2 SLOs doit exister.
    two_slo_blocks = re.findall(r'"slos":\[.*?\]\}', prompt, re.DOTALL)
    assert any(block.count('"metric"') == 2 for block in two_slo_blocks), (
        "aucun exemple à 2 SLOs trouvé dans le system_prompt"
    )

    # La consigne explicite anti-biais doit être présente.
    assert "Ne complète JAMAIS à 3 par habitude" in prompt


def test_system_prompt_ne_mentionne_jamais_la_mutual_information():
    prompt = _capture_system_prompt().lower()

    # Expressions longues : simple recherche de sous-chaîne suffit.
    interdits_substring = [
        "mutual information", "corrélation", "correlation",
        "secondaire", "découverte automatique",
    ]
    for expr in interdits_substring:
        assert expr not in prompt, f"le system_prompt mentionne '{expr}'"

    # "MI" : trop court pour une recherche de sous-chaîne (matcherait "minimal",
    # "estime", etc.) — recherche du mot isolé (bornes de mot).
    assert re.search(r'\bmi\b', prompt) is None, "le system_prompt mentionne 'MI' isolé"
