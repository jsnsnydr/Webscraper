#!/usr/bin/env python3
"""
substack_tool.py - collect Substack articles into JSON/CSV/JSONL and filter them.

Two subcommands:

  fetch   Pull articles from one or more publications into a data file.
  filter  Filter a previously saved file by title, text, date, author, etc.

Examples:
  python substack_tool.py fetch astralcodexten noahpinion -o articles.json
  python substack_tool.py fetch https://www.lennysnewsletter.com --full-text --since 2024-01-01 -o lenny.json
  python substack_tool.py filter articles.json --title "AI" --after 2024-06-01 --free -o ai.csv
  python substack_tool.py filter lenny.json --text "product market fit" --regex -o pmf.jsonl

Notes:
  * Uses Substack's unofficial public endpoints (/api/v1/archive and
    /api/v1/posts/<slug>). They are undocumented and may change.
  * Paywalled posts return only a preview body (if anything).
  * Be polite: the tool sleeps between requests. Respect each site's terms.
"""

import argparse
import csv
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

USER_AGENT = "Mozilla/5.0 (compatible; substack-tool/1.0)"
PAGE_SIZE = 12  # Substack's archive endpoint caps around this per request


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
STATS = {"requests": 0, "retries": 0, "throttle_wait": 0.0, "backoff_wait": 0.0}


class Throttle:
    """Spaces out requests; doubles the gap after a 429 and eases back after successes."""

    def __init__(self, base=0.5, cap=10.0):
        self.base, self.delay, self.cap, self.last = base, base, cap, 0.0

    def wait(self):
        gap = max(self.delay, self.base) - (time.time() - self.last)
        if gap > 0:
            STATS["throttle_wait"] += gap
            time.sleep(gap)
        self.last = time.time()

    def slow(self):
        self.delay = min(max(self.delay, self.base) * 2, self.cap)

    def ok(self):
        self.delay = max(self.base, self.delay * 0.9)


THROTTLE = Throttle()


def http_get_json(url, retries=8):
    """GET + parse JSON. On 429/5xx honors Retry-After, else backs off 5s..120s."""
    for attempt in range(retries):
        THROTTLE.wait()
        STATS["requests"] += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            THROTTLE.ok()
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                try:
                    wait = float(e.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    wait = min(5 * 2 ** attempt, 120)
                wait = min(wait, 300) + random.uniform(0, 1)
                if e.code == 429:
                    THROTTLE.slow()
                print(f"  HTTP {e.code}: waiting {wait:.0f}s "
                      f"(retry {attempt + 1}/{retries - 1})", file=sys.stderr)
                STATS["retries"] += 1
                STATS["backoff_wait"] += wait
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                pause = min(2 ** attempt, 30)
                STATS["retries"] += 1
                STATS["backoff_wait"] += pause
                time.sleep(pause)
                continue
            raise
    return None


def timing_summary(tool, target, n, wall, **extra):
    """Same schema as substack_crawler.py so compare_timing.py can line them up."""
    waits = STATS["throttle_wait"] + STATS["backoff_wait"]
    per = lambda x: round(x / n, 3) if n else None
    return {"tool": tool, "target": target, "articles": n,
            "wall_seconds": round(wall, 2),
            "deliberate_wait_seconds": round(waits, 2),
            "active_seconds": round(wall - waits, 2),
            "seconds_per_article_wall": per(wall),
            "seconds_per_article_active": per(wall - waits),
            "http_requests": STATS["requests"], "retries": STATS["retries"], **extra}


def normalize_base(pub):
    """Accept 'name', 'name.substack.com', or a full/custom-domain URL."""
    pub = pub.strip().rstrip("/")
    if pub.startswith("http://") or pub.startswith("https://"):
        return pub
    if "." in pub:
        return "https://" + pub
    return f"https://{pub}.substack.com"


# --------------------------------------------------------------------------
# HTML -> text
# --------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
             "blockquote", "pre", "tr"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html):
    if not html:
        return ""
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------
def parse_date(value):
    """Parse ISO timestamps or YYYY-MM-DD into an aware UTC datetime."""
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def build_record(post, base, publication):
    bylines = post.get("publishedBylines") or []
    authors = [b.get("name") for b in bylines if b.get("name")]
    return {
        "id": post.get("id"),
        "publication": publication,
        "title": post.get("title"),
        "subtitle": post.get("subtitle"),
        "authors": authors,
        "date": post.get("post_date"),
        "url": post.get("canonical_url") or f"{base}/p/{post.get('slug')}",
        "slug": post.get("slug"),
        "audience": post.get("audience"),  # 'everyone' or 'only_paid'
        "type": post.get("type"),          # newsletter, podcast, thread...
        "word_count": post.get("wordcount"),
        "likes": post.get("reaction_count"),
        "comments": post.get("comment_count"),
        "description": post.get("description"),
        "cover_image": post.get("cover_image"),
        "body_text": None,
    }


