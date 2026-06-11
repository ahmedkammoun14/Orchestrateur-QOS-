import asyncio
import httpx
import logging
import json
import time
from datetime import datetime, timezone
from typing import List, Dict, Any
from shared import config

# --- Structured Logging ---

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "collector",
            "event": getattr(record, "event", "generic_log"),
            "message": record.getMessage()
        }
        if hasattr(record, "extra_data"):
            log_record.update(record.extra_data)
        return json.dumps(log_record)

def setup_logger():
    logger = logging.getLogger("CollectorHandler")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
    return logger

logger = setup_logger()


class CollectorHandler:
    """
    Handles parallel metrics collection with adaptive timeout and reliability tracking.
    Follows Hub-and-Spoke: all external reporting goes through Hub proxy.
    """

    def __init__(self):
        self.vm_registry = config.VM_REGISTRY
        self.alpha = config.COLLECTOR_RELIABILITY_ALPHA

        # Per-VM state tracking (initialized from config)
        self.vm_timeouts = {vm_id: config.COLLECTOR_TIMEOUT_BASE for vm_id in self.vm_registry}
        self.vm_reliability = {vm_id: 1.0 for vm_id in self.vm_registry}

    async def handle(self, active_metrics: List[str], cycle: int) -> Dict[str, Any]:
        """
        Orchestrates the collection cycle and returns results to the Core.
        Background storage is handled by BackgroundTasks in app.py.
        """
        logger.info(
            f"Starting collection cycle {cycle}",
            extra={"event": "collect_requested", "extra_data": {"cycle": cycle, "metrics": active_metrics}}
        )

        start_time = time.perf_counter()

        async with httpx.AsyncClient() as client:
            tasks = [
                self._collect_vm(client, vm_id, info, active_metrics)
                for vm_id, info in self.vm_registry.items()
            ]
            results = await asyncio.gather(*tasks)

        payload = {
            "results": list(results),
            "cycle": cycle,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        logger.info(
            f"Collection cycle {cycle} done",
            extra={"event": "collect_done", "extra_data": {
                "cycle": cycle,
                "duration": round(time.perf_counter() - start_time, 3)
            }}
        )
        return payload

    async def _collect_vm(
        self,
        client: httpx.AsyncClient,
        vm_id: str,
        info: Dict[str, Any],
        active_metrics: List[str]
    ) -> Dict[str, Any]:
        """
        Performs adaptive HTTP GET /metrics on a specific VM.
        Updates EMA timeout and reliability score.
        """
        url = f"http://{info['ip']}:{info['port']}/metrics"
        current_timeout = self.vm_timeouts[vm_id]

        start_ts = time.perf_counter()
        try:
            response = await client.get(url, timeout=current_timeout)
            actual_time = time.perf_counter() - start_ts

            if response.status_code == 200:
                data = response.json()

                self._update_timeout(vm_id, actual_time)
                self._update_reliability(vm_id, 1.0)

                filtered_metrics = {
                    metric: data.get(metric) for metric in active_metrics
                }

                logger.info(
                    f"Collected from {vm_id}",
                    extra={"event": "collect_vm_success", "extra_data": {"vm_id": vm_id}}
                )

                return {
                    "vm_id": vm_id,
                    **filtered_metrics,
                    "reliability": round(self.vm_reliability[vm_id], 3),
                    "reachable": True,
                    "timestamp": data.get("timestamp", datetime.now(timezone.utc).isoformat())
                }
            else:
                return self._handle_vm_failure(vm_id, active_metrics, f"HTTP_{response.status_code}")

        except Exception as e:
            return self._handle_vm_failure(vm_id, active_metrics, str(type(e).__name__))

    def _handle_vm_failure(self, vm_id: str, metrics: List[str], reason: str) -> Dict[str, Any]:
        """Updates reliability and prepares failure payload."""
        self._update_reliability(vm_id, 0.0)
        logger.warning(
            f"Failed to collect from {vm_id}",
            extra={"event": "collect_vm_failed", "extra_data": {"vm_id": vm_id, "reason": reason}}
        )
        return {
            "vm_id": vm_id,
            **{m: None for m in metrics},
            "reliability": round(self.vm_reliability[vm_id], 3),
            "reachable": False,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    def _update_timeout(self, vm_id: str, actual_time: float):
        """EMA Timeout: new = alpha * (actual * factor) + (1-alpha) * current"""
        new_val = self.alpha * (actual_time * config.COLLECTOR_TIMEOUT_FACTOR) + \
                  (1 - self.alpha) * self.vm_timeouts[vm_id]
        self.vm_timeouts[vm_id] = max(
            config.COLLECTOR_MIN_TIMEOUT,
            min(config.COLLECTOR_MAX_TIMEOUT, new_val)
        )

    def _update_reliability(self, vm_id: str, success_val: float):
        """EMA Reliability: new = alpha * val + (1-alpha) * current"""
        self.vm_reliability[vm_id] = \
            self.alpha * success_val + (1 - self.alpha) * self.vm_reliability[vm_id]

    async def _forward_to_database(self, results: List[Dict[str, Any]], cycle: int):
        """
        Asynchronous storage forwarding to database service with retry logic.
        Sends one POST per VM with the correct format expected by database.
        Called via BackgroundTasks in app.py — never blocks the Core response.
        """
        async with httpx.AsyncClient() as client:
            for vm_result in results:
                vm_id = vm_result.get("vm_id")

                # Skip unreachable VMs
                if not vm_id or not vm_result.get("reachable"):
                    continue

                # Build metrics dict — exclude non-metric fields
                metrics = {
                    k: v for k, v in vm_result.items()
                    if k not in ("vm_id", "reliability", "reachable", "timestamp")
                }

                payload = {
                    "vm_id": vm_id,
                    "metrics": metrics,
                    "timestamp": vm_result.get("timestamp"),
                    "reliability": vm_result.get("reliability")
                }

                for attempt in range(1, config.POST_RETRY_COUNT + 1):
                    try:
                        response = await client.post(
                            f"{config.DATABASE_SERVICE_URL}/store/metrics",
                            json=payload,
                            timeout=5.0
                        )
                        if response.status_code in (200, 201, 202):
                            logger.info(
                                f"Stored metrics for {vm_id}",
                                extra={"event": "stored_to_database", "extra_data": {"vm_id": vm_id, "cycle": cycle}}
                            )
                            break
                    except Exception:
                        pass

                    if attempt < config.POST_RETRY_COUNT:
                        await asyncio.sleep(config.POST_RETRY_BACKOFF)
                else:
                    logger.error(
                        f"Database storage failed for {vm_id} after all retries",
                        extra={"event": "storage_failed", "extra_data": {"cycle": cycle, "vm_id": vm_id}}
                    )