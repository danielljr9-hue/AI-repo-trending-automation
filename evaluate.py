#!/usr/bin/env python3
"""
Weekly bullshit filter for the AI GitHub Trending report.

Reads week.json (written by scan.py), scores every repo in this week's report on
evidence rather than star count, then *actually tries to install* the ones that
score well. Writes eval-<date>.json (full detail) and eval_email.html (the second
weekly email).

Two phases, deliberately separated:
  1. Score  - pure GitHub API reads. No repo code is executed.
  2. Install - shallow-clones and installs the winners. This RUNS UNTRUSTED CODE,
     so it only ever runs in the disposable CI container, with every secret
     stripped from the subprocess environment.

Stdlib only, same as scan.py.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# --- Configuration -----------------------------------------------------------

VALUABLE_AT = 45      # score >= this  -> genuinely worth installing
MAYBE_AT = 15         # score >= this  -> borderline, worth a look
MAX_INSTALLS = 8      # cap install attempts per week (each one is slow)
INSTALL_TIMEOUT = 300 # seconds per install command

# Topics/names that mark a repo as a reading list, not software.
FLUFF_TOPICS = {
    "awesome", "awesome-list", "curated-list", "list", "collection", "resources",
    "tutorial", "tutorials", "course", "courses", "roadmap", "papers",
    "paper-list", "book", "books", "notes", "interview", "interview-questions",
    "learning", "study", "cheatsheet", "guide", "handbook", "education",
    "free", "prompts",
}
FLUFF_NAME = re.compile(
    r"awesome|-list\b|roadmap|cheat.?sheet|handbook|tutorial|100.days"
    r"|from.scratch|zero.to.hero|bootcamp|crash.course|\bcourse\b",
    re.I,
)

# Root files that prove a repo is real software. Three tiers, because "can I
# install this automatically" and "is this a real project" are different questions
# -- a Zig plugin or a CMake tool is unmistakably software even though nothing here
# can `pip install` it.
MANIFESTS = {                      # real packages, and we can install them
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "package.json": "node",
    "cargo.toml": "rust",
    "go.mod": "go",
}
BUILD_MANIFESTS = {                # real software, but not auto-installed here
    "build.zig": "zig",
    "cmakelists.txt": "cmake",
    "makefile": "make",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "java",
    "composer.json": "php",
    "gemfile": "ruby",
    "pubspec.yaml": "dart",
    "package.swift": "swift",
    "deno.json": "deno",
    "mix.exs": "elixir",
}
BUILD_SUFFIXES = (".csproj", ".sln", ".cabal", ".nimble")   # matched by extension
WEAK_MANIFESTS = {"requirements.txt": "python"}

# Only these can actually be installed unattended; the rest get a command instead.
INSTALLABLE = {"python", "node", "go"}

API_ROOT = "https://api.github.com"


# --- GitHub API --------------------------------------------------------------

def _headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-github-trending-evaluator",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def api(path: str, params: dict | None = None):
    """GET one API path. Returns (parsed_json_or_None, response_headers_or_None)."""
    url = API_ROOT + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers())
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8")), resp.headers
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                reset = e.headers.get("X-RateLimit-Reset")
                wait = max(0, int(reset) - int(time.time())) + 1 if reset else 10
                time.sleep(min(max(wait, 5), 65))
                continue
            return None, e.headers  # 404 on an empty repo is a real answer
        except urllib.error.URLError as e:
            print(f"  network error: {e.reason}", file=sys.stderr)
            time.sleep(3)
    return None, None


def _last_page(headers) -> int | None:
    """Total count from a paginated endpoint, via the Link rel=last header."""
    link = headers.get("Link") if headers else None
    if not link:
        return None
    m = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link)
    return int(m.group(1)) if m else None


def fetch_extras(full_name: str) -> dict:
    """The three signals the search API doesn't give us: files, people, commits."""
    files, _ = api(f"/repos/{full_name}/contents")
    files = files if isinstance(files, list) else []

    # per_page=1 makes the last page number equal the contributor count.
    contribs, ch = api(f"/repos/{full_name}/contributors", {"per_page": 1, "anon": "1"})
    n_contrib = _last_page(ch) or (len(contribs) if isinstance(contribs, list) else 0)

    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits, _ = api(f"/repos/{full_name}/commits", {"since": since, "per_page": 100})
    n_commits = len(commits) if isinstance(commits, list) else 0

    names = {f.get("name", "").lower(): f for f in files if isinstance(f, dict)}
    readme = next((f for n, f in names.items() if n.startswith("readme")), None)

    return {
        "files": sorted(names),
        "contributors": n_contrib,
        "commits_30d": n_commits,
        "readme_bytes": (readme or {}).get("size", 0),
        "has_tests": any(
            n in ("tests", "test", "spec", "__tests__") or n.startswith("test_")
            for n in names
        ),
        "has_ci": ".github" in names,
        "manifests": [n for n in names if n in MANIFESTS],
        "build_manifests": [
            n for n in names
            if n in BUILD_MANIFESTS or n.endswith(BUILD_SUFFIXES)
        ],
        "weak_manifests": [n for n in names if n in WEAK_MANIFESTS],
    }


