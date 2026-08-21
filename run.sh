#!/usr/bin/env bash
#
# Start everything and open the web UI.
#
#     ./run.sh
#
# Brings up Postgres and Redis, waits for them to be healthy, applies the schema,
# seeds the learned routines on first run, starts the server, and prints the URL.
# Ctrl-C stops the server; the containers keep running (`docker compose down` to
# stop them too).

set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8080}"
URL="http://localhost:${PORT}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

# --- 1. Configuration ------------------------------------------------------
if [[ ! -f .env ]]; then
  say "No .env found — creating one from .env.example"
  cp .env.example .env
  # A local-only password; there is no reason to make a human invent one.
  GENERATED=$(openssl rand -hex 24)
  if [[ "$(uname)" == "Darwin" ]]; then
    sed -i '' "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${GENERATED}|" .env
  else
    sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${GENERATED}|" .env
  fi
  say "Generated a local POSTGRES_PASSWORD"
  warn "Now add your Gemini API key to .env (GEMINI_API_KEY=...)"
  warn "Get one at https://aistudio.google.com/apikey — then re-run ./run.sh"
  exit 1
fi

# shellcheck disable=SC1091
set -a; source .env; set +a

[[ -n "${POSTGRES_PASSWORD:-}" ]] || die "POSTGRES_PASSWORD is empty in .env — generate one: openssl rand -hex 24"

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  warn "GEMINI_API_KEY is not set. The app will still start, but the camera scan"
  warn "and conversation will be disabled. Get a key: https://aistudio.google.com/apikey"
fi

# --- 2. Dependencies -------------------------------------------------------
if [[ ! -d .venv ]]; then
  say "Installing dependencies"
  command -v uv >/dev/null || die "uv is not installed — see https://docs.astral.sh/uv/"
  uv sync
fi

# --- 3. Postgres + Redis ---------------------------------------------------
command -v docker >/dev/null || die "Docker is not installed or not on PATH"
docker info >/dev/null 2>&1 || die "Docker is not running — start Docker Desktop and try again"

say "Starting Postgres and Redis"
docker compose up -d

printf '    waiting for them to be healthy'
for _ in $(seq 1 60); do
  status=$(docker compose ps --format '{{.Service}} {{.Health}}' 2>/dev/null || true)
  if grep -q "postgres healthy" <<<"$status" && grep -q "redis healthy" <<<"$status"; then
    printf ' ok\n'; break
  fi
  printf '.'; sleep 1
done
grep -q "postgres healthy" <<<"$(docker compose ps --format '{{.Service}} {{.Health}}')" \
  || die "Postgres did not become healthy. Check: docker compose logs postgres"

# --- 4. Seed routines (first run only) -------------------------------------
# The schema itself is applied by the app at startup.
if [[ ! -f .seeded ]]; then
  say "Seeding the learned routines"
  PYTHONPATH=src uv run python scripts/seed_demo.py --routines >/dev/null && touch .seeded
fi

# --- 5. Serve --------------------------------------------------------------
# Running ./run.sh twice otherwise dies with a bare "Address already in use",
# which says nothing about what to do next.
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  holder=$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -Fc 2>/dev/null | sed -n 's/^c//p' | head -1)
  warn "Port ${PORT} is already in use by: ${holder:-unknown}"
  if curl -sf "${URL}/health" >/dev/null 2>&1; then
    say "It looks like the agent is already running — open ${URL}"
    [[ -n "${NO_OPEN:-}" ]] || { command -v open >/dev/null && open "${URL}"; }
    exit 0
  fi
  die "Stop it first (kill the process, or run: PORT=8081 ./run.sh)"
fi

say "Starting the agent on ${URL}"
echo
echo "    Open  ${URL}  in your browser."
echo
echo "    Camera and microphone need a secure context. localhost counts, so they"
echo "    work here. A bare LAN IP (192.168.x.x) does NOT — to demo on a phone,"
echo "    deploy to Cloud Run and use the HTTPS URL. See DEPLOYMENT.md."
echo

# Open the browser once the server answers, without blocking the server itself.
# NO_OPEN=1 suppresses this — useful over SSH, or when a camera-enabled page
# popping open unannounced would be unwelcome.
[[ -n "${NO_OPEN:-}" ]] || (
  for _ in $(seq 1 40); do
    if curl -sf "${URL}/health" >/dev/null 2>&1; then
      command -v open    >/dev/null && open "${URL}"    && break
      command -v xdg-open >/dev/null && xdg-open "${URL}" && break
      break
    fi
    sleep 0.5
  done
) &

exec env PYTHONPATH=src uv run uvicorn main:app --reload --port "${PORT}"
