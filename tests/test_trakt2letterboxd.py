import csv
import io
import os
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, Mock, patch

from Trakt2Letterboxd import (
    CONSENT_SELECTOR,
    CLOUDFLARE_PATTERNS,
    IMPORT_ERROR_PATTERNS,
    IMPORT_SUCCESS_PATTERNS,
    IMPORT_ERROR_PATTERNS,
    IMPORT_SUCCESS_PATTERNS,
    LETTERBOXD_HEADERS,
    PLAYWRIGHT_TIMEOUT_MS,
    PLAYWRIGHT_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT,
    TraktClient,
    _serialize_rows,
    csv_chunks,
    LetterboxdUploader,
    main,
)


class FakeResponse:
    def __init__(self, payload, page_count=1):
        self.headers = {"X-Pagination-Page-Count": str(page_count)}
        self._payload = payload

    def json(self):
        return self._payload


def history_entry(title, tmdb=None, imdb=None, watched_at="2026-01-01T00:00:00.000Z"):
    return {
        "watched_at": watched_at,
        "movie": {
            "title": title,
            "year": 2026,
            "ids": {"tmdb": tmdb, "imdb": imdb},
        },
    }


def rating_entry(rating, tmdb=None, imdb=None, rated_at="2026-01-02T00:00:00.000Z"):
    return {
        "rating": rating,
        "rated_at": rated_at,
        "movie": {"title": "Movie", "ids": {"tmdb": tmdb, "imdb": imdb}},
    }


