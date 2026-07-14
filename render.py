#!/usr/bin/env python3
"""render.py - data/ -> index.html + DIGEST.md. Pure presentation. No network."""

import csv
import datetime
import glob
import html
import json
import os
import statistics
import sys

DATA = "data"
STARS = os.path.join(DATA, "stars")
WINDOWS = [(1, "today"), (7, "in 7d"), (30, "in 30d")]


# ---------- load ----------

def load_blocklist():
    try:
        with open(os.path.join(DATA, "blocklist.txt")) as f:
            return {ln.split("#")[0].strip() for ln in f} - {""}
    except OSError:
        return set()


def load_repos():
    try:
        with open(os.path.join(DATA, "repos.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_snapshots(blocked):
    """{date: {repo_id: stars}}, plus the set of dates that came from a .bf.csv."""
    snaps, backfilled = {}, set()
    for path in sorted(glob.glob(os.path.join(STARS, "*.csv"))):
        base = os.path.basename(path)
        date = base.split(".")[0]
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            continue
        bf = base.endswith(".bf.csv")
        if bf and date in snaps:
            continue  # a measured snapshot already claimed this date; it wins
        rows = {}
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                rid = (row.get("repo_id") or "").strip()
                if not rid or rid in blocked:
                    continue
                try:
                    rows[rid] = int(row["stars"])
                except (KeyError, TypeError, ValueError):
                    continue  # junk row, not a zero
        snaps[date] = rows
        if bf:
            backfilled.add(date)
        else:
            backfilled.discard(date)  # measured file overwrote the backfilled one
    return snaps, backfilled


# ---------- the delta math ----------

def deltas(snaps, rid, end, days):
    """(median daily delta, total) over the N-day window ending `end`, or None.

    Only calendar-adjacent day pairs count. A missing snapshot file, or a repo
    missing from one day's CSV, kills the pairs that touch it - it is never
    interpolated across and never read as 0. No pairs -> no delta -> the repo is
    omitted from that window entirely, which is the only honest answer.
    """
    diffs = []
    for i in range(days):
        b = end - datetime.timedelta(days=i)
        a = b - datetime.timedelta(days=1)
        prev = snaps.get(a.isoformat(), {})
        cur = snaps.get(b.isoformat(), {})
        if rid in prev and rid in cur:
            diffs.append(cur[rid] - prev[rid])
    if not diffs:
        return None
    return statistics.median(diffs), sum(diffs)


def growth(repos, snaps, end, days):
    """[(repo_id, total)] ranked by median daily delta. Spikes rank low, growth ranks high."""
    out = []
    for rid in repos:
        d = end and deltas(snaps, rid, end, days)
        if not d:
            continue
        med, total = d
        if med < 0 or total <= 0:
            continue  # spam purges shed stars; nothing negative goes under "growing"
        out.append((med, total, rid))
    out.sort(reverse=True)
    return [(rid, total) for _, total, rid in out]


def created(repo):
    try:
        return datetime.datetime.strptime(repo["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        return None


def brand_new(repos, snaps, latest, now):
    """Repos created in the last 7 days, ranked by stars. No delta math."""
    cut = now - datetime.timedelta(days=7)
    out = []
    for rid, repo in repos.items():
        c = created(repo)
        if c is None or c < cut:
            continue
        out.append((snaps.get(latest, {}).get(rid, 0), rid))
    out.sort(reverse=True)
    return [rid for _, rid in out]


# ---------- render ----------

def num(n):
    return "{:,}".format(n)


def card(rid, repos, snaps, latest, note, now):
    r = repos.get(rid, {})
    name = r.get("name", rid)
    stars = snaps.get(latest, {}).get(rid)
    c = created(r)
    meta = [x for x in (
        r.get("lang"),
        ", ".join(r.get("topics") or [])[:60],
        "%d days old" % (now - c).days if c else "",
    ) if x]
    e = html.escape
    return (
        '<article><h3><a href="https://github.com/%s">%s</a></h3>'
        '<p class="d">%s</p><p class="m"><span class="s">%s%s</span>%s</p></article>'
    ) % (
        e(name), e(name),
        e(r.get("desc") or ""),
        "&#9733; %s" % num(stars) if stars is not None else "",
        '<span class="g">%s</span>' % e(note) if note else "",
        "".join("<span>%s</span>" % e(m) for m in meta),
    )


CSS = """
:root{--bg:#fff;--fg:#111;--dim:#666;--line:#e5e5e5;--acc:#0a5;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--dim:#999;--line:#2a2a2a;--acc:#3d9;--card:#181818}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1rem 4rem;background:var(--bg);color:var(--fg);line-height:1.5;
 font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:52rem;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 .25rem}
.sub{color:var(--dim);margin:0 0 2rem}
input[type=radio]{position:absolute;opacity:0;width:0;height:0}
nav{display:flex;flex-wrap:wrap;gap:.5rem;border-bottom:1px solid var(--line);margin-bottom:2rem}
nav label{padding:.5rem .75rem;cursor:pointer;color:var(--dim);border-bottom:2px solid transparent;margin-bottom:-1px}
nav label:hover{color:var(--fg)}
.p{display:none}
#q{width:100%;padding:.5rem .75rem;margin:0 0 1rem;font:inherit;color:var(--fg);
 background:var(--card);border:1px solid var(--line);border-radius:6px}
article{padding:1.25rem 0;border-bottom:1px solid var(--line)}
h3{margin:0;font-size:1.05rem}
a{color:var(--fg)}
.d{margin:.35rem 0 .6rem;color:var(--dim)}
.m{margin:0;display:flex;flex-wrap:wrap;gap:.75rem;font-size:.875rem;color:var(--dim)}
.s{color:var(--fg)}
.g{color:var(--acc);margin-left:.5rem;font-weight:600}
.empty{color:var(--dim);padding:2rem 0}
footer{max-width:52rem;margin:3rem auto 0;padding-top:1.5rem;border-top:1px solid var(--line);
 color:var(--dim);font-size:.8125rem}
"""


# Subsequence match, like fzf: "opnai" matches "openai". Cards carry their own text,
# so the haystack is just textContent - no index, no search field to keep in sync.
SEARCH_JS = """
var q=document.getElementById('q'),arts=[].slice.call(document.querySelectorAll('article')),
 keys=arts.map(function(a){return a.textContent.toLowerCase()});
function hit(k,t){for(var i=0,j=0;j<t.length;j++){i=k.indexOf(t[j],i)+1;if(!i)return false}return true}
q.addEventListener('input',function(){
 var t=q.value.toLowerCase().replace(/\\s+/g,'');
 arts.forEach(function(a,i){a.hidden=!!t&&!hit(keys[i],t)})});
"""


def html_page(sections, repos, snaps, latest, approx, now):
    ids = list(range(len(sections)))
    css = CSS + "".join(
        "#t%d:checked~.panels #p%d{display:block}"
        "#t%d:checked~nav label[for=t%d]{color:var(--fg);border-bottom-color:var(--acc)}"
        "#t%d:focus-visible~nav label[for=t%d]{outline:2px solid var(--acc)}" % (i, i, i, i, i, i)
        for i in ids
    )
    body = []
    for i, (title, rows) in enumerate(sections):
        cards = "".join(card(rid, repos, snaps, latest, note, now) for rid, note in rows)
        body.append('<section class="p" id="p%d">%s</section>'
                    % (i, cards or '<p class="empty">No snapshots yet.</p>'))
    note = ("<br>Some days are reconstructed history (approximate: un-stars are invisible, "
            "so backfilled growth runs slightly high)." if approx else "")
    return (
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>AI repos by star growth</title><style>%s</style>"
        "<main><h1>AI repos by star growth</h1>"
        '<p class="sub">Ranked by median daily star delta - one spike does not make a trend.</p>'
        "%s<nav>%s</nav>"
        '<input type="search" id="q" placeholder="Filter repos" aria-label="Filter repos">'
        '<div class="panels">%s</div></main>'
        "<footer>Generated %s UTC &middot; %s repos tracked &middot; %s snapshots%s</footer>"
        "<script>%s</script>"
    ) % (
        css,
        "".join('<input type="radio" name="tab" id="t%d"%s>' % (i, " checked" if i == 0 else "")
                for i in ids),
        "".join('<label for="t%d">%s</label>' % (i, html.escape(t)) for i, (t, _) in enumerate(sections)),
        "".join(body),
        now.strftime("%Y-%m-%d %H:%M"), num(len(repos)), num(len(snaps)), note,
        SEARCH_JS,
    )


def digest(sections, repos, snaps, latest, approx, now):
    out = ["# AI repos - %s\n" % now.strftime("%Y-%m-%d")]
    for title, rows in sections:
        out.append("## %s\n" % title)
        if not rows:
            out.append("_No snapshots yet._\n")
            continue
        for rid, note in rows[:10]:
            r = repos.get(rid, {})
            stars = snaps.get(latest, {}).get(rid)
            bits = [b for b in (
                "%s stars" % num(stars) if stars is not None else "",
                note, r.get("lang"), ", ".join(r.get("topics") or []),
            ) if b]
            out.append("- **[%s](https://github.com/%s)** - %s  \n  %s\n"
                       % (r.get("name", rid), r.get("name", rid), r.get("desc") or "", " - ".join(bits)))
        out.append("")
    if approx:
        out.append("\n_Some days are reconstructed history; backfilled growth runs slightly high._\n")
    return "\n".join(out)


def main():
    blocked = load_blocklist()
    repos = {k: v for k, v in load_repos().items() if k not in blocked}
    snaps, backfilled = load_snapshots(blocked)
    now = datetime.datetime.utcnow()
    latest = max(snaps) if snaps else None
    end = datetime.date.fromisoformat(latest) if latest else None

    d24 = dict(growth(repos, snaps, end, 1))
    sections = [("Brand new", [(rid, "+%s today" % num(d24[rid]) if rid in d24 else "new")
                               for rid in brand_new(repos, snaps, latest, now)])]
    # A "trending this month" tab would be identical to the 30d window — same data,
    # same ranking, no age filter. One list, one tab.
    for days, label in WINDOWS:
        title = {1: "Last 24 hours", 7: "This week", 30: "This month"}[days]
        sections.append((title, [(rid, "+%s %s" % (num(t), label))
                                 for rid, t in growth(repos, snaps, end, days)]))

    used = {(end - datetime.timedelta(days=i)).isoformat() for i in range(31)} if end else set()
    approx = bool(used & backfilled)
    with open("index.html", "w") as f:
        f.write(html_page(sections, repos, snaps, latest, approx, now))
    with open("DIGEST.md", "w") as f:
        f.write(digest(sections, repos, snaps, latest, approx, now))
    print("wrote index.html + DIGEST.md (%d repos, %d snapshots)" % (len(repos), len(snaps)))


# ---------- the one test ----------

def _selftest():
    end = datetime.date(2026, 7, 10)
    day = lambda n: (end - datetime.timedelta(days=n)).isoformat()  # n days ago
    snaps = {day(i): {"steady": 1000 - 10 * i, "spiky": 500} for i in range(8)}
    snaps[day(0)]["spiky"] = 1400  # one HN spike yesterday->today, +900 in a day

    # normal series: +10/day for 7 days
    assert deltas(snaps, "steady", end, 7) == (10, 70)
    assert deltas(snaps, "steady", end, 1) == (10, 10)  # 24h: median of one diff

    # the whole point: the spike's total (900) dwarfs steady's (70), but its median is 0,
    # so ranking by median puts the sustained grower first. Endpoint subtraction would not.
    assert deltas(snaps, "spiky", end, 7) == (0, 900)
    assert [r for r, _ in growth(snaps_repos(snaps), snaps, end, 7)] == ["steady", "spiky"]

    # missing day in the middle -> NOT interpolated across
    gap = {day(2): {"a": 100}, day(0): {"a": 300}}  # day(1) file never landed
    assert deltas(gap, "a", end, 2) is None  # not (100, 200) and not 200
    assert deltas(gap, "a", end, 30) is None  # a longer window does not rescue it
    assert deltas({}, "a", end, 30) is None

    # missing row (repo went private today) -> absent, never 0
    gone = {day(i): {"a": 1000 - 10 * i} for i in range(1, 8)}
    gone[day(0)] = {}  # 'a' has no row today
    assert deltas(gone, "a", end, 1) is None  # NOT 0 - 990
    med, total = deltas(gone, "a", end, 7)
    assert med == 10 and total == 60 > 0  # the days it did exist still count, no fake -990

    # a negative delta never reaches a growth list (GitHub purged the spam stars)
    purged = {day(i): {"spam": 1000 + 700 * i, "ok": 1000 - 10 * i} for i in range(8)}
    assert deltas(purged, "spam", end, 7) == (-700, -4900)
    assert [r for r, _ in growth(snaps_repos(purged), purged, end, 7)] == ["ok"]

    # brand new = created within 7d, ranked by stars
    now = datetime.datetime(2026, 7, 10, 12, 0, 0)
    mk = lambda n: {"created_at": (now - datetime.timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    repos = {"fresh": mk(3), "old": mk(9), "edge": mk(6.9), "bad": {"created_at": "?"}}
    last = {day(0): {"fresh": 50, "edge": 900}}
    assert brand_new(repos, last, day(0), now) == ["edge", "fresh"]  # 'old' out, 'bad' out, stars desc

    print("ok")


def snaps_repos(snaps):
    return {rid: {} for rows in snaps.values() for rid in rows}


if __name__ == "__main__":
    if "--test" in sys.argv:
        _selftest()
    else:
        main()
