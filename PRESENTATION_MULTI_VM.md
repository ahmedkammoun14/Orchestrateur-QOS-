# Plan de présentation — Extension multi-VM & multi-provider

> Objectif de la présentation : décrire les changements **d'infrastructure** puis
> les modifications de **l'orchestrateur** apportés pour passer de 4 à 8 VMs et
> rendre la négociation inter-provider démontrable. Chaque section indique **ce
> qu'il faut dire** et **quoi montrer**.

---

## 0. Fil rouge (1 slide)

**Message clé :** « Passer de 4 à 8 VMs *sans matériel supplémentaire*, pour que
l'arbitrage TOPSIS et la négociation inter-provider deviennent visibles en
continu. »

**Le problème de départ (à énoncer) :** avec 4 VMs (1 edge + 1 cloud par
provider), chaque provider couvrait toute la piste → aucune négociation
inter-provider, TOPSIS ne tournait presque jamais.

---

# PARTIE 1 — CHANGEMENTS D'INFRASTRUCTURE

## 1.1 Agent VM universel paramétré (le cœur)

**Avant :** 4 scripts quasi identiques en dur (`edge1_ping_fixeCarac.py`, …),
une VM = un fichier.

**Après :** UN seul `vm_agent_sim.py`, **100 % piloté par variables
d'environnement** (VM_ID, position, capacité, ports, bandes cpu/ram, B/A latence).
On lance **N instances sur la même machine physique** à des ports différents.

**Ce que ça permet :** simuler 3 VMs edge par machine (donc par provider) sans
matériel. Chaque instance = 2 serveurs Flask (`/ping` pour la latence,
`/metrics` pour cpu/ram).

**À montrer :** le fichier `vm_agent_sim.py` (partie `_env(...)`) + un launcher
`launch_edge1_machine.sh` (3 instances, ports 8200/8201/8202).

**Piège rencontré (à mentionner, ça fait sérieux) :** lancer `python3
vm_agent_sim.py` **sans** variables = une seule VM edge par défaut. Toujours
passer par le launcher.

## 1.2 Topologie : 8 VMs, partition transversale préservée

**À dire :** chaque provider possède maintenant **3 edge + 1 cloud**, mais reste
**transversal** — les VMs sont **dispersées sur toute la piste**, pas regroupées
en zones. Provider = propriétaire, PAS zone géographique.

| Provider | VMs |
|---|---|
| provider-1 | edge1, edge1b, edge1c, cloud1 |
| provider-2 | edge2, edge2b, edge2c, cloud2 |

**À montrer :** la carte du simulateur (8 VMs colorées, providers entrelacés).

## 1.3 Placement optimisé par l'étude (pas à l'œil)

**À dire :** positions calculées par **k-means** sur la trajectoire réelle (6 arcs
équilibrés), providers **alternés** le long du tracé pour maximiser les handoffs.
Capacités edge **2/3/4 cœurs** (les 2 cœurs forcent le churn), cloud 8.
Clouds **repositionnés** pour devenir éligibles (couverture < 100 ms : 9 %→52 %).

**À montrer :** le tableau des positions + la carte.

## 1.4 Mapping Kubernetes (la décision de conception)

**À dire :** les 3 edges d'un provider partagent le **même node physique** →
même YAML (space_1 / space_2). Conséquence assumée : migration entre edge1/1b/1c
= no-op physique (le pod reste sur le node), mais **vrai changement d'état
d'orchestration**. Migrations edge↔cloud et inter-provider = physiquement réelles.

**À montrer :** `openstack_client.py` (VM_CLUSTER_MAP, NODE_VM_MAP, YAML_PER_VM).

## 1.5 Chaîne PiCar (bridge + simulateur)

**À dire :** `picar_bridge.py` pingue les 8 VMs en parallèle ;
`picarx_sim.html` affiche 8 zones, la VM active, la trajectoire colorée.
Améliorations d'affichage : carte agrandie, contrôles horizontaux, noms sous les VMs.

**À montrer :** le simulateur en direct.

---

# PARTIE 2 — MODIFICATIONS DE L'ORCHESTRATEUR

## 2.1 Le registre, source unique (config.py)

**À dire :** tout dérive de `VM_REGISTRY`. Ajouter une VM = ajouter une entrée →
collector, hub, dashboard, TOPSIS s'adaptent **automatiquement**. C'est ce qui a
rendu le passage à 8 VMs quasi transparent côté code.

- `VM_REGISTRY` : 8 entrées (ip + port).
- `PROVIDER_REGISTRY` : 3 edge + 1 cloud par provider (`PROVIDER_OF_VM` dérivé).
- `VM_CLUSTER_MAP` : les 6 edges → edge-cluster, clouds → cloud-cluster.

**À montrer :** le diff de `config.py`.

## 2.2 Seuil de latence 40 ms — justifié par l'étude

