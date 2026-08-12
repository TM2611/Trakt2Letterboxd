"""Trakt2Letterboxd - Headless CI/CD exporter & Letterboxd importer.

Fork refactor of the original interactive script so it can run unattended
inside GitHub Actions every week:

  1. Authenticates against the Trakt API using tokens from environment
     variables (no interactive device-code flow, no hardcoded credentials).
  2. Refreshes the access token proactively (7-day buffer before expiry) or
     reactively (on HTTP 401), then emits the rotated tokens to GITHUB_OUTPUT
     so the workflow can update the repository secrets via `gh secret set`.
  3. Fetches the user's watched history and watchlist (movies only - anything
     that is not a movie is explicitly dropped).
  4. Writes Letterboxd-ready CSVs chunked so every file stays under Letterboxd's
     1 MB import limit (we split at 950 KB to leave headroom), with the exact
     column headers repeated at the top of every chunk.
  5. Optionally uploads every chunk automatically to letterboxd.com/import
     using Playwright + playwright-stealth, authenticating via the injected
     `lbx_session` cookie instead of a username/password flow.

Environment variables used:
    TRAKT_CLIENT_ID          -- Trakt API app client id
    TRAKT_CLIENT_SECRET      -- Trakt API app client secret
    TRAKT_ACCESS_TOKEN       -- current OAuth access token
    TRAKT_REFRESH_TOKEN      -- current OAuth refresh token
    TRAKT_TOKEN_EXPIRES_AT   -- epoch seconds when the access token expires
                                (>created_at + expires_in from the OAuth response)
    LETTERBOXD_SESSION_COOKIE-- value of the `lbx_session` cookie from a logged-in
                                Letterboxd browser session (used for upload)
    GITHUB_OUTPUT            -- (optional, set by GitHub Actions) file path where
                                refreshed token outputs are written

CLI flags:
    --lists {all,history,watchlist}  which list(s) to export (default: all)
    --skip-upload                    export CSVs only; do not touch Letterboxd
    --export-dir DIR                 output directory for CSVs (default: exports)
"""

import argparse
import csv
import io
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

API_ROOT = "https://api.trakt.tv"
IMPORT_URL = "https://letterboxd.com/import/"

# Letterboxd import limit is 1 MB per file; 950 KB leaves headroom for
# encoding / column-order surprises.
MAX_CHUNK_BYTES = 950 * 1024

# Refresh the access token when it is within this window of expiring, so the
# weekly cron always has plenty of runway before Trakt cuts it off (~3 months).
REFRESH_BUFFER_SECONDS = 7 * 24 * 60 * 60

# Default lifetime used only if the OAuth response omits expires_in/created_at.
DEFAULT_ACCESS_TOKEN_SECONDS = 90 * 24 * 60 * 60

# Exact Letterboxd column headers; they must appear at the top of every chunk.
LETTERBOXD_HEADERS = ["WatchedDate", "tmdbID", "imdbID", "Title", "Year"]

PAGE_LIMIT = 100
REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Authentication / token management
# ---------------------------------------------------------------------------

