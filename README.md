# nz-altmedia-mirror

Feeds and page renderer for Evan's daily news brief.

The cloud environment that writes the brief blocks most news sites at its network
proxy, but it can clone this repo from github.com. GitHub Actions runners have
unrestricted internet, so this repo fetches the feeds on a schedule and commits the
result where the brief can read it.

Every source is a public feed the outlet publishes itself.

## What's in it

| Path | What it is |
|---|---|
| `data/latest.json` | Alternative media: Reality Check Radio (WordPress API, RSS, YouTube) and The Platform NZ (YouTube, opinion pieces). |
| `data/news.json` | Mainstream headline feeds grouped as `nz`, `world` and `ai`: RNZ, 1News, NZ Herald, Stuff, Newsroom, The Spinoff, interest.co.nz, Waatea, BBC, Al Jazeera, Guardian, NPR, ABC, The Verge, TechCrunch, Ars Technica, MIT Technology Review and Google News sections. |
| `brief/render.py` | Turns the brief's content file into the published page, and refuses repeats (see below). |
| `brief/style.css`, `brief/app.js` | The page's look and behaviour. Change the design here, not in the routine prompt. |
| `brief/sample.json` | A complete example content file (19 Nov 2026), also used by CI. |

Both data files carry `generated_utc` and a `sources` block that records, for every
feed, whether it worked, how many items it returned, the newest item's date and the
error if it failed. The brief shows this as **Source health** at the bottom of the page.
All dates are ISO 8601 UTC and lists are newest first.

## Reading it from the brief

```bash
git clone -q --depth 1 https://github.com/EVAN-1992/nz-altmedia-mirror mirror
```

Use the clone, not the GitHub contents API: unauthenticated API calls from the cloud
runner's shared IP addresses hit GitHub's rate limit and get refused. If the clone
fails, `https://raw.githubusercontent.com/EVAN-1992/nz-altmedia-mirror/master/data/latest.json`
serves the same files.

## The renderer and the repeat rules

```bash
python3 mirror/brief/render.py ledger --prev prev.html      # what ran recently
python3 mirror/brief/render.py check brief.json --prev prev.html
python3 mirror/brief/render.py build brief.json --prev prev.html --data mirror/data -o index.html
```

The published page carries a hidden ledger of the last 14 days of story keys. `check`
fails, with a numbered list of fixes, when:

- a story from the last brief is presented as new rather than as an `update` with `whats_new`;
- a story has run in full on 3 of the last 5 days without an `override_reason` (it belongs in **Still developing**, one line);
- an item's newest development is more than 3 days old;
- a section has more than 6 items, a summary is far outside 60 to 120 words, or a source is missing or malformed.

## Schedule

Every 3 hours (`23 */3 * * *`), plus on demand from the Actions tab. GitHub delays
scheduled runs by hours at busy times; the old once-a-day run often landed after the
brief had already read a day-old snapshot.
