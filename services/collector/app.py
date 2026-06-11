import uvicorn
import asyncio
import httpx
import logging
from datetime import datetime, timezone
from fastapi import FastAPI, Body, BackgroundTasks, status, HTTPException
from typing import Dict, Any
from shared import config
from services.collector.collector import CollectorHandler, C


# ─────────────────────────────────────────────
#  Logger (réutilise le même formatter que collector.py)
# ─────────────────────────────────────────────
class _PrettyFormatter(logging.Formatter):
    LEVEL_STYLES = {
        "INFO":     f"{C.BLUE}[INFO]{C.RESET}",
        "WARNING":  f"{C.YELLOW}[WARNING]{C.RESET}",
        "ERROR":    f"{C.RED}[ERROR]{C.RESET}",
        "CRITICAL": f"{C.RED}{C.BOLD}[CRITICAL]{C.RESET}",
        "DEBUG":    f"{C.CYAN}[DEBUG]{C.RESET}",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts    = datetime.now(timezone.utc).strftime("%H:%M:%S")
        level = self.LEVEL_STYLES.get(record.levelname, f"[{record.levelname}]")
        return f"{C.CYAN}{ts}{C.RESET}  {level}  {record.getMessage()}"


def _setup_app_logger() -> logging.Logger:
    log = logging.getLogger("CollectorApp")
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(_PrettyFormatter())
        log.addHandler(h)
    log.propagate = False
    return log


app_logger = _setup_app_logger()

app_logger.info(
    f"\n{'═'*60}\n"
    f"  📡  {C.BOLD}Collector Service — Démarrage{C.RESET}\n"
    f"{'═'*60}"
)

app = FastAPI(
    title="Advanced Metrics Collector",
    description="Microservice for polling metrics with adaptive logic.",
    version="2.1.0"
)

handler = CollectorHandler()

app_logger.info(
    f"\n{'═'*60}\n"
    f"  {C.GREEN}✅  Collector Service prêt{C.RESET}\n"
    f"  {'Port':<16}: {C.CYAN}{config.COLLECTOR_PORT}{C.RESET}\n"
    f"  {'VMs enregistrées':<16}: {C.CYAN}{len(config.VM_REGISTRY)}{C.RESET} "
    f"({', '.join(config.VM_REGISTRY.keys())})\n"
    f"  {'Timeout base':<16}: {C.CYAN}{config.COLLECTOR_TIMEOUT_BASE}s{C.RESET}\n"
    f"  {'Alpha EMA':<16}: {C.CYAN}{config.COLLECTOR_RELIABILITY_ALPHA}{C.RESET}\n"
    f"{'═'*60}\n"
)


@app.post("/collect", status_code=status.HTTP_200_OK)
async def collect(
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(...)
):
    active_metrics = payload.get("active_metrics")
    cycle          = payload.get("cycle")

    if not active_metrics or cycle is None:
        app_logger.warning(
            f"⚠️  /collect — payload invalide : "
            f"active_metrics={active_metrics}, cycle={cycle}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="active_metrics and cycle are required"
        )

    result = await handler.handle(active_metrics, cycle)

    background_tasks.add_task(
        handler._forward_to_database,
        result["results"],
        cycle
    )

    return result


@app.get("/health")
async def health():
    checks = {"service": "healthy", "vms": {}}

    async def check_vm(client: httpx.AsyncClient, vm_id: str, info: Dict[str, Any]):
        url = f"http://{info['ip']}:{info['port']}/health"
        try:
            resp = await client.get(url, timeout=1.0)
            checks["vms"][vm_id] = "online" if resp.status_code == 200 else "error"
        except Exception:
            checks["vms"][vm_id] = "offline"

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[
            check_vm(client, vid, info)
            for vid, info in config.VM_REGISTRY.items()
        ])

    online  = sum(1 for s in checks["vms"].values() if s == "online")
    total   = len(checks["vms"])
    offline = [vid for vid, s in checks["vms"].items() if s != "online"]

    if offline:
        app_logger.warning(
            f"⚠️  Health check VMs — {C.GREEN}{online}{C.RESET}/{total} en ligne "
            f"| hors ligne : {C.YELLOW}{offline}{C.RESET}"
        )
    else:
        app_logger.info(
            f"✅ Health check VMs — {C.GREEN}{online}{C.RESET}/{total} en ligne"
        )

    return checks


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.COLLECTOR_PORT)