"""Tests for the Pexels search-filter guard.

Pexels sometimes answers a filtered search request by redirecting to the
same results page with the whole query string stripped, which would
silently turn the search into an unfiltered one. These tests drive the
guard with a fake driver so no browser, network or API key is needed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from production.footage.base import VideoProviderError
from production.footage.pexels import PexelsVideoProvider

BASE = "https://www.pexels.com"
FILTERED = (
    f"{BASE}/search/videos/melting%20ice%20cream/"
    "?orientation=portrait&resolution_name=4K"
)
# What Pexels landed on in the reported bug: canonical path, no params.
UNFILTERED = f"{BASE}/search/videos/melting%20ice%20cream/"


class FakeDriver:
    """Minimal stand-in for the Selenium driver.

    `navigations` maps a URL to the URL the "server" redirects it to.
    Redirects are ONE-SHOT (consumed on first use), mirroring Pexels'
    canonicalization redirect: the first filtered request is bounced to
    the unfiltered canonical page, the same URL requested again is
    served with its filters intact. Put a URL in `always_redirect` to
    model a site that keeps stripping them.
    """

    def __init__(self, navigations=None, always_redirect=()):
        self.current_url = ""
        self.title = "Pexels"
        self.navigations = dict(navigations or {})
        self.always_redirect = set(always_redirect)
        self.visited = []
        self.grid = True
        # Pexels' rewrite is client-side: `get()` returns with the filtered
        # URL still in the address bar and the strip lands afterwards.
        self._pending_strip = None

    def get(self, url):
        self.visited.append(url)
        if url in self.always_redirect:
            self.current_url = self.navigations.get(url, url)
            self._pending_strip = self.navigations.get(url, url)
        elif url in self.navigations:
            # One-shot redirect: served with its filters on the retry.
            self.current_url = self.navigations.pop(url)
            self._pending_strip = None
        else:
            self.current_url = url
            self._pending_strip = None

    def _apply_pending_strip(self):
        if self._pending_strip is not None:
            self.current_url = self._pending_strip
            self._pending_strip = None

    def find_elements(self, by, value):
        # Client-side scripts run as the page renders, so the delayed
        # strip lands here - after `get()` has already returned.
        self._apply_pending_strip()
        return [object()] if self.grid else []

    def find_element(self, by, value):
        raise LookupError("no body")


def make_provider(**settings):
    pexels = {"enabled": True, "headless": True}
    pexels.update(settings)
    return PexelsVideoProvider({"pexels": pexels})


def test_search_url_carries_both_filters():
    url = make_provider()._search_url("melting ice cream")
    assert url == FILTERED, url


def test_search_url_normalizes_hyphens_and_whitespace():
    url = make_provider()._search_url("close-up  melting   ice cream")
    assert "/search/videos/close%20up%20melting%20ice%20cream/" in url, url


def test_orientation_vertical_maps_to_portrait():
    provider = make_provider(orientation="vertical")
    assert "orientation=portrait" in provider._search_url("ice cream")


def test_missing_filters_detects_stripped_query_string():
    provider = make_provider()
    assert provider._missing_filters(FILTERED) == []
    assert provider._missing_filters(UNFILTERED) == [
        "orientation", "resolution_name",
    ]


def test_missing_filters_checks_values_not_just_presence():
    provider = make_provider()
    # Orientation kept but the 4K resolution dropped - just as unfiltered.
    downgraded = (
        f"{BASE}/search/videos/ice%20cream/?orientation=portrait"
    )
    assert provider._missing_filters(downgraded) == ["resolution_name"]


def test_missing_filters_ignores_extra_params():
    provider = make_provider()
    assert provider._missing_filters(FILTERED + "&page=2&autoplay=true") == []


def test_refiltered_url_reapplies_filters_to_canonical_path():
    provider = make_provider()
    assert provider._refiltered_url(UNFILTERED) == FILTERED


def test_refiltered_url_preserves_other_params():
    provider = make_provider()
    recovered = provider._refiltered_url(
        f"{BASE}/search/videos/ice%20cream/?page=3"
    )
    assert "page=3" in recovered, recovered
    assert "orientation=portrait" in recovered, recovered
    assert "resolution_name=4K" in recovered, recovered


def test_refiltered_url_refuses_non_search_pages():
    provider = make_provider()
    # Home page / bot check / error page: nothing to re-filter.
    assert provider._refiltered_url(f"{BASE}/") == ""
    assert provider._refiltered_url(f"{BASE}/cdn-cgi/challenge") == ""
    assert provider._refiltered_url("not-a-url") == ""
def test_stripped_filters_are_recovered_not_fatal():
    """The reported bug: the load lost its filters, and the run recovers
    by reloading the canonical page with them re-applied."""
    provider = make_provider(filter_settle_seconds=0.5)
    # The first load already consumed the one-shot redirect, so reloading
    # the same canonical path serves the filtered page.
    driver = FakeDriver()
    driver.current_url = UNFILTERED
    driver.visited = [FILTERED]

    provider._ensure_search_filters(driver, "melting ice cream")

    assert driver.visited == [FILTERED, FILTERED]
    assert driver.current_url == FILTERED


def test_delayed_client_side_strip_is_recovered():
    """Pexels rewrites the URL *after* the page loads, so the guard must
    still notice it and recover."""
    provider = make_provider(filter_settle_seconds=0.5)
    driver = FakeDriver(always_redirect={FILTERED})
    driver.get(FILTERED)  # initial load; the strip lands as it renders

    provider._ensure_search_filters(driver, "melting ice cream")
    assert driver.current_url == FILTERED


def test_recovery_is_bounded_then_fails_loudly():
    """A site that keeps stripping the filters must fail loudly after the
    configured number of retries, naming what was lost."""
    provider = make_provider(filter_recovery_attempts=2, filter_settle_seconds=0.5)
    driver = FakeDriver(
        navigations={FILTERED: UNFILTERED}, always_redirect={FILTERED}
    )
    driver.current_url = UNFILTERED

    with pytest.raises(VideoProviderError) as error:
        provider._ensure_search_filters(driver, "melting ice cream")

    message = str(error.value)
    assert "orientation" in message and "resolution_name" in message
    # Bounded: it retries, then gives up instead of looping forever.
    assert len(driver.visited) == 2, driver.visited


def test_recovery_stops_immediately_when_disabled():
    provider = make_provider(filter_recovery_attempts=0)
    driver = FakeDriver(
        navigations={FILTERED: UNFILTERED}, always_redirect={FILTERED}
    )
    driver.current_url = UNFILTERED

    with pytest.raises(VideoProviderError):
        provider._ensure_search_filters(driver, "melting ice cream")
    assert driver.visited == []


def test_no_recovery_url_fails_without_retrying():
    """Landed somewhere that is not a search page (home/bot check): there
    is nothing to re-filter, so it must not retry blindly."""
    provider = make_provider(filter_recovery_attempts=5)
    driver = FakeDriver()
    driver.current_url = f"{BASE}/"

    with pytest.raises(VideoProviderError):
        provider._ensure_search_filters(driver, "ice cream")
    assert driver.visited == [], driver.visited


def test_filtered_page_passes_through_untouched():
    provider = make_provider()
    driver = FakeDriver()
    driver.current_url = FILTERED
    provider._ensure_search_filters(driver, "melting ice cream")
    assert driver.visited == []


def test_bot_check_page_is_exempt():
    """Cloudflare's interstitial keeps the query params and is not a
    results page, so it must not trip the guard."""
    provider = make_provider()
    driver = FakeDriver()
    driver.current_url = FILTERED
    driver.title = "Just a moment..."
    provider._ensure_search_filters(driver, "melting ice cream")
    assert driver.visited == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))