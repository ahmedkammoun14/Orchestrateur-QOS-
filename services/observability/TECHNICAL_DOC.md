# Observability — Technical Documentation

> **Document type:** Technical documentation (*how the service is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `services.observability` |
| Entry point | `services/observability/app.py` |
| Framework | FastAPI + Uvicorn, SSE |
| Port | `config.OBSERVABILITY_PORT` = `8009 + PORT_OFFSET` |
| State | In-memory, bounded, volatile |
| Lines of code | 1581 (1169 + 412), of which **945 are the embedded HTML** |

---

## 1. Role in the architecture

```text
                    ┌──────────────── Hub :8000 ────────────────┐
                    │                                           │
                    │  POST /audit  (fire-and-forget, 3 sites)  │
                    │  GET  /data   ◄── polled every 2 s        │
                    │  POST /reset  ◄── proxied on demand       │
                    └──────┬─────────────────────▲──────────────┘
                           │                     │
                           ▼                     │
        ┌──────────────────────────────────────────────────────┐
        │              observability :8009                     │
        │                                                      │
        │   _audit_log     deque(maxlen=500)                   │
        │   _subscribers   [asyncio.Queue, …]                  │
        │   _provider_path_counts  {A,B,C,D}                   │
        │                                                      │
        │   _poll_hub()  ──2 s──►  _broadcast({type:metrics})  │
        │   /audit       ──────►   _broadcast({type:audit})    │
        └──────────────────────────┬───────────────────────────┘
                                   │ SSE  text/event-stream
                                   ▼
                          Browser — dashboard HTML
                                   ▲
                                   │ GET /audit/log
                          federation_view :8500
```

The service sits **beside** the cycle, not inside it. Two data paths, deliberately asymmetric:

- **Audit** is *pushed* by the hub — decisions are rare (one per cycle) and must not be missed.
- **Metrics** are *pulled* by this service — they are continuous, and pulling means a dead dashboard costs the hub nothing.

That asymmetry is the whole non-intrusiveness argument.

## 2. Folder structure

```text
services/observability/
├── app.py           # everything: state, SSE, routes, and the 945-line HTML string
├── visualizer.py    # matplotlib dashboard — standalone, imported by NOBODY
├── requirements.txt
├── SPEC.md
└── TECHNICAL_DOC.md
```

`app.py` is the largest single file of the stack, and **81 % of it is the HTML page** (lines 196–1140). The Python logic is roughly 220 lines.

`visualizer.py` (412 lines) is a **matplotlib** dashboard polling the same `/data`, with keyboard navigation between views and an MI panel. Grepping the codebase finds **no importer**: it is a standalone tool, presumably predating the web dashboard. Worth knowing before assuming it runs — see L-1.

## 3. Internal design

### 3.1 Module state — three structures

```python
_audit_log            = deque(maxlen=500)      # decision history
_subscribers          = []                     # one asyncio.Queue per browser
_provider_path_counts = {"A":0, "B":0, "C":0, "D":0}
```

`deque(maxlen=500)` is the memory guarantee: appending past 500 evicts the oldest automatically. No cleanup task, no growth, no configuration — the bound is structural.

All three are module-level globals, mutated by request handlers. Safe here because the whole service runs on a single event loop and none of the mutations awaits between read and write.

### 3.2 SSE — `GET /stream`

```python
queue = asyncio.Queue(); _subscribers.append(queue)

async def generator():
    yield snapshot(_audit_log, _provider_path_counts)     # ① hydration
    while True:
        if await request.is_disconnected(): break         # ② detach
        event = await asyncio.wait_for(queue.get(), timeout=30.0)
        yield f"data: {json.dumps(event)}\n\n"
    except asyncio.TimeoutError:
        yield '{"type":"ping"}'                           # ③ keep-alive
    finally:
        _subscribers.remove(queue)                        # ④ cleanup
```

**① The snapshot is what makes mid-session connection work.** Without it, a browser opened after twenty cycles would show an empty log and fill only as new decisions arrived. The snapshot replays the entire history and the current counters in the first frame — the page is complete before the first live event.

**② `is_disconnected()`** is checked before each wait, so a closed tab is detected rather than accumulating a phantom subscriber.

**③ The 30 s ping** prevents intermediate proxies from closing an idle connection.

**④ The `finally`** guarantees the queue is removed on every exit path, including cancellation.

One structural quirk: the `except asyncio.TimeoutError` sits **outside** the `while`, so a timeout emits one ping and then **exits the generator**, closing the stream. The browser's `EventSource` reconnects automatically — which is why this is invisible in practice — but the ping is a disconnect-then-reconnect, not a keep-alive in the strict sense. See L-2.

`_broadcast` iterates `list(_subscribers)` — a copy — so a subscriber removing itself mid-iteration cannot corrupt the loop.

### 3.3 `POST /audit` — receive without altering

```python
payload.setdefault("received_at", now())        # only if absent
provider_path = payload.get("provider_path")    # READ, never written
if provider_path in _provider_path_counts:
    _provider_path_counts[provider_path] += 1
_audit_log.append(payload)
await _broadcast({"type":"audit", "data": payload,
                  "provider_path_counts": dict(_provider_path_counts)})
```

Two deliberate choices, both commented in the source:

**`setdefault`, not assignment.** If the hub already stamped `received_at`, its value wins. The stored entry stays as close as possible to what was sent.

**The counter is read from the payload, never written into it.** The stored entry therefore contains no field this service invented. The comment states the motivation explicitly: **non-regression of mono-provider mode**, whose payloads have no `provider_path` key and must remain byte-identical after passing through here.

The membership test `if provider_path in _provider_path_counts` doubles as validation — an unexpected value increments nothing rather than creating a spurious counter. And a mono-provider event, with no key at all, is counted in **no** category, which is what makes the counters a truthful measure of federated activity.

The broadcast sends `dict(_provider_path_counts)` — a copy — so a later mutation cannot retroactively alter an event already queued.

### 3.4 `_poll_hub` — the metrics loop

```python
async with httpx.AsyncClient() as client:       # ONE client for the process lifetime
    while True:
        try:
            r = await client.get(f"{CORE_URL}/data", timeout=3.0)
            if r.status_code == 200:
                await _broadcast({"type":"metrics", "data": r.json()})
        except Exception:
            pass                                # SILENT
        await asyncio.sleep(2.0)
```

The bare `except Exception: pass` is unusual in this codebase, and correct here. The hub being briefly unreachable is a normal condition during a demonstration — restarts, migrations, transient load. Logging it would flood the terminal at one line every 2 s while telling the operator nothing they cannot see on the dashboard, which simply stops updating.

The trade-off is real: **a permanently unreachable hub is completely silent.** The only symptom is a frozen dashboard. See L-3.

The client is created once, outside the loop, and lives for the process lifetime — unlike most other services here, which open one per call.

### 3.5 `POST /reset` — the only write path

```python
r = await client.post(f"{CORE_URL}/reset", timeout=5.0)
if r.status_code == 200: return r.json()
return JSONResponse(502, {...})                 # never an unhandled exception
except: return JSONResponse(502, {...})
```

A pure proxy: no reset logic lives here. The docstring insists on the invariant — **this route must never crash the service**, whatever the hub does. Both failure modes are caught and converted to an explicit `502`.

It is worth stating plainly what this button does: the hub rebuilds its bootstrap SLOs and returns to autonomous mode, **discarding the user's intent contract**. That is a destructive action, exposed unauthenticated on a read-only dashboard (SPEC C-8).

### 3.6 The embedded dashboard

`_DASHBOARD_HTML` is a 945-line Python string containing the entire page — markup, CSS, and JavaScript. `GET /` returns it verbatim.

The page contains:

| Element | Source |
|---|---|
| VM cards, metric bars, predictions | `metrics` events |
| Latency history chart (Chart.js) | accumulated `metrics` |
| SLO weight chart | `audit` events |
| Reasoning panel — active SLOs | `audit` events |
| Audit log — cycle, breach, TOPSIS score, migration trace | `snapshot` + `audit` |
| `A`/`B`/`C`/`D` counters | `provider_path_counts` |

`translateReason(e)` (line ~732) is worth noting: the backend emits reasons in English (`"Secondary-only violation"`, `"still best candidate"`, `"Cooldown active"`), and the front translates them for display. The comment states the rule — the **structured** header (decision, from_vm → to_vm) is built locally and *completed* by the backend's `reason`, **never replaced** by it. So a reason the front does not recognise degrades to raw text rather than disappearing.

Embedding the page has one genuine benefit — zero build step, zero asset server, one file to deploy — and two real costs: no linting or syntax highlighting for 945 lines of HTML/JS, and any front-end edit is a Python-string edit.

### 3.7 Startup

```python
@app.on_event("startup")
async def startup():
    asyncio.create_task(_poll_hub())
```

`@app.on_event` is the deprecated FastAPI API; `collector` and `ml_predictor` already use the `lifespan` context manager. Functionally equivalent today (L-5).

Note there is **no shutdown handler**: the poll task is never explicitly cancelled. Harmless — the process is exiting anyway — but inconsistent with `collector`, which cancels its loop cleanly.

## 4. API reference

### `GET /` — the dashboard

Returns the HTML page. Open `http://localhost:8009/`.

### `GET /stream` — SSE

`text/event-stream`. Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no` (disables nginx buffering, which would otherwise hold events until the buffer fills).

```bash
curl -N http://localhost:8009/stream
```

`-N` disables curl's own buffering — without it you see nothing.

### `POST /audit`

Free-form JSON from the hub. Returns `{"status": "ok"}`. Never rejects — an observer must not refuse an observation.

### `GET /audit/log`

```jsonc
{"count": 137, "log": [ … ],
 "provider_path_counts": {"A": 92, "B": 11, "C": 3, "D": 0}}
```

Consumed by `federation_view`. Also the simplest way to export a session for offline analysis:

```bash
curl -s http://localhost:8009/audit/log > session.json
```

### `POST /reset`

Proxies to the hub. `200` with the hub's response, or `502`.

### `GET /health`

```json
{"status": "healthy", "service": "observability"}
```

Liveness only — it does **not** report whether the hub is reachable, which is the one thing worth knowing here (L-4).

## 5. Configuration

| Variable | Default | Used for |
|---|---|---|
| `OBSERVABILITY_PORT` | `8009` (+`PORT_OFFSET`) | Listening port |
| `CORE_URL` | `http://localhost:8000` (+offset) | Hub polled and proxied |

Hardcoded, not configurable:

| Value | Location | Role |
|---|---|---|
| `maxlen=500` | `_audit_log` | History depth |
| `2.0` s | `_poll_hub` | Poll interval |
| `3.0` s | `_poll_hub` | Poll timeout |
| `30.0` s | `/stream` | SSE keep-alive |
| `5.0` s | `/reset` | Proxy timeout |

## 6. Dependencies

**Internal** — `shared.config`, `shared.logging_utils`.

**External** — `fastapi`, `uvicorn`, `httpx`. `visualizer.py` additionally needs `matplotlib`, which the web dashboard does not.

**Front-end** — Chart.js from a CDN. No internet, no charts.

**Runtime**

| Dependency | Nature | On failure |
|---|---|---|
| Hub `/data` | soft | Dashboard freezes, silently |
| Hub `/reset` | soft | `502` on the button |
| Nothing else | — | The service starts and runs alone |

## 7. Data model

Entirely volatile:

| Structure | Bound | Lost on restart |
|---|---|---|
| `_audit_log` | 500 entries | **Yes** |
| `_subscribers` | number of open browsers | Yes — they reconnect |
| `_provider_path_counts` | 4 integers | **Yes** |

The durable trail is `decisions:recent` in Redis. This service is a live view, not an archive.

## 8. Running it standalone

```bash
python -m services.observability.app
```

Then open `http://localhost:8009/`.

Second provider:

```bash
PORT_OFFSET=100 python -m services.observability.app
```

→ `http://localhost:8109/`.

No dependency to start first: the service runs with the hub down, showing an empty dashboard.

The matplotlib tool, if wanted, has to be driven manually — nothing imports it:

```python
from services.observability.visualizer import QoSDashboard
QoSDashboard(logger).run()
```

## 9. Logging and observability

| Marker | Level | Meaning |
|---|---|---|
| `Observability démarré — port 8009 \| dashboard : http://…` | INFO | Startup, with the clickable URL |
| `Audit reçu — cycle=42 decision=migrate breach=proactive` | INFO | One line per decision |
| `… \| chemin=A provider=provider-1` | INFO | Suffix present only in multi-provider mode |
| `⚠️ /reset — le hub a répondu HTTP 500` | WARNING | Proxy failure |
| `❌ /reset — impossible de contacter le hub` | ERROR | Hub unreachable |

The quietest terminal of the stack: one line per cycle, none for the 2 s polling. Deliberate — the information lives in the browser, not the terminal.

The absence of a poll-failure log (§3.4) means **a frozen dashboard produces no log line at all**. When the page stops updating, check the hub, not this terminal.

## 10. Testing

| File | Covers |
|---|---|
| `tests/unit/test_observability_dashboard.py` | The dashboard |
| `tests/unit/test_observability_multi_provider.py` | The path counters |
| `tests/unit/test_observability_reasoning_panel.py` | The reasoning panel |

Three dedicated files — good coverage for a display service.

```bash
pytest tests/unit/test_observability_dashboard.py tests/unit/test_observability_multi_provider.py -v
```

## 11. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | `visualizer.py` (412 lines, matplotlib) is imported by nobody (§2). | Dead or orphaned code; a reader may believe it is part of the service. | Confirm it is obsolete and remove it, or document it as a standalone tool. |
| **L-2** | The SSE timeout exits the generator instead of continuing the loop (§3.2). | The keep-alive is really a disconnect-then-reconnect; invisible only because `EventSource` retries. | Move the `try/except` inside the `while`. |
| **L-3** | Poll failures are silent (§3.4). | A permanently unreachable hub produces no log at all — only a frozen page. | Log once on transition to failure, and once on recovery. |
| **L-4** | `/health` does not report hub reachability (§4). | A "healthy" dashboard may be showing data minutes old. | Add `"hub": "reachable"\|"unreachable"` and the age of the last successful poll. |
| **L-5** | `@app.on_event("startup")` is deprecated, and there is no shutdown handler (§3.7). | Inconsistent with `collector`/`ml_predictor`; the poll task is never cancelled. | Migrate to `lifespan`. |
| **L-6** | 945 lines of HTML/JS embedded in a Python string (SPEC C-3). | No linting, no highlighting, painful to edit. | Move to a `static/` file served by FastAPI. |
| **L-7** | History is volatile and capped at 500 (SPEC C-1, C-2). | A restart loses the session; ~50 minutes of history maximum. | Hydrate from `decisions:recent` on startup. |
| **L-8** | Path counters reset on restart (SPEC C-7). | "Cumulative" means since startup, not since the experiment began. | Persist them, or timestamp the counting window. |
| **L-9** | Chart.js from a CDN (SPEC C-4). | No charts offline. | Vendor the library locally. |
| **L-10** | **`/reset` is unauthenticated and destructive** (SPEC C-8). | Anyone on the network can wipe the hub's intent contract from a browser. | Confirmation dialog at minimum; authentication properly. |
