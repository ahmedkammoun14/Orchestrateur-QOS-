import asyncio
import logging
import httpx
from typing import Any, Dict


async def async_post_with_retry(
    url: str,
    payload: Dict[str, Any],
    *,
    retry_count: int,
    backoff: float,
    timeout: float,
    logger: logging.Logger,
) -> bool:
    """POST with linear retry. Returns True on first 2xx response, False after exhaustion."""
    async with httpx.AsyncClient() as client:
        for attempt in range(1, retry_count + 1):
            try:
                response = await client.post(url, json=payload, timeout=timeout)
                if response.status_code in (200, 201, 202):
                    return True
                logger.warning(
                    f"POST {url} — HTTP {response.status_code} "
                    f"| tentative {attempt}/{retry_count}"
                )
            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.warning(
                    f"POST {url} — {type(e).__name__} "
                    f"| tentative {attempt}/{retry_count}"
                )
            if attempt < retry_count:
                await asyncio.sleep(backoff)
    return False
