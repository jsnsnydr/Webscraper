#!/usr/bin/env python3
"""
substack_crawler.py - browser-based (Selenium) collector with timing, for comparing
against substack_tool.py (the API approach).

Setup:   pip install selenium      (also needs Google Chrome; Selenium downloads the driver)
Run:     python substack_crawler.py astralcodexten --limit 25 --no-scan     # pages only
         python substack_crawler.py astralcodexten --limit 25
                                                                              # + AI scan; logs in once, then
                                                                              #   stays logged in (./chrome-profile)
         python substack_crawler.py astralcodexten --login                    # force a fresh sign-in

What it does per post:
  1. Loads the article page in Chrome and extracts title, date, authors, body text.
  2. Unless --no-scan: clicks the author on the article, opens Posts, then clicks the post into Reader, then uses the More menu to click
     "Scan for AI", and records the Pangram percentages plus any new text the scan shows
     (where a transparency statement would appear) or an "unavailable" status.

PARTLY VERIFIED: the scan-result panel parsing (verdict, percentages, transparency note,
author, summary line) and the article-page extraction are built from real HTML (one
panel, one article page). NOT verified: where the scan button sits on the article page and its
text, and what the panel looks like for unavailable / too-short / no-note cases. Run with --debug on a few posts (saves
screenshots + HTML) and adjust SCAN_* / PANEL_CSS below if a step fails.

Timing: everything is timed and written to timing_crawler.json in the same schema as
substack_tool.py's timing_api.json. Compare with:  python compare_timing.py
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

USER_AGENT = "Mozilla/5.0 (compatible; substack-crawler/1.0)"
SCAN_BUTTON_TEXT = "Scan for AI"          # visible text of the button (unverified)
PANEL_CSS = '[data-modal-role="body"]'   # body of the scan-result modal (from real HTML)
RX_LABEL_FIRST = re.compile(r"\b(human|ai[- ]assisted|ai)\b[ \t]*:?[ \t]*(\d{1,3})[ \t]*%", re.I)  # "AI 64%"
RX_NUM_FIRST = re.compile(r"(\d{1,3})[ \t]*%[ \t]*(human|ai[- ]assisted|ai)\b", re.I)         # "64% AI"
SCAN_RESULT_RE = [RX_LABEL_FIRST, RX_NUM_FIRST]
LABEL_LINE = re.compile(r"^(human|ai[- ]assisted|ai)$", re.I)
VALUE_LINE = re.compile(r"^(\d{1,3})\s*%$")
LABELS = {"human": "pct_human", "ai-assisted": "pct_ai_assisted", "ai": "pct_ai"}


def host_of(pub):
    pub = re.sub(r"^https?://", "", pub.strip().rstrip("/"))
    return pub if "." in pub else f"{pub}.substack.com"


def discover(host, limit):
    """Post URLs from the site's sitemap (newest first). Returns (urls, seconds)."""
    t = time.perf_counter()
    req = urllib.request.Request(f"https://{host}/sitemap.xml",
                                 headers={"User-Agent": USER_AGENT})
    xml = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    urls = [u for u in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml) if "/p/" in u]
    return (urls[:limit] if limit else urls), time.perf_counter() - t


def make_driver(args, headless=None):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    headless = (not args.show) if headless is None else headless
    o = Options()
    if args.attach:
        # Attach to the already-running Chrome that owns the authenticated session.
        o.debugger_address = args.debugger_address
    else:
        if headless:
            o.add_argument("--headless=new")
        o.add_argument("--window-size=1280,1800")
        o.add_argument(f"--user-data-dir={os.path.abspath(args.profile_dir)}")
        o.add_argument("--no-first-run")
        o.add_argument("--no-default-browser-check")
        o.add_argument("--disable-session-crashed-bubble")
        o.add_argument("--remote-debugging-port=0")
    if not args.load_images and not args.attach:
        o.add_experimental_option(
            "prefs", {"profile.managed_default_content_settings.images": 2})
    return webdriver.Chrome(options=o)


def close_driver(driver, args):
    """Close ChromeDriver without closing an externally managed Chrome."""
    if args.attach:
        try:
            driver.service.stop()
        except Exception:
            pass
    else:
        driver.quit()


