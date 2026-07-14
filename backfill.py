#!/usr/bin/env python3
"""Reconstruct the last N days of star history from the GraphQL stargazers connection.

ONE-SHOT, RUN LOCALLY -- not in CI. It takes hours and eats the hourly GraphQL budget.

    GITHUB_TOKEN=$(gh auth token) python3 backfill.py --days 30

Resumable: progress is appended to data/backfill_progress.jsonl. Re-run after a crash
(or a bad night on GitHub's side) and it picks up where it stopped.

HONEST CAVEAT -- read this before trusting the numbers. The stargazers connection holds
only *current* stargazers, so this reconstructs "of the people who star this repo today,
when did they star it" -- NOT the true historical count. Un-stars are retroactively
invisible (survivorship bias), so reconstructed growth runs slightly high. Over 30 days
the drift is small, and for ranking star acquisition it is arguably the better metric --
but it is not a true historical curve. Output is therefore written as YYYY-MM-DD.bf.csv,
never plain .csv: you can never re-snapshot a past date, so backfilled days stay
approximate forever and must stay labelled as such.
"""
import csv, json, os, sys, time, urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

API = "https://api.github.com/graphql"
STARS = "data/stars"
PROGRESS = "data/backfill_progress.jsonl"
TOKEN = os.environ.get("GITHUB_TOKEN")

# One repo per request. Do NOT alias repos into batches -- the stargazers connection is
# server-expensive and 502s under load. This is an overnight job; throughput is irrelevant.
QUERY = """
query($owner:String!, $name:String!, $after:String) {
  rateLimit { remaining }
  repository(owner:$owner, name:$name) {
    stargazerCount
    stargazers(first:100, after:$after, orderBy:{field:STARRED_AT, direction:DESC}) {
      edges { starredAt }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


class Skip(Exception):
    """This repo is a write-off. Log it, move on, never kill the run."""


def gql(owner, name, after):
    body = json.dumps({"query": QUERY, "variables": {"owner": owner, "name": name, "after": after}}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": "bearer " + TOKEN,
        "Content-Type": "application/json",
        "User-Agent": "github-crawler-backfill",
    })
    for attempt in range(7):  # ~10% baseline failure on huge repos, 50%+ during degradation. Retries fix it; smaller pages don't.
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            data = json.loads(raw)  # GUARD 1: the 502 mode returns raw nginx HTML, so there is no `errors` key to check. Parse first.
            errs = data.get("errors")
            if errs:
                if errs[0].get("type") == "NOT_FOUND":
                    raise Skip("not found (deleted/private)")
                raise ValueError(errs[0].get("message", "graphql error"))
            return data["data"]
        except Skip:
            raise
        except Exception as e:
            if attempt == 6:
                raise Skip(repr(e)[:120])
            time.sleep(2 ** attempt)


def walk(owner, name, cutoff):
    """Newest-first walk, stopping once we pass `cutoff`. Returns (stars_now, Counter{date: stars}, pages)."""
    counts, cursor, pages = Counter(), None, 0
    while True:
        data = gql(owner, name, cursor)
        repo = data.get("repository")
        if not repo:
            raise Skip("repository is null")
        conn = repo["stargazers"]
        pages += 1
        for edge in conn["edges"]:
            ts = datetime.strptime(edge["starredAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if ts < cutoff:
                return repo["stargazerCount"], counts, pages
            counts[ts.date().isoformat()] += 1
        if not conn["pageInfo"]["hasNextPage"]:  # GUARD 3: terminate here, not on empty edges.
            return repo["stargazerCount"], counts, pages
        cursor = conn["pageInfo"]["endCursor"]
        if not cursor:  # GUARD 2: `after:""` is ACCEPTED and silently re-serves page 0 -- forever, double-counting, silently. Never "start over".
            raise Skip("empty endCursor")
        if data["rateLimit"]["remaining"] < 50:
            print("  rate limit low, sleeping 60s")
            time.sleep(60)


def load_repos():
    if not os.path.exists("data/repos.json"):
        sys.exit("data/repos.json not found -- run crawl.py first to discover repos.")
    raw = json.load(open("data/repos.json"))
    items = raw.items() if isinstance(raw, dict) else [(r["id"], r) for r in raw]
    repos = []
    for rid, r in items:
        full = r.get("full_name") or r.get("name") or ""
        if "/" not in full:
            print("skipping %s: no owner/name in %r" % (rid, full))
            continue
        repos.append((int(rid), full))
    return sorted(repos)


def pivot(done, today, days):
    """Per-repo star dates -> per-date rows. stars_end_of(d) = stars_now - (stars gained after d)."""
    dates = [(today - timedelta(days=n)).isoformat() for n in range(1, days + 1)]  # yesterday backwards; today is crawl.py's job
    rows = defaultdict(list)
    for rid, rec in done.items():
        running = rec["total"] - rec["counts"].get(today.isoformat(), 0)  # = end of yesterday
        for d in dates:
            rows[d].append((int(rid), max(running, 0)))
            running -= rec["counts"].get(d, 0)  # peel off that day's gains -> end of the day before
    return dates, rows


def main():
    if not TOKEN:
        sys.exit("GITHUB_TOKEN not set. Try: export GITHUB_TOKEN=$(gh auth token)")
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 30
    today = datetime.now(timezone.utc).date()
    cutoff = datetime.combine(today - timedelta(days=days), datetime.min.time(), tzinfo=timezone.utc)
    repos = load_repos()

    done = {}
    if os.path.exists(PROGRESS):
        for line in open(PROGRESS):
            rec = json.loads(line)
            done[rec["id"]] = rec
        print("resuming: %d repos already backfilled" % len(done))

    with open(PROGRESS, "a") as fh:
        for i, (rid, full) in enumerate(repos, 1):
            if rid in done:
                continue
            owner, name = full.split("/", 1)
            try:
                total, counts, pages = walk(owner, name, cutoff)
            except Skip as e:
                print("[%d/%d] %s -- SKIPPED: %s" % (i, len(repos), full, e))
                continue
            rec = {"id": rid, "total": total, "counts": dict(counts)}
            done[rid] = rec
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print("[%d/%d] %s -- %d pages, %d stars in %dd" % (i, len(repos), full, pages, sum(counts.values()), days))

    os.makedirs(STARS, exist_ok=True)
    dates, rows = pivot(done, today, days)
    for d in dates:
        bf = "%s/%s.bf.csv" % (STARS, d)
        if os.path.exists("%s/%s.csv" % (STARS, d)):  # a real snapshot is truth; never shadow it
            print("%s: real snapshot exists, skipping" % d)
            if os.path.exists(bf):
                os.remove(bf)  # an earlier run guessed at a day we have since actually recorded
            continue
        with open(bf, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["repo_id", "stars"])
            w.writerows(sorted(rows[d]))
    print("done: %d repos, %d dates" % (len(done), len(dates)))


def selftest():
    today = datetime(2026, 7, 11, tzinfo=timezone.utc).date()
    done = {7: {"total": 100, "counts": {"2026-07-11": 5, "2026-07-10": 3, "2026-07-09": 2}}}
    dates, rows = pivot(done, today, 3)
    assert dates == ["2026-07-10", "2026-07-09", "2026-07-08"], dates
    assert rows["2026-07-10"] == [(7, 95)], rows["2026-07-10"]  # 100 now, 5 starred today
    assert rows["2026-07-09"] == [(7, 92)]
    assert rows["2026-07-08"] == [(7, 90)]
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
