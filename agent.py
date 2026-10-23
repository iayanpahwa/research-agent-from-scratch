# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27"]
# ///
"""A web research agent with no framework: one file, four tools, one loop.

    uv run agent.py "Who first described prompt injection, and when?"

Set AGENT_API_KEY, and optionally AGENT_BASE_URL and AGENT_MODEL, for any
OpenAI-compatible endpoint. The trace goes to stderr, the answer to stdout.
"""

import argparse
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import time
from contextlib import closing
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

BASE_URL = os.environ.get("AGENT_BASE_URL", "https://api.openai.com/v1")
MODEL = os.environ.get("AGENT_MODEL", "gpt-4o-mini")
API_KEY = os.environ.get("AGENT_API_KEY", "")
DB_PATH = os.environ.get("NOTES_DB", "notes.db")
PRICE_IN = float(os.environ.get("PRICE_IN_PER_MTOK", "0"))
PRICE_OUT = float(os.environ.get("PRICE_OUT_PER_MTOK", "0"))
MAX_STEPS = 12
TOKEN_BUDGET = 60_000
MAX_TOOL_CHARS = 8_000
MAX_BYTES = 2_000_000
USER_AGENT = "research-agent-from-scratch/0.1 (+https://github.com/iayanpahwa/research-agent-from-scratch)"

SYSTEM_PROMPT = """You are a research assistant. Answer the user's question using pages you fetch.
- Search first, then fetch the pages worth reading. Do not answer from memory.
- Save a note with save_note for each claim you rely on, with the page it came from.
- Check read_notes before searching, in case an earlier run already covered the topic.
- Stop when the question is answered. Give a short answer and list your sources."""


# ---------- tools: each is a plain function that returns data, never raises ----------


class TextExtractor(HTMLParser):
    """Keep visible text and links. Drop scripts, styles and page chrome."""

    SKIP = {"script", "style", "nav", "footer", "header", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.skipping, self.title, self.parts, self.links = [], "", [], []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping.append(tag)
        if tag == "title" and not self.skipping:
            self.in_title = True
        if tag == "body":
            self.in_title = False
        if tag == "a" and dict(attrs).get("href"):
            self.links.append(dict(attrs)["href"])

    def handle_endtag(self, tag):
        if self.skipping and tag == self.skipping[-1]:
            self.skipping.pop()
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skipping and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> tuple[str, str, list[str]]:
    p = TextExtractor()
    p.feed(html)
    return p.title.strip(), " ".join(p.parts), p.links


def is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


def search_wikipedia(query: str) -> dict:
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": 5,
        "format": "json",
    }
    try:
        r = httpx.get(
            "https://en.wikipedia.org/w/api.php",
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        hits = r.json()["query"]["search"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return {"error": f"search failed: {exc}"}
    return {
        "results": [
            {
                "title": h["title"],
                "url": "https://en.wikipedia.org/wiki/" + h["title"].replace(" ", "_"),
                "snippet": re.sub(r"<[^>]+>", "", h["snippet"]),
            }
            for h in hits
        ]
    }


def fetch_url(url: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {"error": "only http and https URLs are allowed"}
    if not is_public_host(parsed.hostname):
        return {"error": "refusing to fetch a private or unresolvable address"}
    try:
        with httpx.stream(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
            follow_redirects=False,
        ) as r:
            if 300 <= r.status_code < 400:
                return {
                    "status": r.status_code,
                    "redirect_to": urljoin(url, r.headers.get("location", "")),
                }
            body = b"".join(
                c for _, c in zip(range(MAX_BYTES // 65536 + 1), r.iter_bytes(65536))
            )[:MAX_BYTES]
            kind = r.headers.get("content-type", "")
            status = r.status_code
    except httpx.HTTPError as exc:
        return {"error": f"fetch failed: {exc}"}
    if not any(t in kind for t in ("text/html", "text/plain")):
        return {
            "status": status,
            "error": f"not a text page ({kind or 'unknown type'})",
        }
    title, text, links = html_to_text(body.decode("utf-8", errors="replace"))
    return {
        "status": status,
        "title": title,
        "text": text[:MAX_TOOL_CHARS],
        "truncated": len(text) > MAX_TOOL_CHARS,
        "links": links[:20],
    }


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, ts TEXT, topic TEXT, claim TEXT, source_url TEXT)"
    )
    return con


def save_note(topic: str, claim: str, source_url: str) -> dict:
    with closing(db()) as con, con:
        con.execute(
            "INSERT INTO notes (ts, topic, claim, source_url) VALUES (datetime('now'), ?, ?, ?)",
            (topic, claim, source_url),
        )
    return {"saved": True}


def read_notes(topic: str = "") -> dict:
    """Match any word of the topic against saved topics and claims."""
    words = [w for w in re.findall(r"[^\W_]+", topic) if len(w) > 2][:8] or [""]
    clause = " OR ".join("topic LIKE ? OR claim LIKE ?" for _ in words)
    params = [f"%{w}%" for w in words for _ in (0, 1)]
    with closing(db()) as con:
        rows = con.execute(
            f"SELECT topic, claim, source_url FROM notes WHERE {clause} ORDER BY id DESC LIMIT 20",
            params,
        ).fetchall()
    return {"notes": [{"topic": t, "claim": c, "source_url": s} for t, c, s in rows]}


TOOLS = {f.__name__: f for f in (search_wikipedia, fetch_url, save_note, read_notes)}


def schema(name: str, description: str, **props: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    k: {"type": "string", "description": v} for k, v in props.items()
                },
                "required": list(props) if name != "read_notes" else [],
            },
        },
    }


