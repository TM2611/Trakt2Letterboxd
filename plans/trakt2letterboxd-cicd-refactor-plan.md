# Trakt2Letterboxd — Public History Sync Plan

## Goal

Run a weekly Trakt → Letterboxd sync that reads movie history from a public
Trakt profile, exports Letterboxd-ready CSVs, and optionally uploads them through
Playwright without requiring an interactive Trakt session.

## Configuration

The Trakt side uses only:

- `TRAKT_USERNAME` — required public profile name.
- `TRAKT_CLIENT_ID` — optional override for the supplied default public API key.

The upload step requires three cookies captured from a logged-in Letterboxd
browser session (see "Letterboxd cookie model" below). They are supplied as
GitHub secrets and are only read when the upload step runs.

The Trakt requests are unauthenticated `GET`s to:

```text
https://api.trakt.tv/users/{username}/history/movies
https://api.trakt.tv/users/{username}/ratings/movies
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

- Fetch public movie history and movie ratings.
- Paginate with a limit of 100 and honor `X-Pagination-Page-Count`.
- Keep entries containing a `movie` object and drop non-movie records.
- Preserve one output row per history event and merge the latest rating by
  `rated_at` using TMDB IDs first and IMDb IDs as a fallback.
- Preserve the Letterboxd columns `WatchedDate`, `tmdbID`, `imdbID`, `Title`,
  `Year`, and `Rating10`. Trakt's integer 1–10 rating is written directly to
  `Rating10`, which Letterboxd converts to its 0.5–5.0 scale.

## CSV generation

- Build CSV content with the standard library `csv` module.
- Keep each file below 950 KB to leave room under Letterboxd's 1 MB limit.
- Repeat the exact header row in every numbered part.
- Write `letterboxd_history.csv` or numbered parts into `exports/`.
- Do not create an output file when the public history is empty.

## Letterboxd upload

- Keep the existing Playwright uploader but replace the single `lbx_session`
  cookie with the three real cookies described below.
- Do not use username/password login.
- Upload one CSV at a time and wait for the import confirmation.
- Save diagnostic screenshots to `debug/` when a browser or challenge failure
  occurs.

### Letterboxd cookie model

A working upload needs more than one cookie. The uploader injects all of the
following before navigating to the import page:

| Cookie | Role | Expiry |
|---|---|---|
| `letterboxd.user.CURRENT` | The real session cookie (httpOnly, secure, SameSite=Lax). Proves the browser is logged in. | Session cookie; server-controlled, no fixed timer. Refresh when the run reports a login prompt (weeks-to-months). |
| `com.xk72.webparts.csrf` | CSRF token validated on the import form POST. Without it the submission may be rejected even with a valid session. | Short-lived; refresh alongside the session cookie. |
| `cf_clearance` | Cloudflare proof that the Turnstile challenge was passed; carrying it over is what keeps the headless browser from being challenged. | Long-lived (~1 year). |

The old `lbx_session` name does not exist on Letterboxd and is removed.

> Caveat: `cf_clearance` is bound to the IP address and user agent that solved
> the challenge. GitHub Actions runners use dynamic IPs, so a clearance cookie
> captured from a home browser may be rejected by Cloudflare from CI. The
> session and CSRF cookies are not IP-bound and should work. If `cf_clearance`
> proves unreliable in CI, the uploader still injects it but treats a Cloudflare
> challenge as a recoverable diagnostic rather than a hard failure.

## GitHub Actions workflow

The workflow runs on the Sunday schedule or through manual dispatch:

1. Check out the repository.
2. Install Python dependencies and Chromium.
3. Pass `TRAKT_USERNAME`, the default or overridden `TRAKT_CLIENT_ID`, and
   `LETTERBOXD_SESSION_COOKIE`, `LETTERBOXD_CSRF_COOKIE`, and
   `LETTERBOXD_CF_CLEARANCE` through the step environment.
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
  three Letterboxd cookies.
- Verify history and ratings are fetched independently and merged without
  changing the workflow's secret requirements.
