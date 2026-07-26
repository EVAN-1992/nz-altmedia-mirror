# nz-altmedia-mirror

A tiny relay that keeps recent output from two New Zealand independent outlets —
**Reality Check Radio** and **The Platform NZ** — available at a URL that Evan's
daily news brief can actually reach.

## Why this exists

The cloud environment that generates the daily brief has an outbound egress policy
that returns `403` at the proxy for `realitycheck.radio`, `theplatform.kiwi` and
`youtube.com` (and, on some runs, for most other hosts too). `api.github.com` is on
its permanent allowlist.

GitHub Actions runners have unrestricted internet access. So this repo fetches the
feeds from here, on a schedule, and commits the result — turning an unreachable
source into a reachable one.

It is a workaround for an infrastructure restriction, not a scraper of anything
private: every source below is a public feed the outlets publish themselves.

## What it collects

**Reality Check Radio** (via `realitycheck.radio` — note the legacy domain; the newer
`rcr.media` is a JavaScript app that serves no content to non-browser clients)
- `wp-json/wp/v2/posts` and `wp-json/wp/v2/episodes` — the WordPress REST API
- `?feed=allepisodes` and `/feed/` — RSS, the latter with full article bodies
- YouTube Atom feed (channel `UC5IxWEvo3qMN2ug3t1DclUQ`)

**The Platform NZ** (`theplatform.kiwi` is server-rendered and unblocked, but its
pages are ~1.3 MB, which truncates in most fetch tools)
- YouTube Atom feed (channel `UCYKvkaqOJFwji8-Jgm8pjhA`) — the best source; ~5
  posts a day, with descriptions carrying the interview substance
- `sitemap.xml`, filtered to `/opinions/` with their `lastmod` dates

## Consuming it

The brief reads the mirror through the GitHub API, which its sandbox permits:

```bash
curl -sL -H "Accept: application/vnd.github.raw" \
  https://api.github.com/repos/EVAN-1992/nz-altmedia-mirror/contents/data/latest.json
```

`data/latest.json` carries a `generated_utc` timestamp and a `counts` block, so a
consumer can tell stale data from fresh and say so honestly rather than implying
an outlet went quiet.

## Schedule

Refreshes at 11:05 UTC daily — about 40 minutes ahead of the brief — and can be run
on demand from the Actions tab.
