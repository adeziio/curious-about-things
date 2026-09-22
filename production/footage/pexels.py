import os
import re
import time
import json
import random
import glob
import hashlib
import shutil
import subprocess

from pathlib import Path
from urllib.parse import quote, urlencode

import requests

from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By

from production.footage.base import VideoProvider, VideoProviderError

PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp")
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv", ".avi")

VIDEO_ID_PATTERN = re.compile(r"/video/[^/]*?-(\d+)/?(?:[?#].*)?$", re.IGNORECASE)
CLIP_ID_PATTERN = re.compile(r"/videos?(?:-files)?/(\d+)", re.IGNORECASE)

# Video page links in the results grid. Pexels sometimes renders the
# cards with relative hrefs ("/video/...") and sometimes with absolute
# ones ("https://www.pexels.com/video/..."), so the match must not
# depend on where the href starts. Download links
# ("/download/video/...") are excluded - they are not result cards.
VIDEO_CARD_XPATH = (
    "//a[contains(@href, '/video/') "
    "and not(contains(@href, '/download'))]"
)

# Pexels sits behind Cloudflare, which sometimes answers a navigation
# with its "Just a moment..." bot check instead of the page that was
# requested. That interstitial has no video grid at all, so treating it
# like a merely slow page load makes an attendable check look like a
# broken search ("Search results page did not load"). It is therefore
# detected explicitly and given its own, longer wait - it usually
# clears by itself within seconds, but it can also wait for a human
# click, so the operator is told what is happening.
CHALLENGE_URL_MARKERS = ("__cf_chl", "/cdn-cgi/challenge")
CHALLENGE_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
)
CHALLENGE_TEXT_MARKERS = (
    "verify you are human",
    "performing security verification",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
)


