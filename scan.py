#!/usr/bin/env python3
"""
AI GitHub Trending scanner.

Builds a weekly HTML report with two sections:
  1. New This Week  - top AI repos *created* in the last 7 days, ranked by stars.
  2. Trending Now   - established AI repos (created in the last ~6 months) that are
                      still actively pushed and have lots of stars - i.e. repos that
                      have been out for a while but are clearly gaining traction.

An "AI repo" is any repo tagged with one of the AI-related GitHub topics in TOPICS.

Uses only the Python standard library. If a GITHUB_TOKEN environment variable is
present it is used to authenticate (5x higher rate limits); otherwise it runs
unauthenticated with extra throttling.
"""

from __future__ import annotations

import json
import os
import sys
import time
import html
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# --- Configuration -----------------------------------------------------------

# GitHub topics that mark a repo as "AI". Each is queried separately and the
# results are merged + de-duplicated, because GitHub search ANDs multiple
# `topic:` qualifiers rather than ORing them.
TOPICS = [
    "ai",
    "artificial-intelligence",
    "llm",
    "large-language-models",
    "generative-ai",
    "machine-learning",
    "deep-learning",
    "agents",
    "ai-agents",
    "rag",
    "chatgpt",
    "computer-vision",
]

NEW_WINDOW_DAYS = 7          # "brand new" = created within this many days
TRENDING_WINDOW_DAYS = 180   # established = created within this many days...
ACTIVE_WINDOW_DAYS = 7       # ...and pushed (active) within this many days
TOP_N = 10                   # how many repos to show per section
PER_PAGE = 30                # results requested per topic query
MIN_STARS_TRENDING = 50      # ignore tiny repos in the trending section

API = "https://api.github.com/search/repositories"


# --- GitHub API helpers ------------------------------------------------------

def _headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-github-trending-scanner",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def search(query: str, sort: str = "stars") -> list:
    """Run one repository search and return the items (with retry on rate limit)."""
    params = urllib.parse.urlencode(
        {"q": query, "sort": sort, "order": "desc", "per_page": PER_PAGE}
    )
    url = f"{API}?{params}"
    req = urllib.request.Request(url, headers=_headers())

    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("items", [])
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                # Rate limited - honour Retry-After / reset header, then retry.
                wait = int(e.headers.get("Retry-After", 0) or 0)
                if not wait:
                    reset = e.headers.get("X-RateLimit-Reset")
                    if reset:
                        wait = max(0, int(reset) - int(time.time())) + 1
                wait = min(max(wait, 5), 65)
                print(f"  rate limited, sleeping {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  HTTP {e.code} for query '{query}': {e.reason}", file=sys.stderr)
            return []
        except urllib.error.URLError as e:
            print(f"  network error: {e.reason}", file=sys.stderr)
            time.sleep(3)
    return []


def collect(date_qualifier: str, extra: str = "") -> dict:
    """Query every AI topic with the given qualifiers, merged + deduped by id."""
    throttle = 0.4 if os.environ.get("GITHUB_TOKEN") else 7.0
    repos: dict[int, dict] = {}
    for i, topic in enumerate(TOPICS):
        q = f"topic:{topic} {date_qualifier} {extra}".strip()
        print(f"[{i + 1}/{len(TOPICS)}] {q}", file=sys.stderr)
        for item in search(q):
            repos[item["id"]] = item
        if i < len(TOPICS) - 1:
            time.sleep(throttle)
    return repos


# --- Report data -------------------------------------------------------------

def build_sections() -> tuple[list, list]:
    now = datetime.now(timezone.utc)
    new_since = (now - timedelta(days=NEW_WINDOW_DAYS)).strftime("%Y-%m-%d")
    trend_since = (now - timedelta(days=TRENDING_WINDOW_DAYS)).strftime("%Y-%m-%d")
    active_since = (now - timedelta(days=ACTIVE_WINDOW_DAYS)).strftime("%Y-%m-%d")

    print("Scanning brand-new AI repos...", file=sys.stderr)
    new_repos = collect(f"created:>={new_since}")
    new_sorted = sorted(
        new_repos.values(), key=lambda r: r["stargazers_count"], reverse=True
    )[:TOP_N]

    print("Scanning trending established AI repos...", file=sys.stderr)
    trend_repos = collect(
        f"created:{trend_since}..{new_since}",
        extra=f"pushed:>={active_since} stars:>={MIN_STARS_TRENDING}",
    )
    new_ids = {r["id"] for r in new_sorted}
    trend_sorted = sorted(
        (r for r in trend_repos.values() if r["id"] not in new_ids),
        key=lambda r: r["stargazers_count"],
        reverse=True,
    )[:TOP_N]

    return new_sorted, trend_sorted


# --- HTML rendering ----------------------------------------------------------

def fmt_date(iso: str) -> str:
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%b %-d, %Y")
    except ValueError:
        return iso[:10]


