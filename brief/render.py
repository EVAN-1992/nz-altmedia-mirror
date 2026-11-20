#!/usr/bin/env python3
"""
Render Evan's daily brief from a content file (brief.json) into the page that gets
published, and enforce the rules that keep it fresh.

  python3 brief/render.py ledger  --prev PREV.html
      What ran on recent days: story keys, how many days each has run, and which
      ones today's brief must treat as updates. Run this BEFORE researching.

  python3 brief/render.py check   brief.json --prev PREV.html
      Validate only. Exit 1 with a numbered list of problems to fix.

  python3 brief/render.py build   brief.json --prev PREV.html --data data -o index.html
      Validate, then write the page. Refuses to write when validation fails.

  python3 brief/render.py selftest
      Build the bundled sample (used by CI so a broken renderer is caught early).

Why: each run used to write its own HTML from scratch, so the layout drifted and
broke, and nothing stopped the same war story leading the brief day after day. The
page now carries a hidden ledger of the last 14 days of stories, and `check` refuses
a repeat that doesn't say what's new.
"""

import argparse
import html
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER_DAYS = 14
STREAK_LIMIT = 3          # a story run as a full item on this many of the last 5 days
STREAK_WINDOW = 5         #   needs an override_reason to run in full again
MAX_ITEM_AGE_DAYS = 3     # older than this cannot be a full item
SECTION_ORDER = ["intl", "nz", "ai", "alt"]
SECTION_TAG = {"intl": "World", "nz": "NZ", "ai": "AI", "alt": "Alt"}
SOURCING = {
    "read": "Read in full",
    "official": "Official release",
    "search": "Search results only",
    "feed": "Feed headline and excerpt",
    "mirror": "Mirror feed",
    "video": "Video description only",
}
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
URL = re.compile(r"^https?://[^\s\"'<>]+$")

e = html.escape


# ---------------------------------------------------------------- ledger

LEDGER_RE = re.compile(r'<script type="application/json" id="brief-ledger">(.*?)</script>', re.S)


def read_ledger(prev_path):
    if not prev_path or not os.path.exists(prev_path):
        return {"days": []}
    raw = open(prev_path, encoding="utf-8").read()
    m = LEDGER_RE.search(raw)
    if not m:
        return {"days": []}
    try:
        led = json.loads(m.group(1))
        led.setdefault("days", [])
        return led
    except Exception:  # noqa: BLE001
        return {"days": []}


def prior_days(ledger, today):
    return sorted((d for d in ledger["days"] if d.get("date", "") < today), key=lambda d: d["date"], reverse=True)


def streak(story, today, ledger):
    """Consecutive days (ending today) this story has been in the brief, in any form."""
    n, cursor = 1, date.fromisoformat(today)
    days = {d["date"]: d for d in ledger["days"]}
    while True:
        cursor -= timedelta(days=1)
        d = days.get(cursor.isoformat())
        if not d or (story not in d.get("full", {}) and story not in d.get("developing", {})):
            return n
        n += 1


def full_count(story, today, ledger, window=STREAK_WINDOW):
    start = (date.fromisoformat(today) - timedelta(days=window)).isoformat()
    return sum(1 for d in prior_days(ledger, today) if d["date"] >= start and story in d.get("full", {}))


def ledger_report(ledger, today):
    days = prior_days(ledger, today)
    if not days:
        return "No ledger found (first run of the new format). Every story counts as new."
    last = days[0]
    lines = [f"Ledger covers {len(days)} day(s); most recent {last['date']}.", ""]
    seen = {}
    for d in days:
        for k, h in d.get("full", {}).items():
            seen.setdefault(k, {"label": h, "full": 0, "dev": 0})["full"] += 1
        for k, h in d.get("developing", {}).items():
            seen.setdefault(k, {"label": h, "full": 0, "dev": 0})["dev"] += 1
    lines.append("Stories in the last brief. If you cover one again it MUST be status 'update' with 'whats_new':")
    for k in list(last.get("full", {})) + list(last.get("developing", {})):
        s = seen[k]
        flag = ""
        if full_count(k, today, ledger) >= STREAK_LIMIT:
            flag = "  <- ran in full on 3+ of the last 5 days: developing strip only, unless override_reason"
        lines.append(f"  {k:<40} day {streak(k, today, ledger)}, full {s['full']}x, developing {s['dev']}x  ({s['label'][:70]}){flag}")
    older = [k for k in seen if k not in last.get("full", {}) and k not in last.get("developing", {})]
    if older:
        lines += ["", "Earlier this fortnight (reuse the same key if a story returns):"]
        lines += [f"  {k:<40} {seen[k]['label'][:80]}" for k in older]
    return "\n".join(lines)