def fetch_bodies(records, delay=0.5):
    """Download bodies in place. Stops after 3 consecutive failures."""
    THROTTLE.base = delay
    fails = 0
    for rec in records:
        base = "https://" + rec["publication"]
        try:
            data = http_get_json(f"{base}/api/v1/posts/{rec['slug']}")
            if data:
                rec["body_text"] = html_to_text(data.get("body_html"))
            fails = 0
        except Exception as e:
            fails += 1
            print(f"body failed for {rec.get('url')}: {e}", file=sys.stderr)
            if fails >= 3:
                raise RuntimeError("3 consecutive failures; stopping. "
                                   "Run again later to resume.") from e


def fetch_publication(pub, limit, since, until, full_text, delay, search="",
                      on_batch=None):
    """on_batch(new_records) is called after every page so callers can save progress."""
    THROTTLE.base = delay
    base = normalize_base(pub)
    publication = re.sub(r"^https?://", "", base)
    records, offset = [], 0

    print(f"[{publication}] fetching archive...", file=sys.stderr)
    while limit is None or len(records) < limit:
        url = (f"{base}/api/v1/archive?sort=new&search={urllib.parse.quote(search)}"
               f"&offset={offset}&limit={PAGE_SIZE}")
        batch = http_get_json(url)
        if not batch:
            break

        start, stop = len(records), False
        for post in batch:
            dt = parse_date(post.get("post_date"))
            if dt and since and dt < since:
                stop = True  # archive is newest-first, so we can stop
                break
            if dt and until and dt > until:
                continue
            records.append(build_record(post, base, publication))
            if limit is not None and len(records) >= limit:
                stop = True
                break
        if on_batch and len(records) > start:
            on_batch(records[start:])
        if stop:
            break
        offset += PAGE_SIZE

    if full_text:
        print(f"[{publication}] downloading {len(records)} bodies...", file=sys.stderr)
        fetch_bodies(records, delay)

    print(f"[{publication}] collected {len(records)} posts", file=sys.stderr)
    return records


# --------------------------------------------------------------------------
# Inspecting raw API data
# --------------------------------------------------------------------------
KEY_HINTS = ("pangram", "detect", "disclos", "transparen", "statement",
             "generated", "howimake", "how_i", "assist", "humanwr")


