#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
edge2 (pop1-worker-2) — IP : 194.199.113.28  |  pos map : (30, -10)
Port 5001 : /ping  (latence basée distance)
Port 8200 : /metrics /health  (cpu/ram pour le collector)
"""
from flask import Flask, request, jsonify
import math, time, datetime, threading, psutil

VM_NAME     = "edge 2"
VM_ID       = "edge2"
VM_TYPE     = "edge"
VM_IP       = "194.199.113.28"
VM_X, VM_Y  = 30.0, -10.0
PING_PORT   = 5001
AGENT_PORT  = 8200
BASE_MS     = 0.0
K_MS_PER_CM = 20.0

ping_app = Flask("ping")

@ping_app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp

@ping_app.route("/ping", methods=["POST", "OPTIONS"])
def ping():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(force=True, silent=True) or {}
    try:
        x = float(data["x"]); y = float(data["y"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="POST JSON attendu : {'x':.., 'y':..}"), 400
    dist_cm    = math.hypot(x - VM_X, y - VM_Y)
    latency_ms = BASE_MS + K_MS_PER_CM * dist_cm
    time.sleep(latency_ms / 1000.0)
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {VM_NAME}  car=({x:.1f},{y:.1f})  dist={dist_cm:.1f}cm  lat={latency_ms:.0f}ms")
    return jsonify(vm=VM_NAME, type=VM_TYPE, ip=VM_IP,
                   vm_pos=[VM_X, VM_Y], car_pos=[x, y],
                   distance_cm=round(dist_cm, 2),
                   latency_ms=round(latency_ms, 1))

@ping_app.route("/health")
def ping_health():
    return jsonify(vm=VM_NAME, pos=[VM_X, VM_Y], status="ok")

agent_app = Flask("agent")

@agent_app.route("/metrics")
def metrics():
    return jsonify(vm_id=VM_ID,
                   cpu_usage=psutil.cpu_percent(interval=0.5),
                   ram_usage=psutil.virtual_memory().percent,
                   timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat())

@agent_app.route("/health")
def agent_health():
    return jsonify(status="healthy", vm_id=VM_ID)

if __name__ == "__main__":
    print(f"VM {VM_NAME} | pos=({VM_X},{VM_Y}) | ping:{PING_PORT}  agent:{AGENT_PORT}")
    t = threading.Thread(target=lambda: agent_app.run(host="0.0.0.0", port=AGENT_PORT), daemon=True)
    t.start()
    ping_app.run(host="0.0.0.0", port=PING_PORT)