**À dire :** ce n'est **pas** un réglage arbitraire. L'étude (vrais modules de
décision sur la trajectoire réelle) montre : à T=80 → 3 migrations/tour, 0 %
inter-provider (collant) ; à **T=40 → 14 migrations/tour, 86 % inter-provider**.
Le petit rayon de conformité (~22 cm) crée des trous de couverture par provider →
la voiture qui les traverse déclenche un handoff.

**À montrer :** le tableau seuil vs handoffs (ETUDE_PARAMETRES_DEMO_MULTI_VM.md).

## 2.3 La machine multi-provider (rappel du cœur théorique)

**À dire :** la logique de décision (déjà en place) — chaque provider évalue SES
VMs, TOPSIS reste **intra-provider** (les scores TOPSIS ne sont pas comparables
entre providers à cause de la normalisation min-max), et la comparaison
inter-provider se fait sur un **score de violation normalisé** avec **dead-band**.

**Les 4 chemins (= compteurs dashboard) :**

| Chemin | Compteur | Condition |
|---|---|---|
| A | INTRA | provider courant a des conformes → TOPSIS |
| B | INTER | aucune conforme chez lui, l'autre oui → passation |
| C | NÉGO | aucun provider conforme → négociation (dead-band) |
| D | IMPOSSIBLE | passation refusée / rien d'exploitable |

**À montrer :** le panneau « Raisonnement du cycle » du dashboard.

## 2.4 Le GATE : latence primaire uniquement

**À dire :** en autonomous, **seule la latence (primaire) déclenche** une
migration. Le MI détecte les métriques secondaires (cpu/ram) et les pondère, mais
elles **n'ouvrent jamais la barrière** — elles n'interviennent que dans le
classement TOPSIS *après* qu'une violation de latence a décidé de migrer.

**À montrer :** `decision.py` (le bloc GATE, commentaire explicite) +
`metrics_handler.py` (is_primary True pour latence, False pour MI).

## 2.5 Dashboard & tests

**À dire :** panneau de raisonnement (5 étapes), compteurs de chemins,
badge provider actif. Correctif d'affichage : `vm_active` renseigné même sur un
maintien. Suite de tests : **184 tests verts**, non-régression garantie
(`MULTI_PROVIDER_ENABLED=false` = comportement mono-provider identique).

**À montrer :** `pytest tests/ -q` → 184 passed.

---

# PARTIE 3 — ÉTUDE, CAS & DÉMO

## 3.1 Les cas rencontrés en autonomous (à narrer sur les logs)

- **A.1 Maintien** (le plus fréquent) : latence active < 40 → stay.
- **A.2 Migration intra** : la voiture s'éloigne, une sœur du même provider est proche.
- **B Migration inter** : trou de couverture chez soi, l'autre provider couvre → handoff (dominant à T=40).
- **C Négociation** : personne sous 40 → score de violation + dead-band.
- **D Impossible** : relais injoignable / ML down → maintien forcé.

## 3.2 La démo à 2 intentions (le clou)

- **« contrôle temps réel, latence < 40 ms »** → l'**edge** gagne, migre au fil du
  parcours, handoffs inter-provider (86 %). Montre : valorisation edge + proactif + TOPSIS.
- **« traitement lourd, gros besoin CPU »** → le **cloud** gagne (aucun edge ne
  passe cpu ≥ 1,5). Montre : arbitrage edge/cloud + négociation inter-provider.

**Message :** deux intentions = LLM + MI + TOPSIS + multi-provider démontrés en
une séquence, avec des placements radicalement opposés.

---

# PARTIE 4 — ENSEIGNEMENTS & LIMITES (honnêteté = crédibilité)

- **Couplage physique ↔ ML :** changer un paramètre VM (position, capacité, bande)
  impose de régénérer le dataset et réentraîner ; redémarrer `ml_predictor` (il lit
  `window_size` au démarrage). Vrai enseignement d'ingénierie.
- **Simulation :** migration intra-provider = no-op physique (même node) ; assumé
  et documenté. Le versionnement des scripts VM déployés reste à faire.
- **Transversal préservé :** on n'a PAS regroupé les providers par zone (ce serait
  revenir au partitionnement géographique) — les positions restent dispersées.

---

# ANNEXE — Récapitulatif des fichiers touchés (pour les questions)

**Infrastructure (hors dépôt, sur les machines) :**
- `vm_agent_sim.py` (nouveau, universel) + 4 launchers `launch_*.sh`
- `openstack_client.py` (master) — mappings 8 VMs
- `picar_bridge.py` (Pi) — 8 VMs
- `picarx_sim.html` (Pi) — 8 VMs, carte + affichage

**Orchestrateur (dépôt) :**
- `shared/config.py` — VM_REGISTRY / PROVIDER_REGISTRY / VM_CLUSTER_MAP (8 VMs), seuil 40
- `infrastructure/openstack_client.py` — mappings 8 VMs
- `tests/unit/test_observability_dashboard.py` — assertion rendue dynamique

**Documents produits :**
- `PLAN_SIMULATION_MULTI_VM.md`, `SUIVI_SIMULATION_MULTI_VM.md`,
  `ETUDE_PARAMETRES_DEMO_MULTI_VM.md`, ce plan.
