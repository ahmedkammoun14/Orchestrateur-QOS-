# Placement Arbiter — Specification

> **Document type:** Specification (*what the service must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Service name | `placement_arbiter` |
| Default port | `8011` (`8111` for provider-2, via `PORT_OFFSET`) |
| Component version | 1.0.0 |
| Status | Implemented |
| Position in the architecture | Step 5 of the Contract Net — elects the winning bid |

---

## 1. Context

At the end of a Contract Net round the initiator holds N bids: its own, and one from each peer. Each bid says *"here is my champion VM, and here is its Gap Grade"*. Something must now elect a winner.

That election looks trivial — take the lowest Gap Grade — and it is not, for three reasons.

**First, the wrong number is available.** Every bid also carries a TOPSIS score, and a TOPSIS score is *tempting*: it is in [0,1], higher is better, and it reads like a quality measure. It is not comparable across providers. TOPSIS normalises min-max **inside each provider's own candidate pool**, so the best of any pool scores ≈ 1.0 and the worst ≈ 0.0 regardless of absolute values. A provider whose VMs are all terrible still produces a champion at ≈ 1.0. Comparing those numbers across providers gives a confidently wrong answer. This service is therefore defined as much by what it must **refuse to read** as by what it computes.

**Second, the incumbent deserves protection.** Migration has a cost. A challenger marginally better than the current host would cause oscillation. A dead-band is needed — and, unlike the intra-provider one, it can be **absolute**, because the Gap Grade is already normalised by the SLO thresholds. `0.05` reads directly as *"must win by more than 5 % of the threshold"* — 1.4 ms on a 28 ms SLO.

**Third, several kinds of "nobody wins" must be distinguished.** No provider compliant is not the same as no provider evaluable. The first means the infrastructure cannot honour the contract; the second means the ML chain is down and the federation is blind. The operator must be able to tell them apart.

The founding principle, stated in the module docstring, is that **the arbiter is deliberately the dumbest component of the architecture**: it computes nothing and normalises nothing. It receives N already-comparable Gap Grades and applies a lexicographic policy. All the intelligence lives upstream, in `compute_gap_grade`.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Elect a winner among N bids on the sole basis of the Gap Grade. |
| O-2 | Never read a quantity that is not comparable across providers. |
| O-3 | Protect the incumbent with an absolute dead-band. |
| O-4 | Never elect a non-compliant placement when enforcement is `hard`. |
| O-5 | Distinguish "no compliant provider" from "no evaluable provider". |
| O-6 | Be deterministic: identical inputs always produce an identical verdict. |
| O-7 | Justify the verdict — every bid received, retained or not, with the reason. |

## 3. Functional requirements

### 3.1 Filtering (tier ⓪)

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | A bid SHALL be retained only if, **in this order**: it is `evaluable`; its `gap_grade.value` is present; it proposes a `vm_id`; and — if `enforcement == "hard"` — it is `is_compliant`. | Unit test |
| **FR-2** | The evaluability test SHALL **always** precede the compliance test. | Code review — see C-1 |
| **FR-3** | A malformed bid (not a dict) SHALL be rejected with the reason `bid malformé`, without raising. | Unit test |
| **FR-4** | Field extraction SHALL never raise, whatever fields are missing or null. | Unit test |
| **FR-5** | Every bid received SHALL appear in `considered`, retained or not, with its rejection reason. | `curl` |

### 3.2 Ranking (tier ②)

| # | Requirement | Verification |
|---|---|---|
| **FR-6** | Retained bids SHALL be sorted by **increasing** Gap Grade — lower is better. | Unit test |
| **FR-7** | Ties SHALL be broken by the provider's position in `PROVIDER_REGISTRY`, making the outcome deterministic. | Unit test |
| **FR-8** | A provider absent from the registry SHALL be ranked last on the tie-break. | Unit test |

### 3.3 Dead-band (tier ③)

| # | Requirement | Verification |
|---|---|---|
| **FR-9** | With no incumbent, the best bid SHALL win directly — path `DEPLOY`, dead-band `0.0`. | Unit test |
| **FR-10** | If the incumbent has no retained bid, the best SHALL win — nothing to protect, dead-band `0.0`. | Unit test |
| **FR-11** | If the incumbent **is** the best, it SHALL win — dead-band `0.0`, it does not apply to itself. | Unit test |
| **FR-12** | Otherwise the challenger SHALL win only if `best_gap < incumbent_gap − deadband`. Strict inequality. | Unit test |
| **FR-13** | The dead-band SHALL be **absolute**, subtracted from the Gap Grade — not a ratio. | Code review |

### 3.4 Verdict

| # | Requirement | Verification |
|---|---|---|
| **FR-14** | The verdict SHALL carry a `path` among `A`, `B`, `C`, `D`, `DEPLOY`. | `curl` |
| **FR-15** | `decision` SHALL be `stay` if the winning VM **is** `incumbent_vm`, `migrate` otherwise. | Unit test |
| **FR-16** | A distinct, human-readable `reason` SHALL be produced for each of the five situations. | `curl` |
| **FR-17** | `deadband_applied` SHALL report the value actually used — `0.0` when the dead-band did not apply. | `curl` |
| **FR-18** | The verdict SHALL be an immutable dataclass, serialisable via `to_dict()`. | Code review |

### 3.5 Failure paths

| # | Requirement | Verification |
|---|---|---|
| **FR-19** | With no retained bid but at least one evaluable, the path SHALL be `C` and the alert kind `INFAISABLE`. | Unit test |
| **FR-20** | With no evaluable bid at all, the path SHALL be `D` and the alert kind `SANS_DONNEES`. | Unit test |
| **FR-21** | On path `C`, the alert SHALL carry the **best rejected offer** (`best_effort`) — the placement that *would* have won without the compliance rule. | Unit test |
| **FR-22** | Both failure paths SHALL return `decision = "stay"` with no winner. | Unit test |
| **FR-23** | The alert SHALL list every provider evaluated. | `curl` |

### 3.6 HTTP

| # | Requirement | Verification |
|---|---|---|
| **FR-24** | `POST /arbitrate` SHALL reject with `400` a payload whose `bids` is absent or not a list. An **empty list is valid** — it yields path `D`. | Unit test |
| **FR-25** | `enforcement` and `deadband` SHALL be overridable per request, defaulting to the global configuration. | `curl` |
| **FR-26** | `GET /health` SHALL report the active policy (`enforcement`, `deadband`). | `curl` |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Purity** | The decision logic SHALL be a pure function: no I/O, no network, no state, no FastAPI import in `arbiter.py`. | Fully testable without mocks; the verdict is reproducible from its arguments alone. |
| **NFR-2 — Determinism** | Two identical calls SHALL always produce identical verdicts, including on ties. | An arbitration whose outcome depended on dict ordering would be unauditable. |
| **NFR-3 — Isolation from the hub** | The service SHALL NOT import `hub/provider_arbitration.py`. | A service must not know the hub's internals; the bid format is the only contract. |
| **NFR-4 — Total tolerance** | No malformed bid SHALL ever raise. | One broken peer must not prevent the federation from deciding. |
| **NFR-5 — Auditability** | The verdict SHALL contain everything needed to recompute it by hand. | `considered` + `deadband_applied` + `reason`. |
| **NFR-6 — Latency** | Arbitration SHALL be negligible in the cycle budget. | A sort over N ≤ 10 entries. |
| **NFR-7 — Configurable policy** | `enforcement` and `deadband` SHALL be settable globally and per call. | Allows an experimental campaign to vary the policy without redeploying. |

## 5. Interface contract

### 5.1 Consumed — `POST /arbitrate`

Caller: the initiating hub, at step 5 of the Contract Net, after collecting the bids via `/broadcast`.

```jsonc
{
  "bids": [
    { "provider_id": "provider-1",
      "placement_plan": { "vm_id": "edge1b" },
      "gap_grade": { "value": -0.12, "is_compliant": true, "evaluable": true } },
    { "provider_id": "provider-2",
      "placement_plan": { "vm_id": "edge2b" },
      "gap_grade": { "value": -0.31, "is_compliant": true, "evaluable": true } }
  ],
  "incumbent_provider": "provider-1",
  "incumbent_vm":       "edge1",
  "enforcement":        "hard",     // optional
  "deadband":           0.05        // optional
}
```

Only three fields of a bid are read: `provider_id`, `placement_plan.vm_id`, and the three sub-fields of `gap_grade`. **Everything else — including `topsis_score` and `vm_scores` — is ignored by design.**

### 5.2 Produced — the verdict

```jsonc
{
  "decision":        "migrate",
  "winner_provider": "provider-2",
  "winner_vm":       "edge2b",
  "gap_grade":       -0.31,
  "path":            "B",
  "reason":          "provider-2 prend le service sur edge2b (-0.3100) : bat provider-1 (-0.1200) au-delà du dead-band 0.0500",
  "deadband_applied": 0.05,
  "considered": [
    {"provider_id": "provider-1", "gap_grade": -0.12, "is_compliant": true,
     "evaluable": true, "retained": true, "why": "retenu"},
    {"provider_id": "provider-2", "gap_grade": -0.31, "is_compliant": true,
     "evaluable": true, "retained": true, "why": "retenu"}
  ],
  "alert": null
}
```

### 5.3 The five paths

| Path | Meaning | `decision` |
|---|---|---|
| `DEPLOY` | First placement — no incumbent to compare | `migrate` |
| `A` | The incumbent keeps the service | `stay` or `migrate`¹ |
| `B` | A challenger wins, beyond the dead-band | `migrate` |
| `C` | **INFAISABLE** — evaluable providers exist, none compliant | `stay` |
| `D` | **SANS_DONNEES** — no evaluable provider at all | `stay` |

¹ Path `A` means the *provider* is unchanged; the winning VM may still differ from `incumbent_vm`, in which case it is an **intra-provider** migration.

### 5.4 Responses

| Status | Condition |
|---|---|
| `200` | Verdict rendered — including paths `C` and `D` |
| `400` | `bids` absent or not a list |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **The filter order is not interchangeable.** Evaluability must be tested *before* compliance. By the "ML down" neutrality rule of `hub/provider_arbitration.py::evaluate_provider`, a provider with no evaluable VM returns `is_compliant = true` while having strictly nothing to offer. Testing compliance first would hand the auction to a **blind provider**. |
| **C-2** | **Reading `topsis_score` is forbidden.** Not a style rule — a correctness one. Those scores are pool-relative and comparing them across providers yields a false answer (see §1). |
| **C-3** | **The dead-band protects the provider, not the VM.** A VM change *within* the winning provider is guarded only by the temporal `MIGRATION_COOLDOWN_S`. Deliberate asymmetry: an intra-provider move is cheap, an inter-provider one is not. |
| **C-4** | **`ARBITER_DEADBAND = 0.05` is an engineering parameter**, chosen, not measured. Its readability comes from the Gap Grade already being threshold-normalised. |
| **C-5** | **`SLO_ENFORCEMENT = "hard"` can be relaxed per call.** In `soft` mode a non-compliant placement can win, and path `C` becomes unreachable. The demonstrator runs `hard`. |
| **C-6** | **The service does not know what a VM or an SLO is.** It ranks opaque identifiers by a float. It cannot detect that a bid is absurd. |
| **C-7** | **No authentication.** Any host can submit forged bids and obtain a verdict — and the initiating hub acts on it. |
| **C-8** | **Each provider calls its own arbiter.** The initiator forwards the bids to *its* arbiter (`:8011` or `:8111`). There is no shared arbiter; two initiators arbitrate independently. |

## 7. Out of scope

- Computing the Gap Grade — `hub/provider_arbitration.py::compute_gap_grade`.
- Producing a bid — the peer hub, via `/evaluate`.
- Transporting bids — `provider_relay`.
- Ranking VMs within a provider — `decision_intelligence` (TOPSIS).
- Executing the migration or the award — the Hub.
- Enforcing the temporal cooldown — the Hub.
- Any I/O whatsoever (NFR-1).

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this service |
|---|---|
| O1 — Multi-provider orchestrator | Implements *"aggregate responses and select feasible QoS strategies"* literally: it aggregates the bids and elects a feasible placement, with `hard` enforcement guaranteeing feasibility. It is step 5 of the Contract Net. |
| O2 — Intent–QoS relationship engine | None. It arbitrates on an already-computed scalar. |
| O3 — Visualization & explainability | **Strong.** `considered` lists every bid with its rejection reason, `path` classifies the decision into five cases, `reason` states it in words, and the `INFAISABLE` alert carries the best rejected offer — *"nobody could, and here is who came closest"*. |
| O4 — Experimental validation | The `path` distribution across a session (A/B/C/D/DEPLOY) is a directly reportable experimental result, and the per-call `deadband` override allows a sensitivity study without redeploying. |
