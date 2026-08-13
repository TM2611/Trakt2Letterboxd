"""Trakt2Letterboxd - Headless CI/CD exporter & Letterboxd importer.

The Trakt requests use the public movie-history and movie-ratings endpoints, so
the target Trakt profile must be public. The script can run unattended inside
GitHub Actions:

  1. Fetches the user's public movie history and ratings without an authenticated
     session.
  2. Writes Letterboxd-ready CSVs chunked so every file stays under Letterboxd's
     1 MB import limit (we split at 950 KB to leave headroom), with the exact
     column headers repeated at the top of every chunk.
  3. Optionally uploads every chunk automatically to letterboxd.com/import
     using Playwright + playwright-stealth, authenticating via injected
     session cookies instead of a username/password flow.

Environment variables used:
    TRAKT_USERNAME          -- public Trakt profile name to fetch
    TRAKT_CLIENT_ID         -- API key header; defaults to the supplied public key
    LETTERBOXD_SESSION_COOKIE -- value of the `letterboxd.user.CURRENT` session
                                 cookie from a logged-in Letterboxd browser
                                 session (used for upload)
    LETTERBOXD_CSRF_COOKIE  -- value of the `com.xk72.webparts.csrf` cookie
                               (required for the import form POST)
    LETTERBOXD_CF_CLEARANCE -- optional value of the `cf_clearance` cookie to
                               avoid the Cloudflare Turnstile challenge

CLI flags:
    --skip-upload                    export CSVs only; do not touch Letterboxd
    --export-dir DIR                 output directory for CSVs (default: exports)
    --headed                         show the browser for local upload debugging
"""

import argparse
import csv
import io
import os
import re
import sys
import time
from urllib.parse import quote

from dotenv import load_dotenv
import requests

API_ROOT = "https://api.trakt.tv"
IMPORT_URL = "https://letterboxd.com/import/"

# Load local configuration when present. Explicitly exported variables (and CI
# environment variables) take precedence because python-dotenv does not
# override values that are already set by default.
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Letterboxd import limit is 1 MB per file; 950 KB leaves headroom for
# encoding / column-order surprises.
MAX_CHUNK_BYTES = 950 * 1024

# Public Trakt API key used by default for the trakt-api-key header. It can be
# overridden with TRAKT_CLIENT_ID when a different public API key is supplied.
DEFAULT_CLIENT_ID = "0128c95089a7b58477204806a1b62ee130182b48c121c1eb9fe1d37b915fc5cb"

# Exact Letterboxd column headers; they must appear at the top of every chunk.
LETTERBOXD_HEADERS = ["WatchedDate", "tmdbID", "imdbID", "Title", "Year", "Rating10"]

PAGE_LIMIT = 100
REQUEST_TIMEOUT = 15
PLAYWRIGHT_TIMEOUT_MS = 15_000
PLAYWRIGHT_TIMEOUT_SECONDS = 15
CONSENT_SELECTOR = (
    'button.fc-button.fc-cta-consent.fc-primary-button'
    '[aria-label="Consent"]:visible'
)


# ---------------------------------------------------------------------------
# Trakt API client
# ---------------------------------------------------------------------------