TOOL_SCHEMAS = [
    schema(
        "search_wikipedia",
        "Search Wikipedia. Returns the top 5 titles, URLs and snippets.",
        query="Search terms",
    ),
    schema(
        "fetch_url",
        "Fetch a web page and return its visible text (truncated) and first links.",
        url="Full http(s) URL",
    ),
    schema(
        "save_note",
        "Save one claim you rely on, with the URL it came from. Notes persist between runs.",
        topic="Short topic label",
        claim="The claim, in one sentence",
        source_url="Page the claim came from",
    ),
    schema(
        "read_notes",
        "Read saved notes whose topic or claim contains any word of the given text. Empty text returns the latest notes.",
        topic="Words to match against saved topics and claims",
    ),
]


# ---------- the loop ----------


def call_model(messages: list[dict]) -> dict:
    """One chat-completions request. Retry rate limits and server errors; raise anything else."""
    for attempt in range(4):
        r = httpx.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"} if API_KEY else {},
            json={"model": MODEL, "messages": messages, "tools": TOOL_SCHEMAS},
            timeout=120,
        )
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2**attempt)
            continue
        r.raise_for_status()
        data = r.json()
        if "choices" not in data:
            raise RuntimeError(f"unexpected model reply: {str(data)[:200]}")
        return data
    raise RuntimeError(f"model API still failing after retries (HTTP {r.status_code})")


def run_tool(call: dict) -> str:
    name = call["function"]["name"]
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
        result = (
            TOOLS[name](**args) if name in TOOLS else {"error": f"no such tool: {name}"}
        )
    except Exception as exc:  # a tool must never end the run
        result = {"error": f"tool failed: {type(exc).__name__}: {exc}"}
    return json.dumps(result)


def run(question: str, dump: str | None = None) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tokens_in = tokens_out = fetches = 0
    started, stop, answer = time.time(), "max_steps", ""
    for step in range(1, MAX_STEPS + 1):
        data = call_model(messages)
        msg, usage = data["choices"][0]["message"], data.get("usage") or {}
        tokens_in += usage.get("prompt_tokens", 0)
        tokens_out += usage.get("completion_tokens", 0)
        calls = msg.get("tool_calls") or []
        if not calls:
            answer, stop = msg.get("content") or "", "answer"
            break
        messages.append(msg)
        for call in calls:
            result = run_tool(call)
            fetches += call["function"]["name"] == "fetch_url"
            print(
                f"step {step} | {call['function']['name']}({(call['function']['arguments'] or '')[:70]}) | {len(result)} chars | "
                f"in {usage.get('prompt_tokens', 0)} out {usage.get('completion_tokens', 0)}",
                file=sys.stderr,
            )
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )
        if tokens_in + tokens_out > TOKEN_BUDGET:
            stop = "token_budget"
            break
    cost = tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT
    print(
        f"stop={stop} steps={step} fetches={fetches} tokens_in={tokens_in} tokens_out={tokens_out} "
        f"cost=${cost:.4f} seconds={time.time() - started:.1f}",
        file=sys.stderr,
    )
    if dump:
        json.dump(messages, open(dump, "w"), indent=2)
    return answer or f"(stopped: {stop}, no final answer)"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--dump", help="write the full message history to this file")
    args = ap.parse_args()
    if not API_KEY and "localhost" not in BASE_URL and "127.0.0.1" not in BASE_URL:
        sys.exit(
            "Set AGENT_API_KEY, or point AGENT_BASE_URL at a local server such as Ollama."
        )
    print(run(args.question, args.dump))