def is_logged_in(driver):
    """True if the profile holds a Substack session cookie."""
    driver.get("https://substack.com/")
    time.sleep(1)
    return any(c["name"] in ("substack.sid", "connect.sid") for c in driver.get_cookies())


def inject_sid(driver, sid):
    """Log in without any UI by installing your Substack session cookie."""
    driver.get("https://substack.com/")
    driver.add_cookie({"name": "substack.sid", "value": sid, "domain": ".substack.com",
                       "path": "/", "secure": True, "httpOnly": True})


def do_login(driver):
    driver.get("https://substack.com/sign-in")
    input("Log in to Substack in the browser window, then press Enter here... ")


def alive(driver):
    try:
        return bool(driver.window_handles)
    except Exception:
        return False


def parse_ldjson(texts):
    """Title/date/authors from the page's schema.org JSON-LD blocks."""
    for raw in texts:
        try:
            j = json.loads(raw)
        except Exception:
            continue
        j = j[0] if isinstance(j, list) and j else j
        if isinstance(j, dict) and j.get("headline"):
            a = j.get("author")
            a = a if isinstance(a, list) else ([a] if a else [])
            return {"title": j.get("headline"), "date": j.get("datePublished"),
                    "authors": [x.get("name") for x in a
                                if isinstance(x, dict) and x.get("name")]}
    return {}


def preloads_fields(post):
    """Fields from the page's embedded window._preloads.post (same data the API returns)."""
    if not isinstance(post, dict):
        return {}
    return {"post_id": post.get("id"), "title": post.get("title"),
            "subtitle": post.get("subtitle"), "date": post.get("post_date"),
            "audience": post.get("audience"), "type": post.get("type"),
            "word_count": post.get("wordcount"), "likes": post.get("reaction_count"),
            "comments": post.get("comment_count"), "restacks": post.get("restacks"),
            "tags": [t.get("name") for t in post.get("postTags") or []],
            "authors": [b.get("name") for b in post.get("publishedBylines") or []
                        if b.get("name")]}


def find_post_id(html):
    """Fallback: numeric post id from page source (handles JS-escaped quotes)."""
    for pat in (r'post_id\\*"\s*:\s*(\d+)', r"/i/(\d{6,})/", r"post_preview(?:%2F|/)(\d{6,})"):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def extract_article(driver):
    from selenium.webdriver.common.by import By
    ld = parse_ldjson(t.get_attribute("textContent") for t in driver.find_elements(
        By.CSS_SELECTOR, 'script[type="application/ld+json"]'))
    try:
        pre = preloads_fields(driver.execute_script(
            "return (window._preloads && window._preloads.post) || null;"))
    except Exception:
        pre = {}
    data = {**ld, **{k: v for k, v in pre.items() if v not in (None, [], "")}}
    if not data.get("title"):
        h1 = driver.find_elements(By.CSS_SELECTOR, "h1.post-title")
        data["title"] = h1[0].text if h1 else None
    for k in ("title", "subtitle"):
        if isinstance(data.get(k), str):
            data[k] = data[k].strip()
    data["body_text"] = None
    for sel in ("div.available-content", "div.body.markup", "article"):
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if els:
            data["body_text"] = els[0].text
            break
    return data


def panel_fields(el, text):
    """Pull structured fields out of the scan-result modal body (selectors from real HTML)."""
    from selenium.webdriver.common.by import By
    first = lambda css: next((e.text.strip() for e in el.find_elements(By.CSS_SELECTOR, css)
                              if e.text.strip()), None)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    verdict = first('[class*="weight-semibold"]')          # e.g. "Fully Human-Written"
    if not verdict:
        idx = next((i for i, l in enumerate(lines) if LABEL_LINE.match(l)), None)
        verdict = lines[idx - 1] if idx else None
    note = first('[class*="font-style-italic"]')           # italic text under the author name
    return {"scan_verdict": verdict,
            "transparency_note": note,
            "has_transparency_note": bool(note),
            "scan_author": first('[class*="transform-uppercase"]'),
            "scan_summary": next((l for l in lines if l.lower().startswith("analysis by")), None)}