class TraktAuth:
    """Reads Trakt credentials from the environment and handles token refresh."""

    def __init__(self):
        self.client_id = os.environ.get("TRAKT_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("TRAKT_CLIENT_SECRET", "").strip()
        self.access_token = os.environ.get("TRAKT_ACCESS_TOKEN", "").strip()
        self.refresh_token = os.environ.get("TRAKT_REFRESH_TOKEN", "").strip()
        self.expires_at = self._parse_expiry(os.environ.get("TRAKT_TOKEN_EXPIRES_AT", ""))
        self.session = requests.Session()
        self.refreshed = False
        self._emitted = False

    @staticmethod
    def _parse_expiry(value):
        if not value:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            print("WARNING: TRAKT_TOKEN_EXPIRES_AT is not a number; ignoring it. "
                  "A 401-driven refresh will be used as a fallback.")
            return None

    def ensure_valid(self):
        """Validate config and refresh proactively if the token is near expiry."""
        missing = [
            key for key in (
                "TRAKT_CLIENT_ID", "TRAKT_CLIENT_SECRET",
                "TRAKT_ACCESS_TOKEN", "TRAKT_REFRESH_TOKEN",
            )
            if not os.environ.get(key, "").strip()
        ]
        if missing:
            raise SystemExit(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Follow SETUP.md to generate them."
            )
        if self.expires_at is not None and time.time() >= (self.expires_at - REFRESH_BUFFER_SECONDS):
            print("Access token is expired or expiring within 7 days - refreshing now.")
            self.refresh()

    def refresh(self):
        """Exchange the refresh token for a fresh token pair. Raises on failure."""
        url = API_ROOT + "/oauth/token"
        payload = {
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "grant_type": "refresh_token",
        }
        response = self.session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError(
                f"Token refresh failed with HTTP {response.status_code}: {response.text} "
                "(the refresh token may be inactive/expired - rerun the bootstrap "
                "steps in SETUP.md to mint a fresh pair)."
            )
        data = response.json()
        if not data.get("access_token"):
            raise RuntimeError("Token refresh response contained no access_token.")

        self.access_token = data["access_token"]
        if data.get("refresh_token"):  # Trakt rotates the refresh token too
            self.refresh_token = data["refresh_token"]
        created_at = data.get("created_at") or int(time.time())
        expires_in = data.get("expires_in") or DEFAULT_ACCESS_TOKEN_SECONDS
        self.expires_at = int(created_at) + int(expires_in)
        self.refreshed = True

        human = datetime.fromtimestamp(self.expires_at, tz=timezone.utc).isoformat()
        print(f"Access token refreshed; new tokens valid until {human}.")
        self.emit_outputs()

    def emit_outputs(self):
        """Write refreshed tokens to GITHUB_OUTPUT (CI) or stdout (local run).

        Called from refresh() immediately so secret rotation still happens even
        if a later step (e.g. the upload) fails and the job exits non-zero.
        """
        if not self.refreshed or self._emitted:
            return
        self._emitted = True
        lines = [
            "token_refreshed=true",
            f"trakt_access_token={self.access_token}",
            f"trakt_refresh_token={self.refresh_token}",
            f"trakt_token_expires_at={int(self.expires_at)}",
        ]
        output_file = os.environ.get("GITHUB_OUTPUT", "").strip()
        if output_file:
            with open(output_file, "a", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line + "\n")
            print("Refreshed tokens written to GITHUB_OUTPUT for secret rotation.")
        else:
            print("Refreshed tokens (local run - update your secrets manually):")
            for line in lines:
                print(f"  {line}")


# ---------------------------------------------------------------------------
# Trakt API client
# ---------------------------------------------------------------------------

class TraktClient:
    """Fetches movie-only lists from the Trakt API, handling 401-driven refresh."""

    def __init__(self, auth):
        self.auth = auth

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "User-Agent": "Trakt2Letterboxd/2.0 (fork, CI)",
            "Authorization": "Bearer " + self.auth.access_token,
            "trakt-api-version": "2",
            "trakt-api-key": self.auth.client_id,
        }

    def _get(self, url):
        """GET with one retry after a refresh if the token is rejected."""
        response = self.auth.session.get(url, headers=self._headers(), timeout=REQUEST_TIMEOUT)
        if response.status_code == 401:
            print("HTTP 401 - access token rejected, refreshing and retrying once.")
            self.auth.refresh()
            response = self.auth.session.get(url, headers=self._headers(), timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError(
                f"Trakt API error {response.status_code} for {url}: "
                f"{response.text[:500]}"
            )
        return response

    def fetch_movies(self, list_name):
        """Fetch every page of /sync/<list>/movies and return Letterboxd rows."""
        movies = []
        page = 1
        while True:
            url = (
                f"{API_ROOT}/sync/{list_name}/movies"
                f"?page={page}&limit={PAGE_LIMIT}"
            )
            response = self._get(url)
            page_count = int(response.headers.get("X-Pagination-Page-Count", "1"))
            payload = response.json()
            kept = 0
            for entry in payload:
                row = self._extract_movie_row(entry)
                if row is not None:
                    movies.append(row)
                    kept += 1
            print(f"  {list_name}: page {page}/{page_count} ({kept} movies on this page)")
            if page >= page_count or not payload:
                break
            page += 1
        return movies

    @staticmethod
    def _extract_movie_row(entry):
        """Convert one API entry into a Letterboxd CSV row, or None if not a movie."""
        movie = entry.get("movie")
        if not movie:
            # Belt-and-braces: /sync/*/movies should only ever return movies,
            # but anything without a movie record (shows, seasons, episodes,
            # people) is dropped explicitly to keep Letterboxd happy.
            return None
        title = movie.get("title")
        if not title:
            return None
        ids = movie.get("ids") or {}
        return {
            "WatchedDate": entry.get("watched_at") or "",
            "tmdbID": ids.get("tmdb") or "",
            "imdbID": ids.get("imdb") or "",
            "Title": title,
            "Year": movie.get("year") or "",
        }


# ---------------------------------------------------------------------------
# CSV generation & chunking (Letterboxd 1 MB import limit)
# ---------------------------------------------------------------------------

def _serialize_rows(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LETTERBOXD_HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _header_bytes():
    return len(_serialize_rows([]).encode("utf-8"))


def _row_bytes(row):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LETTERBOXD_HEADERS)
    writer.writerow(row)
    return len(buf.getvalue().encode("utf-8"))


def csv_chunks(rows, max_bytes=MAX_CHUNK_BYTES):
    """Yield row lists whose serialized size (header + rows) stays under max_bytes."""
    if not rows:
        return
    header_size = _header_bytes()
    chunk = []
    chunk_bytes = header_size  # every chunk starts with the header row
    for row in rows:
        row_size = _row_bytes(row)
        if chunk and chunk_bytes + row_size > max_bytes:
            yield chunk
            chunk = [row]
            chunk_bytes = header_size + row_size
        else:
            chunk.append(row)
            chunk_bytes += row_size
    if chunk:
        yield chunk


def write_export(name_stem, rows, export_dir):
    """Write chunked Letterboxd CSVs for one list; returns list of file paths."""
    chunks = list(csv_chunks(rows))
    if not chunks:
        print(f"{name_stem}: no rows, nothing to export.")
        return []
    os.makedirs(export_dir, exist_ok=True)
    paths = []
    for index, chunk_rows in enumerate(chunks, start=1):
        filename = f"{name_stem}_part{index}.csv" if len(chunks) > 1 else f"{name_stem}.csv"
        path = os.path.join(export_dir, filename)
        content = _serialize_rows(chunk_rows)
        size = len(content.encode("utf-8"))
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        paths.append(path)
        status = "OK" if size <= MAX_CHUNK_BYTES else "OVERSIZED"
        print(f"  wrote {path} ({size / 1024:.1f} KB, {len(chunk_rows)} rows) [{status}]")
    return paths


# ---------------------------------------------------------------------------
# Letterboxd upload (Playwright + stealth)
# ---------------------------------------------------------------------------

# Cloudflare / Turnstile indicators. If any of these are present we stop and
# hand the user a screenshot instead of flailing.
CLOUDFLARE_PATTERNS = [
    re.compile(r"verify you are human", re.IGNORECASE),
    re.compile(r"checking your browser", re.IGNORECASE),
    re.compile(r"just a moment", re.IGNORECASE),
    re.compile(r"security check", re.IGNORECASE),
]

# Success indicators on the import page after submitting.
IMPORT_SUCCESS_PATTERNS = [
    re.compile(r"import\s+(?:is\s+)?(?:in\s+progress|processing|started)", re.IGNORECASE),
    re.compile(r"import\s+(?:complete|successful|succeeded)", re.IGNORECASE),
    re.compile(r"(?:films?|movies?)\s+imported", re.IGNORECASE),
    re.compile(r"your\s+import", re.IGNORECASE),
]

# Explicit error indicators shown by Letterboxd's import UI.
IMPORT_ERROR_PATTERNS = [
    re.compile(r"invalid file", re.IGNORECASE),
    re.compile(r"unable to (?:read|parse)", re.IGNORECASE),
    re.compile(r"didn.t match", re.IGNORECASE),
    re.compile(r"something went wrong", re.IGNORECASE),
]


class LetterboxdUploader:
    """Uploads CSV files to letterboxd.com/import using a session cookie."""

    def __init__(self, session_cookie, debug_dir="debug"):
        if not session_cookie:
            raise ValueError("LETTERBOXD_SESSION_COOKIE must not be empty")
        self.session_cookie = session_cookie
        self.debug_dir = debug_dir

    # -- helpers ----------------------------------------------------------

    def _screenshot(self, page, stage):
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
            path = os.path.join(self.debug_dir, f"{int(time.time())}_{stage}.png")
            page.screenshot(path=path, full_page=True)
            print(f"  screenshot saved to {path}")
        except Exception as exc:  # never let screenshotting kill the flow
            print(f"  could not save screenshot: {exc}")

    @staticmethod
    def _cloudflare_blocked(page):
        if page.locator('iframe[src*="challenges.cloudflare.com"]').count() > 0:
            return True
        if page.locator(".cf-turnstile, .g-recaptcha").count() > 0:
            return True
        try:
            body = page.locator("body").inner_text(timeout=3000)
        except Exception:
            return False
        return any(pattern.search(body) for pattern in CLOUDFLARE_PATTERNS)

    def _await_outcome(self, page, timeout_seconds=90):
        """Wait for either a success or an error state after clicking Import."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self._cloudflare_blocked(page):
                self._screenshot(page, "cloudflare_after_submit")
                raise RuntimeError("Cloudflare challenge appeared after submitting the import.")
            try:
                body = page.locator("body").inner_text(timeout=5000)
            except Exception:
                body = ""
            if any(pattern.search(body) for pattern in IMPORT_ERROR_PATTERNS):
                self._screenshot(page, "import_error")
                return False
            if any(pattern.search(body) for pattern in IMPORT_SUCCESS_PATTERNS):
                return True
            time.sleep(2)
        self._screenshot(page, "import_timeout")
        return False

    # -- main upload loop -------------------------------------------------

    def upload(self, csv_paths):
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth  # imported lazily: heavy dep

        browser = None
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Could not launch Chromium ({exc}). In CI run "
                    "`python -m playwright install --with-deps chromium`."
                )

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                locale="en-US",
                timezone_id="Europe/London",
            )

            # Apply stealth before any navigation. API differs slightly across
            # playwright-stealth versions, so try the modern call first.
            page = context.new_page()
            try:
                stealth = Stealth()
                stealth.apply_stealth(page)
                print("  playwright-stealth applied (Stealth.apply_stealth).")
            except (TypeError, AttributeError):
                from playwright_stealth import stealth_sync
                stealth_sync(page)
                print("  playwright-stealth applied (legacy stealth_sync).")

            # Authenticate by injecting the session cookie BEFORE navigating.
            # No username/password flow - that triggers CAPTCHAs.
            context.add_cookies([{
                "name": "lbx_session",
                "value": self.session_cookie,
                "domain": ".letterboxd.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
            }])

            for csv_path in csv_paths:
                print(f"Uploading {csv_path} to Letterboxd...")
                page.goto(IMPORT_URL, wait_until="domcontentloaded", timeout=60000)

                if self._cloudflare_blocked(page):
                    self._screenshot(page, "cloudflare_block")
                    raise RuntimeError(
                        "Cloudflare Turnstile challenge detected on letterboxd.com. "
                        "Upload aborted - check the screenshot artifact."
                    )

                # Confirm we are actually logged in (cookie accepted).
                if "/login" in page.url or page.locator('a[href*="/log-in"], a[href*="/login"]').count() > 0:
                    self._screenshot(page, "not_authenticated")
                    raise RuntimeError(
                        "Letterboxd is asking for login - LETTERBOXD_SESSION_COOKIE "
                        "is invalid or expired. Re-extract it and update the secret."
                    )

                # Pick up the CSV via the import page's file input.
                file_input = page.locator('input[type="file"]').first
                if file_input.count() == 0:
                    self._screenshot(page, "no_file_input")
                    raise RuntimeError("Could not find the file input on the import page.")
                file_input.set_input_files(csv_path)

                # Wait for the "Import X films" confirmation button (up to 60 s).
                confirm = page.locator(
                    "button:visible",
                    has_text=re.compile(r"import\s+\d+\s+films?", re.IGNORECASE),
                ).first
                try:
                    confirm.wait_for(state="visible", timeout=60000)
                except Exception:
                    self._screenshot(page, "no_confirm_button")
                    raise RuntimeError(
                        "The 'Import X films' confirmation button never appeared "
                        "after uploading the file - see screenshot artifact."
                    )

                print("  confirmation button found; clicking Import.")
                confirm.click()

                if not self._await_outcome(page):
                    raise RuntimeError(
                        f"Letterboxd did not confirm the import of {csv_path} - "
                        "see screenshot artifact."
                    )
                print(f"  {os.path.basename(csv_path)} imported successfully.")

            browser.close()
            print("All CSV chunks uploaded to Letterboxd.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="Trakt2Letterboxd",
        description="Export Trakt movie history/watchlist to Letterboxd-ready CSVs and upload them.",
    )
    parser.add_argument(
        "--lists",
        choices=("all", "history", "watchlist"),
        default="all",
        help="which Trakt list(s) to export (default: all)",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="export CSVs only; do not upload to Letterboxd",
    )
    parser.add_argument(
        "--export-dir",
        default="exports",
        help="directory for generated CSV files (default: exports)",
    )
    args = parser.parse_args(argv)

    print("Initializing Trakt2Letterboxd (CI mode)...")

    auth = TraktAuth()
    auth.ensure_valid()
    client = TraktClient(auth)

    lists_to_fetch = ["history", "watchlist"] if args.lists == "all" else [args.lists]
    all_paths = []
    for name in lists_to_fetch:
        print(f"Fetching Trakt {name}...")
        rows = client.fetch_movies(name)
        all_paths.extend(write_export(f"letterboxd_{name}", rows, args.export_dir))

    if not all_paths:
        print("No CSV files were generated - nothing to do.")
        return 0

    upload_dir = os.path.dirname(all_paths[0]) or "."
    print(f"Generated {len(all_paths)} CSV file(s) in '{upload_dir}':")
    for path in all_paths:
        print(f"  - {path}")

    if args.skip_upload:
        print("Skipping Letterboxd upload (--skip-upload).")
        return 0

    session_cookie = os.environ.get("LETTERBOXD_SESSION_COOKIE", "").strip()
    if not session_cookie:
        raise SystemExit(
            "LETTERBOXD_SESSION_COOKIE is required for the upload (see SETUP.md). "
            "Use --skip-upload to export only."
        )

    uploader = LetterboxdUploader(session_cookie=session_cookie, debug_dir="debug")
    uploader.upload(all_paths)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
