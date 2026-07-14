# AI repo tracker — design notes

Nightly job that finds brand-new AI repos and ranks AI repos by star *growth* (24h / 7d / 30d),
including older repos that are re-accelerating.

Every claim here was **tested against the live API**, not assumed. Where a check overturned an
assumption, the design changed. Three agents red-teamed it — the findings they got *wrong* are
noted too, because those were the loudest ones.

---

## The fact that shapes everything

On **2026-06-30 GitHub restricted the stargazers API** — far more narrowly than the internet believes:

```
REST     /repos/{o}/{r}/stargazers               → 404. Dead.
GraphQL  repository { stargazers { starredAt } }  → WORKS. Verified on ~60 repos.
```

Every "star history is dead" writeup describes the REST endpoint. **The GraphQL door is open.**
No scraping, no third-party mirror, no bulk archive. All first-party GitHub.

What *is* permanently true: **no endpoint gives a repo's star count on a past date.** GitHub
reports the count *now*. Last month's number had to be written down last month.

**The recording is the product.** The first morning it runs is the most valuable one.

---

## Architecture

No database, no server, no hosting, no third-party service. **$0/month.** Python, **stdlib only**.

```
.github/workflows/daily.yml   cron → run → commit → Pages deploys
crawl.py                      discover + snapshot → data/
render.py                     data/ → index.html + DIGEST.md   (no network)
backfill.py                   one-shot, local
data/repos.json               {repo_id: {node_id, name, desc, lang, topics, created_at}}
data/stars/YYYY-MM-DD.csv     repo_id,stars     ← the entire database
data/stars/YYYY-MM-DD.bf.csv  backfilled (reconstructed, NOT measured)
data/blocklist.txt            spam/farm repo_ids
```

**CSV in git, not SQLite** — a SQLite file is a binary blob git re-stores *in full* every commit;
the repo would grow by the DB's size daily. CSV is text, so git deltas it.
**Measured:** 10k repos × 365 daily CSVs = 54 MB raw → **13.2 MB packed**. Three years ≈ 40 MB.
~25 years to threaten GitHub's 1 GB soft limit.

**Rows sorted by repo_id.** Measured: unsorted packs to 25.8 MB, sorted to 13.2 MB — **2× better,
free**, and diffs become readable.

---

## 1. Discovery

**Full sweep weekly (Mondays); velocity pass nightly.** Discovery is ~2–3k search requests at
30/min (70+ min of mostly sleeping); the snapshot is ~100 GraphQL requests in 2 min. The repo set
barely moves day to day.

**Put `created:>{window}` on every query.** This is the whole trick:

| Query | total_count |
|---|---|
| `topic:llm stars:>10` (no date bound) | **12,851** ← 92% silently truncated |
| `created:>7d stars:>10` (all repos) | **929** ← under the 1,000 cap |

Search silently truncates at 1,000 results. Stars-band sharding **does not fix this** —
`topic:llm stars:11..20` alone is 3,473. A date bound does, completely. **So: no sharding.**

Three passes, union, dedup on repo id. `fork:false archived:false stars:>10`.

1. **Topic pass** — ~30 topics. **Singular AND plural are different topics**: `ai-agent` (2,500)
   vs `ai-agents` (5,819). Querying one is a measured miss. `claude-code` alone is **6,530 repos**
   — bigger than `rag`. Drop `agi` (198), `llmops` (355), `genai` (634): tiny and subsumed.
2. **Keyword pass** — `llm`/`agent`/`ai`/`gpt`/`rag`/`mcp` `in:name,description`.
3. **Velocity pass** — `created:>7d stars:>25`, **no AI filter** (~417/week). The safety net for
   AI repos using vocabulary no qualifier anticipated.

### Why all three: topic-only misses 50.9% of new AI repos

Measured on 167 confirmed-AI repos created in the last week:

| | % |
|---|---|
| Zero topics at all | **40.7%** |
| Has topics, none in our list | 10.2% |
| **Topic pass would MISS** | **50.9%** |

Major labs ship with **no topics**: `Tencent-Hunyuan/Hy3` (295B reasoning model), `nv-tlabs/ardy`
(NVIDIA, SIGGRAPH), `awslabs/loom`. Topics get added days later, if ever.

### Classifying the velocity pass: description, then README

Repos failing the description keyword check are **not dropped** — check the README (~165 extra
REST calls/week). Require **≥2 distinct word-boundary hits**.

