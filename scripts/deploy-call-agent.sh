#!/usr/bin/env bash
# Deploys the real-time call agent (ADR 0014) from this checkout to LiveKit Cloud: builds the
# folder `lk agent deploy` expects (Dockerfile.call as Dockerfile, requirements-call.lock as
# requirements.txt, since LiveKit's build looks for that name) and deploys it. Secrets stay as
# set on the agent; this only ships code.
#
# Usage: scripts/deploy-call-agent.sh
# Auth: `lk cloud auth` locally, or LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET (CI).
set -euo pipefail

LIVEKIT_SUBDOMAIN="voice-ai-osdkdatu"
LIVEKIT_AGENT="CA_NyUMYUEw2ezk"

root="$(cd "$(dirname "$0")/.." && pwd)"
build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT

sed 's/requirements-call\.lock/requirements.txt/g' "$root/Dockerfile.call" >"$build/Dockerfile"
cp "$root/requirements-call.lock" "$build/requirements.txt"
cp "$root/pyproject.toml" "$root/README.md" "$root/.dockerignore" "$build/"
cp -R "$root/src" "$build/src"
find "$build/src" -name __pycache__ -prune -exec rm -rf {} +
printf '[project]\n  subdomain = "%s"\n\n[agent]\n  id = "%s"\n' \
  "$LIVEKIT_SUBDOMAIN" "$LIVEKIT_AGENT" >"$build/livekit.toml"

lk agent deploy -y "$build"
lk agent status "$build"