def updated_ledger(ledger, brief):
    today = brief["date"]
    full = {it["story"]: it["headline"] for s in brief["sections"] for it in s.get("items", [])}
    dev = {d["story"]: d["label"] for d in brief.get("developing", [])}
    keep = [d for d in ledger["days"] if d.get("date") != today]
    keep.append({"date": today, "full": full, "developing": dev})
    keep.sort(key=lambda d: d["date"], reverse=True)
    return {"version": 1, "days": keep[:LEDGER_DAYS]}


# ---------------------------------------------------------------- validation

def words(s):
    return len(re.findall(r"\w+", s or ""))


def validate(brief, ledger):
    errs, warns = [], []
    E, W = errs.append, warns.append

    for k in ("date", "date_label", "window_note", "sections"):
        if not brief.get(k):
            E(f"top level: '{k}' is required")
    today = brief.get("date", "")
    try:
        today_d = date.fromisoformat(today)
    except Exception:  # noqa: BLE001
        E("top level: 'date' must be YYYY-MM-DD (the NZ date from Step Zero)")
        return errs, warns

    last = (prior_days(ledger, today) or [{}])[0]
    last_stories = set(last.get("full", {})) | set(last.get("developing", {}))
    used = {}

    def check_sources(where, srcs, minimum=1):
        if not isinstance(srcs, list) or len(srcs) < minimum:
            E(f"{where}: needs at least {minimum} source(s)")
            return
        for s in srcs:
            if not isinstance(s, dict) or not s.get("name") or not URL.match(s.get("url", "")):
                E(f"{where}: every source needs a 'name' and an http(s) 'url' (got {json.dumps(s)[:120]})")

    def check_story(where, key):
        if not key or not SLUG.match(key) or len(key) > 48:
            E(f"{where}: 'story' must be a short lowercase-hyphen key like 'iran-us-war' (got {key!r})")
            return False
        if key in used:
            E(f"{where}: story '{key}' already used by {used[key]}; one entry per story per brief")
            return False
        used[key] = where
        return True

    for i, d in enumerate(brief.get("developing", []), 1):
        where = f"developing[{i}]"
        check_story(where, d.get("story"))
        if not d.get("label"):
            E(f"{where}: 'label' is required (e.g. 'Iran–US war')")
        if not d.get("update"):
            E(f"{where}: 'update' is required: one sentence on what changed since the last brief")
        elif words(d["update"]) > 45:
            E(f"{where}: 'update' is {words(d['update'])} words; keep it to one sentence under 45")
        check_sources(where, d.get("sources"))

    secs = brief.get("sections", [])
    ids = [s.get("id") for s in secs]
    for sid in ids:
        if sid not in SECTION_ORDER:
            E(f"sections: unknown id {sid!r}; use {SECTION_ORDER}")
    if len(set(ids)) != len(ids):
        E("sections: each section id may appear once")

    total = 0
    for s in secs:
        sid = s.get("id", "?")
        its = s.get("items", [])
        total += len(its)
        if not s.get("title"):
            E(f"section {sid}: 'title' is required")
        if len(its) > 6:
            E(f"section {sid}: {len(its)} items; the cap is 6, move the rest to 'radar'")
        if not its and not s.get("empty_note"):
            E(f"section {sid}: no items; add 'empty_note' saying honestly why")
        for j, it in enumerate(its, 1):
            where = f"{sid} item {j} ({(it.get('headline') or '')[:50]!r})"
            story = it.get("story")
            ok = check_story(where, story)
            for k in ("headline", "summary", "published", "sourcing", "status"):
                if not it.get(k):
                    E(f"{where}: '{k}' is required")
            if it.get("status") not in (None, "new", "update"):
                E(f"{where}: 'status' must be 'new' or 'update'")
            if it.get("sourcing") and it["sourcing"] not in SOURCING:
                E(f"{where}: 'sourcing' must be one of {list(SOURCING)}")
            if len(it.get("headline", "")) > 150:
                E(f"{where}: headline over 150 characters")
            n = words(it.get("summary"))
            if it.get("summary") and not 35 <= n <= 150:
                E(f"{where}: summary is {n} words; aim for 60-120")
            try:
                age = (today_d - date.fromisoformat(it.get("published", ""))).days
                if age > MAX_ITEM_AGE_DAYS:
                    E(f"{where}: published {it['published']} is {age} days old. Too old for a full item: "
                      f"put it in 'developing' only if something changed, otherwise drop it")
                if age < -1:
                    E(f"{where}: published date {it['published']} is in the future")
            except Exception:  # noqa: BLE001
                if it.get("published"):
                    E(f"{where}: 'published' must be YYYY-MM-DD (date of the newest development)")
            check_sources(where, it.get("sources"), 2 if it.get("sourcing") in ("search", "feed") else 1)
            if ok and story in last_stories:
                if it.get("status") != "update":
                    E(f"{where}: story '{story}' ran in the last brief ({last.get('date')}). Mark it "
                      f"status 'update' with 'whats_new', or move it to 'developing'")
                elif words(it.get("whats_new")) < 6:
                    E(f"{where}: 'whats_new' must say in a sentence what changed since {last.get('date')}")
            if ok and it.get("status") == "update" and story not in last_stories and not it.get("whats_new"):
                W(f"{where}: marked 'update' but not in the last brief; add 'whats_new' or mark it 'new'")
            if ok and full_count(story, today, ledger) >= STREAK_LIMIT and not it.get("override_reason"):
                E(f"{where}: story '{story}' has run in full on {full_count(story, today, ledger)} of the last "
                  f"{STREAK_WINDOW} days. Move it to 'developing', or add 'override_reason' naming the major turn "
                  f"(ceasefire, deal, mass-casualty escalation, resignation...)")
        for k, r in enumerate(s.get("radar", []), 1):
            if not r.get("text"):
                E(f"{sid} radar {k}: 'text' is required")
            for src in r.get("sources", []):
                if not URL.match(src.get("url", "")):
                    E(f"{sid} radar {k}: bad source url {src.get('url')!r}")

    if total < 6:
        W(f"only {total} full items in the whole brief")
    for g in brief.get("glance", []):
        if g not in used:
            E(f"glance: {g!r} is not a story key used in this brief")
    return errs, warns


