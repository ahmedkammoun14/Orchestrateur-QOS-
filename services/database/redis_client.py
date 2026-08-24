import json
import logging
import redis
from typing import Any, Dict, List, Optional

from shared import config, redis_keys


class RedisClient:
    """
    Sole Redis writer for the QoS Orchestrator project.

    Receives a pre-configured logger from the caller (app.py) so that
    all events are emitted through the same JSON-formatted handler.

    All write operations use a Redis pipeline for atomicity and efficiency.
    No business logic lives here — pure storage.
    """

    def __init__(self, logger: logging.Logger) -> None:
        """
        Open and verify the Redis connection.

        Parameters
        ----------
        logger:
            Shared logger injected by the FastAPI application layer.
            Must already have handlers configured (JSON formatter).

        Raises
        ------
        redis.RedisError
            If the initial PING fails, the service cannot function and
            should not start (fail-fast).
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
    # Public write methods
    # ------------------------------------------------------------------

    def store_metrics(
        self,
        vm_id: str,
        metrics: Dict[str, Any],
        timestamp: Optional[str],
        reliability: Optional[float],
    ) -> None:
        """
        Persist a metrics snapshot for one VM.

        For every metric declared in ``METRICS_REGISTRY`` that is also
        present and non-None in *metrics*, three Redis commands are
        issued atomically:

        - ``LPUSH  metrics:{vm_id}:{metric}  <value>``
        - ``LTRIM  metrics:{vm_id}:{metric}  0  METRICS_RETENTION-1``
        - ``EXPIRE metrics:{vm_id}:{metric}  METRICS_TTL``

        Additionally, a full JSON snapshot is always appended to the
        per-VM history list with the same LTRIM / EXPIRE policy.

        All commands are batched in a single pipeline.

        Parameters
        ----------
        vm_id:
            Identifier of the originating VM.
        metrics:
            Raw metric values keyed by metric name.  Values absent from
            ``METRICS_REGISTRY`` are silently ignored.
        timestamp:
            ISO-8601 collection timestamp.
        reliability:
            Optional reliability score attached to the snapshot.
        """
        try:
            pipe = self._r.pipeline()

            # Individual time-series per metric — registry-driven, never hardcoded
            for metric in config.METRICS_REGISTRY:
                val = metrics.get(metric)
                if val is not None:
                    key = redis_keys.METRICS_KEY.format(vm_id=vm_id, metric=metric)
                    pipe.lpush(key, val)
                    pipe.ltrim(key, 0, config.METRICS_RETENTION - 1)
                    # Pas de EXPIRE : les données sont conservées indéfiniment.
                    # LTRIM borne la mémoire à METRICS_RETENTION entrées par clé
                    # — le MAX des fenêtres lues (MI 25, ML 60). Trancher au
                    # seul HISTORY_WINDOW rendait les 60 points du ML
                    # inobtenables : la base ne les avait tout simplement plus.

            # Full history snapshot (all metrics + metadata in one entry)
            hist_key = redis_keys.HISTORY_KEY.format(vm_id=vm_id)
            snapshot = json.dumps({
                "vm_id":       vm_id,
                "metrics":     metrics,
                "reliability": reliability,
                "timestamp":   timestamp,
            })
            pipe.lpush(hist_key, snapshot)
            pipe.ltrim(hist_key, 0, config.METRICS_RETENTION - 1)

            pipe.execute()
            self._logger.info(
                f"Metrics stored for {vm_id}",
                extra={"event": "metrics_stored"},
            )

        except redis.RedisError as exc:
            self._logger.error(
                f"store_metrics failed for {vm_id}: {exc}",
                extra={"event": "storage_failed"},
            )
            raise

    def store_slos(self, slos: List[Dict[str, Any]], timestamp: str) -> None:
        """
        Overwrite the active SLOs configuration in Redis.

        Stored as a JSON string under the key ``slos:active``.
        No TTL is applied — the entry is permanent until the next write.

        Parameters
        ----------
        slos:
            List of SLO dicts as produced by the metrics_manager service.
        timestamp:
            ISO-8601 timestamp of the SLO generation.
        """
        try:
            payload = json.dumps({"slos": slos, "timestamp": timestamp})
            self._r.set(redis_keys.SLOS_KEY, payload)
            self._logger.info(
                f"Active SLOs updated ({len(slos)} SLO(s))",
                extra={"event": "slos_stored"},
            )

        except redis.RedisError as exc:
            self._logger.error(
                f"store_slos failed: {exc}",
                extra={"event": "storage_failed"},
            )
            raise

    def store_decision(self, decision_payload: Dict[str, Any]) -> None:
        """
        Append a migration decision to the recent-decisions FIFO list.

        The list ``decisions:recent`` is capped at ``DECISIONS_FIFO``
        entries (default 50).  No TTL — the list is permanent.

        Commands issued atomically:

        - ``LPUSH decisions:recent <JSON>``
        - ``LTRIM decisions:recent 0 DECISIONS_FIFO-1``

        Parameters
        ----------
        decision_payload:
            Decision dict containing at least ``decision``, ``from_vm``,
            ``to_vm``, ``reason``, and ``timestamp``.
        """
        try:
            pipe = self._r.pipeline()
            pipe.lpush(redis_keys.DECISIONS_KEY, json.dumps(decision_payload))
            pipe.ltrim(redis_keys.DECISIONS_KEY, 0, config.DECISIONS_FIFO - 1)
            pipe.execute()
            self._logger.info(
                f"Decision stored: {decision_payload.get('decision')} "
                f"{decision_payload.get('from_vm')} → {decision_payload.get('to_vm')}",
                extra={"event": "decision_stored"},
            )

        except redis.RedisError as exc:
            self._logger.error(
                f"store_decision failed: {exc}",
                extra={"event": "storage_failed"},
            )
            raise

    def store_llm_history(self, entry: Dict[str, Any]) -> None:
        """
        Persist one LLM intent entry to Redis.
        The list is capped at HISTORY_SIZE entries (newest first).
        """
        try:
            pipe = self._r.pipeline()
            pipe.lpush(redis_keys.LLM_HISTORY_KEY, json.dumps(entry))
            pipe.ltrim(redis_keys.LLM_HISTORY_KEY, 0, config.HISTORY_SIZE - 1)
            pipe.execute()
        except redis.RedisError as exc:
            self._logger.error(
                f"store_llm_history failed: {exc}",
                extra={"event": "storage_failed"},
            )
            raise

    def load_llm_history(self, size: int) -> List[Dict[str, Any]]:
        """
        Return up to *size* LLM history entries in chronological order (oldest first).
        Returns an empty list on error to allow graceful degradation.
        """
        try:
            raw_list = self._r.lrange(redis_keys.LLM_HISTORY_KEY, 0, size - 1)
            # LPUSH stores newest first; reverse to restore chronological order
            return [json.loads(r) for r in reversed(raw_list)]
        except redis.RedisError as exc:
            self._logger.error(
                f"load_llm_history failed: {exc}",
                extra={"event": "storage_failed"},
            )
            return []

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        """
        Return ``True`` if Redis answers PING, ``False`` otherwise.

        Never raises — used by the /health endpoint.
        """
        try:
            return bool(self._r.ping())
        except redis.RedisError:
            return False
