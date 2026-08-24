# AI GitHub Trending

A weekly automation that scans GitHub for **AI repositories** and produces a clean,
self-contained HTML report with two sections:

1. **🆕 New This Week** — the top 10 AI repos *created in the last 7 days*, ranked by stars.
2. **🔥 Trending Now** — established AI repos (created within the last ~6 months) that are
   still actively shipping and gaining stars — repos that have been out for a while but
   are clearly trending.

A repo counts as "AI" if it is tagged with one of the AI-related GitHub **topics**
(`ai`, `llm`, `machine-learning`, `generative-ai`, `agents`, `rag`, …). Edit the
`TOPICS` list in [`scan.py`](scan.py) to tune the filter.

Each repo shows: rank, name + GitHub link, stars, language, creation date, topics, and description.

## How it runs

A scheduled [GitHub Actions workflow](.github/workflows/weekly.yml) runs **every Monday
at 13:00 UTC**. It:

- generates the report,
- archives it to `reports/ai-trending-YYYY-MM-DD.html`,
- writes the latest report to `docs/index.html`,
- commits both back to the repo,
- publishes the latest report to **GitHub Pages** (a live URL you can bookmark), and
- runs the [evaluation pass](#the-second-pass-is-any-of-it-worth-installing) and emails
  you which of the week's repos are actually worth installing.

You can also trigger it any time from the **Actions** tab → *Weekly AI GitHub Trending*
→ **Run workflow**.

## The second pass: is any of it worth installing?

Trending tells you what got starred, not what is worth your time. After the report is
built, a second job runs [`evaluate.py`](evaluate.py) over every repo in it and emails
you a separate verdict.

Each repo is scored on evidence, never on stars:

| Signal | Why it matters |
|---|---|
| Package manifest (`pyproject.toml`, `package.json`, `go.mod`, …) | If there is nothing to install, it is not a tool |
| Contributors, commits in the last 30 days | Is anyone actually maintaining it |
| Tests, CI, licence | Whether it was built to be used by other people |
| README weight vs. code | A 100 KB README with no package is a course, not a library |
| Reading-list topics, `awesome-*` names | Curated lists are not software |
| Stars-per-day, forks-to-stars ratio | Separates a launch spike from real adoption |

Two rules do most of the work. **No package manifest means a repo can never be rated
"valuable"** however many stars it has, and **a bare `requirements.txt` counts as a
folder of scripts, not a package** — that single distinction is what separates a
tutorial repo from a library.

Repos that clear the bar are then **actually installed** — shallow-cloned into a fresh
container and built with `pip` / `npm` / `go`, which is the check no amount of scoring
can fake. The email reports what installed cleanly, what failed and why, what is
borderline, and what was skipped as fluff.

### Safety

`evaluate.py` runs code from repos nobody has reviewed, so the install phase is fenced off:

- it runs in its **own job** with a **read-only** `GITHUB_TOKEN`;
- the checkout uses `persist-credentials: false`, so there is no git credential to steal;
- every variable matching `TOKEN|SECRET|PASSWORD|KEY|AUTH` is **stripped from the
  environment** before the first subprocess starts;
- `npm` runs with `--ignore-scripts`, and each command is hard-timed-out;
- it **refuses to install outside CI** — a local `python3 evaluate.py` scores only.
  Pass `--install` if you really mean it.

Tune the thresholds at the top of `evaluate.py`: `VALUABLE_AT`, `MAYBE_AT`,
`MAX_INSTALLS` (installs attempted per week) and `INSTALL_TIMEOUT`.

Check the judge still behaves with:

```bash
python3 evaluate.py --selftest
```

## One-time setup

1. Create a GitHub repo and push this folder to it:
   ```bash
   git init
   git add .
   git commit -m "Initial commit: AI GitHub trending automation"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. In the repo: **Settings → Pages → Build and deployment → Source = GitHub Actions**.
3. (Already configured) The workflow uses the built-in `GITHUB_TOKEN`, so no secrets are needed.

After the first run, your live report lives at `https://<you>.github.io/<repo>/`.

## Run it locally

No dependencies — pure Python standard library (3.8+):

```bash
python3 scan.py
open docs/index.html
```

Unauthenticated, GitHub's Search API allows only ~10 requests/minute, so a local run
throttles itself and takes a couple of minutes. To run faster, export a token first:

```bash
export GITHUB_TOKEN=$(gh auth token)   # or a personal access token
python3 scan.py
```

## Tuning

All knobs are constants at the top of [`scan.py`](scan.py):

| Constant | Meaning | Default |
|---|---|---|
| `TOPICS` | GitHub topics that define "AI" | 12 topics |
| `NEW_WINDOW_DAYS` | "brand new" lookback | 7 |
| `TRENDING_WINDOW_DAYS` | "established" lookback | 180 |
| `ACTIVE_WINDOW_DAYS` | must be pushed within | 7 |
| `TOP_N` | repos per section | 10 |
| `MIN_STARS_TRENDING` | floor for the trending section | 50 |