# ---------------------------------------------------------------- source health

def age_h(stamp, ref):
    try:
        t = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (ref - t).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return None


def load_health(data_dir, ref):
    out = []
    for fname, label in (("latest.json", "Alt-media mirror"), ("news.json", "Headline feeds")):
        p = os.path.join(data_dir or "", fname)
        if not data_dir or not os.path.exists(p):
            out.append({"label": label, "missing": True})
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception as ex:  # noqa: BLE001
            out.append({"label": label, "missing": True, "error": str(ex)})
            continue
        out.append({
            "label": label,
            "generated": d.get("generated_utc"),
            "age": age_h(d.get("generated_utc", ""), ref),
            "sources": d.get("sources", {}),
        })
    return out


def render_health(health, notes):
    rows, summary = [], []
    for h in health:
        if h.get("missing"):
            summary.append(f'<li><span class="dot d-bad"></span><b>{e(h["label"])}</b>: not available this run</li>')
            continue
        srcs = h["sources"]
        ok = sum(1 for v in srcs.values() if v.get("ok"))
        age = h["age"]
        cls = "d-ok" if (age is not None and age <= 8 and ok == len(srcs)) else "d-warn"
        if age is None or age > 30 or (srcs and ok < len(srcs) / 2):
            cls = "d-bad"
        age_txt = f"{age:.0f} h old" if age is not None else "age unknown"
        summary.append(
            f'<li><span class="dot {cls}"></span><b>{e(h["label"])}</b>: {age_txt}, '
            f"{ok} of {len(srcs)} sources working</li>"
        )
        for name, v in sorted(srcs.items(), key=lambda kv: (kv[1].get("ok", False), kv[0])):
            newest = (v.get("newest_utc") or "")[:16].replace("T", " ")
            rows.append(
                f'<tr><td><span class="dot {"d-ok" if v.get("ok") else "d-bad"}"></span>{e(name)}</td>'
                f'<td>{v.get("items", 0)}</td><td>{e(newest) or "–"}</td>'
                f'<td class="err">{e(v.get("error") or "")}</td></tr>'
            )
    note_html = "".join(f"<li>{e(n)}</li>" for n in notes)
    table = (
        '<details><summary>Every source</summary><div class="tbl"><table><thead><tr>'
        "<th>Source</th><th>Items</th><th>Newest (UTC)</th><th>Problem</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div></details>"
    ) if rows else ""
    return (
        '<section class="health" id="sources" aria-labelledby="h-sources"><h2 id="h-sources">Source health</h2>'
        f'<ul class="sum">{"".join(summary)}</ul>{table}'
        + (f'<ul class="notes">{note_html}</ul>' if note_html else "")
        + "</section>"
    )


