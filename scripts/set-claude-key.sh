#!/usr/bin/env bash
# Stores a Claude Console API key as ANTHROPIC_API_KEY for production (ADR 0013 amendment):
# the API's Vercel project (a sensitive variable, read at its next deploy) and the call agent's
# LiveKit Cloud secrets (the agent restarts). With the key set, chat and calls use the Claude
# API instead of Foundry.
#
# Usage: scripts/set-claude-key.sh   (asks for the key; nothing is echoed, logged or saved)
# Needs: curl, npx (Vercel CLI, signs in if needed) and lk (after `lk cloud auth`).
set -euo pipefail

VERCEL_CLI="vercel@65.9.0"
VERCEL_TEAM="arsalannkhanns-projects"
VERCEL_PROJECT="aster-clinic-api"
LIVEKIT_SUBDOMAIN="voice-ai-osdkdatu"
LIVEKIT_AGENT="CA_NyUMYUEw2ezk"
MODEL="${CLAUDE_MODEL:-claude-haiku-5-5}"
NAME="ANTHROPIC_API_KEY"

for tool in curl npx lk; do
  command -v "$tool" >/dev/null || { echo "Missing $tool." >&2; exit 1; }
done

work="$(umask 077 && mktemp -d)"
trap 'rm -rf "$work"' EXIT

read -rsp "Paste your Claude Console API key (input hidden): " key
echo
key="${key//[[:space:]]/}"
if [[ ! "$key" =~ ^sk-ant-[A-Za-z0-9_-]+$ ]]; then
  echo "That does not look like a Claude API key (sk-ant-...)." >&2
  exit 1
fi

echo "1/3 Checking the key against $MODEL..."
# The key goes to curl on stdin, never on the command line.
status="$(printf 'header = "x-api-key: %s"\n' "$key" | curl -sS -o /dev/null -w '%{http_code}' \
  -K - https://api.anthropic.com/v1/messages \
  -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  --data "{\"model\":\"$MODEL\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}]}")"
if [[ "$status" != "200" ]]; then
  echo "The Claude API answered HTTP $status (401: wrong key, 404: model not on this account)." >&2
  exit 1
fi
echo "    Key works."

echo "2/3 Saving it in Vercel ($VERCEL_PROJECT, production, sensitive)..."
cd "$work"  # So the Vercel CLI links nothing into the repository.
npx -y "$VERCEL_CLI" whoami --scope "$VERCEL_TEAM" >/dev/null 2>&1 || npx -y "$VERCEL_CLI" login
printf '%s' "$key" | npx -y "$VERCEL_CLI" env add "$NAME" --environment production \
  --sensitive --force --yes --project "$VERCEL_PROJECT" --scope "$VERCEL_TEAM" >/dev/null
echo "    Saved. The API uses it from its next deploy."

echo "3/3 Saving it in the call agent's LiveKit secrets..."
printf '[project]\n  subdomain = "%s"\n\n[agent]\n  id = "%s"\n' \
  "$LIVEKIT_SUBDOMAIN" "$LIVEKIT_AGENT" >"$work/livekit.toml"
if lk agent secrets "$work" 2>/dev/null | grep -qw "$NAME"; then
  # Replacing one secret needs --overwrite, which this script does not risk on the others.
  echo "    $NAME is already set on the agent. Replace it in the LiveKit Cloud dashboard" \
    "(Agents > $LIVEKIT_AGENT > Secrets)." >&2
  exit 1
fi
printf '%s=%s\n' "$NAME" "$key" >"$work/secrets.env"
lk agent update-secrets --secrets-file "$work/secrets.env" -y "$work" >/dev/null
unset key
for kept in DATABASE_URL TOKEN_ENCRYPTION_KEYS "$NAME"; do
  lk agent secrets "$work" 2>/dev/null | grep -qw "$kept" || {
    echo "    $kept is missing from the agent's secrets; check them in LiveKit Cloud." >&2
    exit 1
  }
done
echo "    Saved; the agent restarts with it."
echo "Done. Chat and calls switch to the Claude API once the API and agent run the new code."
