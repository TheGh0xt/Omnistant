# Deployment

Target: **Google Cloud Run**, with Cloud SQL for PostgreSQL, Memorystore for
Redis, Secret Manager for credentials, and Cloud Scheduler for the autonomous
jobs.

---

## Quick path

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

export GEMINI_API_KEY=your-key-here
./deployment/deploy.sh
```

The script is idempotent — every step checks whether the resource exists before
creating it, so a partial failure is resumed by running it again.

**Expect ~15 minutes on a cold project**, almost all of it waiting for Cloud SQL
(~10 min) and Memorystore (~5 min) to provision.

### Redis: skipped by default

**Memorystore is Google's managed Redis**, and at ~$35/month plus ~$9 for the
Serverless VPC connector it needs, it was by far the most expensive thing here —
for almost no benefit at demo scale. It is now **opt-in**.

Two reasons it earns so little:

- The browser sends the camera frame **with** the chat turn, so frames never
  need to round-trip through a shared cache. (`/api/frame` exists, but the UI
  does not use it.)
- ADK's session service is in-process regardless, so conversation history does
  not survive an instance change whether Redis exists or not. That is also why
  `--max-instances` now defaults to **1**: a second turn routed to a different
  instance would lose the conversation.

Three options:

```bash
# 1. Default — no managed Redis, in-process cache.                  $0
./deployment/deploy.sh

# 2. External Redis over a public TLS endpoint. No VPC connector.   $0 free tier
REDIS_URL='rediss://default:TOKEN@your-db.upstash.io:6379' ./deployment/deploy.sh

# 3. Memorystore + VPC connector.                                   ~$44/month
USE_MEMORYSTORE=1 ./deployment/deploy.sh
```

Option 2 is the one to pick if you want genuinely shared state without the
Memorystore bill. Any provider with a public `rediss://` endpoint works —
Upstash, Redis Cloud, Aiven. `redis-py` handles TLS natively, and if the host is
unreachable the service logs a warning and falls back to the in-process cache
rather than failing to boot.

**Supabase is not an option for this**, despite being the obvious thought:
Supabase is Postgres plus auth, storage and edge functions — it has no Redis
product. Where Supabase *does* fit is replacing **Cloud SQL**; see below.

### Going (nearly) free

Cloud SQL is the only remaining recurring cost. Point `DATABASE_URL` at a
Supabase free-tier Postgres and skip the Cloud SQL step entirely:

```bash
# Supabase → Project Settings → Database → Connection string (URI)
export DATABASE_URL='postgresql://postgres.PROJECT:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres'
export REDIS_URL='rediss://default:TOKEN@your-db.upstash.io:6379'   # optional
SKIP_CLOUD_SQL=1 ./deployment/deploy.sh
```

That leaves Cloud Run, which scales to zero — call it **$0/month idle**. The
schema is applied automatically at startup against whatever `DATABASE_URL`
points at, so no migration step changes.

Use the **session pooler** connection string rather than the direct one:
Cloud Run opens and drops connections as instances come and go, and the free
tier's direct connection limit is low.

---

## What gets created

| Resource | Name (default) | Why |
|---|---|---|
| Cloud Run service | `personal-context-agent` | the app |
| Cloud SQL (PG 16) | `omnistant-pg`, `db-f1-micro`, **ENTERPRISE edition** | the observation log |
| Secrets | `gemini-api-key`, `omnistant-task-token`, `omnistant-database-url`, `omnistant-db-password` | credentials |
| Scheduler job | `omnistant-morning-brief`, weekdays 08:00 | unprompted pre-departure check |
| Scheduler job | `omnistant-evening-recap`, daily 21:00 | unprompted day recap |
| Memorystore Redis | `omnistant-cache`, 1GB — **only with `USE_MEMORYSTORE=1`** | session state, camera frames |
| VPC connector | `omnistant-vpc` — only with `USE_MEMORYSTORE=1` | Cloud Run → Memorystore private IP |

The edition matters: new projects default to `ENTERPRISE_PLUS`, which rejects
shared-core tiers. Creating a `db-f1-micro` without `--edition=ENTERPRISE` fails
with *"Invalid Tier (db-f1-micro) for (ENTERPRISE_PLUS) Edition"*. The script
passes it explicitly.

Override any of these with environment variables:

```bash
REGION=europe-west2 SERVICE=my-agent TIMEZONE=Europe/London ./deployment/deploy.sh
```

---

## The database schema

**There is no migration step.** `PostgresStore.connect()` runs every
`migrations/*.sql` in name order at startup, and all the statements are
`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`, so it is safe on
every boot.

To add a migration, drop a new numbered file into `migrations/` and keep it
idempotent.

---

## Why `--max-instances=1`

ADK's session service is in-process. A second conversational turn routed to a
different Cloud Run instance would not find the session and would lose the
thread. One instance keeps multi-turn coherent; `--concurrency=40` means that
single instance still serves plenty of simultaneous requests.

Raise `MAX_INSTANCES` only once sessions are backed by something shared — that
means swapping `InMemorySessionService` for ADK's `DatabaseSessionService`
(pointing at the same Postgres), not just adding Redis, since Redis only holds
*our* session document and not ADK's.

```bash
MAX_INSTANCES=4 ./deployment/deploy.sh
```

---

## Credentials

Connection strings carry secrets — the database password, and the token embedded
in a hosted Redis URL. None of them travel as plain environment variables, where
they would show up in `gcloud run services describe`, the Cloud Console, and
every revision's history. They are written to Secret Manager and mounted:

