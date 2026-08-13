import csv
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from Trakt2Letterboxd import (
    LETTERBOXD_HEADERS,
    TraktClient,
    _serialize_rows,
    csv_chunks,
    LetterboxdUploader,
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
