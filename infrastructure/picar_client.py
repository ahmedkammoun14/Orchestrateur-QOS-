"""
infrastructure/picar_client.py — Client PiCar réel (Raspberry Pi).

Standalone — aucune dépendance au projet.
Déploiement :
    scp infrastructure/picar_client.py pi@140.93.64.105:~/picar_client.py
    ssh pi@140.93.64.105 "pip install httpx && python3 picar_client.py"

Ce script :
    - Mesure le RTT applicatif (envoi + exécution + retour) via HTTP GET /health
      vers les 4 VMs OpenStack réelles
    - Envoie les mesures au latency_manager du projet (POST /rtt)
    - Boucle permanente avec compensation d'intervalle exacte
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx

# ---------------------------------------------------------------------------
# Configuration autonome — pas d'import depuis shared/
# ---------------------------------------------------------------------------

TARGET_VMS: Dict[str, Dict[str, Any]] = {
    "edge1":  {"ip": "194.199.113.18", "port": 8200},
    "edge2":  {"ip": "194.199.113.28", "port": 8200},
    "cloud1": {"ip": "194.199.113.66", "port": 8200},
    "cloud2": {"ip": "194.199.113.69", "port": 8200},
}

# URL du latency_manager — configurable via variable d'environnement
LATENCY_MANAGER_ENDPOINT: str = os.getenv(
    "HUB_URL", "http://140.93.89.92:8001/rtt"
)

RTT_TIMEOUT:         float = 2.0    # timeout HTTP par VM (secondes)
UNREACHABLE_RTT:     float = 999.0  # valeur si VM injoignable
INITIAL_DELAY:       float = 3.0    # délai avant premier cycle
COLLECTION_INTERVAL: float = 5.0    # intervalle entre cycles (secondes)
POST_RETRY_COUNT:    int   = 3      # nombre de tentatives POST
POST_RETRY_BACKOFF:  float = 2.0    # délai entre tentatives (secondes)

# ---------------------------------------------------------------------------
# Structured Logging
# ---------------------------------------------------------------------------

class JSONFormatter(logging.Formatter):
    """Émet chaque log en JSON sur une ligne."""

    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
            "event":     getattr(record, "event", "generic_log"),
        }
        if hasattr(record, "extra_data"):
            log_record.update(record.extra_data)
        return json.dumps(log_record)


def setup_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(JSONFormatter())
        logger.addHandler(h)
    return logger


logger = setup_logger("PiCarRealClient")

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class RTTMeasurement:
    vm_id:     str
    ip:        str
    rtt_ms:    float
    raw_ms:    float
    reachable: bool
    timestamp: str


@dataclass
class LatencyPayload:
    timestamp:    str
    source:       str
    cycle:        int
    measurements: List[RTTMeasurement]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp":    self.timestamp,
            "source":       self.source,
            "cycle":        self.cycle,
            "measurements": [asdict(m) for m in self.measurements],
        }

# ---------------------------------------------------------------------------
# Adapter — mesure RTT via HTTP GET /health
# ---------------------------------------------------------------------------

class PiCarAdapter:
    """
    Mesure le RTT applicatif complet (envoi + exécution + retour)
    via HTTP GET /health vers chaque VM OpenStack.
    Protocole HTTP choisi car il inclut le temps d'exécution applicatif,
    contrairement au ping ICMP qui ne mesure que la couche réseau.
    """

    async def measure_vm(
        self,
        client: httpx.AsyncClient,
        vm_id: str,
        vm_info: Dict[str, Any],
    ) -> RTTMeasurement:
        """Mesure le RTT d'une VM via GET /health."""
        ip:   str = vm_info["ip"]
        port: int = vm_info["port"]
        url:  str = f"http://{ip}:{port}/health"
        timestamp: str = datetime.now(timezone.utc).isoformat()

        start = time.perf_counter()
        try:
            response = await client.get(url, timeout=RTT_TIMEOUT)
            raw_ms = (time.perf_counter() - start) * 1000

            if response.status_code == 200:
                logger.info(
                    f"RTT measured for {vm_id}",
                    extra={
                        "event": "rtt_measured",
                        "extra_data": {"vm_id": vm_id, "rtt_ms": round(raw_ms, 3)},
                    },
                )
                return RTTMeasurement(
                    vm_id=vm_id, ip=ip,
                    rtt_ms=raw_ms, raw_ms=raw_ms,
                    reachable=True, timestamp=timestamp,
                )
            return self._unreachable(vm_id, ip, timestamp, f"status_{response.status_code}")

        except (httpx.RequestError, httpx.TimeoutException) as exc:
            return self._unreachable(vm_id, ip, timestamp, type(exc).__name__)

    def _unreachable(
        self, vm_id: str, ip: str, timestamp: str, reason: str
    ) -> RTTMeasurement:
        logger.warning(
            f"VM {vm_id} unreachable",
            extra={
                "event": "vm_unreachable",
                "extra_data": {"vm_id": vm_id, "reason": reason},
            },
        )
        return RTTMeasurement(
            vm_id=vm_id, ip=ip,
            rtt_ms=UNREACHABLE_RTT, raw_ms=UNREACHABLE_RTT,
            reachable=False, timestamp=timestamp,
        )

