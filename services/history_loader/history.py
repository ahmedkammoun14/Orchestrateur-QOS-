import logging
import redis
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared import config, redis_keys


class HistoryReader:
    """
    Read-only Redis client for the history_loader service.

    This class is the sole access point to per-VM metric timeseries stored
    by the database service.  It never writes to Redis.

    The caller (app.py) injects the shared logger so that all events
    are emitted through the same JSON-formatted handler.
    """

    def __init__(self, logger: logging.Logger) -> None:
        """
        Open and verify the Redis connection.

        Parameters
        ----------
        logger:
            Shared logger injected by app.py (JSON-formatted).

        Raises
        ------
        redis.RedisError
            If the initial PING fails.  Fail-fast: the service cannot
            serve history without a working Redis connection.
        """
        self._logger = logger

        try:
            self._r = redis.Redis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                db=config.REDIS_DB,
                decode_responses=True,
                socket_timeout=2.0,
            )
            if self._r.ping():
                self._logger.info(
                    f"Redis connected at {config.REDIS_HOST}:{config.REDIS_PORT}",
                    extra={"event": "redis_connected"},
                )
            else:
                self._logger.error(
                    "Redis PING returned False",
                    extra={"event": "redis_disconnected"},
                )
                raise redis.ConnectionError("Redis PING returned False")

        except redis.RedisError as exc:
            self._logger.critical(
                f"Redis initialization failed: {exc}",
                extra={"event": "redis_disconnected"},
            )
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(
        self,
        vm_id: str,
        metrics: List[str],
        size: int,
    ) -> Dict[str, Any]:
        """
        Load metric timeseries for one VM from Redis.

        Only metrics present in both *metrics* and ``METRICS_REGISTRY``
        are processed.  Unknown metric names are silently ignored.

        For each valid metric the method:

        1. Issues ``LRANGE metrics:{vm_id}:{metric} 0 size-1`` (LIFO order).
        2. Parses each raw string entry as a float via ``_parse_entry``.
        3. Reverses the list to restore chronological order.
        4. Builds ``{"value": float, "timestamp": ""}`` entries.
           Individual metric keys store raw floats only — no per-entry
           timestamp is available at this granularity.

        A metric with an empty or missing key always returns
        ``histories[metric] = []`` and ``sizes[metric] = 0``.
        This method never raises a 404.

        Parameters
        ----------
        vm_id:
            Identifier of the target VM.
        metrics:
            Metric names requested by the core.
        size:
            Maximum number of data points to return per metric.
            Fallback is ``config.HISTORY_WINDOW``.

        Returns
        -------
        dict
            Structure::

                {
                    "vm_id":     str,
                    "histories": {metric: [{"value": float, "timestamp": str}]},
                    "sizes":     {metric: int},
                    "timestamp": str  (ISO-8601, time of this read)
                }
        """
        self._logger.info(
            f"Load requested — vm_id={vm_id} metrics={metrics} size={size}",
            extra={"event": "load_requested"},
        )

        # Guard: only process metrics declared in METRICS_REGISTRY — never hardcoded
        valid_metrics: List[str] = [m for m in metrics if m in config.METRICS_REGISTRY]

        # Single timestamp for all entries of this read cycle — consistent across metrics
        read_ts: str = datetime.now(timezone.utc).isoformat()

        histories: Dict[str, List[Dict[str, Any]]] = {}
        sizes: Dict[str, int] = {}

        for metric in valid_metrics:
            key = redis_keys.METRICS_KEY.format(vm_id=vm_id, metric=metric)

            try:
                raw_entries: List[str] = self._r.lrange(key, 0, size - 1)
            except redis.RedisError as exc:
                self._logger.error(
                    f"LRANGE failed for {key}: {exc}",
                    extra={"event": "internal_error"},
                )
                histories[metric] = []
                sizes[metric] = 0
                continue

            if not raw_entries:
                self._logger.info(
                    f"No history available for {vm_id}/{metric}",
                    extra={"event": "insufficient_history"},
                )
                histories[metric] = []
                sizes[metric] = 0
                continue

            # Parse raw float strings; discard unparseable entries
            parsed: List[float] = []
            for raw in raw_entries:
                val = self._parse_entry(raw, metric)
                if val is not None:
                    parsed.append(val)

            # Reverse LIFO → chronological order
            parsed.reverse()

            histories[metric] = [{"value": v, "timestamp": read_ts} for v in parsed]
            sizes[metric] = len(parsed)

        self._logger.info(
            f"History loaded — vm_id={vm_id} sizes={sizes}",
            extra={"event": "history_loaded"},
        )

        return {
            "vm_id":      vm_id,
            "histories":  histories,
            "sizes":      sizes,
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_entry(self, raw: Any, metric: str) -> Optional[float]:
        """
        Convert a raw Redis entry to a ``float``.

        Redis returns strings when ``decode_responses=True``.  This
        method also tolerates values that are already ``float`` or ``int``
        (defensive against future storage-format changes).

        Parameters
        ----------
        raw:
            The raw value as returned by ``LRANGE``.
        metric:
            Metric name — used only for error-log context.

        Returns
        -------
        float or None
            ``None`` is returned (and a warning logged) for any value
            that cannot be cast to float.
        """
        try:
            return float(raw)
        except (TypeError, ValueError):
            self._logger.warning(
                f"Unparseable entry for metric={metric}: {raw!r}",
                extra={"event": "internal_error"},
            )
            return None

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        """
        Return ``True`` if Redis answers PING, ``False`` otherwise.

        Never raises — safe to call from the /health endpoint.
        """
        try:
            return bool(self._r.ping())
        except redis.RedisError:
            return False
