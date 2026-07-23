#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
picar_bridge.py — Fusion bridge + picar_client pour PiCar-X
Sert le simulateur HTML + Trajectoire.jpg, envoie la position (x,y)
aux VMs OpenStack pour une latence basée sur la distance, et transmet
les mesures à l'orchestrateur (latency_manager).

Démarrage : python picar_bridge.py
HTML      : http://<IP_PICAR>:8080/

Fichiers requis dans le même dossier :
  - picarx_sim.html
  - Trajectoire.jpg
"""

import csv
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_file

# ─── Configuration ────────────────────────────────────────────────────────────

HOST = "0.0.0.0"
PORT = 8080

_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(_DIR, "picarx_sim.html")

# URL du latency_manager de l'orchestrateur
LATENCY_MANAGER_URL = "http://140.93.89.92:8001/rtt"

# URL du hub pour récupérer le statut (VM active)
HUB_STATUS_URL = "http://140.93.89.92:8000/status"

# Délai minimum entre deux envois au latency_manager.
# Aligné sur la durée d'un cycle d'orchestration (~5 s) : évite d'envoyer des
# mesures que le hub ignore (un cycle ne peut pas se chevaucher avec le suivant).
SEND_INTERVAL_S = 5.0

# VMs OpenStack
# - port 5001  /ping  → reçoit {x,y}, calcule distance, répond avec latency_ms
# - port 8200  /health → vm_agent.py (orchestrateur, inchangé)
VMS = [
    {"name": "edge 1",  "id": "edge1",  "type": "edge",  "ip": "194.199.113.18", "ping_port": 5001},
    {"name": "edge 2",  "id": "edge2",  "type": "edge",  "ip": "194.199.113.28", "ping_port": 5001},
    {"name": "cloud 1", "id": "cloud1", "type": "cloud", "ip": "194.199.113.66", "ping_port": 5001},
    {"name": "cloud 2", "id": "cloud2", "type": "cloud", "ip": "194.199.113.69", "ping_port": 5001},
]

TIMEOUT_S      = 5.0   # plus long car la VM simule une latence (time.sleep)
UNREACHABLE_MS = 999.0
CSV_FILE       = os.path.join(_DIR, "latences.csv")

# ─── État partagé ─────────────────────────────────────────────────────────────

_cycle_count  = 0
_last_send_ts = 0.0
_send_count   = 0

# ─── CSV ──────────────────────────────────────────────────────────────────────

_csv_lock   = threading.Lock()
_csv_file   = open(CSV_FILE, "a", newline="")
_csv_writer = csv.writer(_csv_file)

if os.stat(CSV_FILE).st_size == 0:
    _csv_writer.writerow(
        ["time", "x_cm", "y_cm"]
        + [f"latency_{v['name']}" for v in VMS]
        + [f"dist_{v['name']}" for v in VMS]
    )

# ─── Application Flask ────────────────────────────────────────────────────────

app   = Flask(__name__)
_pool = ThreadPoolExecutor(max_workers=len(VMS))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ping_vm(args: tuple) -> dict:
    """
    Envoie POST /ping {x, y} à la VM sur port 5001.
    La VM calcule la distance voiture<->VM, dort latency_ms, répond.
    Le RTT mesuré = latence simulée (incluant le sleep côté VM).
    """
    vm, x, y = args
    url = f"http://{vm['ip']}:{vm['ping_port']}/ping"
    payload = json.dumps({"x": x, "y": y}).encode()
    t0 = time.time()
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = json.loads(resp.read())

        rtt_ms     = round((time.time() - t0) * 1000.0, 1)
        latency_ms = body.get("latency_ms", rtt_ms)
        dist_cm    = body.get("distance_cm")

        return {
            "vm":          vm["name"],
            "type":        vm["type"],
            "ok":          True,
            "latency_ms":  latency_ms,
            "rtt_ms":      rtt_ms,
            "distance_cm": dist_cm,
        }
    except Exception:
        return {
            "vm":          vm["name"],
            "type":        vm["type"],
            "ok":          False,
            "latency_ms":  None,
            "rtt_ms":      None,
            "distance_cm": None,
        }


def _fmt_table(headers: list, rows: list) -> str:
    """Petit tableau ASCII encadré pour des logs lisibles."""
    widths = [
        max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    def _line(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right
    out = [_line("┌", "┬", "┐")]
    out.append("│" + "│".join(f" {h:<{w}} " for h, w in zip(headers, widths)) + "│")
    out.append(_line("├", "┼", "┤"))
    for row in rows:
        out.append("│" + "│".join(f" {str(c):<{w}} " for c, w in zip(row, widths)) + "│")
    out.append(_line("└", "┴", "┘"))
    return "\n".join(out)


def _send_to_latency_manager(results: list, cycle: int, x: float, y: float) -> None:
    """
    Transmet les RTT au latency_manager (thread daemon, non bloquant),
    au plus une fois toutes les SEND_INTERVAL_S secondes (≈ 1 cycle).
    Affiche un tableau récapitulatif structuré à chaque envoi.
    """
    global _last_send_ts, _send_count

    now = time.time()
    if now - _last_send_ts < SEND_INTERVAL_S:
        return
    _last_send_ts = now
    _send_count  += 1
    send_n        = _send_count

    ts = datetime.now(timezone.utc).isoformat()
    measurements = [
        {
            "vm_id":     next(v["id"] for v in VMS if v["name"] == r["vm"]),
            "ip":        next(v["ip"] for v in VMS if v["name"] == r["vm"]),
            "rtt_ms":    r["latency_ms"] if r["ok"] else UNREACHABLE_MS,
            "raw_ms":    r["latency_ms"] if r["ok"] else UNREACHABLE_MS,
            "reachable": r["ok"],
            "timestamp": ts,
        }
        for r in results
    ]

    payload = {
        "timestamp":    ts,
        "source":       "picar_bridge",
        "cycle":        cycle,
        "measurements": measurements,
    }

    # ── Envoi HTTP ────────────────────────────────────────────────────────
    status = ""
    try:
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(
            LATENCY_MANAGER_URL,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            status = f"✅ HTTP {resp.status} — transmis à l'orchestrateur"
    except urllib.error.URLError as e:
        status = f"❌ orchestrateur injoignable : {e.reason}"
    except Exception as e:
        status = f"❌ erreur d'envoi : {e}"

    # ── Tableau récapitulatif ─────────────────────────────────────────────
    rows = []
    for r in results:
        dist = f"{r['distance_cm']:.0f} cm" if r.get("distance_cm") is not None else "—"
        lat  = f"{r['latency_ms']:.1f} ms"  if (r.get("ok") and r.get("latency_ms") is not None) else "—"
        etat = "OK" if r.get("ok") else "INJOIGNABLE"
        rows.append([r["vm"], dist, lat, etat])

    table = _fmt_table(["VM", "Distance", "Latence", "État"], rows)
    print(
        f"\n┏━━ 📡 Envoi #{send_n} → orchestrateur  |  {_ts()}  |  "
        f"cycle #{cycle}  |  position=({x:.1f}, {y:.1f})\n"
        f"{table}\n"
        f"┗━━ {status}\n"
    )


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/", methods=["GET"])
def index():
    """Sert le simulateur HTML."""
    return send_file(HTML_FILE)


@app.route("/Trajectoire.jpg", methods=["GET"])
def trajectoire():
    return send_file(os.path.join(_DIR, "Trajectoire.jpg"))


@app.route("/tick", methods=["POST", "OPTIONS"])
def tick():
    """
    Reçoit la position (x, y) de la voiture simulée.
    Envoie POST /ping {x,y} à chaque VM en parallèle.
    La VM calcule distance voiture<->VM, simule la latence (time.sleep),
    et répond avec latency_ms et distance_cm.
    Retourne les résultats au HTML pour affichage.
    Transmet les RTT à l'orchestrateur (latency_manager).
    """
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True, silent=True) or {}
    try:
        x = float(data["x"])
        y = float(data["y"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error='JSON attendu : {"x": ..., "y": ...}'), 400

    # Pings parallèles (chaque VM reçoit {x,y} et répond après son sleep)
    args    = [(vm, x, y) for vm in VMS]
    results = list(_pool.map(_ping_vm, args))

    global _cycle_count
    _cycle_count += 1
    cycle = _cycle_count

    # Envoi au latency_manager (non bloquant, throttlé à SEND_INTERVAL_S)
    threading.Thread(
        target=_send_to_latency_manager,
        args=(results, cycle, x, y),
        daemon=True,
    ).start()

    # Log CSV
    with _csv_lock:
        _csv_writer.writerow(
            [_ts(), round(x, 2), round(y, 2)]
            + [r["latency_ms"] for r in results]
            + [r["distance_cm"] for r in results]
        )
        _csv_file.flush()

    # Log console compact à chaque tick (le détail par VM est dans le tableau d'envoi, toutes les 5 s)
    print(f"[{_ts()}] 🚗 tick — position=({x:.1f}, {y:.1f})")

    return jsonify(car=[x, y], results=results)


@app.route("/vm-status", methods=["GET"])
def vm_status():
    """Proxifie GET /status du hub pour retourner la VM active au HTML."""
    try:
        with urllib.request.urlopen(HUB_STATUS_URL, timeout=3.0) as resp:
            data = json.loads(resp.read())
        return jsonify(
            service_vm=data.get("service_vm"),
            last_decision=data.get("last_decision"),
            cycle=data.get("cycle"),
            cooldown_active=data.get("cooldown_active", False),
        )
    except Exception as e:
        return jsonify(service_vm=None, error=str(e)), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok", vms=[v["name"] for v in VMS], port=PORT)


# ─── Entrée ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"{'='*60}")
    print(f"  PiCar Bridge  →  http://{HOST}:{PORT}/")
    print(f"  HTML          →  {HTML_FILE}")
    vm_list = [v['name'] + ':' + str(v['ping_port']) for v in VMS]
    print(f"  VMs (/ping)   →  {vm_list}")
    print(f"  Orchestrateur →  {LATENCY_MANAGER_URL}")
    print(f"{'='*60}")
    app.run(host=HOST, port=PORT, threaded=True)
