# Plan d'Implémentation – Architecture Multi-Provider xQoS

## Vue d'ensemble

Ce document décrit les évolutions à apporter au projet afin d'implémenter une orchestration **multi-provider**, **explicable** et **orientée intentions (Intent-Based Networking)**, conformément aux objectifs du papier scientifique.

---

# 1. PROVIDER_REGISTRY — Profils de providers avec vocabulaire QoS propre

## Objectif

Faire exister la notion de **provider** dans le code (aujourd'hui le terme n'apparaît pratiquement que dans un commentaire).

Cette étape constitue le **socle de toute l'architecture** et répond directement à **l'Objectif 2** du papier.

---

## Étapes

### 1. Ajouter un `PROVIDER_REGISTRY`

Dans `shared/config.py`, ajouter un registre à côté de `VM_CLUSTER_MAP`.

```python
PROVIDER_REGISTRY = {
    "edge-provider": {
        "vms": ["edge1", "edge2"],

        "vocabulary": {
            "low_latency": {
                "metric": "latency",
                "operator": "<",
                "value": 20.0
            },

            "high_reliability": {
                ...
            }
        },

        "capabilities": [
            "migration",
            "scaling"
        ],

        "constraints": {
            "latency": {
                "min_feasible": 10.0
            }
        }
    },

    "cloud-provider": {
        ...
        "vocabulary": {
            "low_latency": {
                "value": 40.0
            }
        }
    }
}
```

---

### 2. Ajouter les modèles Pydantic

Dans `shared/models.py` :

* `ProviderProfile`
* `ProviderInterpretation`

---

### 3. Remplacer progressivement `VM_CLUSTER_MAP`

Les lectures devront désormais passer par le `PROVIDER_REGISTRY`.

Pour préserver la compatibilité :

* conserver `VM_CLUSTER_MAP`
* le générer automatiquement comme alias dérivé du registre.

---

## Bénéfices

Les deux clusters deviennent de véritables **acteurs autonomes** possédant leur propre interprétation des concepts QoS.

Exemple :

* Edge :

  * "Low latency" = **< 20 ms**

* Cloud :

  * "Low latency" = **< 40 ms**

C'est exactement l'exemple présenté dans le papier.

Cette évolution ne nécessite aucune modification de l'infrastructure puisqu'il s'agit uniquement d'une nouvelle configuration.

---

# 2. Moteur de traduction Intent → Exigences par Provider

## Objectif

Traduire les SLO globaux extraits par le LLM en interprétations spécifiques à chaque provider.

Il s'agit du **cœur de l'Objectif 2** et de la principale contribution à l'explicabilité.

---

## Étapes

Créer :

```
services/
└── intent_manager/
    └── provider_translator.py
```

avec une classe :

```text
ProviderTranslator
```

---

### Entrée

La sortie actuelle de :

```
LLMHandler.handle()
```

c'est-à-dire :

```
Liste des SLO globaux
```

---

### Sortie

Une structure :

```python
{
    provider_id: [
        provider_specific_SLOs
    ]
}
```

---

### Implémenter la logique de traduction

Cas qualitatif :

```
Intent :
"low latency"
```

↓

Utiliser le vocabulaire du provider.

---

Cas quantitatif :

```
30 ms
```

↓

Comparer avec les contraintes du provider.

Résultat :

* feasible
* degraded
* infeasible

---

### Gérer les connaissances incomplètes

Si un provider ne possède pas une métrique :

* statut = unknown
* fallback vers `METRICS_REGISTRY`
* tracer explicitement ce choix pour l'explication.

---

### Intégration

Après le merge effectué dans :

```
handle()
```

le payload envoyé au Hub devra contenir :

* les SLO globaux
* les interprétations par provider

---

## Bénéfices

Un même intent produira plusieurs interprétations.

Exemple :

```
Intent

↓

Edge :
latency <20 ms

↓

Cloud :
latency <40 ms
```

Cette démonstration correspond exactement à l'argument scientifique développé dans le papier.

Le module est totalement indépendant des entrées/sorties (I/O), ce qui facilite les tests unitaires.

---

# 3. Étape « Faisabilité Provider » dans le cycle du Hub

## Objectif

Simuler la boucle :

```
Intent

↓

Interprétation Provider

↓

Réponse Provider

↓

Sélection finale
```

sans modifier l'infrastructure actuelle.

---

## Étapes

Ajouter dans :

```
hub/orchestrator_core.py
```

une nouvelle étape :

```
_step7b_provider_feasibility()
```

placée entre :

```
_step7_predict()

↓

_step8_decide()
```

---

### Fonctionnement

Pour chaque provider :

Comparer :

```
SLO adaptés
```

avec :

```
Prédictions ML
```

des VMs appartenant à ce provider.

---

### Réponse

Retourner une structure :

```python
{
    "provider": "...",
    "feasible": True,
    "margin": ...,
    "reason": ...
}
```

---

### Modifier `_step8_decide`

Le TOPSIS ne devra considérer que :

* les providers faisables

tout en conservant la règle actuelle :

```
La VM active reste toujours candidate.
```

---

### Instrumentation

Ajouter :

```python
prof.step("provider_feasibility")
```

afin d'intégrer cette étape aux mesures Excel existantes.

---

## Bénéfices

Le pipeline devient :

```
Intent

↓

Interprétations

↓

Réponses Providers

↓

TOPSIS

↓

Décision
```

au lieu d'un simple pool plat de VMs.

Le TOPSIS bénéficie d'un pré-filtrage réduisant les migrations inutiles.

---

# 4. Trace de raisonnement structurée (`ReasoningTrace`)

## Objectif

Matérialiser l'explicabilité demandée dans l'Objectif 3.

