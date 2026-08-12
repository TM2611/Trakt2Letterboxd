# Trakt2Letterboxd

This project exports a user's Trakt movie history into Letterboxd-ready CSV
files. It can run locally or automatically every Sunday in GitHub Actions.

## Automated sync

The workflow:

- Reads movie history from the user's **public Trakt profile**.
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

Install dependencies and set the profile name:

```bash
pip install -r requirements.txt
export TRAKT_USERNAME=your_trakt_username
```

Export history only:

```bash
python Trakt2Letterboxd.py --skip-upload
```

Export and upload:

```bash
export LETTERBOXD_SESSION_COOKIE=your_session_cookie_value
export LETTERBOXD_CSRF_COOKIE=your_csrf_cookie_value
export LETTERBOXD_CF_CLEARANCE=your_cf_clearance_value
python Trakt2Letterboxd.py
```

The optional `TRAKT_CLIENT_ID` environment variable can override the supplied
default when a different public API key is intentionally required.

## Output

Files are written to `exports/`:

- `letterboxd_history.csv`, or numbered parts for a large history export.
- Every file contains `WatchedDate,tmdbID,imdbID,Title,Year` headers.
- TV shows, seasons, and episodes are excluded.

In GitHub Actions, the generated files are also available in the
`letterboxd-ready-csvs` artifact. If an upload fails, screenshots are stored in
the `letterboxd-upload-debug` artifact.

## Privacy and credentials

The Trakt profile must be public for the history endpoint to return data. No
private Trakt account credential is needed. Treat the Letterboxd cookie values
like passwords and store them only in a local environment or GitHub Secrets.