# --- Scoring -----------------------------------------------------------------

def any_manifest(x: dict) -> bool:
    """Any evidence at all that this repo builds or installs into something."""
    return bool(x.get("manifests") or x.get("build_manifests") or x.get("weak_manifests"))


def score(r: dict, x: dict) -> tuple[int, list]:
    """Return (points, notes) where notes are (delta, reason) pairs, best first."""
    notes: list[tuple[int, str]] = []

    def note(delta, reason):
        if delta:
            notes.append((delta, reason))

    if r.get("archived") or r.get("disabled"):
        return -100, [(-100, "Archived or disabled by its owner")]

    topics = {t.lower() for t in (r.get("topics") or [])}
    desc = (r.get("description") or "").strip()
    lang = r.get("language")
    size_kb = r.get("size", 0)
    stars_n = r.get("stargazers_count", 0)
    created = datetime.strptime(r["created_at"][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    age_days = max(1, (datetime.now(timezone.utc) - created).days)

    # --- Is it even software? ---
    fluffy = topics & FLUFF_TOPICS
    if fluffy:
        note(-max(20, 12 * len(fluffy)), f"Reading-list topics: {', '.join(sorted(fluffy))}")
    if FLUFF_NAME.search(r["full_name"]) or FLUFF_NAME.search(desc):
        note(-30, "Named/described as a list, roadmap or tutorial, not a tool")

    if x["manifests"]:
        note(25, f"Real package manifest ({', '.join(x['manifests'])})")
    elif x.get("build_manifests"):
        note(20, f"Builds from source ({', '.join(x['build_manifests'])})")
    elif x.get("weak_manifests"):
        note(5, "Only a requirements.txt — a script folder, not a package")
    else:
        note(-18, "No manifest of any kind — nothing to install or build")

    if lang in (None, "Markdown", "HTML", "CSS", "Text"):
        note(-20, f"Primary language is {lang or 'none'} — it's documents, not code")

    if size_kb < 20:
        note(-25, f"Repo is {size_kb} KB — barely any content")
    elif size_kb < 100:
        note(-12, f"Repo is only {size_kb} KB")

    # --- Is anyone actually maintaining it? ---
    if x["contributors"] >= 5:
        note(15, f"{x['contributors']} contributors")
    elif x["contributors"] >= 2:
        note(7, f"{x['contributors']} contributors")
    else:
        note(-8, "Single contributor")

    if x["commits_30d"] >= 20:
        note(15, f"{x['commits_30d']} commits in the last 30 days")
    elif x["commits_30d"] >= 5:
        note(8, f"{x['commits_30d']} commits in the last 30 days")
    elif x["commits_30d"] == 0:
        note(-12, "No commits in the last 30 days")

    # --- Engineering hygiene ---
    note(12 if x["has_tests"] else 0, "Has a test suite")
    note(12 if x["has_ci"] else 0, "Has CI configured")
    note(8 if r.get("license") else -10,
         "Licensed" if r.get("license") else "No license — can't legally use it")

    if x["readme_bytes"] > 40000:
        note(-15, f"{x['readme_bytes'] // 1000} KB README — that's course material, not docs")
    elif x["readme_bytes"] >= 3000:
        note(8, "Substantial README")
    elif x["readme_bytes"] < 500:
        note(-12, "README is a stub")

    if not desc:
        note(-8, "No description")

    # --- Hype tells ---
    per_day = stars_n / age_days
    if per_day > 400 and x["contributors"] <= 1 and not any_manifest(x):
        note(-20, f"{per_day:.0f} stars/day with one contributor and no code to install")
    forks = r.get("forks_count", 0)
    if stars_n >= 2000 and forks / max(stars_n, 1) < 0.01:
        note(-10, f"{stars_n} stars but only {forks} forks — admired, not used")
    if r.get("open_issues_count", 0) > 5:
        note(6, f"{r['open_issues_count']} open issues — people are using it")

    notes.sort(key=lambda n: -abs(n[0]))
    return sum(d for d, _ in notes), notes


def verdict(points: int, installable: bool = True) -> str:
    # Nothing to install means it can never clear the top bar, however many
    # contributors and stars it has - the whole point is installing it.
    if points >= VALUABLE_AT and installable:
        return "valuable"
    if points >= MAYBE_AT:
        return "maybe"
    return "fluff"


# --- Install (executes untrusted code — CI container only) --------------------

def _clean_env() -> dict:
    """Environment for untrusted subprocesses: no tokens, no mail creds, no git auth."""
    env = {
        k: v for k, v in os.environ.items()
        if not re.search(r"TOKEN|SECRET|PASSWORD|KEY|CREDENTIAL|AUTH", k, re.I)
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


# Build tools trail off into log paths and "run with --verbose" chatter, so scan
# upwards for the line that actually names the failure.
_VERDICT_LINE = re.compile(
    r"npm error code|ERESOLVE|^error(\[\w+\])?:|Error:|^fatal:|No matching distribution"
    r"|could not find|not found|Permission denied|timed out",
    re.I | re.M,
)
_NOISE_LINE = re.compile(r"complete log of this run|for more information|^npm error\s*$"
                         r"|^\s*\^+\s*$|log file|--verbose", re.I)


def last_line(text: str) -> str:
    """The line that names the failure, not whatever the tool printed last."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "no output"
    for ln in reversed(lines):
        if _VERDICT_LINE.search(ln) and not _NOISE_LINE.search(ln):
            return ln
    for ln in reversed(lines):
        if not _NOISE_LINE.search(ln):
            return ln
    return lines[-1]


def _run(cmd, cwd, timeout=INSTALL_TIMEOUT):
    try:
        p = subprocess.run(
            cmd, cwd=cwd, env=_clean_env(), timeout=timeout,
            capture_output=True, text=True,
        )
        return p.returncode, (p.stderr or p.stdout or "")[-600:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, str(e)


def kind_of(x: dict) -> str | None:
    for name in ("pyproject.toml", "setup.py", "package.json", "cargo.toml",
                 "go.mod", "setup.cfg"):
        if name in x["manifests"]:
            return MANIFESTS[name]
    if x.get("weak_manifests"):
        return "python"
    for name in x.get("build_manifests", []):
        if name in BUILD_MANIFESTS:
            return BUILD_MANIFESTS[name]
        return name.rsplit(".", 1)[-1]
    return None


def install_hint(full_name: str, kind: str) -> str:
    """The command a human would run to install this themselves."""
    url = f"https://github.com/{full_name}"
    return {
        "python": f"pip install git+{url}",
        "node": f"npm install {url}",
        "go": f"go install github.com/{full_name}@latest",
        "rust": f"cargo install --git {url}",
    }.get(kind, f"git clone {url}")


def try_install(full_name: str, kind: str, url: str | None = None) -> dict:
    """Shallow-clone and install for real. Returns {ok, how, detail}."""
    work = tempfile.mkdtemp(prefix="eval-")
    src = os.path.join(work, "src")
    try:
        rc, err = _run(
            ["git", "clone", "--depth", "1",
             url or f"https://github.com/{full_name}.git", src],
            cwd=work, timeout=180,
        )
        hint = install_hint(full_name, kind)
        if rc != 0:
            return {"ok": False, "how": "git clone", "hint": hint,
                    "detail": err.strip()}

        if kind == "python":
            venv = os.path.join(work, "venv")
            rc, err = _run([sys.executable, "-m", "venv", venv], cwd=work, timeout=120)
            if rc != 0:
                return {"ok": False, "how": "venv", "detail": err.strip()}
            pip = os.path.join(venv, "bin", "pip")
            files = os.listdir(src)
            if "pyproject.toml" in files or "setup.py" in files:
                cmd, how = [pip, "install", "--no-input", "."], "pip install ."
            elif "requirements.txt" in files:
                cmd, how = [pip, "install", "--no-input", "-r", "requirements.txt"], "pip install -r requirements.txt"
            else:
                return {"ok": False, "how": "python", "hint": hint,
                        "detail": "no installable entry point"}
        elif kind == "node":
            cmd = ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"]
            how = "npm install"
        elif kind == "go":
            cmd, how = ["go", "build", "./..."], "go build ./..."
        else:
            return {"ok": None, "how": kind, "hint": hint,
                    "detail": "no automated install for this ecosystem"}

        rc, err = _run(cmd, cwd=src)
        return {"ok": rc == 0, "how": how, "hint": hint,
                "detail": "" if rc == 0 else err.strip()}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --- Email -------------------------------------------------------------------

BADGE = {
    "installed": ("#16794a", "#e6f4ec", "INSTALLED"),
    "valuable":  ("#8a5a00", "#fdf3e2", "WORTH IT"),
    "maybe":     ("#3f4a5a", "#eef1f5", "BORDERLINE"),
    "fluff":     ("#a03030", "#fdeaea", "FLUFF"),
}


def eval_card(res: dict) -> str:
    r = res["repo"]
    key = "installed" if res.get("install", {}).get("ok") else res["verdict"]
    colour, bg, label = BADGE[key]
    name = html.escape(r["full_name"])
    desc = html.escape(r.get("description") or "No description provided.")
    stars_txt = f"{r['stargazers_count'] / 1000:.1f}k" if r["stargazers_count"] >= 1000 else str(r["stargazers_count"])

    reasons = "".join(
        f'<li style="margin:2px 0;color:{"#166534" if d > 0 else "#991b1b"};">'
        f'{"+" if d > 0 else ""}{d} &nbsp;{html.escape(t)}</li>'
        for d, t in res["notes"][:4]
    )

    inst = res.get("install")
    if inst and inst.get("ok"):
        footer = (
            f'<div style="font-size:12px;color:#166534;margin-top:10px;">'
            f'Installed cleanly in a fresh container. To adopt it locally:<br>'
            f'<code style="background:#f3f4f6;padding:3px 6px;border-radius:4px;'
            f'display:inline-block;margin-top:4px;font-size:12px;">'
            f'{html.escape(inst.get("hint") or inst["how"])}</code></div>'
        )
    elif inst and inst.get("ok") is False:
        footer = (
            f'<div style="font-size:12px;color:#991b1b;margin-top:10px;">'
            f'Scored well but the install failed at <strong>{html.escape(inst["how"])}</strong>: '
            f'{html.escape(last_line(inst["detail"])[:180])}</div>'
        )
    elif res["verdict"] == "valuable":
        kind = kind_of(res["extras"])
        footer = (
            f'<div style="font-size:12px;color:#6b7280;margin-top:10px;">'
            f'Not auto-installed &mdash; only the top {MAX_INSTALLS} are tried each week. '
            f'To install it yourself:<br>'
            f'<code style="background:#f3f4f6;padding:3px 6px;border-radius:4px;'
            f'display:inline-block;margin-top:4px;font-size:12px;">'
            f'{html.escape(install_hint(r["full_name"], kind or "unknown"))}</code></div>'
        )
    else:
        footer = ""

    return f"""
      <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
             style="border:1px solid #e3e6ea;border-radius:8px;margin-bottom:12px;">
        <tr><td style="padding:14px 16px;">
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
            <td style="font-size:15px;color:#111;">
              <a href="{html.escape(r['html_url'])}" style="color:#2563eb;text-decoration:none;">{name}</a>
            </td>
            <td align="right" style="white-space:nowrap;">
              <span style="background:{bg};color:{colour};font-size:11px;font-weight:bold;
                           padding:3px 9px;border-radius:10px;">{label}</span>
            </td>
          </tr></table>
          <div style="font-size:13px;color:#374151;margin:8px 0;">{desc}</div>
          <div style="font-size:12px;color:#6b7280;">
            &#9733; {stars_txt} &middot; {html.escape(r.get('language') or '—')} &middot;
            {res['extras']['contributors']} contributors &middot;
            {res['extras']['commits_30d']} commits/30d &middot;
            <strong>score {res['points']}</strong>
          </div>
          <ul style="font-size:12px;margin:8px 0 0;padding-left:18px;">{reasons}</ul>
          {footer}
        </td></tr>
      </table>"""


def render_eval_email(results: list, stamp: str) -> str:
    installed = [r for r in results if r.get("install", {}).get("ok")]
    valuable = [r for r in results if r["verdict"] == "valuable" and r not in installed]
    maybe = [r for r in results if r["verdict"] == "maybe"]
    fluff = [r for r in results if r["verdict"] == "fluff"]

    def block(title, subtitle, rows):
        if not rows:
            return ""
        return (
            f'<h2 style="font-size:18px;color:#111;margin:28px 0 2px;">{title}</h2>'
            f'<p style="font-size:13px;color:#6b7280;margin:0 0 14px;">{subtitle}</p>'
            + "".join(eval_card(r) for r in rows)
        )

    fluff_names = ", ".join(
        f'<a href="{html.escape(r["repo"]["html_url"])}" style="color:#6b7280;">{html.escape(r["repo"]["full_name"])}</a>'
        for r in fluff
    )
    fluff_block = (
        f'<h2 style="font-size:18px;color:#111;margin:28px 0 2px;">🗑️ Skipped as fluff ({len(fluff)})</h2>'
        f'<p style="font-size:13px;color:#6b7280;margin:0 0 6px;">'
        f'Reading lists, stubs, star-farms and unmaintained repos. Not installed.</p>'
        f'<p style="font-size:12px;color:#9ca3af;line-height:1.7;">{fluff_names}</p>'
        if fluff else ""
    )

    return f"""<!DOCTYPE html>
<html><body style="margin:0;background:#f4f5f7;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#f4f5f7;">
    <tr><td align="center" style="padding:24px 12px;">
      <table width="640" cellpadding="0" cellspacing="0" role="presentation"
             style="max-width:640px;background:#ffffff;border-radius:12px;padding:28px 28px 36px;
                    font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;">
        <tr><td>
          <h1 style="font-size:24px;color:#111;margin:0 0 4px;">Which of these are actually real?</h1>
          <p style="font-size:13px;color:#6b7280;margin:0 0 4px;">
            Verdicts on the {len(results)} repos in the {stamp} report.
            {len(installed)} installed and verified &middot; {len(valuable)} worth installing &middot;
            {len(maybe)} borderline &middot; {len(fluff)} fluff.
          </p>
          {block("✅ Installed and verified",
                 "Scored well on evidence, then actually installed cleanly in a fresh container.",
                 installed)}
          {block("⭐ Worth installing",
                 f"Strong evidence of real software. Only the top {MAX_INSTALLS} get an "
                 f"install attempt each week, so most of these carry a command instead.",
                 valuable)}
          {block("🤔 Borderline",
                 "Some real signal, some gaps. Worth a look before you commit time.",
                 maybe)}
          {fluff_block}
          <p style="font-size:12px;color:#9ca3af;margin-top:32px;text-align:center;">
            Scored on manifests, contributors, commit activity, tests, CI, licence and
            hype ratios &mdash; never on star count alone.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


# --- Main --------------------------------------------------------------------

def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(root, "week.json"), encoding="utf-8") as f:
        week = json.load(f)

    repos = {r["full_name"]: r for r in week["new"] + week["trending"]}
    print(f"Evaluating {len(repos)} repos from {week['generated']}", file=sys.stderr)

    results = []
    for i, (full_name, r) in enumerate(repos.items(), 1):
        x = fetch_extras(full_name)
        points, notes = score(r, x)
        v = verdict(points, installable=any_manifest(x))
        print(f"[{i}/{len(repos)}] {full_name}: {points} -> {v}", file=sys.stderr)
        results.append({"repo": r, "extras": x, "points": points,
                        "notes": notes, "verdict": v})

    # Forks break score ties: they mean people ran it, not just bookmarked it.
    results.sort(key=lambda res: (-res["points"], -res["repo"].get("forks_count", 0)))

    # Phase 2: run untrusted code. Strip the API token first so no subprocess,
    # however deeply nested, can reach it. Only ever in a disposable CI container
    # -- a stray local run must not pip-install a stranger's setup.py.
    os.environ.pop("GITHUB_TOKEN", None)
    attempted = 0
    if os.environ.get("CI") != "true" and "--install" not in sys.argv:
        print("Not in CI: skipping install phase (pass --install to override).",
              file=sys.stderr)
        results_to_install = []
    else:
        results_to_install = results
    for res in results_to_install:
        if res["verdict"] != "valuable" or attempted >= MAX_INSTALLS:
            continue
        kind = kind_of(res["extras"])
        if kind not in INSTALLABLE:
            continue
        attempted += 1
        print(f"Installing {res['repo']['full_name']} ({kind})...", file=sys.stderr)
        res["install"] = try_install(res["repo"]["full_name"], kind)
        print(f"  -> {res['install']}", file=sys.stderr)

    stamp = week["generated"]
    with open(os.path.join(root, f"eval-{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    with open(os.path.join(root, "eval_email.html"), "w", encoding="utf-8") as f:
        f.write(render_eval_email(results, stamp))

    installed = sum(1 for r in results if r.get("install", {}).get("ok"))
    print(f"Done: {installed} installed, {attempted} attempted", file=sys.stderr)
    return 0


def selftest() -> int:
    """The judge must separate an obvious reading list from an obvious library."""
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=40)).strftime("%Y-%m-%d")

    awesome = {
        "full_name": "someone/awesome-ai-tools", "created_at": recent,
        "description": "A curated list of awesome AI tools", "language": "Markdown",
        "size": 300, "stargazers_count": 9000, "forks_count": 700,
        "open_issues_count": 3, "license": {"key": "mit"},
        "topics": ["ai", "awesome", "list"],
    }
    ax = {"files": ["readme.md"], "contributors": 1, "commits_30d": 2,
          "readme_bytes": 90000, "has_tests": False, "has_ci": False,
          "manifests": [], "weak_manifests": []}
    pts, _ = score(awesome, ax)
    assert verdict(pts, False) == "fluff", f"awesome list scored {pts}, expected fluff"

    lib = {
        "full_name": "org/vector-runtime", "created_at": recent,
        "description": "High-throughput inference runtime", "language": "Python",
        "size": 14000, "stargazers_count": 4200, "forks_count": 300,
        "open_issues_count": 60, "license": {"key": "apache-2.0"},
        "topics": ["llm", "inference"],
    }
    lx = {"files": ["pyproject.toml", "tests", ".github"], "contributors": 34,
          "commits_30d": 120, "readme_bytes": 9000, "has_tests": True,
          "has_ci": True, "manifests": ["pyproject.toml"], "weak_manifests": []}
    pts, _ = score(lib, lx)
    assert verdict(pts, True) == "valuable", f"real library scored {pts}, expected valuable"

    # A busy, well-staffed repo with nothing to install still can't be "valuable".
    nx = dict(lx, manifests=[], weak_manifests=[])
    assert verdict(score(lib, nx)[0], False) != "valuable", "no-manifest repo must be gated"

    assert kind_of(lx) == "python"
    assert "GITHUB_TOKEN" not in _clean_env()
    assert "MAIL_PASSWORD" not in {**_clean_env(), **{}} or True
    os.environ["MAIL_PASSWORD"] = "x"
    assert "MAIL_PASSWORD" not in _clean_env(), "secrets must be stripped from installs"
    print("selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest() if "--selftest" in sys.argv else main())
