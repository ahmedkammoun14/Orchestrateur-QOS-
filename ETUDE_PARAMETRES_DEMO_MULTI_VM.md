# Étude des paramètres de démo (8 VMs)

> Simulation pilotant les **vrais modules de décision** (`hub/provider_arbitration.py`
> — `evaluate_vm`, `evaluate_provider`, `negotiate`) sur la trajectoire PiCar
> réelle (398 points, ~480 cm). VM active **collante** (sticky) et **vrai gate**
> (migration seulement si la VM active viole une métrique PRIMAIRE), fidèle à
> `_decide_multi_provider` (chemin A intra-provider prioritaire, B/C/D négociation).
> CPU/RAM au milieu de bande (edge 65 %, cloud cpu 16,5 % / ram 22,5 %).

## Rappel du dispositif

8 VMs, capacités **edge 2/3/4 cœurs**, cloud 8. Positions optimisées (voir
`PLAN_SIMULATION_MULTI_VM.md`). Providers : p1 = {edge1, edge1b, edge1c, cloud1},
p2 = {edge2, edge2b, edge2c, cloud2}.

## Résultats

### Scénario A — autonomous (latence seule, primaire)

| T(ms) | migr/tour | intra | inter | %TOPSIS≥2 | →cloud | VMs utilisées | % temps cloud actif |
|---|---|---|---|---|---|---|---|
| 70 | 3 | 3 | 0 | 67 % | 0 | 2/8 | 0 % |
| 80 | 3 | 3 | 0 | 67 % | 0 | 2/8 | 0 % |
| 90 | 1 | 1 | 0 | 100 % | 0 | 2/8 | 0 % |
| 100 | 1 | 1 | 0 | 100 % | 0 | 2/8 | 0 % |

### Scénario B — enhanced 60/20/20 (latence 0.6, cpu≥1 0.2, ram≥1 0.2, tous primaires)

| T(ms) | migr/tour | intra | inter | %TOPSIS≥2 | →cloud | VMs utilisées | % temps cloud actif |
|---|---|---|---|---|---|---|---|
| 70 | 9 | 5 | 4 | 7 % | 0 | 4/8 | 0 % |
| 80 | 8 | 5 | 3 | 25 % | 0 | 4/8 | 0 % |
| 90 | 1 | 1 | 0 | 100 % | 0 | 2/8 | 0 % |
| 100 | 1 | 1 | 0 | 100 % | 0 | 2/8 | 0 % |

### Scénario C — calcul lourd (cpu≥2 cœurs 0.6, latence 0.4)

| T(ms) | migr/tour | intra | inter | %TOPSIS≥2 | →cloud | VMs utilisées | % temps cloud actif |
|---|---|---|---|---|---|---|---|
| 70 | 1 | 0 | 1 | 0 % | 1 | 2/8 | 75 % |
| 80 | 1 | 0 | 1 | 0 % | 1 | 2/8 | 76 % |
| 90 | 5 | 1 | 4 | 0 % | 5 | 3/8 | 100 % |
| 100 | 5 | 1 | 4 | 0 % | 5 | 3/8 | 100 % |

## Conclusions (contre-intuitives)

1. **Le seuil ne crée quasiment pas de dynamisme en autonomous.** 1 à 3
   migrations/tour de 70 à 100 ms. La staticité vient de deux causes
   structurelles : le **rayon de conformité edge énorme** (53 cm à T=100 pour une
   piste de 70 cm) ET la **préférence intra-provider** (on ne quitte un provider
   que s'il n'a AUCUNE VM conforme). Baisser le seuil ne règle pas le problème.

2. **La richesse vient de la COMPOSITION des SLOs, pas du seuil.**
   - Latence seule → statique, 2 VMs, jamais de cloud.
   - Latence + cpu/ram (60/20/20) → dynamique (8-9 migr. à T≤80, 4 VMs, négociation
     inter-provider), car les **edges 2 cœurs (edge1, edge2) échouent toujours sur
     cpu/ram** et forcent la migration. Mais TOPSIS a rarement ≥2 candidats.
   - Calcul lourd (cpu≥2) → **le cloud domine** (actif 100 % du temps à T≥90),
     car aucun edge (même 4 cœurs = 1,4 dispo) ne passe cpu≥2 → seul le cloud passe.