`Robbyant/lingbot-world-v2` (★1,069) is a frontier world model whose entire description is
*"Infinite Worlds with Versatile Interactions"* — zero AI keywords. Its README hits 4.

**Use word boundaries (`\bai\b`).** Substring matching is worthless: bare `ai` matches *available*,
*domain*, *explain*.

> **A red-team agent claimed we miss "the #1 new AI repo of the week," `withmarbleapp/os-taxonomy`
> (★2,974, no description, no topics). I checked: it's an open taxonomy of what children learn in
> primary school. Not an AI repo at all.** The filter was right to drop it. I nearly added an LLM
> classifier on the strength of that one false claim. The README check is enough; an LLM is not needed.

**Spam:** a star-farm ring (`fintech-*`, same week, near-identical counts, 15+ stuffed topics) sits
in the velocity results. Guard: **drop repos with >15 topics** + a manual `data/blocklist.txt`.

---

## 2. Snapshot — the actual product

**Query by `nodes(ids:)`, NEVER by `owner/name`.** The most important line in the design.

```graphql
{ nodes(ids:["R_kgDO...", ...100...]) { ... on Repository { databaseId nameWithOwner stargazerCount } } }
```
**Verified: 100 repos, cost = 1 point, 3/3 clean.**

### Why: `owner/name` silently returns the WRONG REPO
```
repository(owner:"ry", name:"deno")  → ry/deno         447 stars    ← a fork squatting the path
node(id:"R_kgDOB_QrUA")              → denoland/deno   107,781 stars ← the real repo
```
GraphQL follows renames — until someone re-occupies the vacated path, at which point it serves a
*different repo* with no error. You'd write 447 under deno's id and render a **−107,334 overnight
delta that looks exactly like real data.** An org transfer is the most common thing that happens to
a repo *right as it starts trending* — this bug targets precisely the repos we exist to find.

`node_id` comes free in the search response. `nodes(ids:)` is also ⅓ the query bytes and sidesteps
the fact that `next.js` isn't a legal GraphQL alias.

**Free insurance:** key the CSV row on the `databaseId` GitHub **returns**, never the one requested.

### Three rules against silent, permanent corruption

A dead repo returns **HTTP 200 with partial data + an `errors` array**:
```
{"data":{"good":{...},"dead":null}, "errors":[{"type":"NOT_FOUND","path":["dead"]}]}
```
1. **Never `if errors: raise`** — one deleted repo would destroy the whole night's snapshot, and
   that history is unbuyable. Iterate, skip nulls, count them.
2. **A missing repo → ABSENT ROW, never `0`.** A repo private for one day would post a −140,000
   delta indistinguishable from a real one.
3. **Validate the response PARSES as JSON.** The 502 mode returns **raw HTML from nginx** — there
   is no `errors` key to check.

**Snapshot every repo in `repos.json`, not just today's search hits** — otherwise a repo that drifts
out of the search slice goes dark and its history breaks.

**Scale:** "10k repos" was a guess and it's low — `topic:llm` alone is 12.8k; the union is plausibly
40–60k → 400–600 points/day. Fits `GITHUB_TOKEN`'s 1,000/hr, but not with much room. The workflow
prefers a `GH_PAT` secret (5,000/hr) when present.

---

## 3. Metrics

**Rank by MEDIAN DAILY DELTA over the window, not `stars[t] − stars[t−N]`.** You snapshot daily, so
the per-day series is free. Endpoint subtraction lets one Hacker News spike pin a repo to the 7d list
for a week and the 30d list for a month. The median kills spikes and rewards sustained acceleration —
the actual product. (Show the raw window total as the headline number; rank by the median.)

**Hard rules — each is a bug that looks exactly like real data:**
- Missing snapshot for `t−N` → the delta is **ABSENT**, never computed off the nearest file.
- Missing row → absent, never `0`.
- **Never render a negative delta under a heading that says "growing."** GitHub bulk-purges spam
  accounts; a repo can shed thousands of stars overnight.

---

## 4. Backfill

```graphql
repository(owner:"x", name:"y") {
  stargazerCount
  stargazers(first:100, orderBy:{field:STARRED_AT, direction:DESC}) {
    edges { starredAt }  pageInfo { hasNextPage endCursor }
} }
```

**Stress-tested to exhaustion:**
- **No pagination ceiling.** Walked `duckdb/duckdb` to the end: 393 pages, 39,381 items, exactly
  matching `stargazerCount`. The feared ~40k cap **does not exist**.
