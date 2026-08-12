# Trakt2Letterboxd
This is a simple cross-platform script to export your movies from Trakt to Letterboxd.

## Automated sync (recommended)
This fork adds a fully-automated, headless pipeline that runs every Sunday in
GitHub Actions:

- Exports your Trakt **movie history and watchlist** (TV shows/episodes filtered out).
- Splits the export into Letterboxd-ready CSVs under the **1 MB import limit**
  (950 KB chunks, headers repeated per chunk).
- **Uploads them automatically** to letterboxd.com/import via Playwright using
  your `lbx_session` cookie — no downloads, no clicks.
- Tracks your Trakt access-token expiry and **rotates the tokens** by updating
  the repository secrets, so you never touch it again.
- Leaves the CSVs attached to each run as a downloadable artifact
  (`letterboxd-ready-csvs`, 7-day retention) as a safety net.

**Follow [`SETUP.md`](SETUP.md) to configure it.** It covers creating your own
Trakt API app, minting the initial tokens with cURL (no browser flow), grabbing
your `lbx_session` cookie, and adding the required GitHub Secrets.

The manual instructions below still work if you prefer to run locally.

## What you'll need

- **Python 3.x** — if you don't have it, download and install it from the [official Python website](https://www.python.org/downloads/). The default options during installation are fine.
- The **Trakt2Letterboxd.py** file — download it from this page by clicking the green **Code** button above, then **Download ZIP**, and unzip it somewhere easy to find like your Desktop.

## How to run it (locally)

1. Install dependencies: `pip install -r requirements.txt`
2. Set the environment variables listed in [`SETUP.md`](SETUP.md) (Trakt API app keys + tokens, Letterboxd session cookie).
3. Open your terminal (on Mac, search for **Terminal** in Spotlight; on Windows, search for **Command Prompt**).
4. Navigate to the folder where you put the script. For example, if it's on your Desktop:
   - **Mac:** `cd ~/Desktop/Trakt2Letterboxd`
   - **Windows:** `cd %USERPROFILE%\Desktop\Trakt2Letterboxd`
5. Run the script with: `python3 Trakt2Letterboxd.py`
   - To only generate the CSV files (no Letterboxd upload): `python3 Trakt2Letterboxd.py --skip-upload`

## Authentication

Authentication is now fully headless — no browser prompts, no codes to enter.
All credentials come from environment variables (`TRAKT_CLIENT_ID`,
`TRAKT_CLIENT_SECRET`, `TRAKT_ACCESS_TOKEN`, `TRAKT_REFRESH_TOKEN`,
`TRAKT_TOKEN_EXPIRES_AT`). The access token is refreshed automatically before
it expires (~3 months lifetime) using the refresh token, and the refreshed pair
is written to `GITHUB_OUTPUT` when running in CI so the workflow can update the
corresponding GitHub Secrets for the next run.

See [`SETUP.md`](SETUP.md) for how to produce your initial token pair with
cURL — the same device-code flow the old script used, minus the interactivity.

## What you get

Letterboxd-ready CSV files are written to the `exports/` folder:

- `letterboxd_history.csv` (or `_partN.csv` chunks) — all the movies you've watched on Trakt.
- `letterboxd_watchlist.csv` (or `_partN.csv` chunks) — all the movies on your Trakt watchlist.

Every file is kept under 1 MB (Letterboxd's import limit) with the exact
headers `WatchedDate, tmdbID, imdbID, Title, Year` repeated at the top of each
chunk. TV shows, seasons, and episodes are excluded — only movies are exported.

## Importing into Letterboxd

In GitHub Actions the import is **automatic** via Playwright. If you run the
script locally (or use the `--skip-upload` flag), the generated CSVs are also
attached to every workflow run as the `letterboxd-ready-csvs` artifact for
7 days — download them and go to [letterboxd.com/import](https://letterboxd.com/import/).

## Need help?

If you need more help running Python scripts, check these guides: [Windows](https://docs.python.org/3/faq/windows.html) and [MacOS](https://docs.python.org/3/using/mac.html). (Folks on Linux, you should already know what you're doing!). If nothing works, please feel free to raise a GitHub issue and I will try my best to guide you.

## Note
The script has now been updated to the new Trakt API spec (2026) and everything works as expected.