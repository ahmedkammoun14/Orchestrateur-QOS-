import httpx
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from shared import config

# --- Structured Logging ---

class JSONFormatter(logging.Formatter):
    """
    Custom formatter to output logs in JSON format for the Latency Manager.
    """
    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "latency_manager",
            "event": getattr(record, "event", "generic_log"),
            "message": record.getMessage()
        }
        if hasattr(record, "extra_data"):
            log_record.update(record.extra_data)
        return json.dumps(log_record)

def setup_logger() -> logging.Logger:
    logger = logging.getLogger("LatencyHandler")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
    return logger

logger = setup_logger()

# --- Handler Logic ---

class LatencyHandler:
    """
    Facade for processing and forwarding RTT measurements.
    Isolated from the API layer and Hub implementation details.
    """

    def __init__(self):
        self.hub_url = config.HUB_RTT_URL
        self.retry_count = config.POST_RETRY_COUNT
        self.retry_backoff = config.POST_RETRY_BACKOFF
        self.timeout = config.POST_TIMEOUT

    async def handle(self, payload: Dict[str, Any]) -> bool:
        """
        Main entry point for processing a payload.
        Performs validation and handles the forwarding sequence.
        """
        try:
            # 1. Validation
            if not self._validate(payload):
                return False

            logger.info(
                "RTT payload received",
                extra={"event": "rtt_received", "extra_data": {"cycle": payload.get("cycle")}}
            )

            # 2. Forwarding
            return await self._forward_with_retry(payload)

        except Exception as e:
            logger.error(
                f"Internal error during payload handling: {str(e)}",
                extra={"event": "internal_error", "extra_data": {"error_type": type(e).__name__}}
            )
            return False

    def _validate(self, payload: Dict[str, Any]) -> bool:
        """
        Ensures the payload contains the mandatory 'measurements' field and it is not empty.
        """
        measurements = payload.get("measurements")
        if not measurements or not isinstance(measurements, list) or len(measurements) == 0:
            logger.warning(
                "Invalid payload: 'measurements' is missing or empty",
                extra={"event": "validation_failed"}
            )
            return False
        return True

    async def _forward_with_retry(self, payload: Dict[str, Any]) -> bool:
        """
        Sends the payload to the central Hub with robust retry logic.
        """
        async with httpx.AsyncClient() as client:
            for attempt in range(1, self.retry_count + 1):
                try:
                    response = await client.post(
                        self.hub_url,
                        json=payload,
                        timeout=self.timeout
                    )
                    
                    if response.status_code in (200, 201, 202):
                        logger.info(
                            "Measurements forwarded to Hub",
                            extra={"event": "forwarded_to_core", "extra_data": {"cycle": payload.get("cycle")}}
                        )
                        return True
                    else:
                        logger.error(
                            f"Hub returned error status: {response.status_code}",
                            extra={"event": "retry", "extra_data": {"status": response.status_code, "attempt": attempt}}
                        )
                except (httpx.RequestError, httpx.TimeoutException) as e:
                    logger.error(
                        f"Connection error to Hub: {str(type(e).__name__)}",
                        extra={"event": "retry", "extra_data": {"error": str(e), "attempt": attempt}}
                    )
                
                if attempt < self.retry_count:
                    await asyncio.sleep(self.retry_backoff)

        logger.critical(
            "Failed to forward payload after all retries",
            extra={"event": "send_failed", "extra_data": {"cycle": payload.get("cycle")}}
        )
        return False
