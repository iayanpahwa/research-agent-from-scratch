"""Offline tests: no network, no API key. The model is a scripted local HTTP server."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import agent  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "DB_PATH", str(tmp_path / "notes.db"))


def test_html_to_text_drops_chrome_keeps_text_and_links():
    html = (
        "<html><head><title>T</title><style>x{}</style></head><body><nav>menu</nav>"
        "<p>Hello <b>world</b></p><script>bad()</script><a href='/a'>a</a></body></html>"
    )
    title, text, links = agent.html_to_text(html)
    assert (title, text, links) == ("T", "Hello world a", ["/a"])


def test_notes_round_trip():
    agent.save_note("x", "a claim", "https://example.com")
    assert agent.read_notes("x")["notes"][0]["claim"] == "a claim"
    assert agent.read_notes("nothing-matches")["notes"] == []
    # A long, differently worded topic still finds the note through shared words.
    agent.save_note("prompt injection coinage", "Willison used it in 2022", "u")
    assert (
        agent.read_notes("who coined prompt injection")["notes"][0]["topic"]
        == "prompt injection coinage"
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://localhost:8000/",
        "http://169.254.169.254/",
    ],
)
def test_fetch_url_refuses_bad_targets_as_data(url):
    assert "error" in agent.fetch_url(url)


def test_bad_tool_arguments_come_back_as_data():
    call = {
        "id": "1",
        "function": {"name": "save_note", "arguments": '{"topic": "only"}'},
    }
    assert "tool failed" in agent.run_tool(call)
    call = {"id": "2", "function": {"name": "nope", "arguments": "{}"}}
    assert "no such tool" in agent.run_tool(call)
    call = {"id": "3", "function": {"name": "read_notes", "arguments": "not json"}}
    assert "tool failed" in agent.run_tool(call)


def serve(script):
    """Serve scripted chat-completion responses in order, then repeat the last."""
    replies = list(script)
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(
                json.loads(self.rfile.read(int(self.headers["content-length"])))
            )
            body = json.dumps(
                replies.pop(0) if len(replies) > 1 else replies[0]
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def reply(content=None, calls=None):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [
            {
                "id": f"c{i}",
                "type": "function",
                "function": {"name": n, "arguments": json.dumps(a)},
            }
            for i, (n, a) in enumerate(calls)
        ]
    return {
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
    }


def test_loop_runs_parallel_tools_then_answers(monkeypatch):
    server, seen = serve(
        [
            reply(
                calls=[
                    ("save_note", {"topic": "t", "claim": "c", "source_url": "u"}),
                    ("read_notes", {"topic": "t"}),
                ]
            ),
            reply(content="done"),
        ]
    )
    monkeypatch.setattr(agent, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    assert agent.run("question") == "done"
    tool_msgs = [m for m in seen[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c0", "c1"]
    assert agent.read_notes("t")["notes"][0]["claim"] == "c"


def test_max_steps_stops_a_runaway_loop(monkeypatch):
    server, seen = serve([reply(calls=[("read_notes", {})])])
    monkeypatch.setattr(agent, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    assert "max_steps" in agent.run("question")
    assert len(seen) == agent.MAX_STEPS


def test_token_budget_stops_the_run(monkeypatch):
    server, seen = serve([reply(calls=[("read_notes", {})])])
    monkeypatch.setattr(agent, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(agent, "TOKEN_BUDGET", 250)
    assert "token_budget" in agent.run("question")
    assert len(seen) == 3


def test_hostile_arguments_come_back_as_data_not_exceptions():
    cases = [
        ("fetch_url", {"url": "http://" + "a" * 64 + ".com/"}),
        ("save_note", {"topic": "t", "claim": {"a": 1}, "source_url": "u"}),
        ("read_notes", {"topic": " ".join(f"word{i}" for i in range(600))}),
    ]
    for name, args in cases:
        call = {"id": "x", "function": {"name": name, "arguments": json.dumps(args)}}
        json.loads(agent.run_tool(call))


def test_read_notes_treats_underscore_as_a_separator_not_a_wildcard():
    agent.save_note("alpha", "claim one", "u")
    assert agent.read_notes("zzz_qqq")["notes"] == []
    assert agent.read_notes("claim_nothing")["notes"][0]["claim"] == "claim one"


def test_html_to_text_svg_title_and_mismatched_chrome():
    title, text, _ = agent.html_to_text(
        "<title>Page</title><svg><title>Icon</title></svg><header>menu</footer>LEAK</header>body text"
    )
    assert title == "Page"
    assert "Icon" not in text and "menu" not in text
    assert "body text" in text
