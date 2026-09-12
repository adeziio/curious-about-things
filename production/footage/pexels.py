import re
import time
import random
import glob
import shutil

from pathlib import Path
from urllib.parse import quote, urlencode

import requests

from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from production.footage.base import VideoProvider, VideoProviderError

PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp")
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv", ".avi")

VIDEO_ID_PATTERN = re.compile(r"/video/[^/]*?-(\d+)/?(?:[?#].*)?$", re.IGNORECASE)
CLIP_ID_PATTERN = re.compile(r"/videos?(?:-files)?/(\d+)", re.IGNORECASE)


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

    def fetch(self, query, destination_dir, max_videos=2, downloaded_ids=None):
        """Search Pexels with orientation + 4K via URL params, download
        random clips by hovering each video card."""
        if downloaded_ids is None:
            downloaded_ids = set()
        query = str(query or "").strip()
        if not query:
            return []
        destination_dir = Path(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        if max_videos <= 0:
            return []
        self.notify(f"Searching Pexels for: {query}")
        driver = self._create_driver(download_dir=str(destination_dir))
        try:
            driver.maximize_window()
        except Exception:
            pass
        downloaded = []
        try:
            search_url = self._search_url(query)
            self.notify(f"Opening {search_url}")
            driver.get(search_url)
            self._wait_for_grid(driver)
            downloaded = self._download_random_videos(
                driver, destination_dir, max_videos, downloaded_ids, search_url
            )
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        return downloaded


    def _search_url(self, query):
        """Filtered search URL - orientation + resolution as query params,
        so no on-page filter clicking is ever needed."""
        orientation = str(self._setting("orientation", "portrait")).strip().lower()
        # Pexels' URL param is "portrait" for vertical videos.
        if orientation in ("vertical", "portrait"):
            orientation = "portrait"
        resolution = str(self._setting("resolution_name", "4K")).strip()
        params = urlencode({
            "orientation": orientation or "portrait",
            "resolution_name": resolution or "4K",
        })
        return f"{self._base_url()}/search/videos/{quote(query)}/?{params}"


    def _download_random_videos(
        self, driver, destination_dir, max_videos, downloaded_ids, search_url
    ):
        """Steps 4-6: Hover video cards on the results page and download
        clips directly. After each download, recover a clean results grid
        (Pexels shows a thank-you page/modal) before picking the next card."""
        downloaded = []
        failed_hrefs = set()
        main_handle = driver.current_window_handle
        while len(downloaded) < max_videos:
            card, href, video_id = self._pick_random_card(
                driver, downloaded_ids, failed_hrefs
            )
            if card is None:
                self.notify("No more downloadable videos on page")
                break
            try:
                self.notify(f"Trying {href}")
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
                if new_file:
                    clip_name = f"clip_{len(downloaded) + 1:03d}.mp4"
                    clip_path = destination_dir / clip_name
                    self._move_file(new_file, clip_path)
                    if video_id:
                        downloaded_ids.add(video_id)
                    downloaded.append(str(clip_path))
                    self.notify(f"Saved clip: {clip_name}")
                else:
                    self.notify("Download did not finish in time; trying next video")
                    failed_hrefs.add(href)
                self._human_pause()
            except Exception as error:
                self.notify(f"Download failed for {href}: {error}")
                failed_hrefs.add(href)
                self._human_pause()
            # Pexels shows a thank-you page/modal after each download -
            # recover the results grid before picking the next card.
            self._recover_page(driver, search_url, main_handle)
        return downloaded

    def _recover_page(self, driver, search_url, main_handle):
        """After a download, Pexels may open a thank-you page/modal or a new
        tab. Return to a clean, filtered results grid so the next card can be
        hovered and downloaded."""
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
        self._wait_for_grid(driver)
        self._human_pause()

    def _wait_for_grid(self, driver):
        """Functional wait until the video grid has rendered - returns as
        soon as video links appear instead of sleeping a fixed duration."""
        try:
            WebDriverWait(
                driver, self._seconds("page_timeout_seconds", 60)
            ).until(EC.presence_of_element_located(
                (By.XPATH, "//a[starts-with(@href, '/video/')]")
            ))
            self.notify("Video grid ready")
            return True
        except Exception:
            self.notify("Video grid did not render in time")
            return False

    def _pick_random_card(self, driver, downloaded_ids, failed_hrefs=None):
        """Pick one random video card on the results page that has not been
        downloaded yet. Returns (card, href, video_id) or (None, '', '')."""
        if failed_hrefs is None:
            failed_hrefs = set()
        links = driver.find_elements(By.XPATH, "//a[starts-with(@href, '/video/')]")
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

        When attaching to an existing Chrome, per-session download prefs do
        not apply, so the file may land in the user's Downloads folder.
        """
        directories = [Path(destination_dir)]
        fallback = Path.home() / "Downloads"
        if fallback.is_dir() and fallback not in directories:
            directories.append(fallback)
        return directories

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

    def _download_url(self, url, output_path):
        """Download a direct URL to output_path."""
        download_timeout = self._seconds("download_timeout_seconds", 300)
        response = requests.get(
            url,
            stream=True,
            timeout=download_timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": self._base_url(),
            },
        )
        if response.status_code != 200:
            raise VideoProviderError(f"HTTP {response.status_code}")
        output_path = Path(output_path)
        with output_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)
        if not output_path.is_file() or output_path.stat().st_size < 1024:
            output_path.unlink(missing_ok=True)
            raise VideoProviderError("Downloaded clip was empty")
        return output_path

    def _create_driver(self, download_dir=None):
        if self._attach_to_existing_chrome():
            try:
                return self._attach_driver(download_dir=download_dir)
            except Exception as error:
                self.notify(f"Could not attach to Chrome ({error}); launching new...")
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
        return driver

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

    def _profile_directory(self):
        profile_directory = Path(
            str(self._setting("profile_directory", self.DEFAULT_PROFILE_DIRECTORY))
        ).resolve()
        profile_directory.mkdir(parents=True, exist_ok=True)
        return profile_directory

