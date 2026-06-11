import uvicorn
import asyncio
import httpx
from fastapi import FastAPI, Body, BackgroundTasks, status, HTTPException
from typing import Dict, Any
from shared import config
from services.collector.collector import CollectorHandler

app = FastAPI(
    title="Advanced Metrics Collector",
    description="Microservice for polling metrics with adaptive logic.",
    version="2.1.0"
)

handler = CollectorHandler()


@app.post("/collect", status_code=status.HTTP_200_OK)
async def collect(
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(...)
):
    """
    Triggers a collection cycle.
    Payload: {"active_metrics": List[str], "cycle": int}
    Storage to database is handled in background — does not block Core response.
    """
    active_metrics = payload.get("active_metrics")
    cycle = payload.get("cycle")

    if not active_metrics or cycle is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="active_metrics and cycle are required"
        )

    result = await handler.handle(active_metrics, cycle)

    # Non-blocking background storage via BackgroundTasks (safe reference)
    background_tasks.add_task(
        handler._forward_to_database,
        result["results"],
        cycle
    )

    return result


@app.get("/health")
async def health():
    """
    Checks service health and parallel availability of all VMs.
    Uses a single shared httpx.AsyncClient for all VM checks.
    """
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

    return checks


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.COLLECTOR_PORT)