# ---------------------------------------------------------------- page

ICON_UP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 10v11H4V10h3zm0 0l4-8a3 3 0 013 3v4h5.5a2 2 0 012 2.3l-1.4 8A2 2 0 0118.1 21H7"/></svg>'
ICON_DOWN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 14V3h3v11h-3zm0 0l-4 8a3 3 0 01-3-3v-4H4.5a2 2 0 01-2-2.3l1.4-8A2 2 0 015.9 3H17"/></svg>'
ICON_SPEAK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 010 7M19 5a10 10 0 010 14"/></svg>'


def links(srcs):
    return "".join(f'<a href="{e(s["url"])}" target="_blank" rel="noopener">{e(s["name"])}</a>' for s in srcs)


def render_item(sid, n, it, today, ledger):
    iid = f"{sid}-{n}"
    status = it.get("status", "new")
    pills = [f'<span class="pill p-{status}">{"Update" if status == "update" else "New"}</span>']
    if status == "update":
        day = streak(it["story"], today, ledger)
        if day > 1:
            pills.append(f'<span class="when">day {day} in the brief</span>')
    if it.get("outlet"):
        pills.append(f'<span class="pill p-outlet">{e(it["outlet"])}</span>')
    try:
        pub = date.fromisoformat(it["published"]).strftime("%a %-d %b")
        pills.append(f'<span class="when">{e(pub)}</span>')
    except Exception:  # noqa: BLE001
        pass
    wn = (f'<p class="whatsnew"><b>What\'s new:</b> {e(it["whats_new"])}</p>' if it.get("whats_new") else "")
    disc = f'<p class="disclosure">{e(it["disclosure"])}</p>' if it.get("disclosure") else ""
    corro = f'<p class="corro">{e(it["corroboration"])}</p>' if it.get("corroboration") else ""
    how = SOURCING.get(it.get("sourcing"), "")
    note = f'{how}. {it["verify_note"]}' if it.get("verify_note") else how
    return (
        f'<article class="item" id="{iid}" data-section="{sid}" data-story="{e(it["story"])}" data-headline="{e(it["headline"])}">'
        f'<div class="pills">{"".join(pills)}</div>'
        f'<h3>{e(it["headline"])}</h3>{disc}{wn}'
        f'<p class="summary">{e(it["summary"])}</p>{corro}'
        f'<div class="foot"><span class="srcs">{links(it["sources"])}</span>'
        f'<span class="how">{e(note)}</span>'
        f'<span class="votes"><button class="vote" data-vote="up" aria-pressed="false" aria-label="More like this">{ICON_UP}</button>'
        f'<button class="vote" data-vote="down" aria-pressed="false" aria-label="Less like this">{ICON_DOWN}</button></span></div>'
        "</article>"
    )


