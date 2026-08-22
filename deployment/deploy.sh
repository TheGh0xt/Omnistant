#!/usr/bin/env bash
#
# Deploy the Personal Context Agent to Cloud Run.
#
# Idempotent: safe to re-run. Each step checks whether the resource already
# exists before creating it, so a partial failure can be resumed by running the
# script again.
#
#   ./deployment/deploy.sh
#
# Prerequisites: gcloud CLI, authenticated (`gcloud auth login`), with billing
# enabled on the project.

set -euo pipefail

# Every gcloud call must be non-interactive: this script runs from CI, from a
# background shell, and over nohup, where a prompt is an abort. Equivalent to
# passing --quiet everywhere (accept the default for all prompts).
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

# --- Configuration ---------------------------------------------------------
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-omnistant}"
SQL_INSTANCE="${SQL_INSTANCE:-omnistant-pg}"
# Shared-core, ENTERPRISE edition. db-g1-small if f1-micro feels tight.
SQL_TIER="${SQL_TIER:-db-f1-micro}"
DB_NAME="${DB_NAME:-omnistant}"
DB_USER="${DB_USER:-omnistant}"
REDIS_INSTANCE="${REDIS_INSTANCE:-omnistant-cache}"
VPC_CONNECTOR="${VPC_CONNECTOR:-omnistant-vpc}"
TIMEZONE="${TIMEZONE:-Europe/London}"
# Safe above 1 because sessions are Postgres-backed (see build_session_service):
# a turn routed to any instance finds the same conversation. With the in-process
# session service this had to be 1, or turn two would forget turn one.
MAX_INSTANCES="${MAX_INSTANCES:-4}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.5-flash}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: no project set. Run: gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo "ERROR: export GEMINI_API_KEY before running this script." >&2
  exit 1
fi

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

# Sourcing .env to pick up GEMINI_API_KEY also exports the LOCAL DATABASE_URL and
# REDIS_URL, which point at localhost. Cloud Run cannot reach those, and a
# localhost REDIS_URL would silently take the "external Redis" branch and deploy
# a service that can never connect. Refuse instead.
for _local in DATABASE_URL REDIS_URL; do
  eval "_value=\${${_local}:-}"
  case "${_value}" in
    *localhost*|*127.0.0.1*|*host.docker.internal*)
      die "${_local} points at ${_value} — that is your local stack, not something Cloud Run can reach.
    Unset it before deploying:  unset ${_local}
    Or pass a real one:         ${_local}='...' ./deployment/deploy.sh" ;;
  esac
done

say "Project ${PROJECT_ID} / region ${REGION}"
gcloud config set project "${PROJECT_ID}" >/dev/null

# --- 1. APIs ---------------------------------------------------------------
say "Enabling APIs (no-op if already enabled)"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  sqladmin.googleapis.com \
  redis.googleapis.com \
  vpcaccess.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com \
  sql-component.googleapis.com

# --- 2. Secrets ------------------------------------------------------------
# Cloud Run's *runtime* service account reads the mounted secrets — not the
# account running this script. Without an explicit grant the revision fails with
# "Permission denied on secret ... for Revision service account".
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
RUNTIME_SA="${RUNTIME_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"

create_secret() {
  local name="$1" value="$2"
  if gcloud secrets describe "${name}" >/dev/null 2>&1; then
    echo "  secret ${name} exists; adding a new version"
  else
    gcloud secrets create "${name}" --replication-policy=automatic >/dev/null
  fi
  printf '%s' "${value}" | gcloud secrets versions add "${name}" --data-file=- >/dev/null

  # Bound per-secret rather than project-wide: the service only ever needs to
  # read the handful of secrets it is actually given. Idempotent.
  gcloud secrets add-iam-policy-binding "${name}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role=roles/secretmanager.secretAccessor >/dev/null
}

say "Storing secrets in Secret Manager"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)}"
TASK_TOKEN="${TASK_TOKEN:-$(openssl rand -hex 32)}"
create_secret gemini-api-key "${GEMINI_API_KEY}"
create_secret omnistant-db-password "${DB_PASSWORD}"
create_secret omnistant-task-token "${TASK_TOKEN}"

# --- 3. Cloud SQL (PostgreSQL) --------------------------------------------
# SKIP_CLOUD_SQL=1 with an external DATABASE_URL (Supabase, Neon, self-hosted)
# avoids the only recurring cost in the default setup. The schema is applied by
# the app at startup against whatever DATABASE_URL points at, so nothing else
# changes.
if [[ "${SKIP_CLOUD_SQL:-0}" == "1" ]]; then
  [[ -n "${DATABASE_URL:-}" ]] || die "SKIP_CLOUD_SQL=1 requires DATABASE_URL to be set"
  say "Skipping Cloud SQL — using the external DATABASE_URL you provided"
  SQL_FLAGS=()