class TraktRatingTests(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(TraktClient)
        self.client.username = "test-user"

    def test_fetch_movies_merges_rating_for_every_watch_event_and_paginates(self):
        responses = [
            FakeResponse([
                history_entry("Repeated Movie", tmdb=101, imdb="tt0101"),
            ], page_count=2),
            FakeResponse([
                history_entry(
                    "Repeated Movie", tmdb=101, imdb="tt0101",
                    watched_at="2026-01-03T00:00:00.000Z",
                ),
            ], page_count=2),
            FakeResponse([rating_entry(8, tmdb=101, imdb="tt0101")]),
        ]
        self.client._get = Mock(side_effect=responses)

        rows = self.client.fetch_movies()

        self.assertEqual(len(rows), 2)
        self.assertEqual([row["Rating10"] for row in rows], ["8", "8"])
        self.assertIn("/history/movies?page=1&limit=100", self.client._get.call_args_list[0].args[0])
        self.assertIn("/history/movies?page=2&limit=100", self.client._get.call_args_list[1].args[0])
        self.assertIn("/ratings/movies?page=1&limit=100", self.client._get.call_args_list[2].args[0])

    def test_latest_rating_is_selected_and_imdb_is_fallback(self):
        entries = [
            rating_entry(6, imdb="tt0202", rated_at="2026-01-01T00:00:00.000Z"),
            rating_entry(10, imdb="tt0202", rated_at="2026-02-01T00:00:00.000Z"),
        ]
        index = self.client._build_rating_index(entries)

        self.assertEqual(
            self.client._lookup_rating({"tmdbID": "", "imdbID": "tt0202"}, index),
            "10",
        )

    def test_invalid_or_missing_ratings_are_blank(self):
        self.assertEqual(self.client._normalize_rating10(None), "")
        self.assertEqual(self.client._normalize_rating10(0), "")
        self.assertEqual(self.client._normalize_rating10(10.5), "")
        self.assertEqual(self.client._normalize_rating10("not-a-rating"), "")

        index = self.client._build_rating_index([rating_entry(0, tmdb=303)])
        self.assertEqual(self.client._lookup_rating({"tmdbID": 303, "imdbID": ""}, index), "")

    def test_csv_contains_rating10_header_and_values(self):
        row = {
            "WatchedDate": "2026-01-01T00:00:00.000Z",
            "tmdbID": 101,
            "imdbID": "tt0101",
            "Title": "Movie",
            "Year": 2026,
            "Rating10": "8",
        }

        content = _serialize_rows([row])
        parsed = list(csv.DictReader(io.StringIO(content)))

        self.assertEqual(content.splitlines()[0], ",".join(LETTERBOXD_HEADERS))
        self.assertEqual(parsed[0]["Rating10"], "8")
        self.assertEqual(list(csv_chunks([row], max_bytes=len(content))), [[row]])


class TimeoutAndDebugModeTests(unittest.TestCase):
    def test_timeout_policy_is_fifteen_seconds(self):
        self.assertEqual(REQUEST_TIMEOUT, 15)
        self.assertEqual(PLAYWRIGHT_TIMEOUT_MS, 15_000)
        self.assertEqual(PLAYWRIGHT_TIMEOUT_SECONDS, 15)

    def test_trakt_requests_use_configured_timeout(self):
        client = object.__new__(TraktClient)
        client.client_id = "client-id"
        client.session = SimpleNamespace(get=Mock(return_value=Mock(status_code=200)))

        client._get("https://example.test/movies")

        client.session.get.assert_called_once_with(
            "https://example.test/movies",
            headers=client._headers(),
            timeout=REQUEST_TIMEOUT,
        )

    def test_default_headed_failure_pause_waits_for_local_inspection(self):
        uploader = LetterboxdUploader("session", "csrf", headed=True)

        with patch.dict(os.environ, {"CI": ""}, clear=False):
            with patch("builtins.input", return_value="") as wait_for_input:
                uploader._pause_on_failure(object())

        wait_for_input.assert_called_once()

    def test_headed_ci_failure_does_not_wait_for_input(self):
        uploader = LetterboxdUploader("session", "csrf", headed=True)

        with patch.dict(os.environ, {"CI": "true"}, clear=False):
            with patch("builtins.input") as wait_for_input:
                uploader._pause_on_failure(object())

        wait_for_input.assert_not_called()

    def test_headless_argument_is_ignored(self):
        uploader = LetterboxdUploader("session", "csrf", headed=False)

        self.assertTrue(uploader.headed)
        with patch.dict(os.environ, {"CI": ""}, clear=False):
            with patch("builtins.input", return_value="") as wait_for_input:
                uploader._pause_on_failure(object())

        wait_for_input.assert_called_once()


class FakeLocator:
    def __init__(self, count=0, visible=True, text=""):
        self._count = count
        self._visible = visible
        self._text = text
        self.click_calls = 0
        self.wait_calls = []

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def click(self, **kwargs):
        self.click_calls += 1

    def wait_for(self, **kwargs):
        self.wait_calls.append(kwargs)
        if kwargs.get("state") == "visible" and self._count == 0:
            raise TimeoutError("locator is not visible")

    def is_visible(self):
        return self._visible

    def inner_text(self, **kwargs):
        return self._text


class FakePage:
    def __init__(self, locators, body_text=""):
        self.locators = locators
        self.body_text = body_text

    def locator(self, selector, **kwargs):
        if selector == "body":
            return FakeLocator(count=1, text=self.body_text)
        return self.locators.get(selector, FakeLocator())


class ImportControlTests(unittest.TestCase):
    def test_detects_cloudflare_security_verification_page(self):
        page = FakePage({}, body_text="Performing security verification")

        self.assertTrue(LetterboxdUploader._cloudflare_blocked(page))
        self.assertTrue(
            any(
                pattern.search("This website uses a security service")
                for pattern in CLOUDFLARE_PATTERNS
            )
        )

    def test_aborts_when_cloudflare_appears_after_file_selection(self):
        uploader = LetterboxdUploader("session", "csrf")
        uploader._cloudflare_blocked = Mock(return_value=True)
        uploader._screenshot = Mock()

        with self.assertRaisesRegex(RuntimeError, "after file selection"):
            uploader._abort_if_cloudflare(object(), "after_file_selection")

        uploader._screenshot.assert_called_once_with(
            ANY, "cloudflare_after_file_selection"
        )

    def test_dismisses_exact_consent_button(self):
        consent = FakeLocator(count=1)
        page = FakePage({CONSENT_SELECTOR: consent})
        uploader = LetterboxdUploader("session", "csrf")

        self.assertTrue(uploader._dismiss_consent(page))
        self.assertEqual(consent.click_calls, 1)

    def test_missing_consent_is_not_an_error(self):
        page = FakePage({})
        uploader = LetterboxdUploader("session", "csrf")

        self.assertFalse(uploader._dismiss_consent(page))

    def test_prefers_current_import_titles_anchor(self):
        current_selector = (
            "a.save-users-imported-imdb-history.submit-matched-films:visible"
        )
        current = FakeLocator(count=1)
        page = FakePage({current_selector: current})

        label, control = LetterboxdUploader._import_control(page)

        self.assertEqual(label, "Import Titles anchor")
        self.assertIs(control, current)

    def test_supports_legacy_import_films_button(self):
        legacy_selector = "button:visible"
        legacy = FakeLocator(count=1)
        page = FakePage({legacy_selector: legacy})

        label, control = LetterboxdUploader._import_control(page)

        self.assertEqual(label, "Import films button")
        self.assertIs(control, legacy)

    def test_saved_title_count_is_success_for_singular_and_plural(self):
        self.assertTrue(
            any(pattern.search("Saved 1 title") for pattern in IMPORT_SUCCESS_PATTERNS)
        )
        self.assertTrue(
            any(pattern.search("Saved 191 titles") for pattern in IMPORT_SUCCESS_PATTERNS)
        )

    def test_generic_pre_submit_import_text_is_not_success(self):
        self.assertFalse(
            any(pattern.search("Your import") for pattern in IMPORT_SUCCESS_PATTERNS)
        )

    def test_post_submit_import_text_is_success(self):
        self.assertTrue(
            any(
                pattern.search("Your import has been queued")
                for pattern in IMPORT_SUCCESS_PATTERNS
            )
        )

    def test_zero_saved_titles_is_not_success(self):
        self.assertFalse(
            any(pattern.search("Saved 0 titles") for pattern in IMPORT_SUCCESS_PATTERNS)
        )

    def test_pre_submit_error_text_is_not_new_error(self):
        body = "12 titles didn't match"
        self.assertFalse(
            any(
                len(pattern.findall(body)) > len(pattern.findall(body))
                for pattern in IMPORT_ERROR_PATTERNS
            )
        )


class AuthenticationDetectionTests(unittest.TestCase):
    def test_username_field_detects_expired_session_cookie(self):
        page = FakePage({"#field-username": FakeLocator(count=1)})

        self.assertTrue(LetterboxdUploader._session_cookie_expired(page))

    def test_password_field_detects_expired_session_cookie(self):
        page = FakePage({"#field-password": FakeLocator(count=1)})

        self.assertTrue(LetterboxdUploader._session_cookie_expired(page))

    def test_missing_login_fields_means_session_cookie_is_not_expired(self):
        page = FakePage({})

        self.assertFalse(LetterboxdUploader._session_cookie_expired(page))

    @patch("Trakt2Letterboxd.LetterboxdUploader")
    @patch("Trakt2Letterboxd.write_export", return_value=["exports/history.csv"])
    @patch("Trakt2Letterboxd.TraktClient")
    def test_headed_mode_is_the_default_for_uploader(self, client_type, write_export, uploader_type):
        client_type.return_value.username = "test-user"
        client_type.return_value.fetch_movies.return_value = []

        with patch.dict(
            os.environ,
            {
                "LETTERBOXD_SESSION_COOKIE": "session",
                "LETTERBOXD_CSRF_COOKIE": "csrf",
                "LETTERBOXD_CF_CLEARANCE": "clearance",
            },
            clear=False,
        ):
            result = main([])

        self.assertEqual(result, 0)
        uploader_type.assert_called_once_with(
            session_cookie="session",
            csrf_cookie="csrf",
            cf_clearance="clearance",
            debug_dir="debug",
            headed=True,
        )
        uploader_type.return_value.upload.assert_called_once_with(["exports/history.csv"])


class StealthCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.page = object()

    def test_uses_current_sync_method(self):
        apply_stealth_sync = Mock()
        module = SimpleNamespace(
            Stealth=Mock(return_value=SimpleNamespace(
                apply_stealth_sync=apply_stealth_sync,
            )),
            stealth_sync=Mock(),
        )

        api = LetterboxdUploader._apply_stealth_sync(self.page, module)

        self.assertEqual(api, "Stealth.apply_stealth_sync")
        apply_stealth_sync.assert_called_once_with(self.page)
        module.stealth_sync.assert_not_called()

    def test_supports_previous_stealth_method(self):
        apply_stealth = Mock()
        module = SimpleNamespace(
            Stealth=Mock(return_value=SimpleNamespace(apply_stealth=apply_stealth)),
        )

        api = LetterboxdUploader._apply_stealth_sync(self.page, module)

        self.assertEqual(api, "Stealth.apply_stealth")
        apply_stealth.assert_called_once_with(self.page)

    def test_supports_legacy_module_function(self):
        stealth_sync = Mock()
        module = SimpleNamespace(stealth_sync=stealth_sync)

        api = LetterboxdUploader._apply_stealth_sync(self.page, module)

        self.assertEqual(api, "legacy stealth_sync")
        stealth_sync.assert_called_once_with(self.page)

    def test_rejects_unsupported_api(self):
        with self.assertRaises(ImportError):
            LetterboxdUploader._apply_stealth_sync(self.page, SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
