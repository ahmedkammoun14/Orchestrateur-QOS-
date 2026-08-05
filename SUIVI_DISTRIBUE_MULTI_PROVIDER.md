# Suivi — Multi-provider distribué (N orchestrateurs)

> Journal d'avancement rempli au fil de la réalisation. Chaque étape n'est
> cochée qu'après **vérification indépendante**, avec la preuve associée.

Légende : ⬜ à faire · 🟡 en cours · ✅ vérifié · ⚠️ vérifié avec réserve

## État de départ (constaté)

- Le relais `provider_relay` (:8010) est **déjà** un routeur HTTP sans état :
  lit `PROVIDER_ORCHESTRATOR_URL`, relaie vers `/intent/relay` du provider cible,
  garde anti-boucle (409). → base solide pour le distribué.
- `PROVIDER_ORCHESTRATOR_URL` = **point de topologie unique** (env-surchargeable).
- Aujourd'hui : mono-processus, les 2 URLs pointent sur le même hub.

## Tableau d'avancement

**PHASE 1 — les 2 orchestrateurs debout (isolés) :**

| # | Étape | Statut | Preuve / note |
|---|---|---|---|
| 1 | `PROVIDER_ID` : filtrage VMs + `PORT_OFFSET` + `REDIS_DB` + URLs internes | ✅ | `config.py`+`models.py` ; `ALL_VM_REGISTRY` global + `VM_REGISTRY` filtré ; offset sur les 10 ports par-provider, PAS relais/openstack ; `REDIS_DB` env ; 186 tests verts ; non-régression `all`/offset 0 |
| 1b | Relais PAR orchestrateur + routage relais↔relais (`/handoff`→pair `/inbound`→hub local) | ✅ | `PROVIDER_RELAY_PORT` offset (8010/8110), `PROVIDER_RELAY_URLS` (pairs) ; `/health` des 2 relais expose `peer_relays`+`local_hub` corrects (P1 hub 8000, P2 hub 8100) |
| 2 | Lancement des 2 stacks (relais PAR provider, une fenêtre/provider) | ✅ | `launch_provider.py` (1 fenêtre, 10 svc+hub+relais, logs préfixés) ; `start_provider.ps1` (variante 1 fenêtre/svc). `start_relay.ps1` **obsolète** (relais intégré à chaque stack) |
| 3 | Bridge : mesures partitionnées par provider | ✅ | chaque dashboard affiche les latences de SES 4 VMs (P1 edge1/1b/1c/cloud1, P2 edge2/2b/2c/cloud2) — le bridge alimente bien les 2 latency_managers (8001/8101) |
| 4 | Validation phase 1 (isolation ports + Redis DB) | ✅ | `/status` divergents : service_vm edge1b vs edge2, cycles 58 vs 59, SLOs 2 vs 3 ; relais `/health` OK ; DB Redis 0/1 séparées (état de boucle en mémoire hub → 0 collision) |

### Note Redis (clarification)
Les 3 clés globales (`slos:active`, `decisions:recent`, `llm:history`) sont
**isolées par `REDIS_DB`** (0 pour P1, 1 pour P2) → **aucune collision**, aucune
suppression de clé nécessaire. Un `FLUSHALL` avant lancement reste conseillé pour
repartir propre (vide toutes les DB).

**PHASE 2 — coordination (après discussion) :**

| # | Étape | Statut | Preuve / note |
|---|---|---|---|
| — | DISCUSSION : qui héberge / qui décide | ⬜ | à concevoir ensemble |
| 5 | Actif décide / standby répond (kubectl) | ⬜ | — |
| 6 | Relais : broadcast d'intention à tous | ⬜ | — |
| 7 | Handoff via relais + validation end-to-end | ⬜ | — |

## Décisions verrouillées (rappel)

- Relais **sans état** (routage + broadcast + anti-boucle).
- **Actif/standby** ; kubectl = vérité de « qui héberge ».
- 100 % HTTP via le relais.
- Partition transversale 8 VMs et comparaison inter-provider **inchangées**.
- Distribué **opt-in** (`PROVIDER_ID`, URLs) ; défaut = mono-processus (non-régression).

## Points à trancher plus tard (avec l'utilisateur)

- Comparaison inter-provider **N-way** (repli séquentiel via relais) — principe
  conservé, structure à concevoir ensemble.
- Fenêtre de handoff / split-brain : idempotence + cooldown, ou verrou léger.

## Journal détaillé

### Étape 1 — PROVIDER_ID + filtrage
- **État** : ⬜
- **Attendu** : `PROVIDER_ID=provider-1` → VM_REGISTRY = {edge1, edge1b, edge1c,
  cloud1} ; `provider-2` → {edge2, edge2b, edge2c, cloud2} ; `all` → les 8.
- **Résultat** : _(à remplir)_

### Étape 2 — Actif/standby
- **État** : ⬜
- **Attendu** : un orchestrateur qui n'héberge pas le service (kubectl) ne
  déclenche AUCUNE migration ; il répond correctement à `/intent/relay`.
- **Résultat** : _(à remplir)_

### Étape 3 — Broadcast d'intention
- **État** : ⬜
- **Attendu** : une intention envoyée au relais arrive aux 2 orchestrateurs
  (mêmes SLOs des deux côtés, `GET /status` concordant).
- **Résultat** : _(à remplir)_

### Étape 4 — Handoff via relais
- **État** : ⬜
- **Attendu** : P1 sans VM conforme → handoff routé par le relais vers P2 → P2
  évalue et (si gagnant) exécute la migration kubectl réelle.
- **Résultat** : _(à remplir)_

### Étape 5 — Bridge partitionné
- **État** : ⬜
- **Attendu** : chaque orchestrateur reçoit uniquement les latences de ses 4 VMs.
- **Résultat** : _(à remplir)_

### Étape 6 — 2 processus réels
- **État** : ⬜
- **Attendu** : `start_provider1` (:8000) et `start_provider2` (:8001) tournent ;
  `PROVIDER_ORCHESTRATOR_URL` pointe sur les deux ; le relais route entre eux.
- **Résultat** : _(à remplir)_

### Étape 7 — Validation end-to-end
- **État** : ⬜
- **Attendu** : la voiture roule ; handoffs inter-provider **réels** entre les 2
  processus ; à tout instant **un seul** orchestrateur héberge le service.
- **Résultat** : _(à remplir)_
