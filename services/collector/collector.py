import asyncio
import httpx
import logging
import time
from datetime import datetime, timezone
from typing import List, Dict, Any
from shared import config
from shared.logging_utils import C, PrettyFormatter as _PrettyFormatter
from shared.http_utils import async_post_with_retry


def setup_logger():
    logger = logging.getLogger("CollectorHandler")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_PrettyFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = setup_logger()


class CollectorHandler:
    """
    Handles parallel metrics collection with adaptive timeout and reliability tracking.
    Follows Hub-and-Spoke: all external reporting goes through Hub proxy.
    """

    def __init__(self):
        self.vm_registry   = config.VM_REGISTRY
        self.alpha         = config.COLLECTOR_RELIABILITY_ALPHA
        self.vm_timeouts   = {vm_id: config.COLLECTOR_TIMEOUT_BASE for vm_id in self.vm_registry}
        self.vm_reliability = {vm_id: 1.0 for vm_id in self.vm_registry}

    async def handle(self, active_metrics: List[str], cycle: int) -> Dict[str, Any]:
        logger.info(
            f"📡 Démarrage collecte — cycle #{C.BOLD}{cycle}{C.RESET} "
            f"| métriques : {C.CYAN}{active_metrics}{C.RESET} "
            f"| VMs ciblées : {C.CYAN}{len(self.vm_registry)}{C.RESET}"
        )

        start_time = time.perf_counter()

        async with httpx.AsyncClient() as client:
            tasks = [
                self._collect_vm(client, vm_id, info, active_metrics)
                for vm_id, info in self.vm_registry.items()
            ]
            results = await asyncio.gather(*tasks)

        duration = round(time.perf_counter() - start_time, 3)
        reachable_count = sum(1 for r in results if r.get("reachable"))

        logger.info(
            f"✅ Collecte terminée — cycle #{C.BOLD}{cycle}{C.RESET} "
            f"| durée : {C.CYAN}{duration}s{C.RESET} "
            f"| {C.GREEN}{reachable_count}{C.RESET}/{len(results)} VM(s) joignables"
        )

        return {
            "results":   list(results),
            "cycle":     cycle,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    async def _collect_vm(
        self,
        client: httpx.AsyncClient,
        vm_id: str,
        info: Dict[str, Any],
        active_metrics: List[str]
    ) -> Dict[str, Any]:
        url             = f"http://{info['ip']}:{info['port']}/metrics"
        current_timeout = self.vm_timeouts[vm_id]

        start_ts = time.perf_counter()
        try:
            response    = await client.get(url, timeout=current_timeout)
            actual_time = time.perf_counter() - start_ts

            if response.status_code == 200:
                data = response.json()

                self._update_timeout(vm_id, actual_time)
                self._update_reliability(vm_id, 1.0)

                filtered_metrics = {metric: data.get(metric) for metric in active_metrics}
                reliability      = round(self.vm_reliability[vm_id], 3)

                metric_summary = "  ".join(
                    f"{m}={C.CYAN}{v}{C.RESET}" for m, v in filtered_metrics.items() if v is not None
                )
                logger.info(
                    f"  ✅ {C.GREEN}{C.BOLD}{vm_id}{C.RESET} — "
                    f"{metric_summary}  "
                    f"| fiabilité : {C.GREEN}{reliability}{C.RESET}  "
                    f"| temps : {C.CYAN}{round(actual_time * 1000, 1)} ms{C.RESET}"
                )

                return {
                    "vm_id": vm_id,
                    **filtered_metrics,
                    "reliability": reliability,
                    "reachable":   True,
                    "timestamp":   data.get("timestamp", datetime.now(timezone.utc).isoformat())
                }
            else:
                return self._handle_vm_failure(vm_id, active_metrics, f"HTTP_{response.status_code}")

        except Exception as e:
            return self._handle_vm_failure(vm_id, active_metrics, str(type(e).__name__))

    def _handle_vm_failure(self, vm_id: str, metrics: List[str], reason: str) -> Dict[str, Any]:
        self._update_reliability(vm_id, 0.0)
        reliability = round(self.vm_reliability[vm_id], 3)
        logger.warning(
            f"  ⚠️  {C.YELLOW}{C.BOLD}{vm_id}{C.RESET} — injoignable "
            f"| raison : {C.YELLOW}{reason}{C.RESET} "
            f"| fiabilité : {C.YELLOW}{reliability}{C.RESET}"
        )
        return {
            "vm_id": vm_id,
            **{m: None for m in metrics},
            "reliability": reliability,
            "reachable":   False,
            "timestamp":   datetime.now(timezone.utc).isoformat()
        }

    def _update_timeout(self, vm_id: str, actual_time: float):
        new_val = self.alpha * (actual_time * config.COLLECTOR_TIMEOUT_FACTOR) + \
                  (1 - self.alpha) * self.vm_timeouts[vm_id]
        self.vm_timeouts[vm_id] = max(
            config.COLLECTOR_MIN_TIMEOUT,
            min(config.COLLECTOR_MAX_TIMEOUT, new_val)
        )

    def _update_reliability(self, vm_id: str, success_val: float):
        self.vm_reliability[vm_id] = \
            self.alpha * success_val + (1 - self.alpha) * self.vm_reliability[vm_id]

    async def _forward_to_database(self, results: List[Dict[str, Any]], cycle: int):
        async with httpx.AsyncClient() as client:
            for vm_result in results:
                vm_id = vm_result.get("vm_id")

                if not vm_id or not vm_result.get("reachable"):
                    continue

                metrics = {
                    k: v for k, v in vm_result.items()
                    if k not in ("vm_id", "reliability", "reachable", "timestamp")
                }

                payload = {
                    "vm_id":       vm_id,
                    "metrics":     metrics,
                    "timestamp":   vm_result.get("timestamp"),
                    "reliability": vm_result.get("reliability")
                }

                success = await async_post_with_retry(
                    f"{config.DATABASE_SERVICE_URL}/store/metrics",
                    payload,
                    retry_count=config.POST_RETRY_COUNT,
                    backoff=config.POST_RETRY_BACKOFF,
                    timeout=5.0,
                    logger=logger,
                )
                if success:
                    logger.debug(
                        f"🔍 Métriques transmises à Database — "
                        f"VM : {C.CYAN}{vm_id}{C.RESET} | cycle #{cycle}"
                    )
                else:
                    logger.error(
                        f"❌ Échec persistance Database pour {C.RED}{vm_id}{C.RESET} "
                        f"après {config.POST_RETRY_COUNT} tentatives | cycle #{cycle}"
                    )