#!/usr/bin/env python3
"""
Mirror recent output from NZ independent outlets (Reality Check Radio, The Platform NZ)
and a set of mainstream NZ / world / AI news feeds into JSON files.

Why this exists: the cloud environment that builds Evan's daily news brief has an
outbound egress policy that blocks realitycheck.radio, theplatform.kiwi, youtube.com
and most news RSS hosts, but permits github.com. GitHub Actions runners have
unrestricted internet, so this script runs here and publishes the result where the
brief can actually read it.

Outputs
  data/latest.json  alt-media mirror (schema unchanged, plus a `sources` health block)
  data/news.json    mainstream headline feeds, grouped by section, with health block

Every source gets a health record (ok / error / item count / newest item), so the
brief can say exactly which feed failed instead of guessing.
"""

import html
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (compatible; nz-altmedia-mirror/2.0; +https://github.com/EVAN-1992/nz-altmedia-mirror)"
TIMEOUT = 40
RETRIES = 3

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
SITEMAP = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
DC = "{http://purl.org/dc/elements/1.1/}"

HEALTH = {}  # label -> {ok, error, items, newest_utc}


# ---------------------------------------------------------------- fetching

def get(url):
    """GET with retries and backoff. Raises the last error."""
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/rss+xml, application/atom+xml, application/xml, application/json, text/html;q=0.8, */*;q=0.5",
                },
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            code = getattr(e, "code", None)
            if code in (400, 401, 403, 404, 410):
                break  # permanent; retrying won't help
            time.sleep(2 * (attempt + 1))
    raise last


def try_get(url, label):
    try:
        data = get(url)
        print(f"  ok   {label} ({len(data)} bytes)", file=sys.stderr)
        return data
    except Exception as e:  # noqa: BLE001 - record every failure, never raise
        msg = f"{type(e).__name__}: {e}"[:200]
        print(f"  FAIL {label}: {msg}", file=sys.stderr)
        HEALTH[label] = {"ok": False, "error": msg, "items": 0, "newest_utc": None}
        return None


def record(label, items):
    newest = max((i["date_utc"] for i in items if i.get("date_utc")), default=None)
    prev = HEALTH.get(label)
    if prev and not prev["ok"] and not items:
        return items
    HEALTH[label] = {
        "ok": bool(items),
        "error": None if items else "fetched but no items parsed",
        "items": len(items),
        "newest_utc": newest,
    }
    return items


# ---------------------------------------------------------------- cleaning

def clean_title(s):
    """Normalise entities and stray non-breaking spaces that otherwise surface as
    mojibake ('AÂ Breast Cancer Drug') downstream."""
    if not s:
        return ""
    s = html.unescape(s)
    s = s.replace(" ", " ").replace("​", "")
    return re.sub(r"\s+", " ", s).strip()


def strip_html(s):
    """Drop tags, then resolve entities. Order matters: unescaping first would let
    an encoded '&lt;script&gt;' turn into a tag the tag-stripper already passed."""
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = s.replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def iso(s):
    """Normalise RFC 2822 / ISO 8601 / WordPress dates to 'YYYY-MM-DDTHH:MM:SSZ'.
    Returns '' when unparseable, so consumers can sort on the field directly."""
    s = (s or "").strip()
    if not s:
        return ""
    dt = None
    try:
        dt = parsedate_to_datetime(s)
    except Exception:  # noqa: BLE001
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_xml(raw, label):
    try:
        return ET.fromstring(raw)
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {label} parse: {e}", file=sys.stderr)
        HEALTH[label] = {"ok": False, "error": f"XML parse: {e}"[:200], "items": 0, "newest_utc": None}
        return None


# ---------------------------------------------------------------- parsers

def wp_json(url, label):
    raw = try_get(url, label)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        HEALTH[label] = {"ok": False, "error": f"JSON parse: {e}"[:200], "items": 0, "newest_utc": None}
        return []
    out = []
    for it in items:
        out.append(
            {
                "title": strip_html((it.get("title") or {}).get("rendered", "")),
                "url": it.get("link", ""),
                "date_utc": iso((it.get("date_gmt") or "") + ("Z" if it.get("date_gmt") else "")),
                "summary": strip_html((it.get("excerpt") or {}).get("rendered", ""))[:600],
            }
        )
    return record(label, out)


def feed_items(url, label, limit=15, summary_len=1200):
    """RSS 2.0 or Atom, whichever the URL serves."""
    raw = try_get(url, label)
    if not raw:
        return []
    root = parse_xml(raw, label)
    if root is None:
        return []
    out = []
    if root.tag.endswith("feed"):  # Atom
        for entry in root.findall(f"{ATOM}entry"):
            # Element truthiness means "has children", so test against None explicitly
            link_el = entry.find(f"{ATOM}link[@rel='alternate']")
            if link_el is None:
                link_el = entry.find(f"{ATOM}link")
            out.append(
                {
                    "title": clean_title(entry.findtext(f"{ATOM}title") or ""),
                    "url": link_el.get("href", "") if link_el is not None else "",
                    "date_utc": iso(entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")),
                    "summary": strip_html(entry.findtext(f"{ATOM}summary") or entry.findtext(f"{ATOM}content") or "")[:summary_len],
                }
            )
            if len(out) >= limit:
                break
    else:  # RSS
        for item in root.iter("item"):
            body = item.findtext(f"{CONTENT}encoded") or item.findtext("description") or ""
            src = item.find("source")
            row = {
                "title": clean_title(item.findtext("title") or ""),
                "url": (item.findtext("link") or "").strip(),
                "date_utc": iso(item.findtext("pubDate") or item.findtext(f"{DC}date")),
                "summary": strip_html(body)[:summary_len],
            }
            if src is not None and (src.text or "").strip():
                row["outlet"] = src.text.strip()  # Google News carries the real outlet here
            out.append(row)
            if len(out) >= limit:
                break
    return record(label, [o for o in out if o["title"]])


def youtube_atom(channel_id, label, limit=15):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    raw = try_get(url, label)
    if not raw:
        return []
    root = parse_xml(raw, label)
    if root is None:
        return []
    out = []
    for entry in root.findall(f"{ATOM}entry"):
        link_el = entry.find(f"{ATOM}link")
        group = entry.find(f"{MEDIA}group")
        desc = group.findtext(f"{MEDIA}description") or "" if group is not None else ""
        # Strip the subscription boilerplate the channels append to every description
        desc = re.split(r"Watch The Platform live on YouTube", desc)[0]
        desc = re.split(r"Subscribe to (?:RCR|Reality Check)", desc)[0]
        out.append(
            {
                "title": clean_title(entry.findtext(f"{ATOM}title") or ""),
                "url": link_el.get("href") if link_el is not None else "",
                "date_utc": iso(entry.findtext(f"{ATOM}published")),
                "summary": strip_html(desc)[:1200],
            }
        )
        if len(out) >= limit:
            break
    return record(label, out)


def _slug_title(url):
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").strip().title()


def platform_opinions(limit=12):
    """The Platform's opinion pieces. The flat sitemap.xml stopped listing them in
    early November 2026, so this follows a sitemap index if that's what is served,
    then falls back to reading links off the /opinions listing page."""
    label = "platform opinions"
    rows = {}

    raw = try_get("https://theplatform.kiwi/sitemap.xml", "platform sitemap")
    root = parse_xml(raw, "platform sitemap") if raw else None
    sitemaps = []
    if root is not None:
        if root.tag.endswith("sitemapindex"):
            for sm in root.findall(f"{SITEMAP}sitemap"):
                loc = (sm.findtext(f"{SITEMAP}loc") or "").strip()
                if loc:
                    sitemaps.append(loc)
            # opinion-ish children first, then the rest, capped to keep the run short
            sitemaps.sort(key=lambda u: (0 if re.search(r"opinion|post|article", u, re.I) else 1))
            sitemaps = sitemaps[:6]
        else:
            sitemaps = [None]  # the root itself is a urlset

    for sm in sitemaps:
        node = root
        if sm:
            sub = try_get(sm, "platform sitemap child")
            node = parse_xml(sub, "platform sitemap child") if sub else None
            HEALTH.pop("platform sitemap child", None)
        if node is None:
            continue
        for u in node.findall(f"{SITEMAP}url"):
            loc = (u.findtext(f"{SITEMAP}loc") or "").strip()
            if "/opinions/" in loc and loc.rstrip("/").split("/")[-1] != "opinions":
                rows[loc] = iso(u.findtext(f"{SITEMAP}lastmod"))

    if not rows:  # fallback: scrape the listing page for /opinions/<slug> links
        page = try_get("https://theplatform.kiwi/opinions", "platform opinions page")
        if page:
            for m in re.finditer(rb'href="((?:https://theplatform\.kiwi)?/opinions/[a-z0-9][a-z0-9-]+)/?"', page):
                href = m.group(1).decode()
                if href.startswith("/"):
                    href = "https://theplatform.kiwi" + href
                rows.setdefault(href, "")
            HEALTH.pop("platform opinions page", None)

    out = [
        {
            "title": _slug_title(url),
            "url": url,
            "date_utc": d,
            "summary": "(opinion piece; title derived from URL slug, fetch the URL for full text)",
        }
        for url, d in sorted(rows.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    ]
    HEALTH.pop("platform sitemap", None)
    return record(label, out)


def dedupe(items):
    seen = set()
    out = []
    for it in items:
        if not it.get("title"):
            continue
        key = it.get("url", "").rstrip("/").split("/")[-1] or it["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    out.sort(key=lambda i: i.get("date_utc") or "", reverse=True)
    return out


# ---------------------------------------------------------------- mainstream feeds
#
# Sections mirror the brief's. Each entry: (label, url). Google News feeds carry the
# real outlet in `outlet`; their URLs are news.google.com redirects, which the brief
# should resolve or cite by outlet name. A feed that fails is recorded, not fatal.

GN = "hl=en-NZ&gl=NZ&ceid=NZ:en"
NEWS_FEEDS = {
    "nz": [
        ("RNZ National", "https://www.rnz.co.nz/rss/national.xml"),
        ("RNZ Political", "https://www.rnz.co.nz/rss/political.xml"),
        ("RNZ Business", "https://www.rnz.co.nz/rss/business.xml"),
        ("1News", "https://www.1news.co.nz/arc/outboundfeeds/rss/?outputType=xml"),
        ("NZ Herald", "https://www.nzherald.co.nz/arc/outboundfeeds/rss/curated/78/?outputType=xml"),
        ("Stuff", "https://www.stuff.co.nz/rss"),
        ("Newsroom", "https://newsroom.co.nz/feed/"),
        ("The Spinoff", "https://thespinoff.co.nz/feed"),
        ("interest.co.nz", "https://www.interest.co.nz/rss"),
        ("Waatea News", "https://waateanews.com/feed/"),
        ("Google News NZ top stories", f"https://news.google.com/rss?{GN}"),
        ("Google News NZ", f"https://news.google.com/rss/headlines/section/geo/New%20Zealand?{GN}"),
    ],
    "world": [
        ("RNZ World", "https://www.rnz.co.nz/rss/world.xml"),
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("The Guardian World", "https://www.theguardian.com/world/rss"),
        ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
        ("ABC (AU) World", "https://www.abc.net.au/news/feed/52278/rss.xml"),
        ("Google News World", f"https://news.google.com/rss/headlines/section/topic/WORLD?{GN}"),
        ("Google News Business", f"https://news.google.com/rss/headlines/section/topic/BUSINESS?{GN}"),
        ("Google News Science", f"https://news.google.com/rss/headlines/section/topic/SCIENCE?{GN}"),
    ],
    "ai": [
        ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
        ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("Ars Technica AI", "https://arstechnica.com/ai/feed/"),
        ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
        ("Google News AI", f"https://news.google.com/rss/search?q=%22artificial+intelligence%22+OR+OpenAI+OR+Anthropic+OR+%22Google+DeepMind%22+when:1d&{GN}"),
        ("Google News Technology", f"https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?{GN}"),
    ],
}


def fetch_news():
    out = {}
    for section, feeds in NEWS_FEEDS.items():
        rows = []
        for label, url in feeds:
            for it in feed_items(url, label, limit=20, summary_len=400):
                it.setdefault("outlet", label)
                it["feed"] = label
                rows.append(it)
        out[section] = rows
    return out


# ---------------------------------------------------------------- main

def health_block(labels):
    return {k: HEALTH[k] for k in labels if k in HEALTH}


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("Fetching Reality Check Radio…", file=sys.stderr)
    rcr = []
    rcr += wp_json(
        "https://realitycheck.radio/wp-json/wp/v2/posts?per_page=15&_fields=id,date,date_gmt,title,link,excerpt",
        "rcr wp-json posts",
    )
    rcr += wp_json(
        "https://realitycheck.radio/wp-json/wp/v2/episodes?per_page=15&_fields=id,date,date_gmt,title,link,excerpt",
        "rcr wp-json episodes",
    )
    rcr += feed_items("https://realitycheck.radio/?feed=allepisodes", "rcr allepisodes rss")
    rcr += feed_items("https://realitycheck.radio/feed/", "rcr article rss")
    rcr_yt = youtube_atom("UC5IxWEvo3qMN2ug3t1DclUQ", "rcr youtube")

    print("Fetching The Platform NZ…", file=sys.stderr)
    plat_yt = youtube_atom("UCYKvkaqOJFwji8-Jgm8pjhA", "platform youtube")
    plat_ops = platform_opinions()

    alt_labels = list(HEALTH)
    payload = {
        "generated_utc": now,
        "note": (
            "Mirror of recent NZ independent-media output, refreshed by GitHub Actions every "
            "3 hours. Exists because the brief's cloud runner cannot reach these domains "
            "directly. All date_utc values are ISO 8601 UTC; lists are newest first."
        ),
        "reality_check_radio": {
            "articles_and_episodes": dedupe(rcr)[:20],
            "youtube": rcr_yt,
        },
        "the_platform_nz": {
            "youtube": plat_yt,
            "opinions": plat_ops,
        },
    }
    payload["counts"] = {
        "rcr_items": len(payload["reality_check_radio"]["articles_and_episodes"]),
        "rcr_youtube": len(rcr_yt),
        "platform_youtube": len(plat_yt),
        "platform_opinions": len(plat_ops),
    }
    payload["sources"] = health_block(alt_labels)
    print(f"Alt counts: {payload['counts']}", file=sys.stderr)

    print("Fetching mainstream feeds…", file=sys.stderr)
    before = set(HEALTH)
    news = fetch_news()
    news_labels = [k for k in HEALTH if k not in before]
    news_payload = {
        "generated_utc": now,
        "note": (
            "Headline feeds for the daily brief, newest first per feed. Google News rows carry "
            "the publishing outlet in `outlet`; their URLs are news.google.com redirects. "
            "Summaries are feed excerpts, not article text."
        ),
        "sections": news,
        "counts": {k: len(v) for k, v in news.items()},
        "sources": health_block(news_labels),
    }
    print(f"News counts: {news_payload['counts']}", file=sys.stderr)

    if payload["counts"]["rcr_items"] == 0 and payload["counts"]["platform_youtube"] == 0:
        # Keep yesterday's alt-media file rather than overwrite it with nothing,
        # but still publish the mainstream feeds.
        print("ERROR: no alt-media content retrieved; keeping previous latest.json", file=sys.stderr)
    else:
        with open("data/latest.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print("wrote data/latest.json", file=sys.stderr)

    with open("data/news.json", "w", encoding="utf-8") as f:
        json.dump(news_payload, f, indent=1, ensure_ascii=False)
    print("wrote data/news.json", file=sys.stderr)

    failed = [k for k, v in HEALTH.items() if not v["ok"]]
    if failed:
        print(f"::warning::{len(failed)} source(s) failed: {', '.join(failed)}", file=sys.stderr)


if __name__ == "__main__":
    main()
