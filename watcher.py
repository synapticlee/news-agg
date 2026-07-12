#!/usr/bin/env python3
"""Press release watcher.

Polls newsroom RSS feeds, SEC EDGAR 8-K/6-K atom feeds, and Google News
query feeds; emails a digest of anything new since the last run.

Usage:
  python watcher.py --validate     # test every feed, report live/dead
  python watcher.py --dry-run      # fetch + dedupe, print instead of email
  python watcher.py                # fetch + dedupe + send email

State: seen.json (entry IDs already alerted on). Commit it back to the
repo when running under GitHub Actions.

Email env vars (set as GitHub secrets):
  SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 465),
  SMTP_USER, SMTP_PASS (Gmail app password), MAIL_TO
"""

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import feedparser
import yaml
import urllib.request
import xml.etree.ElementTree as ET

import socket
socket.setdefaulttimeout(20)

SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(" ".join(data.split()))


def page_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        html_body = r.read().decode("utf-8", errors="replace")
    p = _TextExtractor()
    p.feed(html_body)
    return "\n".join(p.parts)


def collect_json_changes(endpoints, seen):
    """Poll JSON endpoints and alert on new items. Config per endpoint:
      name, url            — endpoint to poll
      items_path           — dot path to the list (e.g. 'jobs' or '' for root)
      id_field             — field uniquely identifying an item (e.g. 'id')
      title_field          — field to show as the alert title
      link_field           — optional field holding the item's URL
    """
    items = []
    for ep in endpoints:
        name = ep["name"]
        try:
            req = urllib.request.Request(
                ep["url"], headers={"User-Agent": USER_AGENT,
                                    "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
        except Exception as e:
            print(f"json fetch failed: {name}: {e}")
            continue
        node = data
        for part in filter(None, ep.get("items_path", "").split(".")):
            node = node.get(part, [])
        if not isinstance(node, list):
            print(f"json items_path did not yield a list: {name}")
            continue
        for obj in node:
            oid = str(obj.get(ep.get("id_field", "id"), ""))
            if not oid:
                continue
            key = "js:" + hashlib.sha1(f"{name}|{oid}".encode()).hexdigest()
            if key in seen:
                continue
            seen[key] = int(time.time())
            items.append({
                "feed": name,
                "title": str(obj.get(ep.get("title_field", "title"), oid)),
                "link": str(obj.get(ep.get("link_field", ""), "")),
                "published": "",
            })
        time.sleep(0.3)
    return items


def collect_page_diffs(pages):
    """Diff tracked pages' text content against stored copies in pages/.
    First sight of a page stores a baseline silently. Lines matching any
    regex in a page's `ignore` list are dropped before comparison (for
    live counters and other churn)."""
    items = []
    PAGES_DIR.mkdir(exist_ok=True)
    for p in pages:
        name, url = p["name"], p["url"]
        ignore = [re.compile(pat, re.I) for pat in p.get("ignore", [])]
        slug = "".join(c if c.isalnum() else "_" for c in name.lower())
        path = PAGES_DIR / f"{slug}.txt"
        try:
            new = page_text(url)
        except Exception as e:
            print(f"page fetch failed: {name}: {e}")
            continue
        if ignore:
            new = "\n".join(ln for ln in new.splitlines()
                            if not any(rx.search(ln) for rx in ignore))
        if not path.exists():
            path.write_text(new)
            print(f"page baseline stored: {name}")
            continue
        old = path.read_text()
        if old != new:
            diff = list(difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile="before", tofile="after", lineterm="", n=1))
            path.write_text(new)
            items.append({"feed": f"Page changed: {name}",
                          "title": name, "link": url, "published": "",
                          "detail": "\n".join(diff[:80])})
        time.sleep(0.3)
    return items


def fetch_sitemap_urls(url, depth=0):
    """Return list of (loc, lastmod) from a sitemap, following one level
    of sitemapindex nesting."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    out = []
    if root.tag == f"{SM_NS}sitemapindex" and depth < 1:
        for sm in root.findall(f"{SM_NS}sitemap"):
            loc = sm.findtext(f"{SM_NS}loc")
            if loc:
                out.extend(fetch_sitemap_urls(loc.strip(), depth + 1))
                time.sleep(0.3)
    else:
        for u in root.findall(f"{SM_NS}url"):
            loc = u.findtext(f"{SM_NS}loc")
            lastmod = u.findtext(f"{SM_NS}lastmod") or ""
            if loc:
                out.append((loc.strip(), lastmod.strip()))
    return out


def collect_sitemap_changes(sitemaps, seen):
    """Diff sitemap URL lists against seen state. New URLs always alert;
    lastmod changes alert if track_updates is true for that sitemap."""
    items = []
    for sm in sitemaps:
        name, url = sm["name"], sm["url"]
        include = sm.get("include", "")
        try:
            entries = fetch_sitemap_urls(url)
        except Exception as e:
            print(f"sitemap fetch failed: {name}: {e}")
            continue
        for loc, lastmod in entries:
            if include and include not in loc:
                continue
            key = "sm:" + hashlib.sha1(f"{name}|{loc}".encode()).hexdigest()
            prev = seen.get(key)
            if prev is None:
                seen[key] = lastmod or "seen"
                items.append({"feed": f"{name} (new page)",
                              "title": loc.rstrip("/").rsplit("/", 1)[-1].replace("-", " "),
                              "link": loc, "published": lastmod})
            elif sm.get("track_updates") and lastmod and prev != lastmod:
                seen[key] = lastmod
                items.append({"feed": f"{name} (updated page)",
                              "title": loc.rstrip("/").rsplit("/", 1)[-1].replace("-", " "),
                              "link": loc, "published": lastmod})
        time.sleep(0.3)
    return items

HERE = Path(__file__).parent
SEEN_PATH = HERE / "seen.json"
CONFIG_PATH = HERE / "feeds.yml"
PAGES_DIR = HERE / "pages"

# SEC requires a descriptive User-Agent with contact info.
# https://www.sec.gov/os/webmaster-faq#developers  -- put YOUR email here.
USER_AGENT = "pr-watcher/1.0 (contact: you@example.com)"

EDGAR_FMT = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
    "&CIK={ticker}&type={form}&dateb=&owner=include&count=10&output=atom"
)
GNEWS_FMT = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def build_feed_list(cfg):
    """Returns list of (name, url, keywords). keywords is a list of
    lowercase strings; if non-empty, only entries whose title contains
    at least one keyword are alerted on."""
    feeds = [(f["name"], f["url"], [k.lower() for k in f.get("keywords", [])])
             for f in cfg.get("feeds", [])]
    sixk = set(cfg.get("edgar_6k_tickers", []))
    for t in cfg.get("edgar_tickers", []):
        form = "6-K" if t in sixk else "8-K"
        feeds.append((f"EDGAR {form}: {t}",
                      EDGAR_FMT.format(ticker=t, form=form), []))
    for q in cfg.get("gnews_queries", []):
        if isinstance(q, dict):
            query, kws = q["query"], [k.lower() for k in q.get("keywords", [])]
        else:
            query, kws = q, []
        feeds.append((f"GNews: {query}", GNEWS_FMT.format(q=quote(query)), kws))
    return feeds


def entry_id(feed_name, entry):
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(f"{feed_name}|{raw}".encode()).hexdigest()


def fetch(url, retries=1):
    """Parse a feed, retrying once on transient network errors (e.g. a
    server closing the connection mid-response) before giving up."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            return feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5)
    raise last_err


def validate(feeds):
    ok = bad = 0
    for name, url, _ in feeds:
        try:
            d = fetch(url)
        except Exception as e:
            print(f"✗ {name:<28} EXCEPTION: {e}\n    {url}")
            bad += 1
            time.sleep(0.5)
            continue
        alive = not d.bozo and len(d.entries) > 0
        # some valid feeds set bozo for minor issues; entries are the real test
        alive = alive or len(d.entries) > 0
        status = f"OK ({len(d.entries)} entries)" if alive else f"DEAD/EMPTY ({getattr(d, 'status', '?')})"
        print(f"{'✓' if alive else '✗'} {name:<28} {status}\n    {url}")
        ok += alive
        bad += not alive
        time.sleep(0.5)  # be polite, esp. to sec.gov (10 req/s hard limit)
    print(f"\n{ok} live, {bad} dead — remove or fix dead ones in feeds.yml")


def collect_new(feeds, seen):
    new_items = []
    for name, url, keywords in feeds:
        try:
            d = fetch(url)
        except Exception as e:
            print(f"feed fetch failed: {name}: {e}")
            continue
        for e in d.entries[:15]:
            eid = entry_id(name, e)
            if eid in seen:
                continue
            seen[eid] = int(time.time())
            title = e.get("title", "(no title)")
            if keywords and not any(k in title.lower() for k in keywords):
                continue  # marked seen, but filtered out of the alert
            new_items.append({
                "feed": name,
                "title": title,
                "link": e.get("link", ""),
                "published": e.get("published", e.get("updated", "")),
            })
        time.sleep(0.3)
    return new_items


def render_email(items):
    by_feed = {}
    for it in items:
        by_feed.setdefault(it["feed"], []).append(it)
    parts = ["<h2>New press releases / filings</h2>"]
    for feed, its in sorted(by_feed.items()):
        parts.append(f"<h3>{html.escape(feed)}</h3><ul>")
        for it in its:
            parts.append(
                f'<li><a href="{html.escape(it["link"])}">'
                f'{html.escape(it["title"])}</a>'
                f' <small>{html.escape(it["published"])}</small>'
            )
            if it.get("detail"):
                parts.append(
                    f'<pre style="font-size:12px;background:#f6f6f6;'
                    f'padding:8px">{html.escape(it["detail"])}</pre>')
            parts.append("</li>")
        parts.append("</ul>")
    return "\n".join(parts)


DIGEST_PATH = HERE / "digest.md"


def render_markdown(items):
    by_feed = {}
    for it in items:
        by_feed.setdefault(it["feed"], []).append(it)
    lines = []
    for feed, its in sorted(by_feed.items()):
        lines.append(f"### {feed}")
        for it in its:
            lines.append(f"- [{it['title']}]({it['link']}) {it['published']}")
            if it.get("detail"):
                lines.append("```diff\n" + it["detail"] + "\n```")
        lines.append("")
    return "\n".join(lines)


def send_email(subject, html_body):
    """Prefer Resend API (RESEND_API_KEY), fall back to SMTP creds."""
    to = os.environ["MAIL_TO"]
    resend_key = os.environ.get("RESEND_API_KEY")
    if resend_key:
        payload = json.dumps({
            "from": os.environ.get("MAIL_FROM",
                                   "PR Watch <onboarding@resend.dev>"),
            "to": [to], "subject": subject, "html": html_body}).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails", data=payload,
            headers={"Authorization": f"Bearer {resend_key}",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
        return
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASS"]
    msg = MIMEText(html_body, "html")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL(host, port) as s:
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    feeds = build_feed_list(cfg)

    if args.validate:
        validate(feeds)
        return

    seen = json.loads(SEEN_PATH.read_text()) if SEEN_PATH.exists() else {}
    first_run = not seen

    items = collect_new(feeds, seen)
    items += collect_sitemap_changes(cfg.get("sitemaps", []), seen)
    items += collect_json_changes(cfg.get("json_endpoints", []), seen)
    items += collect_page_diffs(cfg.get("pages", []))
    SEEN_PATH.write_text(json.dumps(seen))

    if first_run:
        # Baseline run: everything is "new"; record it but don't blast
        # a 300-item email. Alerts start from the next run.
        print(f"Baseline established: {len(items)} existing items recorded, no email sent.")
        return
    if not items:
        print("No new items.")
        return

    subject = f"[PR watch] {len(items)} new item{'s' if len(items) != 1 else ''}"
    if args.dry_run:
        print(subject)
        for it in items:
            print(f"- [{it['feed']}] {it['title']}\n  {it['link']}")
    elif os.environ.get("RESEND_API_KEY") or os.environ.get("SMTP_USER"):
        send_email(subject, render_email(items))
        print(f"Emailed {len(items)} items.")
    else:
        # GitHub-issue notification mode: write digest.md; the workflow
        # opens an issue with it, and GitHub emails the issue to you.
        DIGEST_PATH.write_text(f"# {subject}\n\n" + render_markdown(items))
        print(f"Wrote digest.md with {len(items)} items (issue mode).")


if __name__ == "__main__":
    sys.exit(main())