class PexelsVideoProvider(VideoProvider):
    DEFAULT_BASE_URL = "https://www.pexels.com"
    DEFAULT_PROFILE_DIRECTORY = "media/browser_profile/pexels"

    def __init__(self, config, notify=None):
        super().__init__(config, notify=notify)
        load_dotenv()
        self.settings = config.get("pexels", {})
        if not isinstance(self.settings, dict):
            self.settings = {}
        if not self._flag("enabled", True):
            raise VideoProviderError("Pexels disabled.")

    def fetch(self, query, destination_dir, max_videos=2, downloaded_ids=None, downloaded_hashes=None):
        """Search Pexels with orientation + 4K via URL params, download
        random clips by hovering each video card.

        Resume-aware: if `destination_dir` already holds `max_videos`
        complete clips from a previous (possibly interrupted) run, no
        search or download happens at all. If it holds only some of
        them, only the missing remainder is downloaded and the new
        clips are numbered after the existing ones.

        Unique: every new clip is guaranteed unique by content (SHA-256) -
        not just by Pexels video id. The same footage can appear under
        different Pexels pages/ids, so id-only tracking still allows
        byte-identical duplicates across folders. `downloaded_ids`
        (Pexels ids) and `downloaded_hashes` (content hashes) are
        episode-wide mutable sets shared across queries: they are
        updated in place as clips are kept, so later queries never
        re-download content fetched by earlier ones. Old clips already
        on disk are NEVER deleted - only a freshly downloaded file
        whose hash is already known is deleted (it is the "latest"
        file, so no numbering gap is left) and a different card is
        tried instead.

        Strict: exactly `max_videos` clips are guaranteed on return.
        Any failure raises VideoProviderError - a partial result is
        never returned.
        """
        if downloaded_ids is None:
            downloaded_ids = set()
        if downloaded_hashes is None:
            downloaded_hashes = set()
        query = str(query or "").strip()
        if not query:
            raise VideoProviderError(
                "Cannot download clips: search query is empty."
            )
        destination_dir = Path(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        if max_videos <= 0:
            raise VideoProviderError(
                f"Cannot download clips for '{query}': "
                f"max_videos must be positive (got {max_videos})."
            )

        existing = self._existing_clips(destination_dir)

        # NEVER delete old clips: files already on disk are always kept,
        # no matter what. Their content hashes are seeded into the
        # episode-wide `downloaded_hashes` set so later downloads can be
        # compared against them - but if an on-disk clip turns out to be
        # a duplicate of an earlier query, it stays. Only a freshly
        # downloaded clip is ever deleted (see _download_random_videos),
        # because that is the "latest" file and no numbering gap is left
        # behind. The folder simply has to end up with `max_videos`
        # clips; gaps in numbering (e.g. a lone clip_002) are fine.
        for clip_path in existing:
            content_hash = self._file_hash(clip_path)
            if content_hash:
                downloaded_hashes.add(content_hash)

        if len(existing) >= max_videos:
            self.notify(
                f"Already have {len(existing)} clips for '{query}'; "
                "skipping download"
            )
            return [str(clip_path) for clip_path in existing]

        missing = max_videos - len(existing)

        if existing:
            self.notify(
                f"Found {len(existing)} existing clips for '{query}'; "
                f"downloading {missing} more"
            )

        # New downloads continue after the highest clip number already
        # on disk, so a gap (e.g. a lone clip_002 with no clip_001)
        # never causes an overwrite: the next download becomes clip_003,
        # then clip_004, and so on. The folder only has to end up with
        # `max_videos` clips; the numbers themselves just need to be
        # unique within the folder.
        existing_count = self._next_clip_index(existing) - 1
        self.notify(f"Searching Pexels for: {query}")
        downloaded = []
        driver = None
        try:
            driver = self._create_driver(
                download_dir=str(destination_dir)
            )
            try:
                driver.maximize_window()
            except Exception:
                pass
            search_url = self._search_url(query)
            self.notify(f"Opening {search_url}")
            driver.get(search_url)
            if not self._wait_for_grid(driver):
                raise VideoProviderError(
                    self._grid_failure_message(driver, query)
                )
            # The filters are only verified once the real results grid is
            # on screen: Cloudflare's bot check replaces the search page
            # entirely (it keeps the query params, so it would otherwise
            # be mistaken for a page whose filters were dropped), and a
            # rewrite to an unfiltered results page is still caught here
            # before anything is downloaded.
            self._verify_search_filters(driver, query)
            # Give the page a beat to settle (and Pexels' client-side
            # filters a moment to apply) before any card is picked.
            self._human_pause()
            downloaded = self._download_random_videos(
                driver, destination_dir, missing, downloaded_ids, search_url,
                query=query, existing_count=existing_count,
                downloaded_hashes=downloaded_hashes,
            )
        except VideoProviderError:
            raise
        except Exception as error:
            raise VideoProviderError(
                f"Pexels clip download failed for '{query}': {error}"
            ) from error
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
        result = (
            [str(clip_path) for clip_path in existing]
            + downloaded
        )
        if len(result) < max_videos:
            raise VideoProviderError(
                f"Only {len(result)}/{max_videos} clips downloaded "
                f"for '{query}'"
            )
        return result

    @staticmethod
    def _existing_clips(destination_dir):
        """Complete video files already present in the query folder,
        ordered by name. Partial downloads and empty files are ignored
        so an interrupted download never counts as a finished clip."""
        clips = []
        try:
            entries = list(Path(destination_dir).iterdir())
        except OSError:
            return clips
        for path in entries:
            if not path.is_file():
                continue
            if path.suffix.lower() in PARTIAL_SUFFIXES:
                continue
            if path.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            try:
                if path.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            clips.append(path)
        clips.sort(key=lambda path: path.name)
        return clips

    @staticmethod
    def _next_clip_index(clips):
        """Next free clip number: one past the highest clip_NNN already
        on disk (1 when the folder is empty).

        Count-based numbering (`len(existing) + 1`) breaks whenever a
        gap exists - e.g. a lone clip_002 with no clip_001 would make
        the next download clip_002 again and overwrite the kept clip.
        Scanning the actual numbers keeps every filename unique no
        matter which clips were pruned or deleted.
        """
        highest = 0
        for clip_path in clips:
            match = re.search(r"(\d+)", Path(clip_path).stem)
            if not match:
                continue
            try:
                highest = max(highest, int(match.group(1)))
            except ValueError:
                pass
        return highest + 1

    @staticmethod
    def _file_hash(path):
        """SHA-256 of a downloaded clip, or "" when it cannot be read.

        Hashed in chunks so multi-MB clips never load fully into memory.
        """
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return ""

    @staticmethod
    def _clip_id_from_file(path):
        """Recover the Pexels video id for a clip kept from a previous
        run. Resume downloads rename files to clip_NNN.mp4, so the id is
        not recoverable from the filename - the id lives only in the
        in-memory `downloaded_ids` set and is not persisted to disk.
        Currently always returns "" (kept as a hook in case the mapping
        is persisted later). Content-hash seeding is what actually
        protects resume + cross-query uniqueness."""
        return ""

    def _search_url(self, query):
        """Filtered search URL - orientation + resolution as query params,
        so no on-page filter clicking is ever needed.

        The query is normalized to spaces (no hyphens) because Pexels
        rewrites hyphenated path segments client side - e.g.
        "/search/videos/microphone%20close-up/" becomes
        "/search/videos/microphone%20close%20up/" - and that rewrite
        DROPS the orientation and resolution_name query params,
        silently turning the search into an unfiltered one."""
        orientation = str(self._setting("orientation", "portrait")).strip().lower()
        # Pexels' URL param is "portrait" for vertical videos.
        if orientation in ("vertical", "portrait"):
            orientation = "portrait"
        resolution = str(self._setting("resolution_name", "4K")).strip()
        params = urlencode({
            "orientation": orientation or "portrait",
            "resolution_name": resolution or "4K",
        })
        search_term = quote(str(query).replace("-", " "))
        return f"{self._base_url()}/search/videos/{search_term}/?{params}"

    def _verify_search_filters(self, driver, query):
        """Fail loudly if Pexels dropped the orientation/resolution
        params while loading the results page - an unfiltered search
        must never be used for downloads.

        Cloudflare's bot-check interstitial is exempt: it is not a
        search results page at all, so it is skipped here (and the
        filters are re-checked once the real grid has loaded)."""
        if self._page_is_challenge(driver):
            self.notify(
                "Bot check page (no results yet) - filters are checked "
                "again once the search results load"
            )
            return
        final_url = str(driver.current_url or "")
        if "orientation=" not in final_url:
            raise VideoProviderError(
                f"Pexels dropped the search filters while loading "
                f"'{query}' (landed on: {final_url})"
            )


    def _download_random_videos(
        self, driver, destination_dir, max_videos, downloaded_ids, search_url,
        query="", existing_count=0, downloaded_hashes=None
    ):
        """Steps 4-6: Pick random video cards on the results page and
        download the clips. After a browser download, recover a clean
        results grid (Pexels shows a thank-you page/modal) before picking
        the next card. `existing_count` shifts clip numbering so resumed
        downloads do not overwrite clips kept from a previous run.

        Unique: `downloaded_ids` skips Pexels pages already used, while
        `downloaded_hashes` (episode-wide content hashes, updated in
        place) catches the case the IDs miss - the same footage served
        under different Pexels pages/ids. Only the freshly downloaded
        ("latest") file is ever deleted when its SHA-256 is already
        known - old clips on disk are never touched - and a different
        card is tried instead, so the folder always ends up with
        `max_videos` unique clips.

        Strict: every clip must download. Any failure raises
        VideoProviderError so the run fails loudly instead of silently
        continuing with fewer clips than requested."""
        if downloaded_hashes is None:
            downloaded_hashes = set()
        downloaded = []
        main_handle = driver.current_window_handle
        duplicate_skips = 0
        max_duplicate_skips = max(20, max_videos * 5)
        while len(downloaded) < max_videos:
            card, href, video_id = self._pick_random_card(
                driver, downloaded_ids
            )
            if card is None:
                raise VideoProviderError(
                    f"Not enough downloadable videos found on Pexels "
                    f"for '{query}' "
                    f"({len(downloaded)}/{max_videos} downloaded)"
                )
            self.notify(f"Trying {href}")
            clip_name = (
                f"clip_{existing_count + len(downloaded) + 1:03d}.mp4"
            )
            clip_path = destination_dir / clip_name
            used_browser = False
            try:
                if not self._download_direct(video_id, clip_path):
                    used_browser = True
                    self._download_from_card(
                        card, driver, destination_dir, clip_path, href
                    )
                if video_id:
                    downloaded_ids.add(video_id)
                content_hash = self._file_hash(clip_path)
                if content_hash and content_hash in downloaded_hashes:
                    # Same bytes already used in this episode (possibly
                    # under a different Pexels id/folder) - drop it and
                    # try a different card. Clip numbering is derived
                    # from len(downloaded), so the next attempt reuses
                    # this filename and leaves no numbering gap.
                    try:
                        clip_path.unlink()
                    except OSError:
                        pass
                    duplicate_skips += 1
                    self.notify(
                        f"Skipping duplicate clip from {href} "
                        f"(same content already used in this episode)."
                    )
                    if duplicate_skips > max_duplicate_skips:
                        raise VideoProviderError(
                            f"Could not find enough unique clips for "
                            f"'{query}': {duplicate_skips} downloads were "
                            f"duplicates of footage already used in this "
                            f"episode. Try a broader search query."
                        )
                    if used_browser:
                        self._recover_page(driver, search_url, main_handle, query)
                    continue
                if content_hash:
                    downloaded_hashes.add(content_hash)
                downloaded.append(str(clip_path))
                self.notify(f"Saved clip: {clip_name}")
                self._human_pause()
            except VideoProviderError:
                raise
            except Exception as error:
                raise VideoProviderError(
                    f"Clip download failed for '{query}' ({href}): {error}"
                ) from error
            if used_browser:
                # Only a browser download leaves a thank-you page/modal or
                # an extra tab behind. Reloading the results page is also
                # the single biggest source of Cloudflare's bot check, so
                # it is skipped completely whenever the direct download
                # worked - which is the normal case.
                self._recover_page(driver, search_url, main_handle, query)
        return downloaded

    def _download_from_card(
        self, card, driver, destination_dir, clip_path, href
    ):
        """Fallback download path: hover the card, click its Download
        button and wait for the browser to finish writing the file."""
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", card
        )
        webdriver.ActionChains(driver).move_to_element(card).perform()
        self.notify("Hovered video card")
        # Human beat: let the hover-revealed Download button
        # settle and avoid machine-gun-fast movements.
        self._human_pause()
        before = self._snapshot_downloads(destination_dir)
        self._click_card_download(card, driver)
        new_file = self._wait_for_new_file(destination_dir, before)
        if not new_file:
            raise VideoProviderError(
                f"Download did not finish in time for {href}"
            )
        self._move_file(new_file, clip_path)
        return clip_path

    def _recover_page(self, driver, search_url, main_handle, query=""):
        """After a *browser* download, Pexels may open a thank-you
        page/modal or a new tab. Return to a clean, filtered results grid
        so the next card can be hovered and downloaded.

        The direct download path never navigates, so it never needs this
        (and skipping it is what keeps the bot check away)."""
        # Close any extra tabs the download opened.
        try:
            for handle in list(driver.window_handles):
                if handle != main_handle:
                    driver.switch_to.window(handle)
                    driver.close()
                    self.notify("Closed download tab")
            driver.switch_to.window(main_handle)
        except Exception as error:
            self.notify(f"Tab cleanup skipped: {error}")
        # A fresh load clears any thank-you modal and rebuilds the grid.
        self.notify("Reloading results page for next download")
        driver.get(search_url)
        # This reload is as likely to be answered with the bot check as
        # the first load, so the wait (and its error) must be the same:
        # otherwise the next card pick would report "not enough videos"
        # for a page that never showed any results at all.
        if not self._wait_for_grid(driver):
            raise VideoProviderError(
                self._grid_failure_message(driver, query or "next download")
            )
        self._verify_search_filters(driver, query or "next download")
        self._human_pause()

    def _wait_for_grid(self, driver):
        """Functional wait until the video grid has rendered - returns as
        soon as video links appear instead of sleeping a fixed duration.

        Transient Selenium errors (mid-navigation polls while Pexels
        rewrites the URL client side, momentary session hiccups) are
        absorbed and polling continues until the timeout - a single
        transient error must never fail an otherwise fine page.

        Cloudflare's bot check is waited out on a separate, longer
        budget (`challenge_timeout_seconds`): while it is on screen there
        is no grid to find, and it normally clears on its own - so it
        must not be reported as a broken search. The regular page
        timeout keeps applying to everything else.
        """
        timeout = self._seconds("page_timeout_seconds", 60)
        challenge_timeout = self._seconds("challenge_timeout_seconds", 180)
        deadline = time.monotonic() + timeout
        challenge_deadline = None
        challenge_announced = False
        last_notify = time.monotonic()
        while True:
            if self._grid_present(driver):
                self.notify("Video grid ready")
                return True
            now = time.monotonic()
            if self._page_is_challenge(driver):
                if challenge_deadline is None:
                    challenge_deadline = now + challenge_timeout
                if not challenge_announced:
                    self.notify(
                        "Pexels is showing its bot check "
                        "(\"Just a moment...\") instead of the search "
                        f"results - waiting up to {int(challenge_timeout)}s "
                        "for it to clear. If it asks for a click, complete "
                        "it in the browser window."
                    )
                    challenge_announced = True
                # A challenge page invalidates the normal page deadline:
                # whatever loaded before it is gone, so the wait is
                # extended while the check is still on screen.
                deadline = max(deadline, challenge_deadline)
            if now >= deadline:
                break
            if now - last_notify >= 15:
                remaining = int(deadline - now)
                self.notify(
                    f"Waiting for the video grid to load ({remaining}s left)..."
                )
                last_notify = now
            time.sleep(1)
        try:
            landed = driver.current_url
        except Exception:
            landed = "unknown"
        self.notify(
            f"Video grid did not render in time (landed on: {landed})"
        )
        return False

    @staticmethod
    def _grid_present(driver):
        """True when the results grid has rendered video cards.

        Transient errors while polling (e.g. the page is navigating) are
        treated as 'not yet' so the wait continues."""
        try:
            return bool(driver.find_elements(By.XPATH, VIDEO_CARD_XPATH))
        except Exception:
            return False

    @staticmethod
    def _page_is_challenge(driver):
        """True while Cloudflare's bot-check interstitial is on screen.

        The check is identified by its challenge URL params, its
        "Just a moment..." title and its on-page text, so it is still
        recognised if any one of those details changes."""
        try:
            url = str(driver.current_url or "").lower()
        except Exception:
            url = ""
        if any(marker in url for marker in CHALLENGE_URL_MARKERS):
            return True
        try:
            title = str(driver.title or "").strip().lower()
        except Exception:
            title = ""
        if any(marker in title for marker in CHALLENGE_TITLE_MARKERS):
            return True
        try:
            text = driver.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            return False
        return any(marker in text for marker in CHALLENGE_TEXT_MARKERS)

    def _grid_failure_message(self, driver, query):
        """Explain why the results grid never appeared: a bot check that
        did not clear is an attendable condition, not a missing page, and
        it needs a different response from the operator."""
        try:
            landed = driver.current_url
        except Exception:
            landed = "unknown"
        if self._page_is_challenge(driver):
            return (
                f"Search results page did not load for '{query}': Pexels "
                "is showing its bot check (\"Just a moment...\") and it did "
                f"not clear in time. Complete the check in the browser "
                f"window and run again (landed on: {landed})."
            )
        return (
            f"Search results page did not load for '{query}' "
            f"(landed on: {landed})"
        )

    def _pick_random_card(self, driver, downloaded_ids, failed_hrefs=None):
        """Pick one random video card on the results page that has not been
        downloaded yet. Returns (card, href, video_id) or (None, '', '')."""
        if failed_hrefs is None:
            failed_hrefs = set()
        links = driver.find_elements(By.XPATH, VIDEO_CARD_XPATH)
        candidates = []
        for link in links:
            try:
                href = link.get_attribute("href") or ""
            except Exception:
                continue
            if not href or href in failed_hrefs:
                continue
            video_id = self._clip_id(href)
            if video_id and video_id in downloaded_ids:
                self.notify(f"Skipping clip {video_id} (already downloaded)")
                continue
            try:
                card = link.find_element(By.XPATH, "ancestor::article[1] | ..")
            except Exception:
                continue
            candidates.append((card, href, video_id))
        self.notify(f"Found {len(candidates)} matching video(s) on page")
        if not candidates:
            return None, "", ""
        random.shuffle(candidates)
        return candidates[0]

    def _click_card_download(self, card, driver):
        """Step 5: Click the 'Download' button that appears when hovering
        the video card - no page navigation needed."""
        translated = (
            "translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
        )
        selector = f".//*[self::a or self::button][contains({translated}, 'download')]"
        button = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            button = self._first_displayed(card, selector)
            if button is not None:
                break
            # Keep the pointer over the card so the button stays visible.
            webdriver.ActionChains(driver).move_to_element(card).perform()
            time.sleep(1)
        if button is None:
            raise VideoProviderError(
                "No Download button appeared on the hovered video card"
            )
        driver.execute_script("arguments[0].click();", button)
        self.notify("Clicked Download")
        self._human_pause()

    @staticmethod
    def _first_displayed(parent, selector):
        """Return the first displayed element matching selector, or None."""
        try:
            elements = parent.find_elements(By.XPATH, selector)
        except Exception:
            return None
        for element in elements:
            try:
                if element.is_displayed():
                    return element
            except Exception:
                continue
        return None

    def _download_directories(self, destination_dir):
        """Directories to watch for the browser's downloads.

        When attaching to an existing Chrome, per-session download prefs
        do not apply, so the file may land in whatever directory Chrome
        was originally launched with (persisted in the automation
        profile's Preferences) or the user's Downloads folder.
        """
        directories = [Path(destination_dir)]
        candidates = [
            Path.home() / "Downloads",
            *self._profile_download_directories(),
        ]
        for directory in candidates:
            if directory.is_dir() and directory not in directories:
                directories.append(directory)
        return directories

    def _profile_download_directories(self):
        """Download directories Chrome used in past sessions, read from
        the automation profile's persisted Preferences file. Covers the
        attached-browser case where a download lands in the folder a
        previous query passed at launch time."""
        directories = []
        try:
            profile_root = Path(self._profile_directory())
        except Exception:
            return directories
        for preferences_file in profile_root.glob("*/Preferences"):
            try:
                data = json.loads(
                    preferences_file.read_text(encoding="utf-8")
                )
            except Exception:
                continue
            path = str(
                data.get("download", {}).get("default_directory", "")
            ).strip()
            if path:
                directories.append(Path(path))
        return directories

    def _force_download_behavior(self, driver, download_dir):
        """Point an already-running Chrome at `download_dir` for the
        rest of this session via the DevTools protocol. Prefs passed
        through Selenium when attaching to an existing browser are
        ignored by Chrome, so this is the only reliable way to redirect
        its downloads to the current query's folder."""
        if not download_dir:
            return
        try:
            driver.execute_cdp_cmd(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(download_dir),
                    "eventsEnabled": False,
                },
            )
            self.notify(f"Chrome download target: {download_dir}")
        except Exception as error:
            self.notify(
                f"Could not redirect Chrome downloads via CDP ({error}); "
                "falling back to watching known folders"
            )

    def _snapshot_downloads(self, destination_dir):
        snapshot = set()
        for directory in self._download_directories(destination_dir):
            snapshot.update(glob.glob(str(directory / "*")))
        return snapshot

    def _wait_for_new_file(self, destination_dir, before, timeout=None):
        """Wait for a new, fully downloaded video file to appear."""
        if timeout is None:
            timeout = self._seconds("download_timeout_seconds", 120)
        deadline = time.monotonic() + timeout
        last_notify = time.monotonic()
        while time.monotonic() < deadline:
            current = self._snapshot_downloads(destination_dir)
            for path in current - before:
                file_path = Path(path)
                if not file_path.is_file():
                    continue
                if file_path.suffix.lower() in PARTIAL_SUFFIXES:
                    continue
                if file_path.suffix.lower() not in VIDEO_SUFFIXES:
                    continue
                if file_path.stat().st_size > 0:
                    return file_path
            # Keep progress alive during long downloads instead of
            # sitting silently until the timeout.
            if time.monotonic() - last_notify >= 15:
                remaining = int(deadline - time.monotonic())
                self.notify(
                    f"Waiting for download to finish "
                    f"({remaining}s left)..."
                )
                last_notify = time.monotonic()
            time.sleep(1)
        return None

    def _move_file(self, source, destination):
        """Move a downloaded file into the query's clip folder."""
        destination = Path(destination)
        try:
            shutil.move(str(source), str(destination))
        except Exception:
            shutil.copyfile(str(source), str(destination))
            try:
                source.unlink()
            except Exception:
                pass
        return destination

    @classmethod
    def _clip_id(cls, url):
        """Extract the Pexels video id from a video page or file URL."""
        match = VIDEO_ID_PATTERN.search(str(url))
        if match:
            return match.group(1)
        fallback = CLIP_ID_PATTERN.search(str(url))
        return fallback.group(1) if fallback else ""

    def _direct_download_url(self, video_id):
        """Pexels' download endpoint for a video id - the exact URL the
        card's hover Download button points at. It redirects to the file
        on videos.pexels.com, so the clip can be fetched directly instead
        of making the browser stream it."""
        return f"{self._base_url()}/download/video/{video_id}/"

    def _download_direct(self, video_id, clip_path):
        """Primary download path: fetch the picked clip with requests.

        The browser path is kept only as a fallback because the same file
        arrives through Chrome at a fraction of the speed (measured
        ~0.2 MB/s through the browser vs ~30 MB/s direct) and can stall
        mid-transfer, which used to fail whole runs. Fetching the clip
        this way also means the results page is never navigated after a
        download, which is what keeps Pexels' bot check away.

        Returns True when the clip was saved, False when the caller
        should fall back to the browser.
        """
        if not video_id:
            return False
        if not self._flag("direct_download", True):
            self.notify("Direct download disabled; using the browser")
            return False
        try:
            self._download_url(self._direct_download_url(video_id), clip_path)
        except VideoProviderError as error:
            self.notify(
                f"Direct download of clip {video_id} failed ({error}); "
                "falling back to the browser"
            )
            return False
        return True

    def _download_url(self, url, output_path):
        """Download a direct URL to output_path.

        The clip is streamed to a `.part` file and only moved into place
        once it has arrived in full, so an interrupted transfer can never
        be mistaken for a finished clip (`existing clips` ignores
        `.part` files)."""
        download_timeout = self._seconds("download_timeout_seconds", 300)
        output_path = Path(output_path)
        partial_path = output_path.with_name(f"{output_path.name}.part")
        partial_path.unlink(missing_ok=True)
        try:
            response = requests.get(
                url,
                stream=True,
                timeout=(30, download_timeout),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/140.0.0.0 Safari/537.36"
                    ),
                    "Referer": f"{self._base_url()}/",
                },
            )
            if response.status_code != 200:
                raise VideoProviderError(f"HTTP {response.status_code}")
            expected = int(response.headers.get("Content-Length") or 0)
            written = 0
            last_notify = time.monotonic()
            with partial_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    # Keep progress alive on slow connections instead of
                    # sitting silently until the timeout.
                    if time.monotonic() - last_notify >= 15:
                        self.notify(self._download_progress(written, expected))
                        last_notify = time.monotonic()
            if written < 1024:
                raise VideoProviderError("Downloaded clip was empty")
            self._move_file(partial_path, output_path)
        except VideoProviderError:
            partial_path.unlink(missing_ok=True)
            raise
        except Exception as error:
            partial_path.unlink(missing_ok=True)
            raise VideoProviderError(f"Download failed: {error}") from error
        return output_path

    @staticmethod
    def _download_progress(written, expected):
        """Human-readable progress line for a running download."""
        megabytes = written / 1024 / 1024
        if expected > 0:
            return (
                f"Downloading clip: {megabytes:.0f}/"
                f"{expected / 1024 / 1024:.0f} MB"
            )
        return f"Downloading clip: {megabytes:.0f} MB"

    def _create_driver(self, download_dir=None):
        if self._attach_to_existing_chrome():
            try:
                return self._attach_driver(download_dir=download_dir)
            except Exception as error:
                self.notify(f"Could not attach to Chrome ({error}); launching new...")
        try:
            return self._launch_driver(download_dir=download_dir)
        except Exception as error:
            # A Chrome left behind by an interrupted run keeps the
            # automation profile locked, so a fresh launch cannot
            # start (the new process just forwards to the stale one
            # and chromedriver times out). Close only Chrome
            # instances bound to the automation profile - never the
            # user's personal browser - and try once more.
            self.notify(
                f"Could not launch Chrome ({error}); "
                "closing stale profile Chrome..."
            )
            self._close_stale_profile_chrome()
            return self._launch_driver(download_dir=download_dir)

    def _attach_driver(self, download_dir=None):
        options = webdriver.ChromeOptions()
        options.debugger_address = self._debugging_address()
        if download_dir:
            options.add_experimental_option("prefs", {
                "download.default_directory": download_dir,
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
            })
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(self._seconds("page_timeout_seconds", 60))
        # Attached browsers ignore Selenium's prefs dict, so redirect
        # their downloads via the DevTools protocol as well.
        self._force_download_behavior(driver, download_dir)
        return driver

    def _launch_driver(self, download_dir=None):
        options = webdriver.ChromeOptions()
        if self._headless():
            options.add_argument("--headless=new")
        options.add_argument("--window-size=1280,2000")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--log-level=3")
        # Launch with the configured debugging port so that later
        # fetches can attach to this browser instead of launching a
        # fresh one for every search query.
        options.add_argument(
            f"--remote-debugging-port={self._debugging_port()}"
        )
        if download_dir:
            options.add_experimental_option("prefs", {
                "download.default_directory": download_dir,
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
            })
        profile_directory = self._profile_directory()
        options.add_argument(f"--user-data-dir={profile_directory}")
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(self._seconds("page_timeout_seconds", 60))
        # Belt and braces: prefs handle a fresh launch, but make the
        # target explicit via CDP too so behavior never depends on
        # whether Chrome honored the prefs.
        self._force_download_behavior(driver, download_dir)
        return driver

    def _close_stale_profile_chrome(self):
        """Terminate Chrome processes still bound to the automation
        profile. These are leftovers from an interrupted run; they keep
        the profile locked so neither attaching nor a fresh launch can
        use it. Chrome processes using any other profile (the user's
        personal browser) are never touched."""
        marker = str(self._profile_directory()).lower()
        killed = 0
        for pid, command_line in self._chrome_processes():
            if marker not in command_line.lower():
                continue
            if self._kill_process(pid):
                killed += 1
        if killed:
            self.notify(
                f"Closed {killed} stale Chrome process(es) "
                "using the automation profile"
            )
            time.sleep(2)
        return killed

    @staticmethod
    def _chrome_processes():
        """Running Chrome processes as (pid, command_line) tuples."""
        processes = []
        try:
            if os.name == "nt":
                output = subprocess.run(
                    [
                        "powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process "
                        "-Filter \"Name='chrome.exe'\" "
                        "| Select-Object ProcessId, CommandLine "
                        "| ConvertTo-Json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                ).stdout
                data = json.loads(output or "[]")
                if isinstance(data, dict):
                    data = [data]
                for entry in data or []:
                    try:
                        processes.append(
                            (
                                int(entry.get("ProcessId", 0)),
                                str(entry.get("CommandLine") or ""),
                            )
                        )
                    except (TypeError, ValueError):
                        continue
            else:
                output = subprocess.run(
                    ["ps", "-eo", "pid=,args="],
                    capture_output=True,
                    text=True,
                    timeout=30,
                ).stdout
                for line in output.splitlines():
                    parts = line.strip().split(None, 1)
                    if len(parts) != 2 or "chrome" not in parts[1].lower():
                        continue
                    try:
                        processes.append((int(parts[0]), parts[1]))
                    except ValueError:
                        continue
        except Exception:
            return []
        return processes

    @staticmethod
    def _kill_process(pid):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True,
                    timeout=30,
                )
            else:
                subprocess.run(
                    ["kill", "-9", str(pid)],
                    capture_output=True,
                    timeout=30,
                )
            return True
        except Exception:
            return False

    def _human_pause(self):
        low = self._seconds("pause_min_seconds", 1)
        high = max(self._seconds("pause_max_seconds", 3), low)
        if high <= low:
            high = low + 1
        time.sleep(random.uniform(low, high))

    def _setting(self, name, default=""):
        return self.settings.get(name, default)

    def _seconds(self, name, default):
        try:
            value = float(self._setting(name, default))
        except (TypeError, ValueError):
            return float(default)
        if value <= 0:
            return float(default)
        return value

    def _flag(self, name, default=False):
        value = self._setting(name, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _base_url(self):
        base_url = str(self._setting("base_url", self.DEFAULT_BASE_URL)).strip()
        return base_url or self.DEFAULT_BASE_URL

    def _headless(self):
        return self._flag("headless", True)

    def _attach_to_existing_chrome(self):
        return self._flag("attach_to_existing_chrome", False)

    def _debugging_address(self):
        address = str(self._setting("debugging_address", "")).strip()
        return address or "127.0.0.1:9222"

    def _debugging_port(self):
        address = self._debugging_address()
        match = re.search(r"(\d+)\s*$", address)
        if match:
            return int(match.group(1))
        return 9222

    def _profile_directory(self):
        profile_directory = Path(
            str(self._setting("profile_directory", self.DEFAULT_PROFILE_DIRECTORY))
        ).resolve()
        profile_directory.mkdir(parents=True, exist_ok=True)
        return profile_directory