class TraktClient:
    """Fetches movie history and ratings from a public Trakt profile."""

    def __init__(self):
        self.username = os.environ.get("TRAKT_USERNAME", "").strip()
        if not self.username:
            raise SystemExit(
                "TRAKT_USERNAME is required. Set it to the public Trakt profile "
                "name before running the exporter."
            )
        self.client_id = os.environ.get("TRAKT_CLIENT_ID", "").strip() or DEFAULT_CLIENT_ID
        self.session = requests.Session()

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
        }

    def _get(self, url):
        """Perform an unauthenticated GET and raise a useful API error."""
        response = self.session.get(url, headers=self._headers(), timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError(
                f"Trakt API error {response.status_code} for {url}: "
                f"{response.text[:500]}"
            )
        return response

    def _fetch_paginated(self, endpoint, label):
        """Fetch every page from a paginated public user endpoint."""
        entries = []
        page = 1
        username = quote(self.username, safe="")
        while True:
            url = f"{API_ROOT}/users/{username}/{endpoint}?page={page}&limit={PAGE_LIMIT}"
            response = self._get(url)
            page_count = int(response.headers.get("X-Pagination-Page-Count", "1"))
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(f"Unexpected Trakt response for {url}: expected a list")
            entries.extend(payload)
            print(f"  {label}: page {page}/{page_count} ({len(payload)} entries on this page)")
            if page >= page_count or not payload:
                break
            page += 1
        return entries

    def fetch_movies(self):
        """Fetch history and merge the latest Rating10 value into every watch event."""
        history_entries = self._fetch_paginated("history/movies", "history")
        ratings_entries = self._fetch_paginated("ratings/movies", "ratings")
        rating_index = self._build_rating_index(ratings_entries)

        movies = []
        for entry in history_entries:
            row = self._extract_movie_row(entry)
            if row is None:
                continue
            row["Rating10"] = self._lookup_rating(row, rating_index)
            movies.append(row)
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
            "Rating10": "",
        }

    @staticmethod
    def _normalize_id(value):
        """Return a stable lookup representation for a Trakt movie ID."""
        if value is None or value == "":
            return ""
        return str(value)

    @staticmethod
    def _normalize_rating10(value):
        """Validate a Trakt 1-10 rating for Letterboxd's Rating10 column."""
        if isinstance(value, bool):
            return ""
        try:
            rating = float(value)
        except (TypeError, ValueError):
            return ""
        if not rating.is_integer() or not 1 <= rating <= 10:
            return ""
        return str(int(rating))

    @classmethod
    def _build_rating_index(cls, entries):
        """Index the latest valid rating by every TMDB and IMDb ID it contains."""
        rating_index = {}
        for entry in entries:
            movie = entry.get("movie")
            rating = cls._normalize_rating10(entry.get("rating"))
            if not movie or not rating:
                continue

            ids = movie.get("ids") or {}
            rated_at = entry.get("rated_at") or ""
            for id_name in ("tmdb", "imdb"):
                movie_id = cls._normalize_id(ids.get(id_name))
                if not movie_id:
                    continue
                key = (id_name, movie_id)
                current = rating_index.get(key)
                if current is None or rated_at > current[0]:
                    rating_index[key] = (rated_at, rating)
        return rating_index

    @classmethod
    def _lookup_rating(cls, row, rating_index):
        """Find a row's rating, preferring an exact TMDB match over IMDb."""
        for column, id_name in (("tmdbID", "tmdb"), ("imdbID", "imdb")):
            movie_id = cls._normalize_id(row.get(column))
            if movie_id:
                match = rating_index.get((id_name, movie_id))
                if match is not None:
                    return match[1]
        return ""


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
    """Write chunked Letterboxd CSVs for the history; return file paths."""
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
    """Uploads CSV files to letterboxd.com/import using session cookies."""

    def __init__(
        self,
        session_cookie,
        csrf_cookie,
        cf_clearance="",
        debug_dir="debug",
        headed=False,
    ):
        if not session_cookie:
            raise ValueError("LETTERBOXD_SESSION_COOKIE must not be empty")
        if not csrf_cookie:
            raise ValueError("LETTERBOXD_CSRF_COOKIE must not be empty")
        self.session_cookie = session_cookie
        self.csrf_cookie = csrf_cookie
        self.cf_clearance = cf_clearance
        self.debug_dir = debug_dir
        self.headed = headed

    # -- helpers ----------------------------------------------------------

    def _screenshot(self, page, stage):
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
            path = os.path.join(self.debug_dir, f"{int(time.time())}_{stage}.png")
            page.screenshot(path=path, full_page=True)
            print(f"  screenshot saved to {path}")
        except Exception as exc:  # never let screenshotting kill the flow
            print(f"  could not save screenshot: {exc}")

    def _pause_on_failure(self, page):
        """Keep a headed browser open so a local failure can be inspected."""
        if not self.headed:
            return
        try:
            input("  headed debug mode: inspect the browser, then press Enter to close it... ")
        except (EOFError, KeyboardInterrupt):
            print("  headed debug session could not wait for input; closing the browser.")

    def _dismiss_consent(self, page):
        """Dismiss Letterboxd's visible consent dialog when it is present.

        The consent banner is supplied by a third-party consent component and is
        not present on every run. Prefer its stable class/ARIA selector, then use
        an exact visible button-label fallback for minor markup changes.
        """
        candidates = [
            page.locator(CONSENT_SELECTOR).first,
            page.locator(
                "button:visible",
                has_text=re.compile(r"^\s*Consent\s*$", re.IGNORECASE),
            ).first,
        ]

        consent = None
        for candidate in candidates:
            try:
                if candidate.count() > 0:
                    consent = candidate
                    break
            except Exception:
                continue

        if consent is None:
            return False

        try:
            consent.click(timeout=PLAYWRIGHT_TIMEOUT_MS)
            try:
                consent.wait_for(state="hidden", timeout=2_000)
            except Exception:
                # A consent click can detach the banner during a small page
                # update. Only treat it as a failure if it remains visible.
                try:
                    if consent.is_visible():
                        raise RuntimeError("consent button remained visible")
                except RuntimeError:
                    raise
                except Exception:
                    pass
            print("  Letterboxd consent dialog dismissed.")
            return True
        except Exception as exc:
            self._screenshot(page, "consent_click_failed")
            raise RuntimeError(
                f"Could not click Letterboxd's Consent button: {exc}"
            ) from exc

    @staticmethod
    def _import_control(page):
        """Return the first supported import control and its human-readable label."""
        candidates = [
            (
                "Import Titles anchor",
                page.locator(
                    "a.save-users-imported-imdb-history.submit-matched-films:visible",
                    has_text=re.compile(r"^\s*Import Titles\s*$", re.IGNORECASE),
                ).first,
            ),
            (
                "Import Titles link",
                page.locator(
                    "a:visible",
                    has_text=re.compile(r"^\s*Import Titles\s*$", re.IGNORECASE),
                ).first,
            ),
            (
                "Import films button",
                page.locator(
                    "button:visible",
                    has_text=re.compile(r"import\s+\d+\s+films?", re.IGNORECASE),
                ).first,
            ),
        ]

        for label, candidate in candidates:
            try:
                if candidate.count() == 0:
                    continue
                candidate.wait_for(state="visible", timeout=PLAYWRIGHT_TIMEOUT_MS)
                return label, candidate
            except Exception:
                continue
        return None, None

    @staticmethod
    def _cloudflare_blocked(page):
        if page.locator('iframe[src*="challenges.cloudflare.com"]').count() > 0:
            return True
        if page.locator(".cf-turnstile, .g-recaptcha").count() > 0:
            return True
        try:
            body = page.locator("body").inner_text(timeout=PLAYWRIGHT_TIMEOUT_MS)
        except Exception:
            return False
        return any(pattern.search(body) for pattern in CLOUDFLARE_PATTERNS)

    def _await_outcome(self, page, timeout_seconds=PLAYWRIGHT_TIMEOUT_SECONDS):
        """Wait for either a success or an error state after clicking Import."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self._cloudflare_blocked(page):
                self._screenshot(page, "cloudflare_after_submit")
                raise RuntimeError("Cloudflare challenge appeared after submitting the import.")
            try:
                body = page.locator("body").inner_text(timeout=PLAYWRIGHT_TIMEOUT_MS)
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

    @staticmethod
    def _apply_stealth_sync(page, stealth_module):
        """Apply playwright-stealth across the library's supported API shapes.

        playwright-stealth 2.x exposes ``Stealth.apply_stealth_sync`` while
        older releases expose either ``Stealth.apply_stealth`` or the module
        level ``stealth_sync`` function.  Keep this compatibility handling in
        one place so a changed third-party API cannot mask the original error.
        """
        stealth_class = getattr(stealth_module, "Stealth", None)
        if callable(stealth_class):
            stealth = stealth_class()
            for method_name in ("apply_stealth_sync", "apply_stealth"):
                apply_method = getattr(stealth, method_name, None)
                if callable(apply_method):
                    apply_method(page)
                    return f"Stealth.{method_name}"

        legacy_apply = getattr(stealth_module, "stealth_sync", None)
        if callable(legacy_apply):
            legacy_apply(page)
            return "legacy stealth_sync"

        raise ImportError(
            "Installed playwright-stealth does not expose a supported "
            "synchronous API (expected Stealth.apply_stealth_sync, "
            "Stealth.apply_stealth, or stealth_sync)."
        )

    # -- main upload loop -------------------------------------------------

    def upload(self, csv_paths):
        from playwright.sync_api import sync_playwright
        import playwright_stealth  # imported lazily: heavy dependency

        browser = None
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(
                    headless=not self.headed,
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
            context.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
            context.set_default_navigation_timeout(PLAYWRIGHT_TIMEOUT_MS)

            # Apply stealth before any navigation. API differs across
            # playwright-stealth versions.
            page = context.new_page()
            stealth_api = self._apply_stealth_sync(page, playwright_stealth)
            print(f"  playwright-stealth applied ({stealth_api}).")

            # Authenticate by injecting the real Letterboxd cookies BEFORE
            # navigating. No username/password flow - that triggers CAPTCHAs.
            # The session cookie proves login, the CSRF cookie is validated on
            # the import form POST, and cf_clearance (when present) avoids the
            # Cloudflare Turnstile challenge.
            cookies = [
                {
                    "name": "letterboxd.user.CURRENT",
                    "value": self.session_cookie,
                    "domain": ".letterboxd.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "com.xk72.webparts.csrf",
                    "value": self.csrf_cookie,
                    "domain": ".letterboxd.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                },
            ]
            if self.cf_clearance:
                cookies.append({
                    "name": "cf_clearance",
                    "value": self.cf_clearance,
                    "domain": ".letterboxd.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "None",
                })
            context.add_cookies(cookies)

            try:
                for csv_path in csv_paths:
                    print(f"Uploading {csv_path} to Letterboxd...")
                    page.goto(
                        IMPORT_URL,
                        wait_until="domcontentloaded",
                        timeout=PLAYWRIGHT_TIMEOUT_MS,
                    )

                    if self._cloudflare_blocked(page):
                        self._screenshot(page, "cloudflare_block")
                        raise RuntimeError(
                            "Cloudflare Turnstile challenge detected on letterboxd.com. "
                            "Upload aborted - check the screenshot artifact."
                        )

                    self._dismiss_consent(page)

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

                    # A consent banner can appear after the file has been
                    # selected, so dismiss it again immediately before import.
                    self._dismiss_consent(page)

                    # Letterboxd currently renders an anchor labelled
                    # "Import Titles"; retain support for older button markup.
                    control_label, confirm = self._import_control(page)
                    if confirm is None:
                        self._screenshot(page, "no_confirm_button")
                        raise RuntimeError(
                            "The Letterboxd import control never appeared "
                            "after uploading the file - see screenshot artifact."
                        )

                    print(f"  {control_label} found; clicking Import.")
                    try:
                        confirm.click(timeout=PLAYWRIGHT_TIMEOUT_MS)
                    except Exception as exc:
                        self._screenshot(page, "import_click_failed")
                        raise RuntimeError(
                            f"Could not click the {control_label}: {exc}"
                        ) from exc

                    if not self._await_outcome(page):
                        raise RuntimeError(
                            f"Letterboxd did not confirm the import of {csv_path} - "
                            "see screenshot artifact."
                        )
                    print(f"  {os.path.basename(csv_path)} imported successfully.")
            except Exception:
                self._pause_on_failure(page)
                raise
            finally:
                browser.close()

            print("All CSV chunks uploaded to Letterboxd.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="Trakt2Letterboxd",
        description="Export public Trakt movie history and ratings to Letterboxd-ready CSVs and upload them.",
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
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window for local upload debugging",
    )
    args = parser.parse_args(argv)

    mode = "headed local debug" if args.headed else "headless"
    print(f"Initializing Trakt2Letterboxd ({mode} mode)...")

    client = TraktClient()
    print(f"Fetching public Trakt history and ratings for {client.username}...")
    rows = client.fetch_movies()
    all_paths = write_export("letterboxd_history", rows, args.export_dir)

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
    csrf_cookie = os.environ.get("LETTERBOXD_CSRF_COOKIE", "").strip()
    cf_clearance = os.environ.get("LETTERBOXD_CF_CLEARANCE", "").strip()
    if not session_cookie or not csrf_cookie:
        raise SystemExit(
            "LETTERBOXD_SESSION_COOKIE and LETTERBOXD_CSRF_COOKIE are required "
            "for the upload (see SETUP.md). Use --skip-upload to export only."
        )

    uploader = LetterboxdUploader(
        session_cookie=session_cookie,
        csrf_cookie=csrf_cookie,
        cf_clearance=cf_clearance,
        debug_dir="debug",
        headed=args.headed,
    )
    uploader.upload(all_paths)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