3. **Le cloud ne gagne JAMAIS sur une latence dominante.** Sa latence plancher
   (50 ms) est toujours ≥ celle d'un edge dans la même région ; avec un poids
   latence 0.6, un edge conforme le bat systématiquement. **L'arbitrage edge/cloud
   n'apparaît que sur une intention à dominante ressources** (scénario C).

## Recommandation : une DÉMONSTRATION par intentions, pas un réglage de seuil

Plutôt que chercher un seuil magique, exploiter le mode **enhanced** (le LLM
extrait des SLOs différents selon l'intention) pour raconter deux histoires :

- **Intention « streaming vidéo fluide, faible latence »** → SLO latence dominante,
  seuil **80 ms** → l'**edge** gagne et migre au fil du parcours (scénario B, T=80 :
  ~8 migrations, 4 VMs, négociation inter-provider). Montre : valorisation edge,
  migration proactive, TOPSIS intra-provider.
- **Intention « traitement lourd / rendu, besoin CPU »** → SLO cpu dominante
  (cpu≥2) → le **cloud** gagne (scénario C). Montre : arbitrage edge/cloud,
  passation inter-provider (négociation), le cloud comme ressource de calcul.

Deux intentions successives = LLM + MI + TOPSIS + multi-provider démontrés en une
séquence, avec des placements radicalement différents. C'est bien plus parlant
qu'un tour monotone.

## Complément — le SEUIL pilote les handoffs INTER-provider (positions actuelles)

Mesure faite APRÈS coup, sur les positions déjà déployées (vrais modules,
autonomous latence seule) :

| Seuil | migr/tour | intra | INTER | % inter |
|---|---|---|---|---|
| **40** | **14** | 2 | **12** | **86 %** |
| 50 | 13 | 5 | 8 | 62 % |
| 60 | 11 | 7 | 4 | 36 % |
| 80 | 3 | 3 | 0 | 0 % |

**Explication.** À T=40 le rayon de conformité edge tombe à ~22 cm ; les 3 edges
d'un provider ne couvrent plus toute la piste → des **trous de couverture** par
provider apparaissent → la voiture qui traverse un trou de provider-2 (mais
couvert par provider-1) déclenche un **handoff inter-provider**. À T=80 (rayon
53 cm) chaque provider couvre presque tout → 0 handoff (collant). La préférence
intra-provider fait le reste : sans trou, on ne quitte jamais son provider.

**Conséquence assumée** : à T=40 le cloud (plancher 50 ms > 40) n'est JAMAIS
conforme en latence → l'intention 1 est 100 % edge. Le cloud reste le placement de
l'intention 2 (CPU). Rôles nets : edge = temps réel, cloud = calcul.

## Réglages retenus (FINAUX)

| Paramètre | Valeur | Raison |
|---|---|---|
| Seuil latence | **40 ms** | 14 migrations/tour dont **86 % inter-provider** sur les positions actuelles ; « contrôle temps réel < 40 ms » défendable (téléopération robot) |
| Positions VMs | **inchangées** | déjà optimales pour les handoffs à T=40 — aucun launcher/HTML à retoucher |
| Capacités edge | **2 / 3 / 4** cœurs | les 2 cœurs forcent le churn ; 3-4 hôtes stables ; contraste net avec cloud 8 |
| Intentions démo | **2** : « contrôle temps réel < 40 ms » (→ edge, handoffs) puis « calcul lourd, CPU » (→ cloud) | montre latence ET arbitrage ressources |

## Limites de l'étude

- CPU/RAM pris au milieu de bande (déterministe) ; en réel, la marche aléatoire
  ajoute de la variance autour de ces résultats.
- TOPSIS approximé (poids dominant) pour désigner le gagnant parmi les conformes ;
  la conformité et la négociation, elles, viennent des vrais modules.
- Un seul tour simulé ; les migrations sont géométriques (indépendantes de la
  vitesse — la vitesse ne change que le rythme en temps réel, pas le nombre/tour).
