# Trakt2Letterboxd

This project exports a user's Trakt movie history and ratings into
Letterboxd-ready CSV files. It can run locally or automatically every Sunday in
GitHub Actions.

## Automated sync

The workflow:

- Reads movie history from the user's **public Trakt profile**.
- Reads public movie ratings and merges the latest rating onto every matching
  watch event.
- Uses the public Trakt API request without a private account login.
- Excludes non-movie records defensively.
- Splits exports into CSV files below Letterboxd's 1 MB import limit.
- Uploads the files to Letterboxd using injected session, CSRF, and Cloudflare
  clearance cookies.
- Keeps the CSVs attached as a seven-day `letterboxd-ready-csvs` artifact.

Follow [`SETUP.md`](SETUP.md) for the complete configuration walkthrough. The
important Trakt requirement is to make the profile public and add only
`TRAKT_USERNAME` to the repository secrets. The supplied public Client ID is
already the default used by the script and workflow.

## Local usage

Requirements:

- Python 3.x
- A public Trakt profile
- Letterboxd session and CSRF cookies (plus `cf_clearance`) if uploading locally

Install dependencies and create a local environment file from the template:

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set TRAKT_USERNAME and, when uploading, the Letterboxd cookies.
```

Export history only:

```bash
python Trakt2Letterboxd.py --skip-upload
```

Export and upload using the same `.env` values:

```bash
python Trakt2Letterboxd.py
```

For local upload debugging, use headed mode to show the Chromium window:

```bash
python Trakt2Letterboxd.py --headed
```

Headed mode is intended for local runs and pauses after an upload failure so the
current browser page can be inspected before it closes. The Trakt HTTP requests
and all Playwright waits use a 15-second timeout. Scheduled GitHub Actions runs
remain headless unless the flag is explicitly supplied.

The script loads `.env` automatically when run locally. Values explicitly set in
the shell take precedence, so CI and one-off overrides continue to work. Keep
`.env` private; it is ignored by Git.

The optional `TRAKT_CLIENT_ID` environment variable can override the supplied
default when a different public API key is intentionally required.

## Output

Files are written to `exports/`:

- `letterboxd_history.csv`, or numbered parts for a large history export.
- Every file contains `WatchedDate,tmdbID,imdbID,Title,Year,Rating10` headers.
- Trakt's integer 1–10 ratings are written to Letterboxd's `Rating10` column;
  watched-but-unrated movies have a blank value.
- TV shows, seasons, and episodes are excluded.

In GitHub Actions, the generated files are also available in the
`letterboxd-ready-csvs` artifact. If an upload fails, screenshots are stored in
the `letterboxd-upload-debug` artifact.

## Privacy and credentials

The Trakt profile must be public for the history and ratings endpoints to return
data. No private Trakt account credential is needed. Treat the Letterboxd cookie
values like passwords and store them only in a local environment or GitHub
Secrets.