def find_scan_button(driver, seconds):
    """Find a visible Scan for AI item in the Reader menu."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    xpath = ("//*[self::button or self::a or @role='button' or @role='menuitem']"
             "[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
             f"'abcdefghijklmnopqrstuvwxyz'), '{SCAN_BUTTON_TEXT.lower()}')]")
    try:
        return WebDriverWait(driver, seconds).until(
            lambda d: next((e for e in d.find_elements(By.XPATH, xpath)
                            if e.is_displayed()), None))
    except Exception:
        return None


def open_more_menu(driver, seconds):
    """Open the Reader's More (three-dot) menu."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    selectors = [
        "button[aria-label='View more']",
        "button[aria-label*='More' i]", "button[title*='More' i]",
        "[role='button'][aria-label*='More' i]",
        "[role='button'][title*='More' i]",
        "button[aria-label*='options' i]",
        "button[aria-label*='ellipsis' i]",
    ]
    def click_menu(d):
        for selector in selectors:
            for el in d.find_elements(By.CSS_SELECTOR, selector):
                if el.is_displayed() and el.is_enabled():
                    try:
                        el.click()
                    except Exception:
                        d.execute_script("arguments[0].click();", el)
                    return True
        return False
    try:
        return WebDriverWait(driver, seconds).until(click_menu)
    except Exception:
        return False


def open_reader_from_profile(driver, post_id, args):
    """Follow the real article -> author -> Posts -> article click path."""
    from urllib.parse import urlparse
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    reader_url = args.reader_url.format(id=post_id)
    target_path = urlparse(reader_url).path.rstrip('/')
    original_url = driver.current_url
    original_host = urlparse(original_url).hostname or ''
    print(f"  Article starting: {original_url}", file=sys.stderr, flush=True)
    print(f"  Reader target:    {reader_url}", file=sys.stderr, flush=True)

    def click_and_follow(el):
        handles = set(driver.window_handles)
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            el.click()
        except Exception:
            driver.execute_script('arguments[0].click();', el)
        try:
            WebDriverWait(driver, min(args.timeout, 8)).until(
                lambda d: len(set(d.window_handles) - handles) > 0)
            driver.switch_to.window(next(iter(set(driver.window_handles) - handles)))
        except Exception:
            pass
        time.sleep(1)

    # Target the author NAME in the article byline below the title/subtitle,
    # not an avatar, footer link, or generic profile link elsewhere on the page.
    # Restrict the search to the first article header/byline area when possible.
    byline_xpaths = [
        "//article//header//a[contains(normalize-space(.), 'Grace Leuenberger')]",
        "//article//*[contains(@class,'byline') or contains(@class,'author')]//a[contains(normalize-space(.), 'Grace Leuenberger')]",
        "//header//a[contains(normalize-space(.), 'Grace Leuenberger')]",
        "//a[contains(normalize-space(.), 'Grace Leuenberger')]",
    ]
    candidates = []
    for xpath in byline_xpaths:
        for el in driver.find_elements(By.XPATH, xpath):
            try:
                if not el.is_displayed() or not el.is_enabled():
                    continue
                href = el.get_attribute('href') or ''
                if '/@' not in href and '/profile/' not in href:
                    continue
                # Exclude image-only/avatar links and choose the earliest visible
                # author-name link, which is normally directly below the subtitle.
                if 'grace leuenberger' not in (el.text or '').strip().lower():
                    continue
                candidates.append((el, href))
            except Exception:
                continue
        if candidates:
            break
    if not candidates:
        return {'scan_status': 'article_byline_author_link_not_found',
                'reader_requested_url': reader_url, 'reader_final_url': driver.current_url}
    author_el, author_href = candidates[0]
    print(f"  Byline author link: {author_href}", file=sys.stderr, flush=True)
    click_and_follow(author_el)
    print(f"  Profile reached:  {driver.current_url}", file=sys.stderr, flush=True)
    try:
        WebDriverWait(driver, args.timeout).until(
            lambda d: ('profile not found' in d.find_element(By.TAG_NAME, 'body').text.lower())
            or any(e.is_displayed() for e in d.find_elements(By.XPATH,
                "//a[normalize-space()='Posts'] | //button[normalize-space()='Posts'] | //*[@role='tab'][normalize-space()='Posts']")))
    except Exception:
        pass
    if 'profile not found' in driver.find_element(By.TAG_NAME, 'body').text.lower():
        return {'scan_status': 'author_profile_not_found',
                'reader_requested_url': reader_url, 'reader_final_url': driver.current_url}

    # Switch to Posts using the site's own navigation.
    posts_tab = None
    for el in driver.find_elements(By.XPATH,
            "//a[normalize-space()='Posts'] | //button[normalize-space()='Posts'] | //*[@role='tab'][normalize-space()='Posts']"):
        if el.is_displayed():
            posts_tab = el
            break
    if posts_tab is None:
        return {'scan_status': 'profile_posts_tab_not_found',
                'reader_requested_url': reader_url, 'reader_final_url': driver.current_url}
    click_and_follow(posts_tab)
    print(f"  Posts reached:    {driver.current_url}", file=sys.stderr, flush=True)

    def find_post_link():
        for el in driver.find_elements(By.CSS_SELECTOR, 'a[href]'):
            try:
                if not el.is_displayed():
                    continue
                href = el.get_attribute('href') or ''
                parsed = urlparse(href)
                if parsed.path.rstrip('/') == target_path or f'p-{post_id}' in href:
                    return el
                # Profile post cards may link to publication URLs rather than Reader URLs.
                if (urlparse(original_url).path.rstrip('/') == parsed.path.rstrip('/')
                        and parsed.hostname == original_host):
                    return el
            except Exception:
                continue
        return None

    link = None
    for attempt in range(args.profile_scrolls + 1):
        link = find_post_link()
        if link is not None:
            break
        if attempt < args.profile_scrolls:
            driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
            time.sleep(1.2)
    if link is None:
        return {'scan_status': 'profile_post_link_not_found',
                'reader_requested_url': reader_url, 'reader_final_url': driver.current_url}
    print(f"  Post link:        {link.get_attribute('href')}", file=sys.stderr, flush=True)
    click_and_follow(link)
    print(f"  Reader reached:   {driver.current_url}", file=sys.stderr, flush=True)
    if urlparse(driver.current_url).path.rstrip('/') != target_path:
        return {'scan_status': 'profile_click_redirected',
                'reader_requested_url': reader_url, 'reader_final_url': driver.current_url}
    return {'reader_requested_url': reader_url, 'reader_final_url': driver.current_url}


