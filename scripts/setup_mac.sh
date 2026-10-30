#!/usr/bin/env bash
# YONEXIS one-shot setup for macOS. Safe to run again: every step is skipped if already done.
#   1. Docker Desktop: download official DMG, install (asks for your Mac password once), start
#   2. Ollama: official installer, start, download the coder model
#   3. .env, build and start the stack, demo data, open the dashboard
set -euo pipefail
cd "$(dirname "$0")/.."

say()  { printf "\n\033[1;34m==> %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m%s\033[0m\n" "$*"; }
die()  { printf "\n\033[1;31mERROR: %s\033[0m\n" "$*"; exit 1; }

export PATH="/Applications/Docker.app/Contents/Resources/bin:/usr/local/bin:/opt/homebrew/bin:$HOME/.homebrew/bin:$PATH"
ARCH=$(uname -m)            # arm64 (Apple Silicon) or x86_64 (Intel)
DL="$HOME/Downloads/yonexis-installers"; mkdir -p "$DL"

# ------------------------------------------------------------------ checks
say "Checking this Mac"
FREE_GB=$(df -g "$HOME" | awk 'NR==2 {print $4}')
RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
echo "CPU: $ARCH   RAM: ${RAM_GB} GB   Free disk: ${FREE_GB} GB"
[ "$FREE_GB" -ge 15 ] || die "Need at least 15 GB free disk (Docker images + AI model). Free up space and re-run."

# ------------------------------------------------------------------ Docker Desktop
if [ ! -d /Applications/Docker.app ]; then
  say "Installing Docker Desktop (official download from docker.com, ~600 MB)"
  if [ "$ARCH" = "arm64" ]; then URL="https://desktop.docker.com/mac/main/arm64/Docker.dmg"; else URL="https://desktop.docker.com/mac/main/amd64/Docker.dmg"; fi
  [ -s "$DL/Docker.dmg" ] || curl -fsSL -o "$DL/Docker.dmg.part" "$URL"
  [ -f "$DL/Docker.dmg.part" ] && mv "$DL/Docker.dmg.part" "$DL/Docker.dmg"
  echo
  warn "macOS will now ask for YOUR Mac login password (needed to install Docker into /Applications)."
  sudo hdiutil attach -nobrowse -quiet "$DL/Docker.dmg"
  sudo /Volumes/Docker/Docker.app/Contents/MacOS/install --user="$USER"
  sudo hdiutil detach -quiet /Volumes/Docker || true
fi

if ! docker info >/dev/null 2>&1; then
  say "Starting Docker Desktop"
  open -a Docker
  warn "If a Docker window appears: read and accept the Docker Subscription Service Agreement"
  warn "(free for personal use and companies under 250 staff / \$10M revenue), then skip sign-in and the survey."
  printf "Waiting for Docker to be ready"
  for i in $(seq 1 180); do docker info >/dev/null 2>&1 && break; printf "."; sleep 3; done; echo
  docker info >/dev/null 2>&1 || die "Docker did not become ready. Finish the Docker window prompts, then double-click Start YONEXIS again."
fi
echo "Docker: $(docker --version)"

# ------------------------------------------------------------------ .env
if [ ! -f .env ]; then
  cp .env.example .env
  TOKEN=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)
  sed -i '' "s/^ADMIN_TOKEN=.*/ADMIN_TOKEN=${TOKEN}/" .env
fi
# Bigger model when the Mac has room for it (better code from the agents).
if [ "$RAM_GB" -ge 32 ] && grep -q '^LLM_MODEL=qwen2.5-coder:7b$' .env; then
  sed -i '' 's/^LLM_MODEL=.*/LLM_MODEL=qwen2.5-coder:14b/' .env
  echo "RAM ${RAM_GB} GB: using qwen2.5-coder:14b"
fi
set -a; . ./.env; set +a

