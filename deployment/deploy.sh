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

# --- Configuration ---------------------------------------------------------
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-personal-context-agent}"
SQL_INSTANCE="${SQL_INSTANCE:-omnistant-pg}"
DB_NAME="${DB_NAME:-omnistant}"
DB_USER="${DB_USER:-omnistant}"
REDIS_INSTANCE="${REDIS_INSTANCE:-omnistant-cache}"
VPC_CONNECTOR="${VPC_CONNECTOR:-omnistant-vpc}"
TIMEZONE="${TIMEZONE:-Europe/London}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.5-flash}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: no project set. Run: gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo "ERROR: export GEMINI_API_KEY before running this script." >&2
  exit 1
fi

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

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
  artifactregistry.googleapis.com

# --- 2. Secrets ------------------------------------------------------------
create_secret() {
  local name="$1" value="$2"
  if gcloud secrets describe "${name}" >/dev/null 2>&1; then
    echo "  secret ${name} exists; adding a new version"
  else
    gcloud secrets create "${name}" --replication-policy=automatic >/dev/null
  fi
  printf '%s' "${value}" | gcloud secrets versions add "${name}" --data-file=- >/dev/null
}

say "Storing secrets in Secret Manager"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)}"
TASK_TOKEN="${TASK_TOKEN:-$(openssl rand -hex 32)}"
create_secret gemini-api-key "${GEMINI_API_KEY}"
create_secret omnistant-db-password "${DB_PASSWORD}"
create_secret omnistant-task-token "${TASK_TOKEN}"

# --- 3. Cloud SQL (PostgreSQL) --------------------------------------------
if gcloud sql instances describe "${SQL_INSTANCE}" >/dev/null 2>&1; then
  say "Cloud SQL instance ${SQL_INSTANCE} already exists"
else
  say "Creating Cloud SQL instance ${SQL_INSTANCE} (this takes ~10 minutes)"
  gcloud sql instances create "${SQL_INSTANCE}" \
    --database-version=POSTGRES_16 \
    --tier=db-f1-micro \
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

# --- 4. Memorystore (Redis) + VPC connector -------------------------------
# Cloud Run reaches Memorystore over a private IP, which needs a Serverless VPC
# connector. This is the slowest and most failure-prone part of the setup, so
# REDIS_OPTIONAL=1 skips it — the service falls back to an in-process cache.
if [[ "${REDIS_OPTIONAL:-0}" == "1" ]]; then
  say "Skipping Memorystore (REDIS_OPTIONAL=1); using in-process cache"
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
say "Building and deploying ${SERVICE}"
DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${SQL_CONNECTION}"

gcloud run deploy "${SERVICE}" \
  --source . \
  --region="${REGION}" \
  --platform=managed \
  --allow-unauthenticated \
  --add-cloudsql-instances="${SQL_CONNECTION}" \
  "${VPC_FLAGS[@]}" \
  --set-env-vars="DATABASE_URL=${DATABASE_URL},REDIS_URL=${REDIS_URL},GEMINI_MODEL=${GEMINI_MODEL},TIMEZONE=${TIMEZONE},LOG_LEVEL=INFO" \
  --set-secrets="GEMINI_API_KEY=gemini-api-key:latest,TASK_TOKEN=omnistant-task-token:latest" \
  --memory=1Gi \
  --cpu=1 \
  --timeout=120 \
  --concurrency=40 \
  --min-instances=0 \
  --max-instances=4

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
    --headers="X-Task-Token=${TASK_TOKEN}"
    --attempt-deadline=120s
  )
  if gcloud scheduler jobs describe "${name}" --location="${REGION}" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "${name}" "${args[@]}"
  else
    gcloud scheduler jobs create http "${name}" "${args[@]}"
  fi
}

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
