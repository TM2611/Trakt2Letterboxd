# Trakt2Letterboxd — Public History Sync Plan

## Goal

Run a weekly Trakt → Letterboxd sync that reads movie history from a public
Trakt profile, exports Letterboxd-ready CSVs, and optionally uploads them through
Playwright without requiring an interactive Trakt session.

## Configuration

The Trakt side uses only:

- `TRAKT_USERNAME` — required public profile name.
- `TRAKT_CLIENT_ID` — optional override for the supplied default public API key.

`LETTERBOXD_SESSION_COOKIE` remains required only when the upload step is used.

The Trakt request is an unauthenticated `GET` to:

```text
https://api.trakt.tv/users/{username}/history/movies
```

It sends exactly these headers:

```json
{
  "Content-Type": "application/json",
  "trakt-api-version": "2",
  "trakt-api-key": "0128c95089a7b58477204806a1b62ee130182b48c121c1eb9fe1d37b915fc5cb"
}
```

The profile must be configured as **Public** in Trakt Privacy Settings.

## Data extraction

- Fetch only public movie history.
- Paginate with a limit of 100 and honor `X-Pagination-Page-Count`.
- Keep entries containing a `movie` object and drop non-movie records.
- Preserve the Letterboxd columns `WatchedDate`, `tmdbID`, `imdbID`, `Title`, and
  `Year`.

## CSV generation

- Build CSV content with the standard library `csv` module.
- Keep each file below 950 KB to leave room under Letterboxd's 1 MB limit.
- Repeat the exact header row in every numbered part.
- Write `letterboxd_history.csv` or numbered parts into `exports/`.
- Do not create an output file when the public history is empty.

## Letterboxd upload

- Keep the existing Playwright uploader and `lbx_session` cookie injection.
- Do not use username/password login.
- Upload one CSV at a time and wait for the import confirmation.
- Save diagnostic screenshots to `debug/` when a browser or challenge failure
  occurs.

## GitHub Actions workflow

The workflow runs on the Sunday schedule or through manual dispatch:

1. Check out the repository.
2. Install Python dependencies and Chromium.
3. Pass `TRAKT_USERNAME`, the default or overridden `TRAKT_CLIENT_ID`, and
   `LETTERBOXD_SESSION_COOKIE` through the step environment.
4. Run the history-only exporter, optionally with `--skip-upload`.
5. Upload generated CSVs and failure screenshots as artifacts.

No Trakt session data, secret-management PAT, or write permission is needed by
the workflow.

## Files

| File | Responsibility |
|---|---|
| `Trakt2Letterboxd.py` | Public history extraction, CSV generation, and upload |
| `.env.example` | Local environment template |
| `.github/workflows/trakt-sync.yml` | Scheduled and manual automation |
| `SETUP.md` | Public-profile and GitHub configuration guide |
| `README.md` | Project overview and quick start |
| `.gitignore` | Local artifacts and generated CSV protection |

## Validation

- Compile the Python entry point.
- Verify the CLI exposes only `--skip-upload` and `--export-dir`.
- Search the project for removed authentication and list-export concepts.
- Confirm the workflow passes only the current Trakt configuration and the
  Letterboxd cookie.
