import uvicorn
import asyncio
import httpx
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Body, BackgroundTasks, status, HTTPException
from typing import Any, AsyncGenerator, Dict
from shared import config
from shared.logging_utils import C, PrettyFormatter
from services.collector.collector import CollectorHandler


def _setup_app_logger() -> logging.Logger:
    log = logging.getLogger("CollectorApp")
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(PrettyFormatter())
        log.addHandler(h)
    log.propagate = False
    return log


app_logger = _setup_app_logger()

app_logger.info(
    f"\n{'═'*60}\n"
    f"  📡  {C.BOLD}Collector Service — Démarrage{C.RESET}\n"
    f"{'═'*60}"
)

handler = CollectorHandler()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    app_logger.info("🔧 Sondage initial des VMs avant démarrage du service...")
    await handler.poll_once()
    handler.launch_background_polling()
    app_logger.info(
        f"\n{'═'*60}\n"
        f"  {C.GREEN}✅  Collector Service prêt{C.RESET}\n"
        f"  {'Port':<20}: {C.CYAN}{config.COLLECTOR_PORT}{C.RESET}\n"
        f"  {'VMs enregistrées':<20}: {C.CYAN}{len(config.VM_REGISTRY)}{C.RESET} "
        f"({', '.join(config.VM_REGISTRY.keys())})\n"
        f"  {'Timeout base':<20}: {C.CYAN}{config.COLLECTOR_TIMEOUT_BASE}s{C.RESET}\n"
        f"  {'Alpha EMA':<20}: {C.CYAN}{config.COLLECTOR_RELIABILITY_ALPHA}{C.RESET}\n"
        f"  {'Intervalle sondage':<20}: {C.CYAN}{config.COLLECTOR_POLL_INTERVAL}s{C.RESET}\n"
        f"{'═'*60}\n"
    )
    yield
    app_logger.info("🛑 Arrêt du sondage de fond...")
    await handler.shutdown()


app = FastAPI(
    title="Advanced Metrics Collector",
    description="Microservice for polling metrics with adaptive logic.",
    version="3.0.0",
    lifespan=lifespan,
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