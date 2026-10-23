# research-agent-from-scratch

A web research agent in one Python file, with no agent framework. It has four tools (search Wikipedia,
fetch a page, save a note, read notes), a loop, stop rules, and a SQLite notes file that survives between
runs. It accompanies the post "Build an AI agent from scratch in Python" on the Humanbound blog.

## Run it

Any OpenAI-compatible endpoint works. `uv` installs the one dependency (`httpx`) from the file's header.

```bash
export AGENT_API_KEY=...                         # your key
export AGENT_BASE_URL=https://api.openai.com/v1  # default
export AGENT_MODEL=gpt-4o-mini                   # default
uv run agent.py "Who coined the term 'prompt injection', and how is it different from jailbreaking?"
```

The trace goes to stderr, the answer to stdout. `--dump messages.json` writes the full message history.

OpenRouter: set `AGENT_BASE_URL=https://openrouter.ai/api/v1` and a model id such as `openai/gpt-5.6-luna`.

Local with Ollama, no key: `AGENT_BASE_URL=http://localhost:11434/v1 AGENT_MODEL=llama3.1:8b uv run agent.py "..."`.
Small local models are weak at tool calling. In the recorded run (`examples/trace-ollama-llama3.1-8b.txt`)
the model answered from search snippets without fetching a page and got the answer wrong.

Set `PRICE_IN_PER_MTOK` and `PRICE_OUT_PER_MTOK` to see a cost estimate in the trace. It is computed from
the API's `usage` numbers, not read from a bill.

## Test it

The tests need no network and no key. The model is a scripted local server.

```bash
uv run --with httpx --with pytest pytest tests
```

## What is in `examples/`

The traces, answers and message histories quoted in the post, recorded on 23 October 2026, plus
`first-attempt/`, the runs from before `read_notes` was fixed to match on any word. `page-sizes.txt` comes
from `measure_pages.py`; token counts there are characters divided by four.

The recorded runs predate a review pass that hardened the HTML extractor, the notes queries and the error
handling in `run_tool`, and reworded the `read_notes` tool description. The logic the traces show is unchanged.
`postfix/` holds two runs of the hardened code on the same question. The second one reads the notes the first one saved.

## What could go wrong

This agent is not hardened, and it is meant to be read before it is trusted.

- Fetched text is written by strangers. The model can be steered by it.
- Notes persist, so a note written because of a hostile page can mislead a later run.
- `fetch_url` will request any public address, including one that carries data out in its query string.
  It refuses private and loopback addresses, and it does not follow redirects, so each hop is checked.
  The check resolves the name once and the HTTP client resolves it again when it connects, so a hostile
  DNS server can pass the check and then point at a private address (DNS rebinding). A production agent
  should connect to the address it checked.
- It does not read `robots.txt`.
- Cost is bounded only by `MAX_STEPS` and `TOKEN_BUDGET`.

[Humanbound](https://github.com/humanbound/humanbound) is an open-source tool that tests an agent like this
over HTTP. It finds what it thinks to try, so a clean result is not proof of safety.

Apache-2.0.
