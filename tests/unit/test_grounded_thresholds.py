"""
Ancrage des seuils de latence sur les mesures (INTENT_GROUNDED_THRESHOLDS).

Ce que ces tests protegent :

1. Drapeau OFF — le prompt doit rester MOT POUR MOT celui des campagnes
   d'aout. Si ce test tombe, les runs d'aout et ceux d'apres ne sont plus
   comparables et toute la section evaluation du papier est a refaire.

2. Drapeau ON — la bande d'a priori "50-100 ms" doit DISPARAITRE et etre
   remplacee par les percentiles reellement mesures.

3. Degradation gracieuse — pas assez de points, ou historique injoignable :
   on revient au prompt d'origine plutot que de calculer un percentile sur
   une poignee de valeurs.
"""
import asyncio
import importlib
import sys

import pytest

APRIORI = "alerte/temps réel critique ≈ 50-100 ms"
ANCRE   = "latences_mesurees_ms"


_MODULES = ("shared.config", "services.intent_manager.llm_handler")


@pytest.fixture(autouse=True)
def _preserver_sys_modules():
    """
    Ces tests rechargent shared.config pour faire varier le drapeau. Sans
    restauration, les fichiers de test suivants heritent d'un objet config
    different de celui qu'ils ont importe, et echouent sans rapport avec
    leur sujet (constate le 17/08 sur test_provider_registry).
    """
    modules = {k: sys.modules.get(k) for k in _MODULES}
    parents = {}
    for chemin in _MODULES:
        paquet, _, enfant = chemin.rpartition(".")
        mod_parent = sys.modules.get(paquet)
        if mod_parent is not None:
            parents[chemin] = (mod_parent, enfant, getattr(mod_parent, enfant, None))
    yield
    for cle, valeur in modules.items():
        if valeur is None:
            sys.modules.pop(cle, None)
        else:
            sys.modules[cle] = valeur
    for mod_parent, enfant, ancien in parents.values():
        if ancien is not None:
            setattr(mod_parent, enfant, ancien)


def _recharger(monkeypatch, actif: bool):
    """
    Recharge config puis llm_handler avec le drapeau demande.

    import_module (et non reload) : purger sys.modules ne suffit pas, car
    `from shared import config` retrouve l'ancien objet par l'attribut du
    paquet parent. import_module reecrit les deux.
    """
    monkeypatch.setenv("INTENT_GROUNDED_THRESHOLDS", "true" if actif else "false")
    for nom in [k for k in list(sys.modules)
                if k == "shared.config" or "llm_handler" in k]:
        del sys.modules[nom]
    config = importlib.import_module("shared.config")
    assert config.INTENT_GROUNDED_THRESHOLDS is actif
    return importlib.import_module("services.intent_manager.llm_handler")


def _prompt(module, percentiles):
    """Recupere le system_prompt sans appeler aucun LLM ni aucun service."""
    capture = {}

    async def faux_laas(self, sp, up, texte):
        capture["system"] = sp
        capture["user"] = up
        return None

    async def faux_ollama(self, prompt, texte):
        return None

    async def fausses_mesures(self):
        return percentiles

    module.LLMHandler._call_laas = faux_laas
    module.LLMHandler._call_ollama = faux_ollama
    module.LLMHandler._observed_latency_percentiles = fausses_mesures

    asyncio.run(
        module.LLMHandler()._level1_llm(
            "test", {"active_slos": [], "last_intention": None}
        )
    )
    return capture["system"], capture["user"]


MESURES = {"p10": 23.6, "p25": 57.2, "p50": 79.0,
           "p75": 122.9, "p90": 154.7, "n": 200}


def test_off_conserve_le_prompt_des_campagnes(monkeypatch):
    mod = _recharger(monkeypatch, actif=False)
    system, user = _prompt(mod, MESURES)
    assert APRIORI in system, "la bande d'a priori a disparu alors que le drapeau est OFF"
    assert ANCRE not in user, "des mesures ont fuite dans le prompt alors que le drapeau est OFF"


def test_on_remplace_lapriori_par_les_mesures(monkeypatch):
    mod = _recharger(monkeypatch, actif=True)
    system, user = _prompt(mod, MESURES)
    assert APRIORI not in system, "la bande d'a priori subsiste malgre l'ancrage"
    assert "23.6" in system and "154.7" in system, "les percentiles mesures sont absents"
    assert ANCRE in user, "la distribution n'a pas ete transmise dans le contexte"


def test_on_sans_mesures_revient_au_prompt_dorigine(monkeypatch):
    """Historique injoignable ou trop court : ne jamais inventer un percentile."""
    mod = _recharger(monkeypatch, actif=True)
    system, user = _prompt(mod, None)
    assert APRIORI in system, "le repli vers le prompt d'origine n'a pas eu lieu"
    assert ANCRE not in user


def test_relachement_legitime_reste_possible(monkeypatch):
    """
    Une intention qui accepte plus de latence ("encrypt even if it increases
    latency") doit pouvoir monter vers P90. Aucun verrou de non-regression ne
    doit avoir ete ajoute : il ecraserait ce cas, qui est le resultat le plus
    fort d'UC5.
    """
    mod = _recharger(monkeypatch, actif=True)
    system, _ = _prompt(mod, MESURES)
    assert "P75-P90" in system or "154.7" in system
    assert "DOIT monter vers P90" in system


@pytest.mark.parametrize("serie,p,attendu", [
    ([10.0], 50, 10.0),
    ([10.0, 20.0], 0, 10.0),
    ([10.0, 20.0], 100, 20.0),
    ([10.0, 20.0], 50, 15.0),
    ([0.0, 10.0, 20.0, 30.0], 50, 15.0),
])
def test_percentile_interpolation(monkeypatch, serie, p, attendu):
    mod = _recharger(monkeypatch, actif=False)
    assert mod.LLMHandler._percentile(serie, p) == pytest.approx(attendu)