def stars(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def repo_card(rank: int, r: dict) -> str:
    name = html.escape(r["full_name"])
    url = html.escape(r["html_url"])
    desc = html.escape(r.get("description") or "No description provided.")
    lang = html.escape(r.get("language") or "—")
    created = fmt_date(r["created_at"])
    topic_chips = "".join(
        f'<span class="topic">{html.escape(t)}</span>'
        for t in (r.get("topics") or [])[:8]
    )
    return f"""
      <article class="card">
        <div class="rank">{rank}</div>
        <div class="body">
          <div class="row">
            <a class="name" href="{url}" target="_blank" rel="noopener">{name}</a>
            <span class="stars">&#9733; {stars(r['stargazers_count'])}</span>
          </div>
          <p class="desc">{desc}</p>
          <div class="meta">
            <span class="lang">{lang}</span>
            <span class="dot">&middot;</span>
            <span class="created">Created {created}</span>
          </div>
          <div class="topics">{topic_chips}</div>
        </div>
      </article>"""


def section(title: str, subtitle: str, repos: list, empty: str) -> str:
    if repos:
        cards = "".join(repo_card(i + 1, r) for i, r in enumerate(repos))
    else:
        cards = f'<p class="empty">{empty}</p>'
    return f"""
    <section>
      <h2>{title}</h2>
      <p class="subtitle">{subtitle}</p>
      {cards}
    </section>"""


def render(new_repos: list, trend_repos: list) -> str:
    now = datetime.now(timezone.utc)
    generated = now.strftime("%A, %B %-d, %Y")
    new_from = (now - timedelta(days=NEW_WINDOW_DAYS)).strftime("%b %-d")

    new_section = section(
        "🆕 New This Week",
        f"Top {TOP_N} AI repositories created since {new_from}, ranked by stars.",
        new_repos,
        "No new AI repos cleared the bar this week.",
    )
    trend_section = section(
        "🔥 Trending Now",
        "Established AI repos (last ~6 months) still actively shipping and gaining stars.",
        trend_repos,
        "No trending AI repos found this week.",
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI GitHub Trending &middot; {generated}</title>
<style>
  :root {{
    --bg: #0d1117; --panel: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
    --star: #f0b429; --chip: #1f2937; --chip-text: #9fb6d6;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    line-height: 1.5;
  }}
  .wrap {{ max-width: 860px; margin: 0 auto; padding: 40px 20px 80px; }}
  header h1 {{ font-size: 28px; margin: 0 0 4px; }}
  header .gen {{ color: var(--muted); margin: 0 0 8px; font-size: 14px; }}
  section {{ margin-top: 44px; }}
  h2 {{ font-size: 22px; margin: 0 0 2px; }}
  .subtitle {{ color: var(--muted); margin: 0 0 20px; font-size: 14px; }}
  .card {{
    display: flex; gap: 16px; background: var(--panel);
    border: 1px solid var(--border); border-radius: 12px;
    padding: 18px 20px; margin-bottom: 14px;
  }}
  .card:hover {{ border-color: var(--accent); }}
  .rank {{
    font-size: 22px; font-weight: 700; color: var(--muted);
    min-width: 34px; text-align: center; padding-top: 2px;
  }}
  .body {{ flex: 1; min-width: 0; }}
  .row {{ display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }}
  .name {{
    color: var(--accent); font-weight: 600; font-size: 17px;
    text-decoration: none; word-break: break-word;
  }}
  .name:hover {{ text-decoration: underline; }}
  .stars {{ color: var(--star); font-weight: 600; white-space: nowrap; font-size: 15px; }}
  .desc {{ margin: 8px 0 10px; color: var(--text); font-size: 14px; }}
  .meta {{ color: var(--muted); font-size: 13px; }}
  .lang {{ color: var(--text); }}
  .dot {{ margin: 0 6px; }}
  .topics {{ margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; }}
  .topic {{
    background: var(--chip); color: var(--chip-text); font-size: 12px;
    padding: 2px 9px; border-radius: 999px;
  }}
  .empty {{ color: var(--muted); font-style: italic; }}
  footer {{ margin-top: 60px; color: var(--muted); font-size: 13px; text-align: center; }}
  footer a {{ color: var(--accent); text-decoration: none; }}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>AI GitHub Trending</h1>
      <p class="gen">Generated {generated} (UTC)</p>
    </header>
    {new_section}
    {trend_section}
    <footer>
      Auto-generated every Monday &middot; data from the
      <a href="https://docs.github.com/en/rest/search" target="_blank" rel="noopener">GitHub Search API</a>.
    </footer>
  </div>
</body>
</html>"""


# --- Main --------------------------------------------------------------------

def main() -> int:
    new_repos, trend_repos = build_sections()
    html_out = render(new_repos, trend_repos)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    root = os.path.dirname(os.path.abspath(__file__))
    reports_dir = os.path.join(root, "reports")
    docs_dir = os.path.join(root, "docs")
    os.makedirs(reports_dir, exist_ok=True)
    os.makedirs(docs_dir, exist_ok=True)

    dated = os.path.join(reports_dir, f"ai-trending-{stamp}.html")
    latest = os.path.join(docs_dir, "index.html")
    for path in (dated, latest):
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_out)

    print(f"Wrote {dated}", file=sys.stderr)
    print(f"Wrote {latest}", file=sys.stderr)
    print(
        f"New: {len(new_repos)} repos | Trending: {len(trend_repos)} repos",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
