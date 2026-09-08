import html
import random
import re

from pathlib import Path

import requests

from production.music.base import (
    MusicProvider,
    MusicProviderError
)


"""
Maps narration mood words to Free Safe Music genre slugs. Only
subtle, soft, instrumental-leaning genres are listed here - the
goal is a background bed that supports the narration, never
overpowers it. Every track on Free Safe Music is published under
the same site-wide license (freesafemusic.com/usage): free for
commercial and monetized use on YouTube, Instagram Reels, TikTok
and more, with no attribution and no payment required.
"""

MOOD_GENRE_MAP = {
    "curious": ["cinematic", "mysterious", "dramatic"],
    "uplifting": ["inspiring", "uplifting", "upbeat"],
    "inspiring": ["inspiring", "uplifting", "cinematic"],
    "wonder": ["cinematic", "atmospheric", "epic"],
    "mysterious": ["dramatic", "cinematic", "dark"],
    "dramatic": ["dramatic", "epic", "cinematic"],
    "tense": ["dramatic", "cinematic", "suspense"],
    "dark": ["dark", "dramatic", "cinematic"],
    "happy": ["happy", "uplifting", "fun"],
    "sad": ["emotional", "piano", "melancholic"],
    "melancholic": ["emotional", "piano", "melancholic"],
    "calm": ["calm", "peaceful", "soft"],
    "peaceful": ["peaceful", "soft", "ambient"],
    "relaxing": ["relaxing", "soft", "chill"],
    "cozy": ["cozy", "warm", "gentle"],
    "warm": ["warm", "gentle", "acoustic"],
    "hopeful": ["inspiring", "emotional", "cinematic"],
    "playful": ["fun", "happy", "quirky"],
    "quirky": ["quirky", "fun", "funky"],
    "energetic": ["energetic", "upbeat", "groove"],
    "exciting": ["exciting", "energetic", "adventure"],
    "adventure": ["adventure", "epic", "cinematic"],
    "romantic": ["romantic", "emotional", "piano"],
    "nostalgic": ["nostalgic", "vintage", "mellow"],
    "dreamy": ["dreamy", "ambient", "atmospheric"],
    "epic": ["epic", "cinematic", "orchestral"],
    "smooth": ["smooth", "jazz", "lounge"],
    "gentle": ["gentle", "soft", "piano"],
    "soft": ["soft", "gentle", "ambient"],
    "chill": ["chill", "lofi", "mellow"],
    "scary": ["dark", "horror", "suspense"],
    "horror": ["horror", "dark", "suspense"],
    "suspense": ["suspense", "dramatic", "tense"],
    "funny": ["fun", "quirky", "comedy"],
    "comedy": ["comedy", "fun", "quirky"],
    "action": ["action", "epic", "dramatic"],
    "sad": ["emotional", "piano", "melancholic"],
}


TRACK_ID_PATTERN = re.compile(
    r'\{"id":\[0,"([^"]+)"\]'
)

TRACK_FIELD_PATTERN = {
    "title": re.compile(r'"title":\[0,"([^"]*)"'),
    "slug": re.compile(r'"slug":\[0,"([^"]*)"'),
    "artist": re.compile(r'"artist":\[0,"([^"]*)"'),
    "mp3_url": re.compile(r'"mp3Url":\[0,"([^"]*)"')
}

TRACK_TAGS_PATTERN = re.compile(
    r'"tags":\[1,\[(.*?)\]\]',
    re.DOTALL
)

TRACK_TAG_PATTERN = re.compile(
    r'\[0,"([^"]+)"\]'
)