def build(brief, ledger, data_dir):
    today = brief["date"]
    secs = sorted(brief["sections"], key=lambda s: SECTION_ORDER.index(s["id"]))
    ref = datetime.now(timezone.utc)
    if brief.get("generated_utc"):
        try:
            ref = datetime.strptime(brief["generated_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            pass

    n_items = sum(len(s.get("items", [])) for s in secs)
    n_words = sum(words(it.get("headline")) + words(it.get("summary")) + words(it.get("whats_new"))
                  for s in secs for it in s.get("items", []))
    n_words += sum(words(d.get("update")) for d in brief.get("developing", []))
    mins = max(1, math.ceil(n_words / 230))
    n_new = sum(1 for s in secs for it in s.get("items", []) if it.get("status") != "update")

    # at a glance: chosen story keys, or the first item of each section
    by_story = {}
    for s in secs:
        for n, it in enumerate(s.get("items", []), 1):
            by_story[it["story"]] = (s["id"], f'{s["id"]}-{n}', it["headline"])
    keys = brief.get("glance") or [s["items"][0]["story"] for s in secs if s.get("items")]
    glance = "".join(
        f'<li><span class="tag t-{sid}">{SECTION_TAG[sid]}</span><a href="#{iid}">{e(h)}</a></li>'
        for sid, iid, h in (by_story[k] for k in keys if k in by_story)
    )

    dev_html = ""
    if brief.get("developing"):
        rows = "".join(
            f'<li><div class="row1"><span class="lbl">{e(d["label"])}</span>'
            f'<span class="day">day {streak(d["story"], today, ledger)}</span></div>'
            f'<span class="upd">{e(d["update"])}</span><span class="src">{links(d["sources"])}</span></li>'
            for d in brief["developing"]
        )
        dev_html = (
            '<section class="dev" aria-labelledby="h-dev"><h2 id="h-dev">Still developing</h2>'
            '<p class="lede">Long-running stories, in a line each. They get a full write-up again only when something big changes.</p>'
            f"<ul>{rows}</ul></section>"
        )

    nav = "".join(
        f'<a href="#{s["id"]}">{e(s["title"])} <b>{len(s.get("items", []))}</b></a>' for s in secs
    )

    sections = []
    for s in secs:
        sid = s["id"]
        items = "".join(render_item(sid, n, it, today, ledger) for n, it in enumerate(s.get("items", []), 1))
        if not items:
            items = f'<p class="empty">{e(s.get("empty_note", ""))}</p>'
        radar = ""
        if s.get("radar"):
            radar = '<div class="radar"><h4>Also on the radar</h4><ul>' + "".join(
                f'<li>{e(r["text"])}{links(r.get("sources", []))}</li>' for r in s["radar"]
            ) + "</ul></div>"
        note = f'<p class="sec-note">{e(s["note"])}</p>' if s.get("note") else ""
        sections.append(
            f'<section class="sec" id="{sid}" aria-labelledby="h-{sid}">'
            f'<div class="sec-h"><span class="sw" style="background:var(--sec-{sid})"></span>'
            f'<h2 id="h-{sid}">{e(s["title"])}</h2><span class="n">{len(s.get("items", []))} stories</span></div>'
            f"{note}{items}{radar}</section>"
        )

    health = render_health(load_health(data_dir, ref), brief.get("run_notes", []))
    led = json.dumps(updated_ledger(ledger, brief), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    meta = json.dumps({"date": today}).replace("</", "<\\/")
    css = open(os.path.join(HERE, "style.css"), encoding="utf-8").read()
    js = open(os.path.join(HERE, "app.js"), encoding="utf-8").read()
    short = date.fromisoformat(today).strftime("%a %-d %b")

    return f"""<title>Evan's Daily Brief</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;600;700&family=Newsreader:opsz,wght@6..72,400;6..72,500&family=Schibsted+Grotesk:wght@500;600;700;800&display=swap">
<style>{css}</style>
<header class="bar gutter"><div class="col bar-row">
<span class="brand">Daily Brief <small>{e(short)}</small></span>
<button class="btn" id="listenBtn" aria-pressed="false">{ICON_SPEAK}<span id="listenLbl">Listen</span></button>
<button class="btn" id="fbBtn">Feedback</button>
</div><nav class="col nav" aria-label="Sections">{nav}</nav></header>
<main class="gutter"><div class="col">
<div class="mast"><span class="kicker">Evan's Daily Brief · NZ morning edition</span>
<h1>{e(brief["date_label"])}</h1>
<div class="stats"><span><b>{n_items}</b> stories</span><span><b>{n_new}</b> new since yesterday</span><span><b>{len(brief.get("developing", []))}</b> still developing</span><span>about <b>{mins}</b> min</span></div>
<p class="sec-note">{e(brief["window_note"])}</p></div>
<section class="glance" aria-labelledby="h-glance"><h2 id="h-glance">At a glance</h2><ol>{glance}</ol></section>
{dev_html}
{"".join(sections)}
{health}
<footer><p>Thumbs up or down on any story is saved for tomorrow's run, which uses it to pick what to cover. The Feedback button copies your votes as text too.</p>
<p>Every story says how it was sourced. Nothing here is invented; anything unconfirmed is marked as such.</p></footer>
<textarea id="fb-text" hidden readonly aria-label="Feedback text" style="width:100%;min-height:8rem;margin-bottom:2rem"></textarea>
</div></main>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script type="application/json" id="brief-meta">{meta}</script>
<script type="application/json" id="brief-ledger">{led}</script>
<script>{js}</script>
"""


# ---------------------------------------------------------------- cli

def load_json(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception as ex:  # noqa: BLE001
        sys.exit(f"Cannot read {p}: {ex}")


def report(errs, warns):
    for w in warns:
        print(f"warning: {w}", file=sys.stderr)
    if errs:
        print(f"\n{len(errs)} problem(s) to fix before publishing:", file=sys.stderr)
        for i, x in enumerate(errs, 1):
            print(f"  {i}. {x}", file=sys.stderr)
        return False
    print("check: OK", file=sys.stderr)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ledger", "check", "build", "selftest"])
    ap.add_argument("brief", nargs="?")
    ap.add_argument("--prev")
    ap.add_argument("--data")
    ap.add_argument("--today", help="NZ date for 'ledger' (default: today in NZ)")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()

    if a.cmd == "selftest":
        brief = load_json(os.path.join(HERE, "sample.json"))
        ok = report(*validate(brief, {"days": []}))
        page = build(brief, {"days": []}, os.path.join(HERE, "..", "data"))
        assert 'id="brief-ledger"' in page and page.count('class="item"') >= 1
        print(f"selftest: built {len(page)} bytes", file=sys.stderr)
        sys.exit(0 if ok else 1)

    ledger = read_ledger(a.prev)
    if a.prev and not ledger["days"]:
        print(f"note: no ledger found in {a.prev}; treating every story as new", file=sys.stderr)

    if a.cmd == "ledger":
        today = a.today
        if not today:
            try:
                from zoneinfo import ZoneInfo
                today = datetime.now(ZoneInfo("Pacific/Auckland")).date().isoformat()
            except Exception:  # noqa: BLE001
                today = (datetime.now(timezone.utc) + timedelta(hours=13)).date().isoformat()
        print(ledger_report(ledger, today))
        return

    if not a.brief:
        sys.exit("brief.json path required")
    brief = load_json(a.brief)
    ok = report(*validate(brief, ledger))
    if a.cmd == "check" or not ok:
        sys.exit(0 if ok else 1)
    page = build(brief, ledger, a.data)
    with open(a.out or "index.html", "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {a.out or 'index.html'} ({len(page)} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