```
--set-secrets  GEMINI_API_KEY, TASK_TOKEN, DATABASE_URL[, REDIS_URL]
--set-env-vars GEMINI_MODEL, TIMEZONE, LOG_LEVEL   # nothing sensitive
```

To rotate any of them, add a new secret version and redeploy:

```bash
printf '%s' 'new-value' | gcloud secrets versions add omnistant-database-url --data-file=-
gcloud run services update personal-context-agent --region=us-central1
```

---

## Securing the scheduler endpoints

`/api/tasks/*` are the endpoints Cloud Scheduler calls. They are protected by a
shared secret in the `X-Task-Token` header, generated by the deploy script and
stored in Secret Manager.

If `TASK_TOKEN` is unset the endpoints are **open to anyone** and the service
logs a warning on every call. That is fine locally and not fine in production —
the deploy script always sets one.

The main service is deployed `--allow-unauthenticated` because the frontend is
served from the same origin and there is no user login. **This means anyone with
the URL can talk to your agent and read your observation log.** For anything
beyond a demo, put Identity-Aware Proxy in front of it or add real auth and drop
`DEFAULT_USER_ID`.

---

## Verifying a deployment

```bash
URL=$(gcloud run services describe personal-context-agent \
        --region=us-central1 --format='value(status.url)')

curl -s "$URL/healthz" | jq
# {"status":"ok","postgres":"up","redis":"up","gemini":"configured", ...}

# Seed the routines against the deployed database
curl -s -X PUT "$URL/api/routines/work" \
  -H 'Content-Type: application/json' \
  -d '{"expected_items":["phone","wallet","keys","laptop","airpods","badge"]}' | jq

# Trigger an autonomous job by hand
TOKEN=$(gcloud secrets versions access latest --secret=omnistant-task-token)
curl -s -X POST "$URL/api/tasks/morning-brief" -H "X-Task-Token: $TOKEN" | jq .message
```

Then open `$URL` on a phone. Cloud Run serves HTTPS, which is what the Camera and
Web Speech APIs require.

---

## Logs

Logs are structured JSON on stdout, so Cloud Logging promotes `severity` and
`message` automatically.

```bash
gcloud run services logs read personal-context-agent --region=us-central1 --limit=50

# Just the leave scans
gcloud logging read \
  'resource.labels.service_name="personal-context-agent" AND jsonPayload.message="leave scan"' \
  --limit=20 --format='value(jsonPayload.routine, jsonPayload.missing)'
```

Useful log messages: `leave scan`, `item recall`, `timeline built`,
`routine refined`, `turn complete`, `gemini quota exhausted`.

---

## Cost

At demo scale, on `min-instances=0`, the dominant costs are the two always-on
managed services — Cloud Run itself rounds to nothing when idle.

**Default setup:**

| | approx / month |
|---|---|
| Cloud Run (scales to zero) | ~$0 |
| Cloud SQL `db-f1-micro`, ENTERPRISE | ~$8 |
| Redis | $0 — not provisioned |
| Cloud Scheduler (2 jobs) | free tier |
| **Total** | **~$8** |

**If you opt into Memorystore** (`USE_MEMORYSTORE=1`), add ~$35 for the 1GB
Basic tier and ~$9 for the VPC connector it requires — **~$52/month**. Worth it
only once you are running multiple instances with shared session state.

**Free-tier route** (Supabase Postgres + optional Upstash Redis, Cloud Run only):
**~$0/month**. See *Going (nearly) free* above.

**Tear it all down:**

```bash
gcloud run services delete personal-context-agent --region=us-central1
gcloud sql instances delete omnistant-pg

# Only if you deployed with USE_MEMORYSTORE=1:
gcloud redis instances delete omnistant-cache --region=us-central1
gcloud compute networks vpc-access connectors delete omnistant-vpc --region=us-central1
gcloud scheduler jobs delete omnistant-morning-brief --location=us-central1
gcloud scheduler jobs delete omnistant-evening-recap --location=us-central1
```

---

## Troubleshooting

**`status: degraded` on `/healthz`**
Postgres or Redis is unreachable. The service keeps running on in-memory
fallbacks — so it looks fine in the UI while silently losing every observation on
restart. Check `DATABASE_URL` uses the Cloud SQL unix socket form:
`postgresql://user:pass@/omnistant?host=/cloudsql/PROJECT:REGION:INSTANCE`, and
that `--add-cloudsql-instances` was passed.

**Camera button does nothing**
`getUserMedia` needs a secure context. Cloud Run gives you HTTPS; opening the
page over a raw IP or plain HTTP will not work. The frontend reports this
explicitly rather than failing silently.

**HTTP 429 from `/api/chat`**
Gemini free-tier rate limit — 5 req/min on `gemini-3.5-flash`, and a turn costs
2–3. The response carries `Retry-After`. Enable billing, or set `GEMINI_MODEL` to
something with a larger free allowance.

**Redis unreachable from Cloud Run**
Memorystore only has a private IP. The service needs the VPC connector *and*
`--vpc-egress=private-ranges-only`; both are set by the deploy script. Confirm
the connector is `READY`:
`gcloud compute networks vpc-access connectors describe omnistant-vpc --region=us-central1`

**Cold start feels slow**
The first request after idle pays container start plus the migration pass. Set
`--min-instances=1` for a demo (~$13/month) so the recording doesn't open on a
spinner.