def scan_ai(driver, post_id, args):
    """Navigate from the author's profile into Reader and run the AI scan."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    out = {"scan_status": None, "pct_human": None, "pct_ai_assisted": None, "pct_ai": None,
           "scan_verdict": None, "has_transparency_note": None, "transparency_note": None,
           "scan_author": None, "scan_summary": None, "scan_panel_text": None}
    if not post_id:
        out["scan_status"] = "missing_post_id"
        return out
    wait = WebDriverWait(driver, args.timeout)
    body = lambda: driver.find_element(By.TAG_NAME, "body").text
    navigation = open_reader_from_profile(driver, post_id, args)
    out.update(navigation)
    if navigation.get("scan_status"):
        _debug(driver, args, "reader_profile_navigation")
        return out
    try:
        WebDriverWait(driver, args.timeout).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "button, [role='button']"))
    except Exception:
        pass
    time.sleep(1)
    btn = find_scan_button(driver, 2)
    if btn is None:
        if not open_more_menu(driver, min(args.timeout, 8)):
            out["scan_status"] = "reader_more_menu_not_found"
            _debug(driver, args, "reader_no_menu")
            return out
        btn = find_scan_button(driver, args.timeout)
    if btn is None:
        out["scan_status"] = classify_message(body()) or "scan_button_not_found"
        _debug(driver, args, "reader_nobutton")
        return out
    before = set(body().splitlines())
    _debug(driver, args, "reader_before")
    try:
        btn.click()
    except Exception:
        driver.execute_script("arguments[0].click();", btn)

    def result(d):
        for el in reversed(d.find_elements(By.CSS_SELECTOR, PANEL_CSS)):
            t = el.text
            if parse_scan(t) or classify_message(t):
                return el
        t = "\n".join(l for l in body().splitlines() if l not in before)
        return t if classify_message(t) else None

    try:
        res = wait.until(result)
    except Exception:
        out["scan_status"] = "timeout"
        _debug(driver, args, "reader_timeout")
        return out
    _debug(driver, args, "reader_after")
    text = res if isinstance(res, str) else res.text
    out["scan_panel_text"] = text
    scores = parse_scan(text)
    msg = None if scores else classify_message(text)  # a note could contain 'unavailable'
    if msg:
        out["scan_status"] = msg
        return out
    out["scan_status"] = "ok"
    out.update(scores)
    if not isinstance(res, str):
        out.update(panel_fields(res, text))
    return out


def classify_message(text):
    """Name the message actually shown, not its assumed cause (causes are inferred later)."""
    low = text.lower()
    if "not enough text" in low or "too short" in low:
        return "too_short_message"
    if "unavailable" in low:
        return "unavailable_message"
    return None


def parse_scan(text):
    """Extract {'pct_human','pct_ai_assisted','pct_ai'} from scan text.
    Tries 'AI 64%' ordering first, then '64% AI'; uses whichever finds 2+ labels."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    best = {}
    for label, value in zip(lines, lines[1:]):       # label and number on adjacent lines
        m, v = LABEL_LINE.match(label), VALUE_LINE.match(value)
        key = m and LABELS.get(re.sub(r"[ ]", "-", m.group(1).lower()))
        if key and v and key not in best:
            best[key] = int(v.group(1))
    if len(best) >= 2:
        return best
    for rx, (li, ni) in ((RX_LABEL_FIRST, (1, 2)), (RX_NUM_FIRST, (2, 1))):
        found = {}
        for m in rx.finditer(text):
            key = LABELS.get(re.sub(r"[ ]", "-", m.group(li).lower()))
            if key and key not in found:
                found[key] = int(m.group(ni))
        if len(found) >= 2:
            return found
        best = best or found
    return best


