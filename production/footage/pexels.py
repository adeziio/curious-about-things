import re
import time
import random

from pathlib import Path
from urllib.parse import quote

import requests

from dotenv import (
    load_dotenv
)

from selenium import webdriver

from selenium.webdriver.common.by import (
    By
)

from production.footage.base import (
    VideoProvider,
    VideoProviderError
)


MP4_URL_PATTERN = re.compile(
    r"https://videos\.pexels\.com/[^\s\"'<>\\]+?\.mp4[^\s\"'<>\\]*",
    re.IGNORECASE
)

DIMENSION_PATTERN = re.compile(
    r"_(\d{3,4})_(\d{3,4})_(\d{2,3})fps",
    re.IGNORECASE
)

PARTIAL_SUFFIXES = (
    ".crdownload",
    ".part",
    ".tmp"
)


class PexelsVideoProvider(
    VideoProvider
):

    """
    Stock-video provider backed by Pexels (https://www.pexels.com).

    Uses the existing Selenium architecture: a Chrome browser is
    driven to the Pexels video search results for each AI-generated
    search query, the direct MP4 URLs of the preview players on the
    results page are collected, and several candidate clips are
    downloaded locally.

    All Pexels-specific settings come from config/pexels.json.
    """

    DEFAULT_BASE_URL = "https://www.pexels.com"

    DEFAULT_PROFILE_DIRECTORY = (
        "media/browser_profile/pexels"
    )

    def __init__(
        self,
        config,
        notify=None
    ):

        super().__init__(
            config,
            notify=notify
        )

        # Callers may already have loaded .env; loading again is
        # harmless and keeps the provider usable standalone.

        load_dotenv()

        self.settings = (
            config.get(
                "pexels",
                {}
            )
        )

        if not isinstance(
            self.settings,
            dict
        ):

            self.settings = {}

        if not self._flag(
            "enabled",
            True
        ):

            raise VideoProviderError(
                "The Pexels video provider is disabled "
                "in config/pexels.json."
            )

    def fetch(
        self,
        query,
        destination_dir,
        max_videos=3
    ):

        query = str(
            query or ""
        ).strip()

        if not query:

            return []

        destination_dir = Path(
            destination_dir
        )

        destination_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        if max_videos <= 0:

            return []

        self.notify(
            f"Searching Pexels for: {query}"
        )

        driver = self._create_driver()

        downloaded = []

        try:

            video_urls = (
                self._collect_video_urls(
                    driver,
                    query,
                    max_videos
                )
            )

            if not video_urls:

                self.notify(
                    "No usable Pexels results found "
                    f"for: {query}"
                )

                return []

            self.notify(
                f"Found {len(video_urls)} candidate "
                "clip(s). Downloading..."
            )

            for index, video_url in enumerate(
                video_urls,
                start=1
            ):

                if len(
                    downloaded
                ) >= max_videos:

                    break

                output_path = (
                    destination_dir
                    /
                    f"clip_{index:03d}.mp4"
                )

                try:

                    self._download_video(
                        video_url,
                        output_path
                    )

                    downloaded.append(
                        output_path
                    )

                    self.notify(
                        f"Downloaded clip "
                        f"{len(downloaded)}/"
                        f"{max_videos}."
                    )

                except Exception as error:

                    self.notify(
                        f"Download failed for one "
                        f"candidate: {error}"
                    )

                if len(
                    downloaded
                ) < max_videos:

                    self._human_pause()

        finally:

            try:

                driver.quit()

            except Exception:

                pass

        return downloaded
    def _collect_video_urls(
        self,
        driver,
        query,
        limit
    ):

        base_url = (
            self._base_url()
        )

        search_url = (
            f"{base_url}/search/videos/"
            f"{quote(query)}/"
        )

        self.notify(
            f"Opening {search_url}"
        )

        driver.get(
            search_url
        )

        page_timeout = (
            self._seconds(
                "page_timeout_seconds",
                60
            )
        )

        deadline = (
            time.monotonic()
            + page_timeout
        )

        video_urls = []

        while time.monotonic() < deadline:

            video_urls = (
                self._extract_video_urls(
                    driver.page_source,
                    limit
                )
            )

            if video_urls:

                break

            time.sleep(
                1.0
            )

        return video_urls

    def _extract_video_urls(
        self,
        page_source,
        limit
    ):

        """
        Pexels search results render preview players whose source
        points at a direct videos.pexels.com MP4. Collect unique
        MP4 URLs from the page source and prefer vertical clips,
        which fit the 9:16 Shorts format best.

        When orientation is set to "vertical", only vertical clips
        are returned so landscape footage is never stretched to fit
        the 9:16 format.
        """

        urls = []

        seen = set()

        for match in MP4_URL_PATTERN.finditer(
            page_source
        ):

            url = match.group(0)

            url = url.replace(
                "&amp;",
                "&"
            )

            base = url.split(
                "?",
                1
            )[0]

            if base in seen:

                continue

            seen.add(
                base
            )

            urls.append(
                url
            )

        if not urls:

            return []

        vertical = []

        landscape = []

        for url in urls:

            if self._is_vertical(
                url
            ):

                vertical.append(
                    url
                )

            else:

                landscape.append(
                    url
                )

        orientation = str(
            self._setting(
                "orientation",
                "any"
            )
        ).strip().lower()

        if orientation == "vertical":

            return vertical[:limit]

        ordered = vertical + landscape

        return ordered[:limit]

    def _is_vertical(
        self,
        url
    ):

        match = DIMENSION_PATTERN.search(
            url
        )

        if match is None:

            return False

        width = int(
            match.group(1)
        )

        height = int(
            match.group(2)
        )

        return height >= width

    def _download_video(
        self,
        url,
        output_path
    ):

        """
        Downloads one clip. Tries upgraded HD variants of the
        same file before falling back to the preview URL, so
        Shorts get the best quality the CDN offers.
        """

        last_error = None

        for candidate in self._variants(
            url
        ):

            try:

                return self._download_url(
                    candidate,
                    output_path
                )

            except VideoProviderError as error:

                last_error = error

                continue

        raise VideoProviderError(
            str(
                last_error
                or "Download failed."
            )
        )

    def _variants(
        self,
        url
    ):

        match = DIMENSION_PATTERN.search(
            url
        )

        if match is None:

            return [url]

        width = int(
            match.group(1)
        )

        height = int(
            match.group(2)
        )

        fps = match.group(3)

        if height >= width:

            # Vertical source - try progressively larger
            # vertical renders.

            sizes = [
                (1080, 1920),
                (720, 1280),
                (width, height)
            ]

        else:

            # Landscape source - try larger landscape renders.

            sizes = [
                (1920, 1080),
                (1280, 720),
                (width, height)
            ]

        variants = []

        seen = set()

        for new_width, new_height in sizes:

            replacement = (
                f"_{new_width}_{new_height}_{fps}fps"
            )

            candidate = DIMENSION_PATTERN.sub(
                replacement,
                url,
                count=1
            )

            base = candidate.split(
                "?",
                1
            )[0]

            if base in seen:

                continue

            seen.add(
                base
            )

            variants.append(
                candidate
            )

        return variants

    def _download_url(
        self,
        url,
        output_path
    ):

        download_timeout = (
            self._seconds(
                "download_timeout_seconds",
                300
            )
        )

        response = requests.get(
            url,
            stream=True,
            timeout=download_timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                ),
                "Referer": (
                    self._base_url()
                )
            }
        )

        if response.status_code != 200:

            raise VideoProviderError(
                f"HTTP {response.status_code} while "
                "downloading a clip."
            )

        output_path = Path(
            output_path
        )

        with output_path.open(
            "wb"
        ) as file:

            for chunk in response.iter_content(
                chunk_size=1024 * 256
            ):

                if chunk:

                    file.write(
                        chunk
                    )

        if (
            not output_path.is_file()
            or output_path.stat().st_size < 1024
        ):

            output_path.unlink(
                missing_ok=True
            )

            raise VideoProviderError(
                "Downloaded clip was empty."
            )

        return output_path

    def _create_driver(
        self
    ):

        if self._attach_to_existing_chrome():

            try:

                return self._attach_driver()

            except Exception as error:

                self.notify(
                    "Could not attach to an existing Chrome "
                    f"({error}); launching a new one..."
                )

        return self._launch_driver()

    def _attach_driver(
        self
    ):

        """
        Attaches to an already-running Chrome through its remote
        debugging interface, so the real browser session and any
        Cloudflare clearance are reused. This is the recommended
        mode - a visible Chrome started outside Selenium passes
        Cloudflare checks reliably.
        """

        options = webdriver.ChromeOptions()

        options.debugger_address = (
            self._debugging_address()
        )

        driver = webdriver.Chrome(
            options=options
        )

        driver.set_page_load_timeout(
            self._seconds(
                "page_timeout_seconds",
                60
            )
        )

        return driver

    def _launch_driver(
        self
    ):

        options = webdriver.ChromeOptions()

        if self._headless():

            options.add_argument(
                "--headless=new"
            )

        options.add_argument(
            "--window-size=1280,2000"
        )

        options.add_argument(
            "--disable-gpu"
        )

        options.add_argument(
            "--no-sandbox"
        )

        options.add_argument(
            "--disable-blink-features=AutomationControlled"
        )

        options.add_argument(
            "--log-level=3"
        )

        profile_directory = (
            self._profile_directory()
        )

        options.add_argument(
            f"--user-data-dir={profile_directory}"
        )

        if self._attach_to_existing_chrome():

            options.debugger_address = (
                self._debugging_address()
            )

        driver = webdriver.Chrome(
            options=options
        )

        page_timeout = (
            self._seconds(
                "page_timeout_seconds",
                60
            )
        )

        driver.set_page_load_timeout(
            page_timeout
        )

        return driver

    def _human_pause(
        self
    ):

        """
        Waits a short random time between downloads so the
        automated flow does not fire requests back-to-back.
        """

        low = self._seconds(
            "pause_min_seconds",
            1
        )

        high = max(
            self._seconds(
                "pause_max_seconds",
                3
            ),
            low
        )

        if high <= low:

            high = low + 1

        time.sleep(
            random.uniform(
                low,
                high
            )
        )

    def _setting(
        self,
        name,
        default=""
    ):

        return self.settings.get(
            name,
            default
        )

    def _seconds(
        self,
        name,
        default
    ):

        try:

            value = float(
                self._setting(
                    name,
                    default
                )
            )

        except (
            TypeError,
            ValueError
        ):

            return float(
                default
            )

        if value <= 0:

            return float(
                default
            )

        return value

    def _flag(
        self,
        name,
        default=False
    ):

        value = self._setting(
            name,
            default
        )

        if isinstance(
            value,
            bool
        ):

            return value

        return str(
            value
        ).strip().lower() in (
            "1",
            "true",
            "yes",
            "on"
        )

    def _base_url(
        self
    ):

        base_url = str(
            self._setting(
                "base_url",
                self.DEFAULT_BASE_URL
            )
        ).strip()

        return (
            base_url
            or self.DEFAULT_BASE_URL
        )

    def _headless(
        self
    ):

        return self._flag(
            "headless",
            True
        )

    def _attach_to_existing_chrome(
        self
    ):

        return self._flag(
            "attach_to_existing_chrome",
            False
        )

    def _debugging_address(
        self
    ):

        address = str(
            self._setting(
                "debugging_address",
                ""
            )
        ).strip()

        if not address:

            return "127.0.0.1:9222"

        return address

    def _profile_directory(
        self
    ):

        profile_directory = Path(
            str(
                self._setting(
                    "profile_directory",
                    self.DEFAULT_PROFILE_DIRECTORY
                )
            )
        ).resolve()

        profile_directory.mkdir(
            parents=True,
            exist_ok=True
        )

        return profile_directory