# ---------------------------------------------------------------------------
# Main Client
# ---------------------------------------------------------------------------

class PiCarClient:
    """
    Client PiCar production-ready.
    Mesures parallèles + retry POST + compensation d'intervalle.
    """

    def __init__(self) -> None:
        self.adapter     = PiCarAdapter()
        self.hub_url     = LATENCY_MANAGER_ENDPOINT
        self.cycle_count = 0
        self.source_id   = "picar_real"

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        payload: LatencyPayload,
    ) -> bool:
        """POST vers latency_manager avec retry x3 + backoff."""
        data = payload.to_dict()
        for attempt in range(1, POST_RETRY_COUNT + 1):
            try:
                resp = await client.post(self.hub_url, json=data, timeout=5.0)
                if resp.status_code in (200, 201, 202):
                    logger.info(
                        "Payload sent to latency_manager",
                        extra={
                            "event": "sent_to_latency_manager",
                            "extra_data": {"cycle": payload.cycle},
                        },
                    )
                    return True
                logger.error(
                    f"Latency manager returned {resp.status_code} (attempt {attempt})",
                    extra={"event": "retry",
                           "extra_data": {"status": resp.status_code, "attempt": attempt}},
                )
            except httpx.RequestError as exc:
                logger.error(
                    f"Network error (attempt {attempt}): {type(exc).__name__}",
                    extra={"event": "retry",
                           "extra_data": {"error": type(exc).__name__, "attempt": attempt}},
                )

            if attempt < POST_RETRY_COUNT:
                await asyncio.sleep(POST_RETRY_BACKOFF)

        logger.critical(
            "Exhausted retries — latency manager unreachable",
            extra={"event": "send_failed", "extra_data": {"cycle": payload.cycle}},
        )
        return False

    async def run_cycle(self, client: httpx.AsyncClient) -> float:
        """Exécute un cycle complet : mesure parallèle + envoi."""
        self.cycle_count += 1
        logger.info(
            f"Cycle {self.cycle_count} started",
            extra={"event": "cycle_start",
                   "extra_data": {"cycle": self.cycle_count}},
        )

        start = time.perf_counter()

        # Mesures parallèles vers toutes les VMs
        tasks = [
            self.adapter.measure_vm(client, vm_id, vm_info)
            for vm_id, vm_info in TARGET_VMS.items()
        ]
        measurements: List[RTTMeasurement] = list(await asyncio.gather(*tasks))

        payload = LatencyPayload(
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=self.source_id,
            cycle=self.cycle_count,
            measurements=measurements,
        )

        await self._post_with_retry(client, payload)
        return time.perf_counter() - start

    async def start(self) -> None:
        """Point d'entrée principal — boucle permanente."""
        logger.info(
            f"PiCar client starting — hub={self.hub_url} "
            f"vms={list(TARGET_VMS.keys())} "
            f"interval={COLLECTION_INTERVAL}s",
            extra={"event": "client_init"},
        )
        await asyncio.sleep(INITIAL_DELAY)

        async with httpx.AsyncClient() as client:
            while True:
                cycle_duration = await self.run_cycle(client)
                sleep_time = max(0.0, COLLECTION_INTERVAL - cycle_duration)
                await asyncio.sleep(sleep_time)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    client = PiCarClient()
    try:
        asyncio.run(client.start())
    except KeyboardInterrupt:
        logger.info(
            "PiCar client stopped by user.",
            extra={"event": "client_stop"},
        )