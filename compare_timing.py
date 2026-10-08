#!/usr/bin/env python3
"""Compare two timing files (default: timing_api.json vs timing_crawler.json).

  python compare_timing.py
  python compare_timing.py timing_api.json timing_crawler.json
"""
import json
import sys

a_path, b_path = (sys.argv[1:3] + ["timing_api.json", "timing_crawler.json"][len(sys.argv[1:3]):])
A, B = json.load(open(a_path)), json.load(open(b_path))

ROWS = [("articles", "Articles"), ("wall_seconds", "Total time (s)"),
        ("deliberate_wait_seconds", "Deliberate waiting (s)"),
        ("active_seconds", "Active time (s)"),
        ("seconds_per_article_wall", "Sec / article (total)"),
        ("seconds_per_article_active", "Sec / article (active)")]

print(f"{'':28}{A['tool']:>20}{B['tool']:>22}{'ratio B/A':>12}")
for key, label in ROWS:
    a, b = A.get(key), B.get(key)
    ratio = f"{b / a:.1f}x" if isinstance(a, (int, float)) and a and isinstance(b, (int, float)) else ""
    print(f"{label:28}{a!s:>20}{b!s:>22}{ratio:>12}")

for key, label in [("http_requests", "HTTP requests"), ("retries", "Retries (429s etc.)"),
                   ("discovery_seconds", "Finding posts (s)"),
                   ("browser_startup_seconds", "Browser startup (s)"),
                   ("page_load_seconds_total", "Page loads, total (s)"),
                   ("scan_seconds_total", "AI-scan step, total (s)")]:
    if key in A or key in B:
        print(f"{label:28}{A.get(key, '-')!s:>20}{B.get(key, '-')!s:>22}")

print("\nProjected total time at each tool's measured pace (linear; ignores throttling changes):")
for n in (1486, 10000, 50000):
    pa, pb = (x.get("seconds_per_article_wall") for x in (A, B))
    if pa and pb:
        print(f"  {n:>6,} articles:  {A['tool']} {n * pa / 3600:6.1f} h   |   {B['tool']} {n * pb / 3600:6.1f} h")

if B.get("scan_enabled") is False:
    print("\nNote: the crawler ran with --no-scan, so this compares page text only.")
if A.get("full_text") is False:
    print("Note: the API run had no --full-text, so it fetched metadata only "
          "(not comparable to a crawler that reads every body). Re-run with --full-text.")
