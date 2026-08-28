"""
Test cible, sans voiture ni campagne : mesure directement le cout d'une
negociation reelle (/broadcast -> pair /inbound/evaluate -> hub local),
sur le vrai reseau LAAS, pour comparer au chiffre deja etabli sur 5 runs
(Federation Total : 2,6 a 3,2s en moyenne selon le run).

Lecture seule : /broadcast et /inbound/evaluate n'executent aucune decision,
aucune migration, aucun appel kubectl (voir hub/orchestrator_core.py::evaluate,
docstring "NE DECLENCHE AUCUNE decision"). Sans danger pour le service en cours.

PREREQUIS : les processus provider-1 ET provider-2 (hub + relay) doivent
deja tourner normalement, avec au moins un cycle de collecte deja fait
(sinon /evaluate renvoie 503 "last_collected vide"). Lancer ce script SUR
LA MACHINE de provider-1 (ou la ou PROVIDER_RELAY_URLS resout correctement),
dans le meme venv que le reste du projet.

Usage :
    ./venv/Scripts/python.exe scripts/analyse/bench_negotiation.py
    ./venv/Scripts/python.exe scripts/analyse/bench_negotiation.py -n 50 --from-provider provider-2
"""
import argparse
import statistics
import sys
import time

import httpx

from shared import config

parser = argparse.ArgumentParser()
parser.add_argument("-n", "--count", type=int, default=30, help="nombre d'appels (defaut 30)")
parser.add_argument("--from-provider", default="provider-1", help="provider initiateur (defaut provider-1)")
parser.add_argument("--timeout", type=float, default=10.0, help="timeout par appel, secondes")
args = parser.parse_args()

if args.from_provider not in config.PROVIDER_RELAY_URLS:
    sys.exit(f"provider inconnu : {args.from_provider} (attendu parmi {list(config.PROVIDER_RELAY_URLS)})")

url = f"{config.PROVIDER_RELAY_URLS[args.from_provider]}/broadcast"
payload = {
    "slos": [{"metric": "latency", "threshold": 28.0, "operator": "<", "is_primary": True, "weight": 1.0}],
    "intent_id": "bench-negotiation-timing",
    "incumbent_vm": None,
    "from_provider": args.from_provider,
}

print(f"Cible : {url}")
print(f"Depuis : {args.from_provider}")
print(f"{args.count} appels...\n")

durees_ms = []
erreurs = 0

with httpx.Client(timeout=args.timeout) as client:
    for i in range(1, args.count + 1):
        t0 = time.perf_counter()
        try:
            resp = client.post(url, json=payload)
            dt_ms = (time.perf_counter() - t0) * 1000
            if resp.status_code == 200:
                body = resp.json()
                n_bids = len(body.get("bids", []))
                n_err = len(body.get("errors", []))
                print(f"  [{i:2d}/{args.count}] {dt_ms:7.1f} ms   bids={n_bids} erreurs={n_err}")
                durees_ms.append(dt_ms)
                if n_err:
                    print(f"           -> {body['errors']}")
            else:
                erreurs += 1
                print(f"  [{i:2d}/{args.count}] HTTP {resp.status_code} — {resp.text[:200]}")
        except Exception as exc:
            erreurs += 1
            print(f"  [{i:2d}/{args.count}] ECHEC — {type(exc).__name__}: {exc}")

print(f"\n{'='*60}")
if durees_ms:
    print(f"  n={len(durees_ms)} reussis, {erreurs} echecs")
    print(f"  moyenne = {statistics.mean(durees_ms):.0f} ms")
    print(f"  mediane = {statistics.median(durees_ms):.0f} ms")
    print(f"  min     = {min(durees_ms):.0f} ms")
    print(f"  max     = {max(durees_ms):.0f} ms")
    print(f"\n  Reference (5 runs, avant ce correctif) : 2600 a 3200 ms en moyenne.")
    print(f"  Si la moyenne ci-dessus est nettement en dessous -> le correctif aide.")
else:
    print(f"  Aucun appel reussi ({erreurs} echecs) — verifier que provider-1 ET")
    print(f"  provider-2 tournent, et qu'au moins un cycle de collecte a eu lieu.")
