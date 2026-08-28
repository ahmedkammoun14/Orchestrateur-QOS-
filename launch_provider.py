#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launch_provider.py — Lance UNE stack orchestrateur complete (11 microservices +
hub) dans UNE SEULE fenetre, avec les logs prefixes par service.

Usage :
    python launch_provider.py --provider provider1     # hub 8000, relais 8010, DB 0
    python launch_provider.py --provider provider2     # hub 8100, relais 8110, DB 1

Ctrl+C arrete proprement tous les processus de la stack.
Pour aussi sauver les logs dans un fichier :
    python launch_provider.py --provider provider1 *> logs\p1.log   (PowerShell)
"""
import argparse
import io
import os
import subprocess
import sys
import threading
import time

# Reconfigure stdout/stderr en UTF-8 : sans ca, rediriger la sortie de ce
# lanceur vers un fichier (> log.txt) fait heriter l'encodage de la console
# Windows (cp1252), qui plante sur le moindre accent ou emoji renvoye par un
# service (ex: "Decision", box-drawing). Le crash tue le thread _stream sans
# tuer le processus enfant : son pipe stdout n'est plus draine, se remplit,
# et le service se bloque en silence des son prochain print() -- observe en
# pratique : le hub gele, zero nouveau cycle, aucune erreur visible.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ordre de demarrage (le hub EN DERNIER : il verifie la sante des spokes).
SERVICES = [
    "services.database.app",
    "services.history_loader.app",
    "services.collector.app",
    "services.metrics_manager.app",
    "services.ml_predictor.app",
    "services.decision_intelligence.app",
    "services.intent_manager.app",
    "services.latency_manager.app",
    "services.observability.app",
    "services.placement_arbiter.app",
    "services.provider_relay.app",
]
HUB = "hub.orchestrator_core"

_procs = []


def _env_for(provider: str) -> dict:
    """Variables d'environnement de la stack (offset + provider + redis + URLs)."""
    e = dict(os.environ)
    if provider == "provider1":
        e.update(PROVIDER_ID="provider-1", PORT_OFFSET="0", REDIS_DB="0")
    else:
        e.update(PROVIDER_ID="provider-2", PORT_OFFSET="100", REDIS_DB="1")
    e.update(
        MULTI_PROVIDER_ENABLED="true",   # arbitrage federe : broadcast + bids + arbitre
        ORCHESTRATOR_URL_PROVIDER_1="http://localhost:8000",
        ORCHESTRATOR_URL_PROVIDER_2="http://localhost:8100",
        RELAY_URL_PROVIDER_1="http://localhost:8010",
        RELAY_URL_PROVIDER_2="http://localhost:8110",
        PYTHONUNBUFFERED="1",
    )
    return e


def _stream(name: str, proc: subprocess.Popen) -> None:
    """Recopie la sortie d'un service sur la console, prefixee par son nom."""
    for line in iter(proc.stdout.readline, ""):
        sys.stdout.write(f"[{name:<12}] {line}")
        sys.stdout.flush()


def _spawn(module: str, env: dict) -> subprocess.Popen:
    name = module.split(".")[1] if module.startswith("services") else "hub"
    p = subprocess.Popen(
        [sys.executable, "-u", "-m", module],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    _procs.append(p)
    threading.Thread(target=_stream, args=(name, p), daemon=True).start()
    return p


def _flush_redis(db: str) -> None:
    """
    Vide la base Redis du provider AVANT de demarrer la stack.

    Redis tourne hors de cette stack (WSL) et survit donc a son arret. Les
    listes metrics:{vm}:{metrique} conservent les 50 derniers points (LTRIM),
    y compris ceux de la session precedente. Au redemarrage, la fenetre de
    prediction du ml_predictor (18 points pour la latence) contient d'abord
    100% de donnees de la veille, puis un MELANGE des deux sessions : une
    marche artificielle au milieu de la fenetre, que le GRU n'a jamais vue a
    l'entrainement et qui le fait diverger (predictions > 2000 ms).

    Purge uniquement la base de CE provider (DB 0 ou DB 1) : l'autre provider
    n'est pas touche. Les donnees d'entrainement ne sont pas perdues, elles
    sont persistees a part dans data/qos_history_*.xlsx.
    """
    try:
        import redis
        redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=int(db),
        ).flushdb()
        print(f"  Redis DB {db} purgee — fenetre de prediction repartie a zero")
    except Exception as exc:
        print(f"  /!\\ Purge Redis DB {db} impossible : {exc}")
        print(f"      Purge manuelle : redis-cli -n {db} FLUSHDB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=["provider1", "provider2"])
    ap.add_argument("--wait", type=float, default=3.0,
                    help="secondes entre chaque demarrage de service (defaut 3)")
    ap.add_argument("--keep-redis", action="store_true",
                    help="ne PAS purger Redis au demarrage (garde l'historique "
                         "de la session precedente — deconseille)")
    args = ap.parse_args()

    env    = _env_for(args.provider)
    offset = int(env["PORT_OFFSET"])
    print("=" * 60)
    print(f"  Orchestrateur {args.provider}  ({env['PROVIDER_ID']})")
    print(f"  hub {8000+offset} | dashboard {8009+offset} | relais {8010+offset} | arbitre {8011+offset} | Redis DB {env['REDIS_DB']}")
    print("=" * 60)

    if args.keep_redis:
        print("  Redis CONSERVEE (--keep-redis) : la fenetre de prediction peut")
        print("  contenir l'historique de la session precedente.")
    else:
        _flush_redis(env["REDIS_DB"])

    for mod in SERVICES:
        _spawn(mod, env)
        time.sleep(args.wait)
    _spawn(HUB, env)   # hub en dernier

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[launcher   ] Arret de la stack...")
        for p in _procs:
            p.terminate()
        for p in _procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


if __name__ == "__main__":
    main()
