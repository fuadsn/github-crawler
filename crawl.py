"""Discover AI repos and snapshot their star counts. `python crawl.py [--full]`"""

import base64
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

TOKEN = os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("GITHUB_TOKEN is not set")

REPOS_JSON = "data/repos.json"
STARS_DIR = "data/stars"
BLOCKLIST = "data/blocklist.txt"

BASE = "fork:false archived:false"

TOPICS = """llm llms large-language-models ai artificial-intelligence generative-ai ai-agent
ai-agents agent agents agentic-ai multi-agent rag mcp model-context-protocol claude claude-code
anthropic openai chatgpt gpt gemini ollama langchain chatbot agent-skills prompt-engineering
llm-inference diffusion-models stable-diffusion transformers embeddings""".split()

KEYWORDS = ["llm", "agent", "ai", "gpt", "rag", "mcp"]

WORDS = """ai llm llms gpt agent agents agentic neural rag mcp prompt claude openai gemini
diffusion transformer embedding chatbot inference multimodal anthropic ollama nlp gguf llama
fine-tuning quantization rlhf huggingface pytorch vlm moe reasoning""".split()

# Word boundaries, not substrings: bare "ai" matches "available", "domain", "explain".
WORD_RE = re.compile(r"\b(" + "|".join(WORDS) + r")\b", re.I)


def get(url, body=None):
    """Retries transient failures. A full discovery run is ~600 requests over 20 minutes,
    so a socket timeout somewhere in there is close to certain — and without this, one
    blip loses the entire run's work. HTTPError is re-raised: callers handle 403/422.
    """
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-crawler",
    })
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise
        except Exception as e:  # socket.timeout, URLError, ssl, connection reset
            if attempt == 4:
                raise
            print("  retry %d/4 (%s)" % (attempt + 1, type(e).__name__))
            time.sleep(2 ** attempt)


# --- discovery -------------------------------------------------------------

def search(q, lo, hi):
    """Search results are silently truncated at 1000, so halve the created: window until
    each slice fits. A single 90d bound does NOT fit: `ai in:name,description` = 6714."""
    full = "%s %s created:%s..%s" % (q, BASE, lo, hi)
    out = []
    for page in range(1, 11):  # 10 pages x 100 = the 1000 cap
        url = "https://api.github.com/search/repositories?q=%s&per_page=100&page=%d" % (
            urllib.parse.quote(full), page)
        time.sleep(2.2)  # search API: 30 req/min
        try:
            r = json.loads(get(url))
        except urllib.error.HTTPError as e:
            print("  search failed (%s): %s" % (e.code, full))
            break
        if page == 1 and r.get("total_count", 0) >= 1000:
            if lo < hi:
                mid = lo + (hi - lo) // 2
                return search(q, lo, mid) + search(q, mid + timedelta(days=1), hi)
            print("  truncated, single day over cap: %s" % full)
        out += r["items"]
        if len(r["items"]) < 100:
            break
    return out


def top(q, pages=3):
    """Established repos, sliced by rank instead of date.

    Every other pass is `created:`-bounded to dodge the 1000-result cap, but that also
    means nothing older than the window can ever enter the tracked set — so a long-lived
    repo could never show up as re-accelerating. Sorting by stars and taking the top N
    is bounded by construction, so the cap is irrelevant.
    """
    out = []
    for page in range(1, pages + 1):
        url = ("https://api.github.com/search/repositories?q=%s&sort=stars&order=desc"
               "&per_page=100&page=%d" % (urllib.parse.quote("%s %s" % (q, BASE)), page))
        time.sleep(2.2)
        try:
            r = json.loads(get(url))
        except urllib.error.HTTPError as e:
            print("  search failed (%s): %s" % (e.code, q))
            break
        out += r["items"]
        if len(r["items"]) < 100:
            break
    return out


def words_in(text):
    return {m.group(0).lower() for m in WORD_RE.finditer(text or "")}


def readme_words(full_name):
    try:
        r = json.loads(get("https://api.github.com/repos/%s/readme" % full_name))
        text = base64.b64decode(r.get("content", "")).decode("utf-8", "replace")
        return words_in(text[:6000])
    except Exception:
        return set()


def is_ai(item):
    text = "%s %s %s" % (item["full_name"], item.get("description") or "",
                         " ".join(item.get("topics") or []))
    if words_in(text):
        return True
    # Frontier releases routinely have a useless description ("Infinite Worlds with Versatile
    # Interactions" = a world model). Never drop on description-miss alone; the README decides.
    return len(readme_words(item["full_name"])) >= 2