else
  if gcloud sql instances describe "${SQL_INSTANCE}" >/dev/null 2>&1; then
    say "Cloud SQL instance ${SQL_INSTANCE} already exists"
  else
    say "Creating Cloud SQL instance ${SQL_INSTANCE} (this takes ~10 minutes)"
    # --edition is required: new projects default to ENTERPRISE_PLUS, which
    # rejects shared-core tiers like db-f1-micro with
    #   "Invalid Tier (db-f1-micro) for (ENTERPRISE_PLUS) Edition".
    # ENTERPRISE is the cheaper edition and the only one offering db-f1-micro.
    gcloud sql instances create "${SQL_INSTANCE}" \
      --database-version=POSTGRES_16 \
      --edition=ENTERPRISE \
      --tier="${SQL_TIER}" \
      --region="${REGION}" \
      --storage-size=10GB \
      --storage-auto-increase
  fi

  gcloud sql databases describe "${DB_NAME}" --instance="${SQL_INSTANCE}" >/dev/null 2>&1 \
    || gcloud sql databases create "${DB_NAME}" --instance="${SQL_INSTANCE}"

  if gcloud sql users list --instance="${SQL_INSTANCE}" --format='value(name)' | grep -qx "${DB_USER}"; then
    gcloud sql users set-password "${DB_USER}" --instance="${SQL_INSTANCE}" --password="${DB_PASSWORD}"
  else
    gcloud sql users create "${DB_USER}" --instance="${SQL_INSTANCE}" --password="${DB_PASSWORD}"
  fi

  SQL_CONNECTION="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
  SQL_FLAGS=(--add-cloudsql-instances="${SQL_CONNECTION}")
  DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${SQL_CONNECTION}"
fi

# --- 4. Redis --------------------------------------------------------------
# Three options, cheapest first. The default is NO managed Redis, because at
# demo scale it buys almost nothing here:
#
#   * The browser sends the camera frame *with* the chat turn, so frames never
#     need to round-trip through a shared cache.
#   * ADK's session service is in-process regardless, so conversation history
#     does not survive an instance change whether Redis exists or not.
#
#   (default)              in-process cache.            $0
#   REDIS_URL=rediss://…   external, e.g. Upstash.      $0 on a free tier
#                          Public TLS endpoint, so no VPC connector is needed.
#   USE_MEMORYSTORE=1      Memorystore + VPC connector. ~$44/month
#
if [[ -n "${REDIS_URL:-}" ]]; then
  say "Using the external REDIS_URL you provided (no VPC connector needed)"
  VPC_FLAGS=()
elif [[ "${USE_MEMORYSTORE:-0}" != "1" ]]; then
  say "No managed Redis (set USE_MEMORYSTORE=1 or REDIS_URL to change this)"
  echo "    Session state and camera frames will use an in-process cache."
  REDIS_URL=""
  VPC_FLAGS=()
else
  if gcloud redis instances describe "${REDIS_INSTANCE}" --region="${REGION}" >/dev/null 2>&1; then
    say "Memorystore instance ${REDIS_INSTANCE} already exists"
  else
    say "Creating Memorystore instance ${REDIS_INSTANCE} (~5 minutes)"
    gcloud redis instances create "${REDIS_INSTANCE}" \
      --size=1 --region="${REGION}" --redis-version=redis_7_0
  fi
  REDIS_HOST=$(gcloud redis instances describe "${REDIS_INSTANCE}" --region="${REGION}" --format='value(host)')
  REDIS_URL="redis://${REDIS_HOST}:6379/0"

  if ! gcloud compute networks vpc-access connectors describe "${VPC_CONNECTOR}" --region="${REGION}" >/dev/null 2>&1; then
    say "Creating VPC connector ${VPC_CONNECTOR}"
    gcloud compute networks vpc-access connectors create "${VPC_CONNECTOR}" \
      --region="${REGION}" --network=default --range=10.8.0.0/28
  fi
  VPC_FLAGS=(--vpc-connector="${VPC_CONNECTOR}" --vpc-egress=private-ranges-only)
fi

