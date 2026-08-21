# Personal Context Agent

An autonomous agent that watches your context and acts on it without being asked.

People with ADHD don't forget that they own things. They forget what *happened*
to them. This agent holds that thread: it notices what you have, remembers where
it was, and tells you before it becomes a problem.

```
You:   I'm going to work
Agent: You're missing your AirPods. You usually take them to work.
       Last seen on the kitchen counter at 8:42 AM.
```

---

## The three workflows

### 1. Leave detection — *catch it before you're out the door*

You say you're heading somewhere. The agent takes a camera frame, asks Gemini
what is actually in it, and compares that against the routine it has learned for
that destination. Anything missing comes back with a last-known location.

`agent/workflows.py::leave_detection`

### 2. Item recall — *where was it last seen?*

Searches the observation log for a thing and returns every sighting, newest
first, each with a confidence that **decays with age**. A sighting from ten
minutes ago is an answer; one from yesterday is a lead, and the agent says so
rather than sending you to the wrong room.

`agent/workflows.py::item_recall`

### 3. Daily timeline — *what did I actually do today?*

Reconstructs the day from the observation log and has Gemini narrate it. The
model is given the events and forbidden from inventing any; when the log is
thin, the agent says the log is thin.

`agent/workflows.py::daily_timeline`

---

## What makes it an agent, not a chat box

- **It acts unprompted.** Cloud Scheduler hits `/api/tasks/morning-brief` before
  your usual departure time and `/api/tasks/evening-recap` at night. Nobody asks
  it to; it runs and reports.
- **It learns.** Carry something on most of your recent trips and it gets
  promoted into that destination's routine automatically
  (`_refine_routine`). The second week is better than the first.
- **It decides what to do.** Google ADK gives Gemini eight tools and the agent
  picks. There is no `if intent == "recall"` switch on the conversational path.
- **Its memory is a log, not a model's recollection.** Every decision — what's
  missing, when something was last seen — is computed from an append-only
  Postgres table. Gemini phrases the answer; it does not source the facts.

---

## Architecture

```
   Browser (Camera API + Web Speech API)
                 │  text + camera frame
                 ▼
   ┌───────────────────────────────┐
   │  FastAPI  (Cloud Run)         │
   │  ├─ /api/chat        human    │
   │  └─ /api/tasks/*     cron     │
   └───────────────┬───────────────┘
                   │
   ┌───────────────▼───────────────┐
   │  ADK Agent Runtime            │
   │  rules-first intent pass      │
   │  → Gemini picks a tool        │
   └───────────────┬───────────────┘
                   │
      ┌────────────┼────────────┐
      ▼            ▼            ▼
  context      memory       timeline      ← tools/ (what Gemini can call)
   tools        tools         tools
      └────────────┼────────────┘
                   ▼
   ┌───────────────────────────────┐
   │  Workflows (deterministic)    │  ← agent/workflows.py
   └───────────────┬───────────────┘
      ┌────────────┴────────────┐
      ▼                         ▼
  PostgreSQL                 Redis
  observations (append-only) sessions, camera frames,
  routines, leave_scans      last-seen index
```

The split that matters: **tools are the surface Gemini can reach; workflows are
where decisions get made.** A tool never decides what is missing — it calls a
workflow that computes it from the log. That is why the agent can't hallucinate
a sighting.

Full diagram: [`docs/architecture.md`](docs/architecture.md).

---

## Run it locally

**Requirements:** Python 3.14, Docker (for Postgres + Redis), and a Gemini API
key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

```bash
# 1. Dependencies
uv sync                      # or: pip install -r requirements.txt

# 2. Configuration
cp .env.example .env

# 3. Fill in two values in .env:
#      GEMINI_API_KEY     — from https://aistudio.google.com/apikey
#      POSTGRES_PASSWORD  — any local value; generate one with:
openssl rand -hex 24

# 4. Postgres + Redis (schema is applied automatically on first boot)
docker compose up -d

# 5. Seed the learned routines
uv run python scripts/seed_demo.py --routines

# 6. Run
PYTHONPATH=src uv run uvicorn main:app --reload --port 8080
```

`POSTGRES_PASSWORD` has **no default anywhere** — `docker compose up` refuses to
start without it rather than falling back to a value that would end up committed.
It is defined once in the gitignored `.env`, and `DATABASE_URL` is interpolated
from it, so the credential lives in exactly one place.

Open <http://localhost:8080>. Camera and microphone need a secure context —
`localhost` counts, a bare LAN IP does not. To demo on a phone, deploy to Cloud
Run (below) and open the HTTPS URL.

### Check it's healthy

```bash
curl -s localhost:8080/healthz
# {"status":"ok","postgres":"up","redis":"up","gemini":"configured", ...}
```

`status: degraded` means Postgres or Redis is unreachable. The service still
runs — it falls back to in-memory storage — but nothing survives a restart.

---

## Try the workflows without the UI

