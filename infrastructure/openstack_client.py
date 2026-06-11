import json
import logging
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Any, Optional

# ── Configuration ─────────────────────────────────────────────
PORT      = 8024
STAGE_DIR = "/home/ubuntu/stage"

# Mapping VM → cluster kubectl
VM_CLUSTER_MAP: Dict[str, str] = {
    "edge1":  "edge-cluster",
    "edge2":  "edge-cluster",
    "cloud1": "cloud-cluster",
    "cloud2": "cloud-cluster",
}

# Mapping node Kubernetes → vm_id
NODE_VM_MAP: Dict[str, str] = {
    "pop1-worker-1": "edge1",
    "pop1-worker-2": "edge2",
    "pop2-worker-1": "cloud1",
    "pop2-worker-2": "cloud2",
}

K8S_NAMESPACE       = "tc"
STREAM_SOURCE_FILE  = "tc-stream-source-cloud.yaml"
STREAM_SOURCE_LABEL = "app=stream-source"

# ── Logger JSON ───────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level":     record.levelname,
            "service":   "openstack_client",
            "event":     getattr(record, "event", "generic"),
            "message":   record.getMessage(),
        })

log = logging.getLogger("OpenStackClient")
log.setLevel(logging.INFO)
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(JSONFormatter())
    log.addHandler(h)
log.propagate = False


# ── Helpers kubectl ───────────────────────────────────────────

def _run_kubectl(args: list, timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = ["kubectl"] + args
    log.info(f"kubectl {' '.join(args)}", extra={"event": "kubectl_exec"})
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _get_active_vm() -> Optional[str]:
    """
    Cherche sur quel node physique tc-stream-source tourne (Running).
    Retourne le vm_id exact (edge1/edge2/cloud1/cloud2) via NODE_VM_MAP.
    """
    seen_clusters = set()
    for vm_id, cluster in VM_CLUSTER_MAP.items():
        if cluster in seen_clusters:
            continue
        seen_clusters.add(cluster)
        try:
            result = _run_kubectl([
                "--context", cluster,
                "get", "pods",
                "-n", K8S_NAMESPACE,
                "-l", STREAM_SOURCE_LABEL,
                "--field-selector", "status.phase=Running",
                "-o", "jsonpath={.items[0].spec.nodeName}"
            ], timeout=10)

            if result.returncode == 0 and result.stdout.strip():
                node_name = result.stdout.strip()
                found_vm  = NODE_VM_MAP.get(node_name)
                if found_vm:
                    log.info(
                        f"Active pod on node={node_name} → vm={found_vm} ({cluster})",
                        extra={"event": "active_vm_found"}
                    )
                    return found_vm
                else:
                    log.warning(
                        f"Unknown node {node_name} — using first VM of {cluster}",
                        extra={"event": "node_unknown"}
                    )
                    for v, c in VM_CLUSTER_MAP.items():
                        if c == cluster:
                            return v

        except Exception as e:
            log.warning(
                f"kubectl check failed for {cluster}: {e}",
                extra={"event": "kubectl_check_failed"}
            )
    return None


def _delete_stream_source(cluster: str) -> bool:
    """Supprime tc-stream-source sur un cluster (avant migration)."""
    try:
        result = _run_kubectl([
            "--context", cluster,
            "delete", "deployment", "tc-stream-source",
            "-n", K8S_NAMESPACE,
            "--ignore-not-found"
        ])
        if result.returncode == 0:
            log.info(f"Deleted stream-source on {cluster}",
                     extra={"event": "delete_success"})
            return True
        log.warning(
            f"Delete returned {result.returncode}: {result.stderr[:200]}",
            extra={"event": "delete_warn"}
        )
        return False
    except Exception as e:
        log.warning(f"Delete failed on {cluster}: {e}",
                    extra={"event": "delete_failed"})
        return False


def _apply_stream_source(cluster: str) -> bool:
    """Applique tc-stream-source sur le cluster cible."""
    try:
        result = _run_kubectl([
            "--context", cluster,
            "apply", "-f", f"{STAGE_DIR}/{STREAM_SOURCE_FILE}",
            "-n", K8S_NAMESPACE
        ])
        if result.returncode == 0:
            log.info(f"Stream source applied on {cluster}",
                     extra={"event": "apply_success"})
            return True
        log.error(
            f"apply failed on {cluster}: {result.stderr[:300]}",
            extra={"event": "apply_failed"}
        )
        return False
    except subprocess.TimeoutExpired:
        log.error("kubectl apply timeout", extra={"event": "apply_timeout"})
        return False
    except Exception as e:
        log.error(f"Unexpected error: {e}", extra={"event": "apply_error"})
        return False


# ── HTTP Handler ──────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Silencer les logs HTTP par défaut

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {
                "status":    "healthy",
                "service":   "openstack_client",
                "master":    "194.199.113.8",
                "stage_dir": STAGE_DIR,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

        elif self.path == "/active_vm":
            active = _get_active_vm()
            if active is None:
                self._send_json(200, {
                    "active_vm": None,
                    "status":    "no_active_pod",
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            else:
                self._send_json(200, {
                    "active_vm": active,
                    "cluster":   VM_CLUSTER_MAP.get(active),
                    "node":      next((n for n, v in NODE_VM_MAP.items() if v == active), None),
                    "status":    "running",
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/migrate":
            try:
                payload      = self._read_body()
                from_vm: str = payload.get("from_vm", "")
                to_vm:   str = payload.get("to_vm", "")

                if not to_vm:
                    self._send_json(400, {"error": "to_vm is mandatory"})
                    return

                from_cluster = VM_CLUSTER_MAP.get(from_vm)
                to_cluster   = VM_CLUSTER_MAP.get(to_vm)

                if not to_cluster:
                    self._send_json(400, {"error": f"Unknown VM: {to_vm}"})
                    return

                log.info(
                    f"Migration {from_vm}({from_cluster}) → {to_vm}({to_cluster})",
                    extra={"event": "migration_requested"}
                )

                # ← Toujours supprimer sur la source
                # que ce soit même cluster ou cluster différent
                if from_cluster:
                    deleted = _delete_stream_source(from_cluster)
                    log.info(
                        f"Delete on {from_cluster}: {'OK' if deleted else 'WARN (ignored)'}",
                        extra={"event": "migration_delete"}
                    )

                # Appliquer sur la cible
                success = _apply_stream_source(to_cluster)

                if not success:
                    self._send_json(502, {
                        "error": f"Migration to {to_vm} ({to_cluster}) failed"
                    })
                    return

                log.info(
                    f"Migration complete → {to_vm} ({to_cluster})",
                    extra={"event": "migration_executed"}
                )

                self._send_json(200, {
                    "status":    "migrated",
                    "from_vm":   from_vm,
                    "to_vm":     to_vm,
                    "cluster":   to_cluster,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })

            except Exception as e:
                log.error(f"Error in /migrate: {e}", extra={"event": "migrate_error"})
                self._send_json(500, {"error": str(e)})

        else:
            self._send_json(404, {"error": "Not found"})


# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info(f"openstack_client started on port {PORT}",
             extra={"event": "startup"})
    log.info(f"stage_dir={STAGE_DIR}", extra={"event": "startup"})
    server.serve_forever()