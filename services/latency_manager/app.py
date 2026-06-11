from fastapi import FastAPI, Request, Response, status
from services.latency_manager.latency_handler import LatencyHandler
import uvicorn
from shared import config
from typing import Dict, Any

app = FastAPI(
    title="Latency Manager",
    description="Stateless proxy for PiCar RTT measurements forwarding.",
    version="1.1.0"
)

# Singleton handler instance (Facade)
handler = LatencyHandler()

@app.post("/rtt")
async def receive_rtt(payload: Dict[str, Any], response: Response):
    """
    Inbound endpoint for RTT measurements.
    Delegates all logic to the LatencyHandler Facade.
    """
    # Note: We use raw Dict here as per requirement to check existence manually 
    # instead of full Pydantic validation if strict minimal validation is requested.
    
    success = await handler.handle(payload)
    
    if not success:
        # If handle returns False, it's either a validation error or a terminal sending failure.
        # We check measurements manually to return 400 specifically for validation.
        measurements = payload.get("measurements")
        if not measurements or not isinstance(measurements, list) or len(measurements) == 0:
            response.status_code = status.HTTP_400_BAD_REQUEST
            return {"error": "Invalid payload: measurements is missing or empty"}
        
        # Terminal transmission failure
        response.status_code = status.HTTP_502_BAD_GATEWAY
        return {"error": "Failed to forward measurements to Hub"}
    
    response.status_code = status.HTTP_202_ACCEPTED
    return {"status": "forwarded", "cycle": payload.get("cycle")}

@app.get("/health")
async def health_check():
    """Service health status."""
    return {"status": "healthy", "service": "latency_manager"}

if __name__ == "__main__":
    # Launch uvicorn using configuration constants
    uvicorn.run(
        "services.latency_manager.app:app", 
        host="0.0.0.0", 
        port=config.LATENCY_PORT,
        reload=False
    )
