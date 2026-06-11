from dataclasses import dataclass, field
from typing import List, Optional, Any, Dict
from pydantic import BaseModel


# ── Dataclasses internes ──────────────────────────────────────

@dataclass
class RTTMeasurement:
    vm_id:     str
    ip:        str
    rtt_ms:    float
    raw_ms:    float
    reachable: bool
    timestamp: str


@dataclass
class SLO:
    metric:           str
    operator:         str
    threshold:        float
    unit:             str
    weight:           float = 1.0
    target:           float = 0.0
    window:           str   = "5m"
    budget_remaining: float = 100.0
    violations:       int   = 0
    confidence:       float = 1.0

    def dict(self) -> Dict[str, Any]:
        return {
            "metric":           self.metric,
            "operator":         self.operator,
            "threshold":        self.threshold,
            "unit":             self.unit,
            "weight":           self.weight,
            "target":           self.target,
            "window":           self.window,
            "budget_remaining": self.budget_remaining,
            "violations":       self.violations,
            "confidence":       self.confidence,
        }


# ── Pydantic models (FastAPI) ─────────────────────────────────

class RTTMeasurementModel(BaseModel):
    vm_id:     str
    ip:        str
    rtt_ms:    float
    raw_ms:    float
    reachable: bool
    timestamp: str


class LatencyPayload(BaseModel):
    timestamp:    str
    source:       str
    cycle:        int
    measurements: List[RTTMeasurementModel]


class IntentToHubPayload(BaseModel):
    intent_id:  str
    intention:  str
    slos:       List[Dict[str, Any]]
    timestamp:  str