def discover(full, blocked):
    today = datetime.now(timezone.utc).date()
    hits, velocity = {}, {}

    if full:
        for t in TOPICS:
            print("topic:%s" % t)
            for it in search("topic:%s stars:>10" % t, today - timedelta(days=90), today):
                hits[it["id"]] = it
        for k in KEYWORDS:
            print("keyword:%s" % k)
            for it in search("%s in:name,description stars:>10" % k,
                             today - timedelta(days=90), today):
                hits[it["id"]] = it
        for t in TOPICS:  # established repos, so an older one can re-accelerate
            print("top:%s" % t)
            for it in top("topic:%s stars:>100" % t):
                hits[it["id"]] = it

    print("velocity")
    for it in search("stars:>25", today - timedelta(days=7), today):
        velocity[it["id"]] = it
    for rid, it in velocity.items():
        if rid not in hits and is_ai(it):  # no AI filter in the query, so classify here
            hits[rid] = it

    keep = {}
    for rid, it in hits.items():
        if str(rid) in blocked:
            continue
        if len(it.get("topics") or []) > 15:  # topic-stuffing = star-farm ring
            continue
        keep[str(rid)] = {
            "node_id": it["node_id"],
            "name": it["full_name"],
            "desc": it.get("description") or "",
            "lang": it.get("language") or "",
            "topics": it.get("topics") or [],
            "created_at": it["created_at"],
        }
    return keep


# --- snapshot --------------------------------------------------------------

def graphql(query):
    for attempt in range(5):
        try:
            body = json.dumps({"query": query}).encode()
            return json.loads(get("https://api.github.com/graphql", body))
        except Exception as e:
            # A 502 returns raw nginx HTML, so this catches the JSON parse too.
            print("  graphql retry %d: %s" % (attempt + 1, e))
            time.sleep(2 ** attempt)
    return None


def snapshot(repos):
    """Query by node_id only. repository(owner:,name:) resolves the *current* occupant of a
    path: for ry/deno that is a squatting fork with 447 stars, not denoland/deno's 107k."""
    ids = [r["node_id"] for r in repos.values() if r.get("node_id")]
    rows, nulls = [], 0
    for i in range(0, len(ids), 100):  # 100 ids = 1 request = 1 point
        q = "{nodes(ids:%s){... on Repository{databaseId nameWithOwner stargazerCount}}}" % (
            json.dumps(ids[i:i + 100]))
        r = graphql(q)
        if r is None or r.get("data") is None:
            sys.exit("graphql: data is null, aborting before we write a bad snapshot")
        # A deleted/private repo returns HTTP 200 + an `errors` array + a null node. Raising on
        # `errors` would throw away the whole night's snapshot, and that history is unbuyable.
        for n in r["data"]["nodes"]:
            if not n or n.get("databaseId") is None:
                nulls += 1  # missing repo -> absent row. Never 0: that reads as a -140k delta.
                continue
            rows.append((n["databaseId"], n["stargazerCount"]))  # id GitHub RETURNS, not ours
        print("  snapshot %d/%d" % (min(i + 100, len(ids)), len(ids)))

    rows.sort(key=lambda r: r[0])  # integer sort: halves the git pack size
    path = "%s/%s.csv" % (STARS_DIR, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["repo_id", "stars"])
        w.writerows(rows)
    return path, len(rows), nulls


# --- main ------------------------------------------------------------------

def main():
    os.makedirs(STARS_DIR, exist_ok=True)
    blocked = set()
    if os.path.exists(BLOCKLIST):
        for line in open(BLOCKLIST):
            line = line.split("#")[0].strip()
            if line:
                blocked.add(line)

    repos = {}
    if os.path.exists(REPOS_JSON):
        with open(REPOS_JSON) as f:
            repos = json.load(f)

    found = discover("--full" in sys.argv, blocked)
    repos.update(found)  # merge: never lose a repo, refresh metadata in place
    for rid in blocked:
        repos.pop(rid, None)
    with open(REPOS_JSON, "w") as f:
        json.dump(repos, f, indent=1, sort_keys=True)

    path, n, nulls = snapshot(repos)
    print("discovered %d | tracked %d | snapshotted %d | skipped-null %d | %s"
          % (len(found), len(repos), n, nulls, path))


def selftest():
    assert words_in("available domain explain") == set()
    assert words_in("An AI agent") == {"ai", "agent"}
    assert len(words_in("uses PyTorch for LLM inference")) == 3
    print("ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
