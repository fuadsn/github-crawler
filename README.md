# AI repo tracker

Finds brand-new AI repos and ranks AI repos by star *growth* — 24h, 7d, 30d — including
older repos that are re-accelerating. Runs every morning on GitHub Actions, commits the
result, and publishes to GitHub Pages. No server, no database, $0/month.

## The thing to understand

**No endpoint tells you a repo's star count on a past date.** GitHub reports the count *now*.
Last month's number has to have been written down last month.

So the daily snapshot *is* the product, and every metric here is a subtraction between two
recordings.

And there is no way to buy that history back later, because **every route to the past is
closing**:

| Source | Status |
|---|---|
| REST `/stargazers` | **dead** — 404 since 2026-06-30 |
| Public events firehose → GH Archive → ClickHouse | **gutted** — GitHub throttled `WatchEvent` ~90% (3.73% of the firehose in Jul 2025 → 0.30% by May 2026) |
| GraphQL `stargazers { starredAt }` | **the last door open** — and it leaks the same usernames the lockdown exists to protect, so don't count on it |
| `stargazerCount` (the current count) | **works, and always will** |

`backfill.py` is a one-time raid on that last open door — run it now, while it's open. But the
product doesn't depend on it. **The snapshot is the only thing.** Every morning it doesn't run is
a day of history that no API, archive, or mirror can reconstruct for you.

## Files

| | |
|---|---|
| `crawl.py` | discover repos + snapshot star counts → `data/` |
| `render.py` | `data/` → `index.html` + `DIGEST.md`. No network. |
| `backfill.py` | one-shot, run locally: reconstruct the last 30 days |
| `data/stars/*.csv` | the entire database. `repo_id,stars`, one file per day |
| `data/stars/*.bf.csv` | **backfilled** days — reconstructed, not measured |

## Setup

1. Push to GitHub (public repo — Actions is free on public repos).
2. **Settings → Pages → Source: "Deploy from a branch" → `main` / root.**
   Not the Actions-based Pages source: pushes made with the default `GITHUB_TOKEN`
   deliberately don't trigger workflows, so an Actions Pages deploy would never fire.
3. Run the workflow once by hand (Actions → daily → Run workflow) and **confirm
   `pages-build-deployment` fires.** If it doesn't, the site freezes at day 1 while the data
   pipeline stays green — and you won't notice for weeks.
4. Optional: `backfill.py` locally to populate the 7d/30d tabs immediately instead of
   waiting a month.

The nightly snapshot fits inside the default `GITHUB_TOKEN` budget (1,000 GraphQL points/hr).
If the tracked set grows past ~50k repos, add a PAT as the `GH_PAT` secret (5,000/hr) — the
workflow already prefers it when present.

## Gotchas that will bite you

- **The workflow auto-disables after 60 days of repo inactivity.** The daily bot commit resets
  that timer, so it stays alive on its own — *as long as commits keep landing*. If the crawl
  breaks and commits stop for 60 days, the workflow disables and needs a **manual** re-enable.
  **Don't filter the failure email.**
- Backfilled days stay approximate **forever** — you cannot re-snapshot the past. They keep the
  `.bf.csv` suffix permanently so the two are never conflated.