```bash
U=00000000-0000-0000-0000-000000000001

# Workflow 1 — needs a camera frame, so use the browser. Or scan with no frame:
curl -s -X POST localhost:8080/api/leave-scan \
  -H 'Content-Type: application/json' \
  -d '{"destination":"work","user_id":"'$U'"}' | jq .speech

# Workflow 2 — costs zero model calls
curl -s "localhost:8080/api/recall?item=AirPods&user_id=$U" | jq .speech

# Workflow 3
curl -s "localhost:8080/api/timeline?user_id=$U" | jq .speech
curl -s "localhost:8080/api/timeline?user_id=$U&question=what+was+I+doing+at+2pm" | jq .speech

# The autonomous jobs, triggered by hand
curl -s -X POST localhost:8080/api/tasks/morning-brief | jq .message
```

---

## Heads-up: free-tier quota

Measured on a free-tier key while building this, `gemini-3.5-flash` allows:

- **5 requests per minute**, and
- **20 requests per day.**

One conversational turn costs 2–3 requests (intent escalation, the agent's turn,
the follow-up after a tool returns), so the daily budget is roughly **seven
turns**. That is not enough to record a demo.

Options, in order of preference:

1. **Enable billing** on the project. The limits above are free-tier only.
2. **Use a model with a larger free allowance.** `gemini-2.5-flash` is fully
   multimodal and was still serving after `gemini-3.5-flash` was exhausted:
   ```bash
   echo 'GEMINI_MODEL=gemini-2.5-flash' >> .env
   uv run python scripts/list_models.py   # see everything your key can reach
   ```
3. **Rehearse on the zero-cost paths.** `/api/recall` is pure SQL,
   `/api/tasks/morning-brief` makes no model calls at all, and the rules-based
   intent classifier handles every phrasing in the demo script without a model.

The service handles exhaustion properly rather than crashing: a quota refusal
returns HTTP 429 with `Retry-After` and a spoken "try again in about 30
seconds"; a failed timeline narration falls back to a deterministic list of the
day's events; a failed vision scan says it could not check instead of guessing.

Two of the three workflows have zero-model-call paths (`/api/recall` is pure SQL;
the rules-based intent classifier answers common phrasings without a model), so
you can rehearse most of a demo without spending quota.

---

## Layout

```
src/
├── agent/
│   ├── engine.py      ADK runtime, one turn per call, quota handling
│   ├── workflows.py   the three workflows — all decisions live here
│   ├── intents.py     rules-first classifier, Gemini only when unsure
│   └── prompts.py     system instruction
├── tools/
│   ├── registry.py    the eight tools handed to Gemini
│   ├── context_tools.py   observe / record / describe the present
│   ├── memory_tools.py    retrieve sightings
│   ├── timeline_tools.py  reconstruct a day
│   └── vision.py      Gemini vision: frame → structured item list
├── utils/
│   ├── db.py          Postgres event log (+ in-memory fallback)
│   ├── cache.py       Redis session state (+ in-process fallback)
│   ├── config.py      env-driven config
│   ├── errors.py      quota errors → HTTP 429
│   ├── gemini.py      shared genai client
│   └── logger.py      structured JSON logs for Cloud Logging
├── frontend/          vanilla HTML/JS: camera, speech, live state view
└── main.py            FastAPI app — the Cloud Run entry point
```

**Two deliberate deviations from the original spec:**

1. `location` is stored as `(location_label, lat, lon)` with a generated
   `POINT` column, rather than a bare `POINT`. Every workflow keys off the
   human-readable label — that is what the agent says out loud — and core
   PostgreSQL's `point` has no distance index without PostGIS. The `POINT`
   column is kept for future geo queries.
2. `vision.py` lives under `tools/` but is not in the agent's tool registry.
   It is infrastructure a workflow uses, not something Gemini calls directly —
   the agent asks "check before leaving", and the workflow decides whether a
   camera scan is part of that.

---

## Tests

```bash
uv run pytest -q          # 60 tests, ~0.4s
```

The suite is hermetic: no Postgres, no Redis, no API key, no network. Credentials
are blanked in `conftest.py` before config loads, so every Gemini call takes the
deterministic stub path.

`tests/test_regressions.py` covers three bugs found while building this, each
with the failure it actually produced:

- **Recall matched a leave-scan's `missing` list.** The query searched the whole
  JSONB blob, so asking "where are my keys?" returned the scan row that had just
  established the keys were *nowhere* — reporting a sighting at the exact moment
  of their absence.
- **The timeline collapsed distinct items.** Dedupe dropped any item logged
  within 120s of another item, silently deleting every item but the first in a
  scan. Now it only collapses repeats of the *same* subject.
- **A circular import between `agent.workflows` and `tools.context_tools`** that
  only stayed hidden because `main.py` happened to import in the lucky order.

---

## Deploy

See [DEPLOYMENT.md](DEPLOYMENT.md).

```bash
export GEMINI_API_KEY=...
./deployment/deploy.sh
```

Provisions Cloud SQL, Memorystore, Secret Manager, Cloud Run and the two Cloud
Scheduler jobs. Idempotent — safe to re-run after a partial failure.

---

## License

MIT
