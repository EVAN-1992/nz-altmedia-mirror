#!/usr/bin/env python3
"""
Mirror recent output from NZ independent outlets (Reality Check Radio, The Platform NZ)
into a single JSON file.

Why this exists: the cloud environment that builds Evan's daily news brief has an
outbound egress policy that blocks realitycheck.radio, theplatform.kiwi and youtube.com,
but permits api.github.com. GitHub Actions runners have unrestricted internet, so this
script runs here and publishes the result where the brief can actually read it.
"""

import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = "Mozilla/5.0 (compatible; nz-altmedia-mirror/1.0)"
TIMEOUT = 45

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def try_get(url, label):
    try:
        data = get(url)
        print(f"  ok   {label} ({len(data)} bytes)", file=sys.stderr)
        return data
    except Exception as e:  # noqa: BLE001 - we want every failure recorded, not raised
        print(f"  FAIL {label}: {e}", file=sys.stderr)
        return None


def strip_html(s):
    """Drop tags, then resolve entities. Order matters: unescaping first would let
    an encoded '&lt;script&gt;' turn into a tag that the tag-stripper already passed."""
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)          # handles &#8230; &#8211; &rsquo; &amp; etc.
    s = s.replace(" ", " ")  # nbsp survives unescape as a real character
    return re.sub(r"\s+", " ", s).strip()


def wp_json(url, label):
    raw = try_get(url, label)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {label} parse: {e}", file=sys.stderr)
        return []
    out = []
    for it in items:
        out.append(
            {
                "title": strip_html((it.get("title") or {}).get("rendered", "")),
                "url": it.get("link", ""),
                "date_utc": it.get("date_gmt", "") + "Z" if it.get("date_gmt") else "",
                "summary": strip_html((it.get("excerpt") or {}).get("rendered", ""))[:600],
            }
        )
    return out


def rss_items(url, label, limit=15):
    raw = try_get(url, label)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {label} parse: {e}", file=sys.stderr)
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = item.findtext("description") or ""
        content = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
        body = strip_html(content or desc)
        out.append(
            {
                "title": title,
                "url": link,
                "date_utc": pub,
                "summary": body[:1200],
            }
        )
        if len(out) >= limit:
            break
    return out


def youtube_atom(channel_id, label, limit=15):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    raw = try_get(url, label)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {label} parse: {e}", file=sys.stderr)
        return []
    out = []
    for entry in root.findall(f"{ATOM}entry"):
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        published = (entry.findtext(f"{ATOM}published") or "").strip()
        link_el = entry.find(f"{ATOM}link")
        link = link_el.get("href") if link_el is not None else ""
        group = entry.find(f"{MEDIA}group")
        desc = ""
        if group is not None:
            desc = group.findtext(f"{MEDIA}description") or ""
        # Strip the subscription boilerplate the channels append to every description
        desc = re.split(r"Watch The Platform live on YouTube", desc)[0]
        desc = re.split(r"Subscribe to (?:RCR|Reality Check)", desc)[0]
        out.append(
            {
                "title": title,
                "url": link,
                "date_utc": published,
                "summary": strip_html(desc)[:1200],
            }
        )
        if len(out) >= limit:
            break
    return out


def platform_opinions(limit=12):
    raw = try_get("https://theplatform.kiwi/sitemap.xml", "platform sitemap")
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL sitemap parse: {e}", file=sys.stderr)
        return []
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    rows = []
    for u in root.findall(f"{ns}url"):
        loc = (u.findtext(f"{ns}loc") or "").strip()
        mod = (u.findtext(f"{ns}lastmod") or "").strip()
        if "/opinions/" in loc:
            rows.append({"url": loc, "date_utc": mod})
    rows.sort(key=lambda r: r["date_utc"], reverse=True)
    out = []
    for r in rows[:limit]:
        slug = r["url"].rstrip("/").split("/")[-1]
        title = slug.replace("-", " ").strip().title()
        out.append(
            {
                "title": title,
                "url": r["url"],
                "date_utc": r["date_utc"],
                "summary": "(opinion piece — title derived from URL slug; fetch the URL for full text)",
            }
        )
    return out


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
    return out


def main():
    print("Fetching Reality Check Radio…", file=sys.stderr)
    rcr = []
    rcr += wp_json(
        "https://realitycheck.radio/wp-json/wp/v2/posts?per_page=15"
        "&_fields=id,date,date_gmt,title,link,excerpt",
        "rcr wp-json posts",
    )
    rcr += wp_json(
        "https://realitycheck.radio/wp-json/wp/v2/episodes?per_page=15"
        "&_fields=id,date,date_gmt,title,link,excerpt",
        "rcr wp-json episodes",
    )
    rcr += rss_items("https://realitycheck.radio/?feed=allepisodes", "rcr allepisodes rss")
    rcr += rss_items("https://realitycheck.radio/feed/", "rcr article rss")
    rcr_yt = youtube_atom("UC5IxWEvo3qMN2ug3t1DclUQ", "rcr youtube")

    print("Fetching The Platform NZ…", file=sys.stderr)
    plat_yt = youtube_atom("UCYKvkaqOJFwji8-Jgm8pjhA", "platform youtube")
    plat_ops = platform_opinions()

    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": (
            "Mirror of recent NZ independent-media output, refreshed by GitHub Actions. "
            "Exists because the brief's cloud runner cannot reach these domains directly. "
            "date_utc formats vary by source (RFC 2822 for RSS, ISO 8601 for JSON/Atom)."
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

    counts = {
        "rcr_items": len(payload["reality_check_radio"]["articles_and_episodes"]),
        "rcr_youtube": len(rcr_yt),
        "platform_youtube": len(plat_yt),
        "platform_opinions": len(plat_ops),
    }
    payload["counts"] = counts
    print(f"Counts: {counts}", file=sys.stderr)

    if counts["rcr_items"] == 0 and counts["platform_youtube"] == 0:
        print("ERROR: no content retrieved from either outlet", file=sys.stderr)
        sys.exit(1)

    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print("wrote data/latest.json", file=sys.stderr)


if __name__ == "__main__":
    main()