def _debug(driver, args, name):
    if args.debug and args.debug_left > 0:
        driver.save_screenshot(f"debug_{name}.png")
        with open(f"debug_{name}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print(f"  [debug] saved debug_{name}.png/.html", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Selenium Substack crawler with timing.")
    ap.add_argument("publication", help="subdomain, domain or URL")
    ap.add_argument("--limit", type=int, help="max posts (newest first)")
    ap.add_argument("--url", action="append",
                    help="crawl just this post URL (repeatable; skips the sitemap)")
    ap.add_argument("--no-scan", action="store_true", help="skip the AI-scan step")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds between posts (default 0.5, same as the API tool)")
    ap.add_argument("--profile-scrolls", type=int, default=12,
                    help="maximum profile scrolls when searching for each article (default 12)")
    ap.add_argument("--reader-url", default=None,
                    help="Reader URL pattern ({id} = post id); defaults to the publication Reader URL")
    ap.add_argument("--login", action="store_true",
                    help="force the Substack sign-in step (normally done automatically, once, "
                         "when the saved profile isn't logged in)")
    ap.add_argument("--sid", default=os.environ.get("SUBSTACK_SID"),
                    help="your substack.sid cookie value (or set env SUBSTACK_SID): logs in "
                         "with no manual step. Copy it from your normal browser's dev tools.")
    ap.add_argument("--timeout", type=int, default=20, help="per-step wait, seconds")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    ap.add_argument("--profile-dir", default="chrome-profile",
                    help="Chrome profile dir that keeps you logged in (default ./chrome-profile). "
                         "Use a dedicated folder, not your everyday Chrome profile.")
    ap.add_argument("--attach", action="store_true",
                    help="attach Selenium to an already-running Chrome on the remote-debugging port")
    ap.add_argument("--debugger-address", default="127.0.0.1:9222",
                    help="Chrome remote-debugging address for --attach")
    ap.add_argument("--load-images", action="store_true", help="don't block images")
    ap.add_argument("--debug", action="store_true",
                    help="save screenshots/HTML for the first post")
    ap.add_argument("-o", "--output", default="crawl.json")
    ap.add_argument("--timing-out", default="timing_crawler.json")
    args = ap.parse_args()
    args.debug_left = 1 if args.debug else 0

    t_all = time.perf_counter()
    host = host_of(args.publication)
    if args.reader_url is None:
        if host.endswith(".substack.com"):
            publication_slug = host[:-len(".substack.com")]
            args.reader_url = f"https://substack.com/@{publication_slug}/p-{{id}}"
        elif not args.no_scan:
            ap.error("Custom domains require --reader-url 'https://substack.com/@PUBLICATION/p-{id}'")
    urls, t_disc = (args.url, 0.0) if args.url else discover(host, args.limit)
    print(f"Found {len(urls)} posts via sitemap in {t_disc:.1f}s", file=sys.stderr)

    t = time.perf_counter()
    if args.attach:
        # Reuse the authenticated Chrome the user already opened.
        driver = make_driver(args, headless=False)
        if not args.no_scan and not is_logged_in(driver):
            close_driver(driver, args)
            raise RuntimeError("Attached Chrome is not logged into Substack. Log in and rerun.")
    elif not args.no_scan or args.login:
        probe = make_driver(args)
        try:
            if args.sid:
                inject_sid(probe, args.sid)
            need_login = args.login or not is_logged_in(probe)
        finally:
            close_driver(probe, args)
        if need_login:
            t_login = time.perf_counter()
            probe = make_driver(args, headless=False)
            try:
                do_login(probe)
            finally:
                close_driver(probe, args)
            t_all += time.perf_counter() - t_login
            time.sleep(1)
        driver = make_driver(args)
    else:
        driver = make_driver(args)
    t_start = time.perf_counter() - t

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    records, waited, t_nav, t_scan = [], 0.0, 0.0, 0.0
    try:
        for i, url in enumerate(urls, 1):
            rec = {"url": url, "publication": host,
                   "slug": url.rsplit("/p/", 1)[1].split("?")[0]}
            t = time.perf_counter()
            post_id = None
            try:
                if not alive(driver):                   # Chrome died: relaunch (same profile)
                    try:
                        close_driver(driver, args)
                    except Exception:
                        pass
                    driver = make_driver(args, headless=False if args.attach else None)
                driver.get(url)
                WebDriverWait(driver, args.timeout).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, 'h1.post-title, script[type="application/ld+json"]')))
                rec.update(extract_article(driver))
                if not args.no_scan:
                    post_id = rec.get("post_id") or find_post_id(driver.page_source)
                _debug(driver, args, "article")
            except Exception as e:
                rec["error"] = str(e).splitlines()[0][:200]
            rec["nav_seconds"] = round(time.perf_counter() - t, 2)
            t_nav += rec["nav_seconds"]

            if not args.no_scan and "error" not in rec:
                t = time.perf_counter()
                try:
                    rec.update(scan_ai(driver, post_id, args))
                except Exception as e:
                    rec["scan_status"] = "error: " + str(e).splitlines()[0][:150]
                rec["scan_seconds"] = round(time.perf_counter() - t, 2)
                t_scan += rec["scan_seconds"]
            args.debug_left = 0
            records.append(rec)

            time.sleep(args.delay)
            waited += args.delay
            if i % 10 == 0 or i == len(urls):
                el = time.perf_counter() - t_all
                print(f"  {i}/{len(urls)} done, {el:.0f}s elapsed "
                      f"(~{el / i * (len(urls) - i):.0f}s left)", file=sys.stderr)
    finally:
        close_driver(driver, args)

    wall = time.perf_counter() - t_all
    n = len(records)
    per = lambda x: round(x / n, 3) if n else None
    summary = {"tool": "crawler(selenium)", "target": host, "articles": n,
               "wall_seconds": round(wall, 2),
               "deliberate_wait_seconds": round(waited, 2),
               "active_seconds": round(wall - waited, 2),
               "seconds_per_article_wall": per(wall),
               "seconds_per_article_active": per(wall - waited),
               "discovery_seconds": round(t_disc, 2),
               "browser_startup_seconds": round(t_start, 2),
               "page_load_seconds_total": round(t_nav, 2),
               "scan_seconds_total": round(t_scan, 2),
               "scan_enabled": not args.no_scan,
               "errors": sum(1 for r in records if "error" in r)}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    with open(args.timing_out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nCrawled {n} posts in {wall:.1f}s ({summary['seconds_per_article_wall']}s/post; "
          f"{summary['errors']} errors). Timing saved to {args.timing_out}", file=sys.stderr)
    if not args.no_scan:
        ok = sum(1 for r in records if r.get("scan_status") == "ok")
        notes = sum(1 for r in records if r.get("has_transparency_note"))
        print(f"AI scan: {ok}/{n} returned scores, {notes} with a transparency note; statuses: "
              f"{sorted({r.get('scan_status') for r in records if 'scan_status' in r}, key=str)}",
              file=sys.stderr)


if __name__ == "__main__":
    main()