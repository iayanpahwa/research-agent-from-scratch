#!/bin/bash
# Records the runs quoted in the post. Needs a key for an OpenAI-compatible endpoint (here OpenRouter).
# Usage: AGENT_API_KEY=... ./examples/record_runs.sh   (run from the repo root, outside any venv)
set -u
cd "$(dirname "$0")/.."
export AGENT_BASE_URL="${AGENT_BASE_URL:-https://openrouter.ai/api/v1}"
Q="Who coined the term 'prompt injection', and how is it different from jailbreaking?"
rm -f notes.db
run() { # label model price_in price_out
  AGENT_MODEL="$2" PRICE_IN_PER_MTOK="$3" PRICE_OUT_PER_MTOK="$4" \
    uv run agent.py "$Q" --dump "examples/messages-$1.json" > "examples/answer-$1.txt" 2> "examples/trace-$1.txt"
  echo "== $1 ($2)"; cat "examples/trace-$1.txt"
}
run luna-first openai/gpt-5.6-luna 0.20 1.20
run luna-second openai/gpt-5.6-luna 0.20 1.20   # same notes.db: does it read its notes?
rm -f notes.db
run haiku anthropic/claude-haiku-4.5 1.00 5.00
