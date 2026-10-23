# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27"]
# ///
"""Raw HTML size vs extracted text size for a few pages. Tokens are chars/4 estimates."""

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
import agent

PAGES = [
    "https://en.wikipedia.org/wiki/Prompt_injection",
    "https://www.anthropic.com/engineering/building-effective-agents",
    "https://docs.python.org/3/library/sqlite3.html",
]
print(f"{'page':62} {'html chars':>11} {'~tok':>7} {'text chars':>11} {'~tok':>6}")
for url in PAGES:
    html = httpx.get(url, headers={"User-Agent": agent.USER_AGENT}, timeout=20, follow_redirects=True).text
    _, text, _ = agent.html_to_text(html)
    print(f"{url[8:70]:62} {len(html):>11,} {len(html)//4:>7,} {len(text):>11,} {len(text)//4:>6,}")