# --- 5. Build & deploy -----------------------------------------------------
# Connection strings carry credentials — the DB password, and the token embedded
# in a hosted Redis URL. Passing them via --set-env-vars would expose them in
# `gcloud run services describe`, the Cloud Console, and every revision's
# history. They go through Secret Manager instead; only non-sensitive settings
# travel as plain env vars.
create_secret omnistant-database-url "${DATABASE_URL}"
SECRETS="GEMINI_API_KEY=gemini-api-key:latest"
SECRETS+=",TASK_TOKEN=omnistant-task-token:latest"
SECRETS+=",DATABASE_URL=omnistant-database-url:latest"

ENV_VARS="GEMINI_MODEL=${GEMINI_MODEL},TIMEZONE=${TIMEZONE},LOG_LEVEL=INFO"

# Where the scheduled jobs deliver their results. A webhook URL is itself the
# credential — anyone holding it can post to the channel — so it is a secret.
if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
  create_secret omnistant-slack-webhook "${SLACK_WEBHOOK_URL}"
  SECRETS+=",SLACK_WEBHOOK_URL=omnistant-slack-webhook:latest"
  say "Slack notifications enabled"
else
  warn "SLACK_WEBHOOK_URL not set — the scheduled jobs will run but tell nobody"
fi
if [[ -n "${REDIS_URL}" ]]; then
  create_secret omnistant-redis-url "${REDIS_URL}"
  SECRETS+=",REDIS_URL=omnistant-redis-url:latest"
else
  # Empty means "no Redis" — an empty value cannot be a secret payload.
  ENV_VARS+=",REDIS_URL="
fi

# `run deploy --source` offers to create this repo interactively; with prompts
# disabled that offer would be declined and the deploy would fail.
if ! gcloud artifacts repositories describe cloud-run-source-deploy \
      --location="${REGION}" >/dev/null 2>&1; then
  say "Creating the Artifact Registry repo for source builds"
  gcloud artifacts repositories create cloud-run-source-deploy \
    --repository-format=docker --location="${REGION}" \
    --description="Cloud Run source deployments"
fi

say "Building and deploying ${SERVICE}"

# Empty-array expansion below uses the ${arr[@]+...} form deliberately: macOS
# still ships bash 3.2, where "${arr[@]}" on an empty array trips `set -u`.
gcloud run deploy "${SERVICE}" \
  --source . \
  --region="${REGION}" \
  --platform=managed \
  --allow-unauthenticated \
  ${SQL_FLAGS[@]+"${SQL_FLAGS[@]}"} \
  ${VPC_FLAGS[@]+"${VPC_FLAGS[@]}"} \
  --set-env-vars="${ENV_VARS}" \
  --set-secrets="${SECRETS}" \
  --memory=1Gi \
  --cpu=1 \
  --timeout=120 \
  --concurrency=40 \
  --min-instances=0 \
  --max-instances="${MAX_INSTANCES}"

SERVICE_URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)')

# --- 6. Cloud Scheduler ----------------------------------------------------
# This is what makes it an agent rather than a chatbot: it acts with nobody
# there to ask it to.
say "Scheduling the autonomous jobs"
schedule_job() {
  local name="$1" cron="$2" path="$3"
  local args=(
    --location="${REGION}"
    --schedule="${cron}"
    --time-zone="${TIMEZONE}"
    --uri="${SERVICE_URL}${path}"
    --http-method=POST
    --attempt-deadline=120s
  )
  # `create` takes --headers; `update` only accepts --update-headers. Using the
  # wrong one fails every re-run of this otherwise idempotent script.
  if gcloud scheduler jobs describe "${name}" --location="${REGION}" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "${name}" "${args[@]}" \
      --update-headers="X-Task-Token=${TASK_TOKEN}"
  else
    gcloud scheduler jobs create http "${name}" "${args[@]}" \
      --headers="X-Task-Token=${TASK_TOKEN}"
  fi
}

# Every five minutes: deliver any reminder that has come due. This is what makes
# "tell me a few minutes after I leave" possible on a service that scales to zero.
schedule_job omnistant-drain-nudges  "*/5 * * * *"   "/api/tasks/drain-nudges"
schedule_job omnistant-morning-brief "0 8 * * 1-5" "/api/tasks/morning-brief"
schedule_job omnistant-evening-recap "0 21 * * *"  "/api/tasks/evening-recap"

say "Done"
echo
echo "  Service:  ${SERVICE_URL}"
echo "  Health:   ${SERVICE_URL}/healthz"
echo
echo "  The database schema is applied automatically on first boot."
echo "  Open the service URL on your phone — camera and microphone need HTTPS,"
echo "  which Cloud Run provides."