Aujourd'hui, les informations sont dispersées entre :

* logs MI
* TOPSIS
* audit log

---

## Étapes

Créer dans :

```
shared/models.py
```

le modèle :

```
ReasoningTrace
```

contenant :

```
Intent brut

↓

SLOs

↓

Interprétations Provider

↓

Réponses de faisabilité

↓

Scores TOPSIS

↓

Décision

↓

Type d'action
```

---

### Construction

Le remplir progressivement dans :

```
_FlowContext
```

Chaque étape complète la partie qui la concerne.

Les données existent déjà.

Il s'agit simplement de les conserver.

---

### Persistance

Sauvegarder dans Redis :

```
reasoning:{cycle}
```

---

### API

Ajouter :

```
GET /reasoning/{cycle}
```

---

### Optionnel

Réutiliser :

```
LLMHandler
```

pour générer automatiquement un résumé en langage naturel.

Le papier cite explicitement cette possibilité.

---

## Bénéfices

Chaque décision devient :

* explicable
* auditable
* rejouable

Cette trace servira également d'entrée au dashboard.

---

# 5. Dashboard — Intent → Interprétations → Décision

## Objectif

Transformer le dashboard actuel en véritable dashboard d'explicabilité.

---

## Étapes

### Ajouter un panneau "Intent"

Afficher :

* texte brut
* tableau des SLOs

avec :

* métrique
* opérateur
* seuil
* unité

---

### Ajouter une comparaison Providers

Deux colonnes :

```
Edge Provider

Cloud Provider
```

montrant :

* interprétation locale
* faisabilité

avec un code couleur.

---

### Ajouter la `ReasoningTrace`

Afficher une chronologie :

```
Intent

↓

Traduction

↓

Faisabilité

↓

TOPSIS

↓

Décision
```

en utilisant le mécanisme SSE existant.

---

### Ajouter le résumé LLM

Afficher un résumé généré automatiquement sous la décision.

---

## Bénéfices

La démonstration ne montre plus uniquement des métriques.

Elle expose également le raisonnement complet ayant conduit à la décision.

---

# 6. Types d'action au-delà de la migration

## Objectif

Étendre les capacités de décision.

Aujourd'hui :

```
Migration uniquement
```

Demain :

* migration
* scaling
* priorisation
* stay

---

## Étapes

Modifier la sortie de :

```
decision_intelligence
```

pour retourner :

```python
{
    "action": "...",
    "target": "...",
    "params": ...
}
```

---

### Première règle

Violation CPU ou RAM :

↓

```
Scale
```

---

Violation Latence :

↓

```
Migration
```

---

### OpenStack

Ajouter les commandes :

```
kubectl scale
```

et

```
Istio VirtualService patch
```

en réutilisant le mécanisme SSH existant.

---

### Journalisation

Enregistrer :

* type d'action
* paramètres

dans :

* ReasoningTrace
* Audit Log

tout en respectant :

```
MIGRATION_COOLDOWN_S
```

---

## Bénéfices

L'orchestrateur choisit désormais l'action la moins coûteuse.

Cela réduit :

* les migrations inutiles
* le thrashing

et couvre davantage les capacités fonctionnelles du papier.

---

# 7. Scénario d'évaluation — Cohérence des interprétations

## Objectif

Produire les résultats expérimentaux démontrant la cohérence des interprétations multi-provider.

---

## Étapes

Créer :

```
scripts/
└── eval_interpretation_consistency.py
```

---

### Rejouer plusieurs catégories d'intents

* quantitatifs
* qualitatifs
* ambigus
* conflictuels

---

### Générer un rapport Excel

Colonnes :

* Intent
* Interprétation Edge
* Interprétation Cloud
* Verdict Edge
* Verdict Cloud
* Écarts

---

### Ajouter un scénario de démonstration

Cas 1 :

```
Seul Edge satisfait l'intent
```

↓

Décision Edge

---

Cas 2 :

```
Seul Cloud satisfait l'intent
```

↓

Décision Cloud

Chaque décision est accompagnée de sa trace d'explication.

---

## Bénéfices

Le script fournit :

* des tableaux exploitables dans le mémoire
* des résultats reproductibles
* un scénario de démonstration pour la soutenance

---

# Ordre recommandé d'implémentation

```
1. Provider Registry
        │
        ▼
2. Provider Translator
        │
        ▼
3. Provider Feasibility
        │
        ▼
4. Reasoning Trace
        │
        ▼
5. Dashboard
```

Les éléments suivants peuvent ensuite être développés en parallèle :

```
6. Nouveaux types d'action

7. Scripts d'évaluation
```

---

# Priorités

## Priorité 1 (bloquante)

* Provider Registry
* Provider Translator
* Provider Feasibility

---

## Priorité 2

* ReasoningTrace
* Dashboard

---

## Priorité 3

* Types d'action
* Évaluation expérimentale

---

# Conclusion

Les trois premiers éléments (**Provider Registry**, **Provider Translator** et **Provider Feasibility**) constituent la chaîne fonctionnelle indispensable de l'architecture :

```
Intent
      │
      ▼
Provider Registry
      │
      ▼
Provider Translator
      │
      ▼
Provider Feasibility
      │
      ▼
TOPSIS
      │
      ▼
Décision
```

Une fois cette base en place, les mécanismes d'explicabilité (`ReasoningTrace`, Dashboard) viennent rendre chaque décision totalement transparente et auditable. Les nouveaux types d'action (migration, scaling, priorisation) ainsi que les scripts d'évaluation permettent ensuite de compléter les objectifs scientifiques et de produire les démonstrations attendues dans le cadre du projet xQoS.