def _flatten(obj, prefix=""):
    """Yield (path, value) for every nested key, e.g. 'publishedBylines[].name'."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            yield path, v
            yield from _flatten(v, path)
    elif isinstance(obj, list):
        for v in obj[:3]:
            yield from _flatten(v, prefix + "[]")


def _suspicious(path):
    leaf = path.split(".")[-1]
    tokens = [t.lower() for t in re.split(r"[_\W]+|(?<=[a-z])(?=[A-Z])", leaf) if t]
    return "ai" in tokens or any(h in leaf.lower() for h in KEY_HINTS)


def _shorten(obj):
    if isinstance(obj, dict):
        return {k: _shorten(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shorten(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 300:
        return obj[:300] + f"...[{len(obj)} chars]"
    return obj


def inspect_publication(pub, n, slugs, out):
    """Save raw archive + post JSON and report fields that might relate to AI detection."""
    base = normalize_base(pub)
    archive = http_get_json(f"{base}/api/v1/archive?sort=new&search=&offset=0&limit={n}") or []
    slugs = slugs or [p["slug"] for p in archive[:3] if p.get("slug")]
    posts = {sl: http_get_json(f"{base}/api/v1/posts/{sl}") for sl in slugs}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(_shorten({"archive": archive, "posts": posts}), f,
                  ensure_ascii=False, indent=2)

    def report(label, items):
        paths = {}
        for item in items:
            for path, val in _flatten(item):
                paths.setdefault(path, val)
        print(f"\n{label}: {len(paths)} distinct fields")
        hits = {p: v for p, v in paths.items() if _suspicious(p)}
        if hits:
            print("  Possibly relevant fields:")
            for p, v in hits.items():
                print(f"    {p} = {str(v)[:80]!r}")
        else:
            print("  No field names suggesting AI detection / disclosure statements.")

    report("Archive endpoint", archive)
    report("Single-post endpoint", [v for v in posts.values() if v])
    print(f"\nFull raw JSON (long text shortened) saved to {out}")


# --------------------------------------------------------------------------
# Reading / writing
# --------------------------------------------------------------------------
CSV_FIELDS = ["id", "publication", "title", "subtitle", "authors", "date", "url",
              "audience", "type", "word_count", "likes", "comments",
              "description", "body_text"]


def write_records(records, path):
    ext = path.lower().rsplit(".", 1)[-1] if "." in path else "json"
    if ext == "csv":
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in records:
                row = dict(r)
                row["authors"] = "; ".join(r.get("authors") or [])
                w.writerow(row)
    elif ext == "jsonl":
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(records)} records to {path}", file=sys.stderr)


def read_records(path):
    ext = path.lower().rsplit(".", 1)[-1] if "." in path else "json"
    if ext == "csv":
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            r["authors"] = [a for a in (r.get("authors") or "").split("; ") if a]
            for k in ("word_count", "likes", "comments"):
                r[k] = int(r[k]) if r.get(k) not in (None, "") else None
        return rows
    if ext == "jsonl":
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------
def make_matcher(patterns, use_regex, match_all):
    """Return fn(text) -> bool for one or more search terms."""
    if not patterns:
        return None
    flags = re.IGNORECASE
    compiled = [re.compile(p if use_regex else re.escape(p), flags) for p in patterns]
    combine = all if match_all else any
    return lambda text: combine(c.search(text or "") for c in compiled)


def apply_filters(records, a):
    since, until = parse_date(a.after), parse_date(a.before)
    if until and a.before and len(a.before) == 10:
        until = until.replace(hour=23, minute=59, second=59)  # inclusive day

    title_ok = make_matcher(a.title, a.regex, a.match_all)
    text_ok = make_matcher(a.text, a.regex, a.match_all)
    any_ok = make_matcher(a.query, a.regex, a.match_all)
    authors = [x.lower() for x in (a.author or [])]
    pubs = [x.lower() for x in (a.publication or [])]

    if (text_ok or any_ok) and not any(r.get("body_text") for r in records):
        print("Warning: no article bodies in this file; text search will only "
              "match titles/subtitles/descriptions. Re-run fetch with --full-text.",
              file=sys.stderr)

    out = []
    for r in records:
        dt = parse_date(r.get("date"))
        if since and (not dt or dt < since):
            continue
        if until and (not dt or dt > until):
            continue
        if title_ok and not title_ok(r.get("title")):
            continue
        if text_ok and not text_ok(r.get("body_text")):
            continue
        if any_ok and not any_ok(" ".join(str(r.get(k) or "") for k in
                                          ("title", "subtitle", "description", "body_text"))):
            continue
        if authors and not any(x in " ".join(r.get("authors") or []).lower() for x in authors):
            continue
        if pubs and not any(x in (r.get("publication") or "").lower() for x in pubs):
            continue
        if a.free and r.get("audience") != "everyone":
            continue
        if a.paid and r.get("audience") != "only_paid":
            continue
        if a.min_words is not None and (r.get("word_count") or 0) < a.min_words:
            continue
        if a.max_words is not None and (r.get("word_count") or 0) > a.max_words:
            continue
        if a.min_likes is not None and (r.get("likes") or 0) < a.min_likes:
            continue
        out.append(r)

    if a.sort:
        key = {"date": lambda r: r.get("date") or "",
               "likes": lambda r: r.get("likes") or 0,
               "words": lambda r: r.get("word_count") or 0,
               "title": lambda r: (r.get("title") or "").lower()}[a.sort]
        out.sort(key=key, reverse=not a.asc)
    if a.top:
        out = out[:a.top]
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Collect and filter Substack articles.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download articles from publications")
    f.add_argument("publications", nargs="+",
                   help="subdomain (e.g. noahpinion), domain, or URL")
    f.add_argument("-o", "--output", default="articles.json",
                   help="output file: .json, .jsonl or .csv (default articles.json)")
    f.add_argument("--limit", type=int, help="max posts per publication")
    f.add_argument("--since", help="only posts on/after YYYY-MM-DD")
    f.add_argument("--until", help="only posts on/before YYYY-MM-DD")
    f.add_argument("--search", default="",
                   help="only posts matching this term (applied by Substack)")
    f.add_argument("--full-text", action="store_true",
                   help="also download each article body (slower, enables text search)")
    f.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    f.add_argument("--timing-out", default="timing_api.json",
                   help="write a timing summary here (default timing_api.json)")

    i = sub.add_parser("inspect", help="dump raw API JSON and flag AI-detection-related fields")
    i.add_argument("publication")
    i.add_argument("--n", type=int, default=5, help="archive posts to sample (default 5)")
    i.add_argument("--slug", action="append",
                   help="inspect this specific post slug (repeatable), e.g. one you know has detection disabled")
    i.add_argument("-o", "--output", default="raw_samples.json")

    g = sub.add_parser("filter", help="filter a saved file")
    g.add_argument("input")
    g.add_argument("-o", "--output", help="write results here (.json/.jsonl/.csv); "
                                          "omit to print a summary")
    g.add_argument("--title", action="append", help="title contains (repeatable)")
    g.add_argument("--text", action="append", help="body contains (repeatable)")
    g.add_argument("--query", action="append",
                   help="title/subtitle/description/body contains (repeatable)")
    g.add_argument("--regex", action="store_true", help="treat search terms as regexes")
    g.add_argument("--match-all", action="store_true",
                   help="require ALL terms of a repeated flag (default: any)")
    g.add_argument("--author", action="append")
    g.add_argument("--publication", action="append")
    g.add_argument("--after", help="date on/after YYYY-MM-DD")
    g.add_argument("--before", help="date on/before YYYY-MM-DD")
    g.add_argument("--free", action="store_true", help="free posts only")
    g.add_argument("--paid", action="store_true", help="paywalled posts only")
    g.add_argument("--min-words", type=int)
    g.add_argument("--max-words", type=int)
    g.add_argument("--min-likes", type=int)
    g.add_argument("--sort", choices=["date", "likes", "words", "title"])
    g.add_argument("--asc", action="store_true", help="ascending sort (default descending)")
    g.add_argument("--top", type=int, help="keep only the first N results")

    args = ap.parse_args()

    if args.cmd == "fetch":
        t0 = time.perf_counter()
        since, until = parse_date(args.since), parse_date(args.until)
        if until and len(args.until) == 10:
            until = until.replace(hour=23, minute=59, second=59)
        all_records = []
        for pub in args.publications:
            try:
                all_records += fetch_publication(pub, args.limit, since, until,
                                                 args.full_text, args.delay, args.search)
            except Exception as e:  # keep going if one publication fails
                print(f"[{pub}] failed: {e}", file=sys.stderr)
        wall = time.perf_counter() - t0
        summary = timing_summary("api", ", ".join(args.publications), len(all_records),
                                 wall, full_text=bool(args.full_text))
        with open(args.timing_out, "w") as tf:
            json.dump(summary, tf, indent=2)
        print(f"\nFetch took {wall:.1f}s for {len(all_records)} articles "
              f"({summary['seconds_per_article_wall']}s/article; "
              f"{summary['deliberate_wait_seconds']}s of that was deliberate waiting). "
              f"Timing saved to {args.timing_out}", file=sys.stderr)
        write_records(all_records, args.output)

    elif args.cmd == "inspect":
        inspect_publication(args.publication, args.n, args.slug, args.output)

    else:
        results = apply_filters(read_records(args.input), args)
        if args.output:
            write_records(results, args.output)
        else:
            for r in results:
                print(f"{(r.get('date') or '')[:10]}  {r.get('title')}\n    {r.get('url')}")
            print(f"\n{len(results)} matching articles", file=sys.stderr)


if __name__ == "__main__":
    main()