- **Ordering is perfect.** 634 page boundaries across 3 repos: zero violations.
- **1 point per page**, any page size. 30-day depth: react 11 pages, vscode 18, langchain 28.

**One repo at a time. Do NOT alias.** Aliasing *looks* like a 10× win, but the `stargazers`
connection is server-expensive and 502s under load — a 10-alias batch of mega-repos failed 3/3.
Batch size isn't the constraint, *total work* is. It's a one-time overnight job; throughput is
irrelevant.

### The four guards

1. **Validate the response PARSES as JSON before touching the cursor** — the 502 is raw HTML.
2. **Never pass an empty/null cursor.** ⚠️ **Verified:** `after: ""` is *accepted* and silently
   returns **page 0**:
   ```
   no cursor  → newest starredAt 2026-07-14T05:20:06Z
   after: ""  → newest starredAt 2026-07-14T05:20:06Z   ← identical. Same page.
   ```
   A loop that pulls a cursor from a malformed response and passes it on **re-reads page 0 forever,
   inflating every star count, with nothing in the logs.** Hard abort. (`after:"garbage"` errors
   cleanly — only the *empty string* is dangerous.)
3. **Terminate on `hasNextPage == false`.** Not on empty edges.
4. **Retry with backoff, 5–8 attempts.** ~10% baseline failure on 400k+ star repos, 50–75% during
   GitHub degradation windows. Smaller pages don't help; retries do.

**Backfill everything, not a top-N.** Cost is `ceil(stars_gained_30d / 100)` and the long tail is 1
point each. A top-N cut would exclude exactly the product: the small repo that's exploding.

### Provenance: `.bf.csv`, forever
Reconstructed rows will **never** be "replaced by real snapshots" — **you cannot re-snapshot a past
date.** Those days stay approximate permanently. Without the marker you'd have a leaderboard mixing
measured and estimated history with no way to ever tell which is which.

**Caveat:** the connection holds only *current* stargazers, so this reconstructs *"of the people who
star this today, when did they star it"* — not the true historical count. Un-stars are retroactively
invisible (survivorship bias), so backfilled growth runs slightly high.

---

## 5. Actions

- **Cron is UTC**, `:17` deliberate (top-of-hour is the most congested slot). Scheduled runs are
  best-effort: 10–30 min late is normal. *"Every morning"* honestly means *"most mornings."* A hard
  guarantee means a real VPS and a real crontab.
- `concurrency:` + `git push || (git pull --rebase && git push)` — a manual run overlapping the cron
  makes the push non-fast-forward, and that day's CSV existed only on a runner that's now gone.
- **The 60-day auto-disable is not a threat.** Verified against a live repo running this exact
  architecture (`simonw/pge-outages`): last *human* commit 2024-02-05, bot commits and
  `event=schedule` runs still firing **29 months later**. Bot commits reset the timer.
  *The real risk:* if the crawl breaks and commits stop for 60 days, the workflow disables and needs
  a **manual** re-enable. **Don't filter the failure email.**
- **Pages: "Deploy from a branch"**, not the Actions Pages source — `GITHUB_TOKEN` pushes
  deliberately don't trigger workflows, so an Actions Pages deploy would never fire. **Day-1: confirm
  `pages-build-deployment` actually runs**, or the site freezes at day 1 while the pipeline stays green.

---

## Deliberately not building

No database. No server. No scraping (nothing to scrape — the word "crawler" is a misnomer).
No third-party data service. **No LLM classifier** (README keyword check is enough — proven).
No JS framework (CSS-only tabs). No sparklines/avatars. No BigQuery/GCP. No stars-band sharding
(a date bound replaces it). No un-star correction. No API, auth, or users.

## Rejected after testing

- **ClickHouse GH Archive mirror** — looked perfect, then: ~33k stars/day in April, **1.4k/day in
  July (~5% complete)**. Ranking on it would have been silently, plausibly wrong.
- **GH Archive raw files** — work (20 MB/hr, 14 GB per 30 days) but unnecessary now that GraphQL
  `starredAt` is confirmed open. **Keep as the escape hatch:** GitHub closed the REST door to stop
  harvesting, and it's not unreasonable the GraphQL door narrows next. The event firehose is unaffected.
- **REST `/repos/{o}/{r}/events`** — capped at 300 events / 90 days. On a hot repo that's ~3 hours.
- **github.com/trending** — no API. We compute a better one anyway.
