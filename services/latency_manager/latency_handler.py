import logging
from typing import Dict, Any
from shared import config
from shared.logging_utils import C, PrettyFormatter
from shared.http_utils import async_post_with_retry


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("LatencyHandler")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(PrettyFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = setup_logger()


# ─────────────────────────────────────────────
#  Handler Logic
# ─────────────────────────────────────────────

class LatencyHandler:
    """
    Facade for processing and forwarding RTT measurements.
    Isolated from the API layer and Hub implementation details.
    """

    def __init__(self):
        self.hub_url      = config.HUB_RTT_URL
        self.retry_count  = config.POST_RETRY_COUNT
        self.retry_backoff = config.POST_RETRY_BACKOFF
        self.timeout      = config.POST_TIMEOUT

    async def handle(self, payload: Dict[str, Any]) -> bool:
        try:
            # 1. Validation
            if not self._validate(payload):
                return False

            measurements = payload.get("measurements", [])
            cycle        = payload.get("cycle", "?")
            reachable    = sum(1 for m in measurements if m.get("reachable", True))

            logger.info(
                f"📡 Mesures RTT reçues — cycle #{C.CYAN}{cycle}{C.RESET} "
                f"| {C.CYAN}{len(measurements)}{C.RESET} VM(s) "
                f"| {C.GREEN}{reachable}{C.RESET} joignables"
            )

            # Log détaillé par VM
            for m in measurements:
                vm_id     = m.get("vm_id", "?")
                rtt       = m.get("rtt_ms")
                reachable_vm = m.get("reachable", True)
                tag = f"{C.GREEN}OK{C.RESET}" if reachable_vm else f"{C.RED}INJOIGNABLE{C.RESET}"
                rtt_str = f"{rtt:.1f} ms" if rtt is not None else "N/A"
                logger.debug(
                    f"🔍 VM {C.BOLD}{vm_id:<12}{C.RESET} "
                    f"RTT : {C.CYAN}{rtt_str:<10}{C.RESET} [{tag}]"
                )

            # 2. Forwarding
            return await self._forward_with_retry(payload)

        except Exception as e:
            logger.error(
                f"❌ Erreur interne lors du traitement : {type(e).__name__} — {e}"
            )
            return False

    def _validate(self, payload: Dict[str, Any]) -> bool:
        measurements = payload.get("measurements")
        if not measurements or not isinstance(measurements, list) or len(measurements) == 0:
            logger.warning(
                "⚠️  Payload invalide — champ 'measurements' manquant ou vide"
            )
            return False
        return True

    async def _forward_with_retry(self, payload: Dict[str, Any]) -> bool:
        cycle   = payload.get("cycle", "?")
        success = await async_post_with_retry(
            self.hub_url, payload,
            retry_count=self.retry_count,
            backoff=self.retry_backoff,
            timeout=self.timeout,
            logger=logger,
        )
        if success:
            logger.info(
                f"✅ Mesures transmises au Hub — "
                f"cycle #{C.CYAN}{cycle}{C.RESET}"
            )
        else:
            logger.critical(
                f"🔴 Toutes les tentatives ont échoué — "
                f"cycle #{cycle} non transmis au Hub "
                f"({self.retry_count} tentatives épuisées)"
            )
        return success
    