# Provider Relay — Technical Documentation

> **Document type:** Technical documentation (*how the service is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `services.provider_relay` |
| Entry point | `services/provider_relay/app.py` |
| Framework | FastAPI + Uvicorn |
| Port | `config.PROVIDER_RELAY_PORT` = `8010 + PORT_OFFSET` |
| Business state | **None** |
| Lines of code | 590 (single file) |

---

## 1. Role in the architecture

```text
        PROVIDER 1                                      PROVIDER 2
  ┌────────────────────┐                          ┌────────────────────┐
  │   hub P1  :8000    │                          │   hub P2  :8100    │
  └─────────┬──────────┘                          └──────────▲─────────┘
            │ localhost                             localhost │
            ▼                                                 │
  ┌────────────────────┐        network            ┌──────────┴─────────┐
  │  relay P1  :8010   │ ───────────────────────►  │  relay P2  :8110   │
  │                    │                           │                    │
  │  OUT               │                           │  IN                │
  │   /broadcast       │ ─────────────────────►    │   /inbound/evaluate│
  │   /award           │ ─────────────────────►    │   /inbound/award   │
  │   /intent/propagate│ ─────────────────────►    │   /inbound/intent  │
  │   /handoff         │ ─────────────────────►    │   /inbound         │
  └────────────────────┘                           └────────────────────┘
```

Four outbound routes, four inbound counterparts, perfectly symmetric. Each relay is simultaneously both halves — the diagram shows one direction only.

**The invariant to internalise:** a hub is only ever contacted by *its own* relay, over localhost. `CORE_URL` is always local. `PROVIDER_RELAY_URLS` is always remote. Confusing the two is the mistake this design exists to prevent.

## 2. Folder structure

```text
services/provider_relay/
├── __init__.py         # empty — makes the package importable
├── app.py              # everything: 9 routes, no business module
├── requirements.txt
├── SPEC.md
└── TECHNICAL_DOC.md
```

The only service of the stack with **no `*_handler.py`**. That is the point: there is no business layer to separate. The two-file convention would create an empty module.

The module docstring (lines 1–25) is worth reading before the code — it states the routing rule and the mono-process behaviour more compactly than any comment.

## 3. Internal design

### 3.1 The three route families

| Family | Pattern | Routes |
|---|---|---|
| **Unicast** | one target, response relayed | `/handoff`, `/award` |
| **Scatter-gather** | all peers in parallel | `/broadcast`, `/intent/propagate` |
| **Terminal delivery** | to the local hub | the four `/inbound/*` |

### 3.2 `/handoff` — unicast with three guards

```python
1. target_provider ∉ PROVIDER_RELAY_URLS        → 400
2. target_provider == from_provider             → 409  (caller bug)
3. target_provider ∈ attempted_providers        → 409  (ANTI-LOOP)
4. POST {target_relay}/inbound
5. response + relayed_by + target_orchestrator
```

**Guard 3 is the one that matters.** Without it, two non-compliant providers would bounce the same intent between them forever: P1 cannot satisfy it, hands off to P2; P2 cannot either, hands back to P1; repeat. The `attempted_providers` list, carried in `slo_intent` and appended to by each hub, breaks the cycle. The code comment is explicit that this guard — and it alone — is what makes scaling to N real orchestrators safe on this path.

Note the dependency it implies: **the guard only works if the caller populates `attempted_providers`.** The relay reads that field but never writes it. This is the single place where the relay's "pure transport" purity is bent — it inspects one field of the payload — and the docstring flags it explicitly as the exception.

The response is enriched with `relayed_by` and `target_orchestrator` before being returned. In a four-hop chain, knowing *which* hop produced a given response is otherwise impossible.

### 3.3 `/broadcast` — the scatter-gather that matters

```python
targets = {pid: url for pid, url in PROVIDER_RELAY_URLS.items()
           if pid != from_provider}          # self-exclusion

if not targets: → {"bids": [], "errors": []}  # single-member federation, VALID

target_items = list(targets.items())          # ORDER FROZEN
async with httpx.AsyncClient() as client:     # ONE shared client
    tasks   = [_call(url, client) for _pid, url in target_items]
    results = await asyncio.gather(*tasks, return_exceptions=True)

for (pid, _url), result in zip(target_items, results):
    exception            → errors
    status ≥ 400         → errors
    dict body            → bids
    non-dict body        → errors
```

Four properties, each load-bearing:

**Self-exclusion.** The initiator evaluates its own VMs locally — no network round-trip to itself. Same convention as `/handoff`'s `target != from` rule.

**Parallel, not sequential.** The comment quantifies the stake: 3 peers at 800 ms costs ~800 ms with `gather`, ~2400 ms with a `for` loop. A sequential dispatch would make cycle time grow **linearly with federation size** — which would make the whole multi-provider design unscalable in exactly the dimension it is meant to demonstrate.

**`return_exceptions=True`.** Without it, the first peer to raise would cancel the whole gather and lose the bids that had already arrived. With it, every task's outcome is collected and classified.

**Frozen ordering.** `target_items` is materialised as a list *before* dispatch and reused in the `zip`. Iterating the dict again after the gather would risk misaligning provider ids with results.

**Always `200`.** The only rejections are a missing `slos` or `from_provider`. A dead peer, a timeout, a malformed body — all become `errors` entries. The alternative would let one broken provider paralyse the entire federation, which is exactly the failure mode a federation is supposed to survive.

### 3.4 `/award` — best-effort by design

```python
if target unknown:  return {"delivered": False, "error": …}   # 200
try:    POST {relay}/inbound/award  timeout=AWARD_TIMEOUT_S   # 3 s, SHORT
except: return {"delivered": False, "error": str(exc)}        # 200
return  {"delivered": True}
```

Two deliberate departures from the other routes.

**A short timeout** (3 s vs 5 s). The award is not on the critical path of a decision — it is a hint.

**No error status, ever.** Every failure returns `200`. The docstring explains the purpose of the award: it tells the winning peer *which VM* was elected, so it does not have to rediscover it via kubectl. And kubectl resolves to the **node**, not the VM — with `edge1`, `edge1b`, `edge1c` sharing one physical node, kubectl would report `edge1` when the service actually runs on `edge1c`. The award carries the precise answer.

But if it does not arrive, the peer simply falls back to kubectl discovery — the behaviour that existed before this optimisation. So a failed award degrades precision, not correctness, and must never surface as an error the caller has to handle.

### 3.5 `/intent/propagate` — broadcast without aggregation

Structurally identical to `/broadcast`: same self-exclusion, same frozen ordering, same `gather` with `return_exceptions`. Two differences:

- `from_provider` is **stripped** from the relayed body (the peer knows who it is).
- No response body is collected — only `delivered` (a list of provider ids) and `errors`. It is a notification, not a collection.

The duplication with `/broadcast` is real and acknowledged in SPEC C-4.

### 3.6 The `/inbound/*` routes — terminal by construction

All four share one shape:

```python
local_hub = f"{config.CORE_URL}/<route>"      # ALWAYS localhost
POST local_hub, json=payload
unreachable → 502
status ≥ 400 → propagate
else → return body as-is
```

| Route | Local hub endpoint | Special behaviour |
|---|---|---|
| `/inbound` | `/intent/relay` | — |
| `/inbound/evaluate` | `/evaluate` | Returns the local hub's bid |
| `/inbound/award` | `/award` | — |
| `/inbound/intent` | `/intent` | **Forces `propagate: false`** |

**The terminal property is the strongest loop guarantee in the system.** These routes never call another relay and never re-broadcast. There is nobody to forward to, so a loop is impossible *by construction* — no payload inspection required. This is why the docstrings state that `/handoff`'s `attempted_providers` guard is unnecessary here.

`/inbound/intent` adds one explicit guard on top:

```python
body = {**payload, "propagate": False}
```

Without it, the receiving hub would propagate the intention onward, the peer would propagate it back, and the two would repropagate indefinitely. Forcing the flag at the relay — rather than trusting the hub to notice — puts the guard at the boundary where it cannot be forgotten.

### 3.7 Response normalisation

Repeated in five routes:

```python
try:    body = resp.json()
except ValueError: body = {"raw_response": resp.text}
if not isinstance(body, dict): body = {"response": body}
```

Guarantees the caller always receives a JSON object, even if a peer returns plain text or a bare array. Defensive, and duplicated — a candidate for a helper (L-2).

## 4. API reference

### `POST /handoff`

| Field | Type | Required | Notes |
|---|---|---|---|
| `target_provider` | string | **yes** | Must be a `PROVIDER_RELAY_URLS` key |
| `from_provider` | string | **yes** | Must differ from the target |
| `slo_intent` | object | yes | Opaque, **except** `attempted_providers` |
| `offer` | object | no | Opaque |
| `incumbent_provider` / `incumbent_vm` | string | no | Opaque, needed by the receiving hub for TOPSIS |

`200` relayed · `400` unknown target · `409` self-handoff or anti-loop · `502` peer unreachable

### `POST /broadcast`

| Field | Type | Required |
|---|---|---|
| `slos` | array | **yes** |
| `from_provider` | string | **yes** |
| `intent_id`, `incumbent_vm` | — | no |

Returns `{bids, errors, relayed_by, timestamp}`. **Always `200`** unless a required field is missing.

### `POST /award`

| Field | Type | Required |
|---|---|---|
| `target_provider` | string | **yes** |
| `vm_id`, `intent_id`, `from_provider` | — | no, opaque |

Returns `{"delivered": true\|false, "error"?: …}`. **Always `200`.**

### `POST /intent/propagate`

| Field | Type | Required |
|---|---|---|
| `from_provider` | string | **yes** |
| *everything else* | — | opaque, relayed |

Returns `{delivered: [provider_ids], errors: [...]}`.

### `POST /inbound`, `/inbound/evaluate`, `/inbound/award`, `/inbound/intent`

Received from a peer relay. Deliver to the local hub. `200` or `502`.

### `GET /health`

```json
{"status": "healthy", "service": "provider_relay",
 "peer_relays": {"provider-1": "http://localhost:8010",
                 "provider-2": "http://localhost:8010"},
 "local_hub":   "http://localhost:8000"}
```

**The most useful health endpoint of the stack for diagnosis.** It prints the routing table, so a misconfiguration is visible immediately. Two identical `peer_relays` entries means mono-process mode; two different ones means distributed.

**Example**

```bash
curl -X POST http://localhost:8010/broadcast -H "Content-Type: application/json" -d '{"from_provider":"provider-1","intent_id":"demo-001","incumbent_vm":"edge1","slos":[{"metric":"latency","operator":"<","threshold":28.0,"weight":1.0,"is_primary":true}]}'
```

## 5. Configuration

| Variable | Default | Used for |
|---|---|---|
| `PROVIDER_RELAY_PORT` | `8010` (+`PORT_OFFSET`) | Listening port |
| `RELAY_URL_PROVIDER_1` | `http://localhost:8010` | **Peer relay** address |
| `RELAY_URL_PROVIDER_2` | `http://localhost:8010` | idem |
| `CORE_URL` | `http://localhost:8000` (+offset) | **Local hub** — always localhost |
| `POST_TIMEOUT` | `5.0` s | All routes except award |
| `AWARD_TIMEOUT_S` | `3.0` s | Award only |
| `MULTI_PROVIDER_ENABLED` | `false` | Gates the hub's state machine — with it off, the relay receives nothing |

By default **both** `RELAY_URL_PROVIDER_*` point at `:8010`, so a handoff loops back to the same process. Moving to distributed means:

```bash
RELAY_URL_PROVIDER_1=http://192.168.1.10:8010 \
RELAY_URL_PROVIDER_2=http://192.168.1.20:8010 \
MULTI_PROVIDER_ENABLED=true python -m services.provider_relay.app
```

That, plus `CORE_URL` per machine, is the **entire** change required. No service code is touched — the claim in the README that "scaling to N real orchestrators means changing those URLs, and nothing else" is verifiable here.

## 6. Dependencies

**Internal** — `shared.config` (routing tables and timeouts), `shared.logging_utils`. **Notably absent:** `shared.models`, `hub.provider_arbitration`. The relay imports no domain type, which is what makes "pure transport" enforceable rather than aspirational.

**External** — `fastapi`, `uvicorn`, `httpx`.

**Runtime**

| Dependency | Nature | On failure |
|---|---|---|
| Peer relays | soft | `errors` (broadcast) or `502` (handoff) |
| Local hub | hard for `/inbound/*` | `502` |

## 7. Data model

None. No state, no cache, no persistence. Every message is forwarded and forgotten. Two relays are fully interchangeable and restartable at any time.

## 8. Running it standalone

```bash
python -m services.provider_relay.app
```

Second provider:

```bash
PORT_OFFSET=100 python -m services.provider_relay.app
```

Distributed federation: see §5.

Start it **before** the hub — the hub health-checks its dependencies at boot.

## 9. Logging and observability

| Marker | Level | Meaning |
|---|---|---|
| `🚀 Provider Relay — Démarrage` | INFO | **Full routing table** — the first thing to check when a federation misbehaves |
| `📤 /handoff — provider-1 → provider-2 (url)` | INFO | Outbound unicast |
| `🔁 GARDE ANTI-BOUCLE déclenchée` | WARNING | **A loop was prevented** — always worth investigating |
| `📡 /broadcast — provider-1 → N cible(s)` | INFO | Broadcast dispatched |
| `📬 /broadcast — 1 bid(s) reçu(s), 0 erreur(s)` | INFO | **The federation health line** |
| `📨 /award — provider-1 → provider-2 : vm=edge2b` | INFO | Award relayed |
| `📥 /inbound/evaluate — relais pair → hub local` | INFO | Inbound delivery |
| `❌ hub local injoignable` | ERROR | `502` — the local hub is down |
| `⚠️ relais cible injoignable` | WARNING | The peer is down |

Two lines carry most of the diagnostic value:

- **The startup routing table.** Two identical peer URLs = mono-process. Different = distributed. A wrong entry here explains every subsequent federation anomaly.
- **`📬 N bid(s), M erreur(s)`.** With `0 bid, 1 erreur` on every cycle, the federation is nominally enabled but effectively degraded to a single provider — and nothing else in the system will say so out loud.

## 10. Testing

| File | Covers |
|---|---|
| `tests/unit/test_provider_relay.py` | The relay's routes |
| `tests/unit/test_relay_broadcast.py` | The scatter-gather path |
| `tests/unit/test_award_message.py` | The award path |
| `tests/unit/test_hub_relay_endpoint.py` | Relay ↔ hub integration |

Well covered — four dedicated files, more than any other service.

```bash
pytest tests/unit/test_provider_relay.py tests/unit/test_relay_broadcast.py tests/unit/test_award_message.py -v
```

## 11. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | No retry on any route (SPEC C-6). | A transient glitch loses a bid, an award or an intention for that cycle. | Use `async_post_with_retry`, with a budget below one cycle. |
| **L-2** | Response normalisation is duplicated in five routes (§3.7). | Divergence risk on maintenance. | Extract a `_normalise_response(resp)` helper. |
| **L-3** | `/broadcast` and `/intent/propagate` duplicate the dispatch pattern (§3.5). | Two implementations of one mechanism. | Factor out a `_scatter(targets, path, body, aggregate: bool)`. |
| **L-4** | A permanently failing `/award` is invisible to the caller (§3.4). | Silent, permanent degradation to kubectl discovery. | Count consecutive failures and log a warning past a threshold. |
| **L-5** | Mono-process by default: the two relay URLs are identical (SPEC C-1). | The real network path between two relays is never exercised in the current deployment. | Run a two-machine test before claiming distributed operation. |
| **L-6** | The anti-loop guard depends on the caller filling `attempted_providers` (§3.2). | A hub that forgets it reopens the loop on the `/handoff` path. | Have the relay append `from_provider` to the list itself. |
| **L-7** | No authentication, no signing (SPEC C-5). | Any host can inject a forged bid or award into the federation. | mTLS between relays, or a shared HMAC. |
| **L-8** | `POST_TIMEOUT` = 5 s against a 6 s cycle (SPEC C-8). | A slow peer consumes most of the cycle budget. | A dedicated, shorter federation timeout. |
| **L-9** | `/handoff` is described in the README as *legacy, pre-Contract-Net*. | Two coexisting handoff mechanisms, only one of which is current. | Confirm it is unused and remove it, or document why it is kept. |
