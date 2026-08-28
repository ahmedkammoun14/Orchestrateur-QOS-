# Placement Arbiter — Technical Documentation

> **Document type:** Technical documentation (*how the service is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `services.placement_arbiter` |
| Entry point | `services/placement_arbiter/app.py` |
| Framework | FastAPI + Uvicorn |
| Port | `config.PLACEMENT_ARBITER_PORT` = `8011 + PORT_OFFSET` |
| I/O | **None** — pure function behind an HTTP envelope |
| Lines of code | 365 (101 + 264) |

---

## 1. Role in the architecture

```text
   ① violation detected on a PRIMARY SLO
                    │
        ┌───────────┴───────────┐   (asyncio.gather — parallel)
        ▼                       ▼
   own bid                 relay /broadcast ──► peers ──► their bids
   (local evaluate)                                          │
        └───────────┬──────────────────────────────────────┘
                    ▼
        ┌────────────────────────────────────────────┐
        │      placement_arbiter :8011               │
        │                                            │
        │  ⓪ filter   evaluable → gap → vm → compliant│
        │  ②  rank     Gap Grade ascending            │
        │  ③  dead-band  protect the incumbent        │
        │  ④  tie-break  registry order               │
        │                                            │
        │  ⛔ NEVER reads topsis_score / vm_scores    │
        └───────────────────┬────────────────────────┘
                            │ ArbitrationVerdict
                            ▼
        the initiating hub applies it:
           local migration, or /award to the winning peer
```

Step 5 of the Contract Net. The service is deliberately **the dumbest component of the architecture** — its docstring says so. It computes nothing and normalises nothing; the intelligence is upstream in `compute_gap_grade`.

Note that each provider calls **its own** arbiter. There is no shared arbiter instance; the initiator forwards the bids to `:8011` or `:8111` depending on which stack it belongs to.

## 2. Folder structure

```text
services/placement_arbiter/
├── __init__.py     # empty
├── app.py          # HTTP envelope: 2 routes, validation, logging
├── arbiter.py      # PURE module: ArbitrationVerdict + arbitrate()
├── requirements.txt
├── SPEC.md
└── TECHNICAL_DOC.md
```

The cleanest separation in the project. `arbiter.py` imports **only** `dataclasses`, `typing` and `shared.config` — no FastAPI, no httpx, no logging. It can be imported and tested from a plain Python REPL.

## 3. Internal design

### 3.1 `ArbitrationVerdict` — an immutable result

```python
@dataclass(frozen=True)
class ArbitrationVerdict:
    decision, winner_provider, winner_vm, gap_grade,
    path, reason, deadband_applied, considered, alert
```

`frozen=True` makes the verdict immutable: once rendered, no caller can alter it before acting. `to_dict()` produces a JSON-serialisable dict, copying `considered` and `alert` so the caller cannot mutate the internals either.

### 3.2 Tolerant extraction — `_safe_get` and `_extract`

```python
def _safe_get(d, key, default=None):
    return d.get(key, default) if isinstance(d, dict) else default
```

Used to walk two levels (`bid → placement_plan → vm_id`, `bid → gap_grade → value`) without ever raising. If `placement_plan` is absent, `_safe_get(None, "vm_id")` returns `None` rather than an `AttributeError`.

This matters because the bids come **from a peer over the network**. A peer running an older version, or partially broken, must not be able to crash the arbitration for everyone. `_extract` reads exactly five fields and ignores everything else — including `topsis_score`, which is present in the bid and deliberately never touched.

### 3.3 `_classify` — the filter, and its critical ordering

```python
if not isinstance(bid, dict):        → "bid malformé"
if not evaluable:                    → "non évaluable"        ← ALWAYS FIRST
if gap_value is None:                → "gap_grade absent"
if vm_id is None:                    → "aucune VM proposée"
if enforcement == "hard" and not is_compliant:
                                     → "non conforme (mode hard)"
                                     → "retenu"
```

**The order of tests (a) before (d) is a correctness requirement, not a style choice.** The docstring explains it precisely, and it is the subtlest point of the whole service:

By the "ML down" neutrality rule of `hub/provider_arbitration.py::evaluate_provider`, a provider whose VMs are **all non-evaluable** returns `is_compliant = True`. The reasoning upstream is defensible — with no data, you cannot declare non-compliance — but it produces a bid that claims compliance while proposing nothing real.

If compliance were tested first, that blind provider would be retained and could **win the auction**. Testing evaluability first eliminates it before compliance is ever consulted. Swapping two lines here would hand placements to providers that cannot see their own infrastructure — and the symptom would be a migration towards a provider whose ML chain is down, which is close to unfalsifiable from the outside.

Each rejection returns its reason, and every reason ends up in `considered`.

### 3.4 Ranking and tie-break

```python
def _tie_break_key(entry):
    return (entry["gap_value"], order_index.get(entry["provider_id"], len(order)))

ranked = sorted(retained, key=_tie_break_key)
best   = ranked[0]
```

A tuple sort: Gap Grade ascending first (**lower is better** — the opposite polarity from TOPSIS), then the provider's index in `PROVIDER_REGISTRY`. An unknown provider gets `len(order)`, i.e. last.

The tie-break exists purely for determinism. Two providers with identical Gap Grades must always yield the same winner, or an arbitration could not be replayed or audited. `.get(..., len(order))` also guarantees the key never raises on a provider absent from the registry.

### 3.5 The dead-band — four cases

```python
if incumbent_provider is None:                 winner = best,  db = 0.0, path = "DEPLOY"
elif incumbent_bid is None:                    winner = best,  db = 0.0
elif best.provider == incumbent_provider:      winner = best,  db = 0.0
else:
    db = deadband
    winner = best if best.gap < incumbent.gap - db else incumbent
```

Three of the four cases apply **no** dead-band, and each for a different reason:

- **No incumbent** — first placement, there is nothing to protect.
- **The incumbent submitted no retained bid** — it is non-compliant or blind; protecting it would mean keeping the service on a provider that cannot honour the contract.
- **The incumbent *is* the best** — the dead-band protects the incumbent *from* challengers, so applying it against itself would be meaningless.

Only the fourth case — a challenger better than a still-viable incumbent — triggers it:

```
best.gap < incumbent.gap − 0.05
```

**Absolute subtraction, not a ratio.** This works precisely because the Gap Grade is already normalised by the SLO thresholds upstream, so `0.05` reads directly as "5 % of the threshold": 1.4 ms on a 28 ms latency SLO. Note the contrast with `decision_intelligence`'s intra-provider hysteresis, which is *multiplicative* (`× 1.05`) on a pool-relative score — different scale, different semantics, and the two must not be conflated.

The strict `<` means an exact tie at the dead-band boundary favours the incumbent.

### 3.6 Path and decision — two orthogonal questions

```python
path     = "A" if winner.provider == incumbent_provider else "B"
decision = "stay" if winner.vm_id == incumbent_vm else "migrate"
```

`path` answers *"did the provider change?"*; `decision` answers *"did the VM change?"*. They are independent, which produces a combination worth knowing:

**Path `A` with `decision = "migrate"` is an intra-provider migration** — the same provider keeps the service but moves it to a different VM of its own pool. This is the most frequent case in the PiCar demonstration, since a provider covers an arc of the lap with several edge VMs.

### 3.7 The two failure paths

Reached when `retained` is empty:

```python
any_evaluable = any(e["evaluable"] for e in all_extracted)
path = "C" if any_evaluable else "D"
kind = "INFAISABLE" if path == "C" else "SANS_DONNEES"
```

| Path | Meaning | Diagnosis |
|---|---|---|
| `C` — INFAISABLE | Providers can see, none can comply | **Infrastructure problem** — the contract is too strict, or every VM is degraded |
| `D` — SANS_DONNEES | Nobody can even evaluate | **System problem** — the ML chain is down federation-wide |

The distinction is the operational value of this service. Both produce `stay`, but the actions they call for are opposite: path `C` means relax the contract or fix the VMs; path `D` means restart the ML APIs.

On path `C`, the alert carries `best_effort` — the best **rejected** offer:

```python
be = min(evaluable_with_gap, key=_tie_break_key)
alert.best_effort = {provider_id, vm_id, gap_grade}
```

This is what turns "nobody could" into "nobody could, and here is who came closest, at this distance from the contract". For an operator deciding whether to relax an SLO, that number is the whole answer.

### 3.8 The five reasons

A distinct sentence per situation, all naming the actual values:

| Situation | Reason template |
|---|---|
| First placement | *"…prend le service sur X (g) : premier placement, aucun tenant à comparer"* |
| Incumbent has no bid | *"…: le tenant 'P' n'a aucun bid retenu, rien à protéger"* |
| Incumbent is best | *"P conserve le service sur X (g) : déjà le meilleur Gap Grade"* |
| Challenger wins | *"P2 prend le service sur X (g2) : bat P1 (g1) au-delà du dead-band d"* |
| Dead-band blocks | *"P1 conserve le service : P2 (g2) ne bat pas g1 du dead-band d requis"* |

They are the audit trail. Each contains enough numbers to recompute the verdict by hand.

### 3.9 `app.py` — a thin envelope

Validation is minimal:

```python
if not isinstance(bids, list): → 400
```

An **empty list is valid** — it produces path `D`, which is the correct answer to "nobody bid". Rejecting it would force the caller to special-case a legitimate federation state.

`enforcement` and `deadband` are read from the payload and passed through; `arbitrate()` falls back to the global config when they are `None`. The per-call override is what makes a sensitivity study possible without redeploying.

Note `provider_order` is a parameter of `arbitrate()` but is **not** exposed over HTTP — it exists for testing, defaulting to `PROVIDER_REGISTRY` order.

## 4. API reference

### `POST /arbitrate`

**Request**

| Field | Type | Required | Description |
|---|---|---|---|
| `bids` | array | **yes** | May be empty. Each entry: `provider_id`, `placement_plan.vm_id`, `gap_grade.{value,is_compliant,evaluable}` |
| `incumbent_provider` | string | no | `null` → path `DEPLOY` |
| `incumbent_vm` | string | no | Determines `stay` vs `migrate` |
| `enforcement` | string | no | `"hard"` (default) or `"soft"` |
| `deadband` | float | no | Default `ARBITER_DEADBAND` = 0.05 |

**Response `200`** — see SPEC §5.2. `400` if `bids` is not a list.

**Example**

```bash
curl -X POST http://localhost:8011/arbitrate -H "Content-Type: application/json" -d '{"incumbent_provider":"provider-1","incumbent_vm":"edge1","bids":[{"provider_id":"provider-1","placement_plan":{"vm_id":"edge1"},"gap_grade":{"value":0.12,"is_compliant":false,"evaluable":true}},{"provider_id":"provider-2","placement_plan":{"vm_id":"edge2b"},"gap_grade":{"value":-0.31,"is_compliant":true,"evaluable":true}}]}'
```

In `hard` mode, provider-1 is rejected as non-compliant; provider-2 wins on path `B` with `decision = migrate`.

### `GET /health`

```json
{"status": "healthy", "service": "placement_arbiter",
 "enforcement": "hard", "deadband": 0.05}
```

Returns the **active policy**, not just liveness — the fastest way to confirm which enforcement mode a running federation is using.

## 5. Configuration

| Variable | Default | Used for |
|---|---|---|
| `PLACEMENT_ARBITER_PORT` | `8011` (+`PORT_OFFSET`) | Listening port |
| `SLO_ENFORCEMENT` | `hard` | `hard` = never elect a non-compliant placement |
| `ARBITER_DEADBAND` | `0.05` | Absolute dead-band on the Gap Grade |
| `PROVIDER_REGISTRY` | 2 providers | Tie-break order |

Both policy values are overridable per request, which is unusual in this codebase and deliberate: it lets an experimental campaign sweep the parameter space against a live federation.

## 6. Dependencies

**Internal** — `shared.config` only (in `arbiter.py`), plus `shared.logging_utils` in `app.py`. **Notably absent:** `hub.provider_arbitration`. The arbiter does not know how a Gap Grade is computed, only that it is comparable.

**External** — `fastapi`, `uvicorn`. Nothing else: no numpy, no httpx.

**Runtime** — **none**. The service calls nobody and can run in complete isolation.

## 7. Data model

Stateless. No cache, no history, no persistence. The verdict exists only in the HTTP response; the initiating hub persists it via `database` if it acts on it.

## 8. Running it standalone

```bash
python -m services.placement_arbiter.app
```

Second provider:

```bash
PORT_OFFSET=100 python -m services.placement_arbiter.app
```

No dependency to start first. `arbiter.py` can also be exercised without any server:

```python
from services.placement_arbiter.arbiter import arbitrate
arbitrate(bids=[...], incumbent_provider="provider-1", incumbent_vm="edge1")
```

## 9. Logging and observability

| Marker | Level | Meaning |
|---|---|---|
| `🚀 Placement Arbiter — Démarrage` | INFO | Banner with the active policy and the "never reads topsis_score" reminder |
| `⚖️ /arbitrate — chemin B \| gagnant : provider-2/edge2b \| gap_grade=-0.31 \| 2/3 bid(s) retenu(s)` | INFO | **One line per arbitration — everything essential is in it** |
| `⚠️ /arbitrate — 'bids' absent ou n'est pas une liste` | WARNING | `400` |

A single log line per decision, which makes this the quietest terminal of the stack — and the easiest to read during a demonstration.

Four things to read in it:

- **`chemin`** — `A` incumbent keeps, `B` challenger wins, `C` infeasible, `D` no data, `DEPLOY` first placement.
- **`gagnant`** — provider/VM.
- **`gap_grade`** — negative means every primary SLO is met with margin; positive quantifies the worst breach.
- **`2/3 bid(s) retenu(s)`** — if this ratio is persistently low, the filter is eliminating peers and the `considered` array says why.

A recurring `chemin C` means the contract is unsatisfiable federation-wide; a recurring `chemin D` means the ML chain is down. Those two lines are the fastest federation diagnosis available.

## 10. Testing

`tests/unit/test_placement_arbiter.py` covers the service. Being a pure module, it needs no mocks — every path is reachable by constructing a bid list.

```bash
pytest tests/unit/test_placement_arbiter.py -v
```

The case most worth guarding explicitly is the filter ordering of §3.3: a bid with `evaluable = false` **and** `is_compliant = true` must be rejected. That is the exact shape a blind provider produces, and it is the one defect here that would be nearly invisible in production.

## 11. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | `provider_order` is not exposed over HTTP (§3.9). | Tie-break order cannot be varied without editing `PROVIDER_REGISTRY`. | Accept it in the payload, as `enforcement` and `deadband` already are. |
| **L-2** | The verdict is not persisted. | Path distribution over a session must be reconstructed from the logs. | Have the Hub store the verdict alongside the decision. |
| **L-3** | The arbiter cannot detect an absurd Gap Grade. | A buggy peer reporting `-999` wins every auction. | Sanity-check the range, e.g. reject `value < -1` (the Gap Grade is floored at −1 upstream). |
| **L-4** | `deadband` is not validated. | A negative dead-band would *encourage* migration; a huge one would freeze the federation. | Clamp to `[0, 1]`. |
| **L-5** | The dead-band protects the provider, not the VM (SPEC C-3). | Intra-provider oscillation is guarded only by the temporal cooldown. | Deliberate; document it as such. |
| **L-6** | Each provider has its own arbiter (SPEC C-8). | Two initiators arbitrating simultaneously could reach conflicting verdicts. | Not reachable today — only the `ACTIVE` provider initiates. |
| **L-7** | No authentication. | Any host can submit forged bids; the initiating hub acts on the verdict. | Acceptable on the demonstrator's private network. |