# ------------------------------------------------------------------ Ollama (native, uses the Mac GPU)
if [ ! -d /Applications/Ollama.app ] && ! command -v ollama >/dev/null 2>&1; then
  say "Installing Ollama (official installer from ollama.com)"
  warn "You may be asked for your Mac password again."
  curl -fsSL https://ollama.com/install.sh -o "$DL/ollama-install.sh"
  # The installer's own last step ("open -a Ollama") can fail on a brand-new install; we start it ourselves below.
  sh "$DL/ollama-install.sh" || warn "Ollama installer reported a problem starting the app; continuing."
fi
OLLAMA=$(command -v ollama || true)
if [ -z "$OLLAMA" ] && [ -d /Applications/Ollama.app ]; then
  OLLAMA=$(find /Applications/Ollama.app/Contents -type f -name ollama -perm -u+x 2>/dev/null | head -1)
fi
[ -n "$OLLAMA" ] || die "Ollama is not installed. Install it from https://ollama.com/download and re-run."
echo "Ollama CLI: $OLLAMA"
if ! curl -fs http://localhost:11434/api/tags >/dev/null 2>&1; then
  say "Starting Ollama"
  open /Applications/Ollama.app 2>/dev/null || true
  for i in $(seq 1 20); do curl -fs http://localhost:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
  if ! curl -fs http://localhost:11434/api/tags >/dev/null 2>&1; then
    nohup "$OLLAMA" serve >"$HOME/Library/Logs/ollama-yonexis.log" 2>&1 &
    for i in $(seq 1 40); do curl -fs http://localhost:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
  fi
  curl -fs http://localhost:11434/api/tags >/dev/null 2>&1 || die "Ollama did not start. Open the Ollama app from Applications once, then re-run."
fi
if ! curl -fs http://localhost:11434/api/tags | grep -q "\"${LLM_MODEL}\""; then
  say "Downloading AI model ${LLM_MODEL} (several GB, one time)"
  "$OLLAMA" pull "${LLM_MODEL}"
fi

# ------------------------------------------------------------------ stack
for PORT in 8000 "${DB_HOST_PORT:-55432}"; do
  OWNER=$( (lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true) | awk 'NR==2 {print $1}')
  if [ -n "$OWNER" ] && ! echo "$OWNER" | grep -qi docker; then
    die "Port $PORT is already used by '$OWNER' on this Mac. Stop that program, or change the port in docker-compose.yml / .env, then re-run."
  fi
done

say "Building and starting YONEXIS (first build takes a few minutes)"
docker compose up -d --build

say "Waiting for the API"
for i in $(seq 1 120); do curl -fs http://localhost:8000/api/health >/dev/null 2>&1 && break; sleep 2; done
curl -fs http://localhost:8000/api/health >/dev/null 2>&1 || { docker compose ps; docker compose logs --tail=60 api; die "API did not start (logs above)."; }
curl -fs http://localhost:8000/api/health; echo

say "Checking the AI model is reachable from Docker"
if docker compose exec -T api curl -fs "${LLM_BASE_URL%/v1}/api/tags" >/dev/null 2>&1; then
  echo "LLM reachable: ${LLM_MODEL}"
else
  warn "Containers cannot reach Ollama at ${LLM_BASE_URL}. Agents will be offline; everything else works."
fi

if [ "${LIVE_CONNECTOR:-simulated}" = "simulated" ] && ! curl -fs -H "X-Admin-Token: ${ADMIN_TOKEN}" http://localhost:8000/api/data/inventory | grep -q RELIANCE; then
  say "Loading demo data (simulated, for testing only)"
  curl -fs -X POST -H "X-Admin-Token: ${ADMIN_TOKEN}" -H "Content-Type: application/json" \
    "http://localhost:8000/api/data/simulate?start=2024-01-01&end=2025-06-30" -d '["RELIANCE","HDFCBANK","INFY"]' >/dev/null || true
fi

printf "\n\033[1;32mYONEXIS is running.\033[0m\n"
echo "Dashboard:   http://localhost:8000"
echo "Admin token: ${ADMIN_TOKEN}"
open "http://localhost:8000" 2>/dev/null || true