class FreeSafeMusicProvider(MusicProvider):

    """
    Background-music provider backed by freesafemusic.com.

    Every track on the site is published under one site-wide
    license (https://freesafemusic.com/usage):

        - free to download (320 kbps MP3), no signup, no fees
        - cleared for commercial and monetized use, including
          YouTube, Instagram Reels, TikTok, Twitch and podcasts
        - no attribution required
        - nothing registered with Content ID or any rights
          management system

    Tracks are discovered by fetching the site's public genre
    pages, which embed structured track data (title, artist, tags
    and MP3 URL). The track file is downloaded temporarily into
    the episode's music/ directory; the caller cleans it up after
    the video is rendered. Tracks are never redistributed.
    """

    name = "freesafemusic"

    def __init__(
        self,
        config,
        notify=None
    ):

        super().__init__(
            config,
            notify=notify
        )

        provider_config = (
            config.get(
                "freesafemusic",
                {}
            )
        )

        self.enabled = bool(
            provider_config.get(
                "enabled",
                True
            )
        )

        self.base_url = str(
            provider_config.get(
                "base_url",
                "https://freesafemusic.com"
            )
        ).rstrip("/")

        self.license_url = str(
            provider_config.get(
                "license_url",
                "https://freesafemusic.com/usage"
            )
        )

        self.genres = list(
            provider_config.get(
                "genres",
                []
            )
        )

        self.preferred_tags = [
            str(tag).strip().lower()
            for tag in provider_config.get(
                "preferred_tags",
                []
            )
            if str(tag).strip()
        ]

        self.max_genre_pages = max(
            1,
            int(
                provider_config.get(
                    "max_genre_pages",
                    2
                )
            )
        )

        self.timeout = max(
            5,
            int(
                provider_config.get(
                    "request_timeout_seconds",
                    30
                )
            )
        )

        self.download_timeout = max(
            10,
            int(
                provider_config.get(
                    "download_timeout_seconds",
                    180
                )
            )
        )

        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent":
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "CuriousAboutThings/1.0"
            }
        )

    def fetch(
        self,
        mood_tags,
        destination_dir
    ):

        if not self.enabled:

            self.notify(
                "Background music is disabled in the "
                "provider configuration."
            )

            return None

        mood_tags = [
            str(tag).strip().lower()
            for tag in (mood_tags or [])
            if str(tag).strip()
        ]

        genre_slugs = (
            self._genres_for_mood(
                mood_tags
            )
        )

        if not genre_slugs:

            self.notify(
                "No music genres configured; "
                "skipping background music."
            )

            return None

        self.notify(
            "Searching background music: "
            + ", ".join(
                genre_slugs
            )
            + "..."
        )

        candidates = []

        for slug in genre_slugs:

            try:

                tracks = (
                    self._fetch_genre_tracks(
                        slug
                    )
                )

            except Exception as error:

                # A single genre page failing must not
                # prevent the episode from getting music
                # from the remaining genres.

                self.notify(
                    f"Could not read music genre "
                    f"'{slug}': {error}"
                )

                continue

            candidates.extend(
                tracks
            )

        if not candidates:

            self.notify(
                "No background music tracks found; "
                "continuing without music."
            )

            return None

        track = (
            self._select_track(
                candidates,
                mood_tags
            )
        )

        if track is None:

            self.notify(
                "No suitable background music track "
                "found; continuing without music."
            )

            return None

        destination_dir = Path(
            destination_dir
        )

        destination_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        track_path = (
            destination_dir
            /
            "background-music.mp3"
        )

        self.notify(
            "Downloading music: "
            f"{track['title']}"
            + (
                f" by {track['artist']}"
                if track["artist"]
                else ""
            )
            + "..."
        )

        try:

            self._download(
                track["mp3_url"],
                track_path
            )

        except Exception as error:

            raise MusicProviderError(
                f"Music download failed for "
                f"'{track['title']}': {error}"
            )

        track_page_url = (
            f"{self.base_url}/tracks/"
            f"{track['slug']}/"
        )

        return {
            "provider": self.name,
            "title": track["title"],
            "artist": track["artist"],
            "source_url": track_page_url,
            "license": (
                "Free Safe Music License "
                f"({self.license_url}) - free for "
                "commercial and monetized use "
                "(YouTube, Instagram, TikTok), "
                "no attribution required, "
                "no Content ID registration."
            ),
            "tags": track["tags"],
            "mp3_url": track["mp3_url"],
            "file": str(track_path)
        }

    def _genres_for_mood(
        self,
        mood_tags
    ):

        slugs = []

        for mood in mood_tags:

            for genre in (
                MOOD_GENRE_MAP.get(
                    mood,
                    []
                )
            ):

                if genre not in slugs:

                    slugs.append(
                        genre
                    )

        for genre in self.genres:

            if genre not in slugs:

                slugs.append(
                    genre
                )

        return slugs[
            :self.max_genre_pages
        ]

    def _fetch_genre_tracks(
        self,
        genre_slug
    ):

        url = (
            f"{self.base_url}"
            f"/genres/{genre_slug}/"
        )

        response = self.session.get(
            url,
            timeout=self.timeout
        )

        response.raise_for_status()

        return self._parse_tracks(
            response.text
        )

    @staticmethod
    def _parse_tracks(
        page_html
    ):

        """
        Parses the structured track data embedded in the genre
        pages. The data is a JSON-like blob with a verbose
        serialization prefix ([0, "value"]) on every field.
        """

        text = html.unescape(
            page_html
        )

        tracks = []

        matches = list(
            TRACK_ID_PATTERN.finditer(
                text
            )
        )

        for index, match in enumerate(
            matches
        ):

            start = match.start()

            end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else min(
                    start + 6000,
                    len(text)
                )
            )

            chunk = text[start:end]

            track = {
                "id": match.group(1)
            }

            for field, pattern in (
                TRACK_FIELD_PATTERN.items()
            ):

                field_match = (
                    pattern.search(
                        chunk
                    )
                )

                track[field] = (
                    field_match.group(1).strip()
                    if field_match
                    else ""
                )

            tags_match = (
                TRACK_TAGS_PATTERN.search(
                    chunk
                )
            )

            if tags_match:

                track["tags"] = [
                    tag.lower()
                    for tag in TRACK_TAG_PATTERN.findall(
                        tags_match.group(1)
                    )
                ]

            else:

                track["tags"] = []

            if (
                track.get("mp3_url")
                and track.get("title")
            ):

                tracks.append(
                    track
                )

        return tracks

    def _select_track(
        self,
        tracks,
        mood_tags
    ):

        """
        Scores tracks by how well their tags match the episode's
        mood plus the configured preference for subtle, soft,
        instrumental music, then picks randomly among the best
        matches so episodes vary.
        """

        wanted = []

        for tag in mood_tags:

            if tag not in wanted:

                wanted.append(
                    tag
                )

        for tag in self.preferred_tags:

            if tag not in wanted:

                wanted.append(
                    tag
                )

        scored = []

        for track in tracks:

            tags = set(
                track.get(
                    "tags",
                    []
                )
            )

            if not tags:

                continue

            score = len(
                tags.intersection(
                    wanted
                )
            )

            if wanted and score <= 0:

                continue

            scored.append(
                (score, track)
            )

        if not scored:

            # No tag overlap (for example, an unusual mood word
            # with no preferred tags configured). The genre pages
            # themselves already pre-filter for subtle, soft
            # music, so any track from them is acceptable.

            scored = [
                (0, track)
                for track in tracks
                if track.get("tags")
            ]

        if not scored:

            return None

        best_score = max(
            score
            for score, _ in scored
        )

        best = [
            track
            for score, track in scored
            if score >= best_score
        ]

        return random.choice(
            best
        )



    def _download(
        self,
        url,
        destination_path
    ):

        with self.session.get(
            url,
            stream=True,
            timeout=self.download_timeout
        ) as response:

            response.raise_for_status()

            content_type = str(
                response.headers.get(
                    "Content-Type",
                    ""
                )
            ).lower()

            if (
                content_type
                and "audio" not in content_type
                and "mpeg" not in content_type
                and "octet-stream" not in content_type
            ):

                raise MusicProviderError(
                    f"Unexpected content type for the "
                    f"music file: {content_type}"
                )

            with open(
                destination_path,
                "wb"
            ) as file:

                for chunk in response.iter_content(
                    chunk_size=256 * 1024
                ):

                    if chunk:

                        file.write(
                            chunk
                        )

        size = Path(
            destination_path
        ).stat().st_size

        if size < 10_000:

            raise MusicProviderError(
                "The downloaded music file is "
                "incomplete."
            )

