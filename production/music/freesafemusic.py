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
Maps narration mood words to Free Safe Music genre slugs. Every slug
below exists on the site's /genres/ index, so genre pages never
404. The goal is a background bed that supports the narration,
never overpowers it. Every track on Free Safe Music is published
under the same site-wide license (freesafemusic.com/usage): free
for commercial and monetized use on YouTube, Instagram Reels,
TikTok and more, with no attribution and no payment required.
"""

MOOD_GENRE_MAP = {
    "curious": ["cinematic", "mysterious", "dramatic"],
    "fascinating": ["cinematic", "mysterious", "atmospheric"],
    "interesting": ["cinematic", "inspiring", "mysterious"],
    "uplifting": ["uplifting", "inspiring", "upbeat"],
    "inspiring": ["inspiring", "uplifting", "cinematic"],
    "wonder": ["cinematic", "atmospheric", "epic"],
    "amazing": ["cinematic", "epic", "uplifting"],
    "mysterious": ["mysterious", "dramatic", "cinematic"],
    "mystery": ["mysterious", "dramatic", "dark"],
    "dramatic": ["dramatic", "epic", "cinematic"],
    "tense": ["dramatic", "mysterious", "dark"],
    "dark": ["dark", "dramatic", "haunting"],
    "happy": ["happy", "uplifting", "fun"],
    "joyful": ["happy", "uplifting", "fun"],
    "sad": ["sad", "emotional", "piano"],
    "melancholic": ["sad", "emotional", "piano"],
    "calm": ["calm", "peaceful", "soft"],
    "peaceful": ["peaceful", "soft", "ambient"],
    "relaxing": ["relaxing", "soft", "chill"],
    "cozy": ["cozy", "warm", "gentle"],
    "warm": ["warm", "gentle", "acoustic"],
    "hopeful": ["inspiring", "emotional", "cinematic"],
    "playful": ["fun", "happy", "funky"],
    "quirky": ["funky", "fun", "pop"],
    "weird": ["funky", "fun", "mysterious"],
    "strange": ["mysterious", "funky", "cinematic"],
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
    "scary": ["dark", "haunting", "halloween"],
    "creepy": ["dark", "haunting", "mysterious"],
    "horror": ["haunting", "dark", "halloween"],
    "suspense": ["mysterious", "dramatic", "dark"],
    "funny": ["fun", "funky", "pop"],
    "comedy": ["fun", "funky", "happy"],
    "action": ["action", "epic", "dramatic"],
    "beautiful": ["peaceful", "cinematic", "emotional"],
    "mindblowing": ["cinematic", "epic", "mysterious"],
}


# Total-random variety lives here: each energy tier lists a wide
# pool of real site genres. Episodes draw genre pages at random
# from their tier, so any compatible style can play - calm videos
# roam across piano/ambient/lofi/jazz/etc, energetic ones across
# dance/rock/party/etc. No history is kept, so back-to-back
# repeats are allowed by design. The mid and low tiers overlap
# heavily on purpose: the channel's default curious/wonder tone
# should roam across both cinematic and soft beds.
ENERGY_GENRE_POOL = {
    "high": [
        "energetic",
        "upbeat",
        "dance",
        "party",
        "hype",
        "action",
        "exciting",
        "epic",
        "powerful",
        "rock",
        "house",
        "techno",
        "funk",
        "funky",
        "groove",
        "disco",
        "pop",
        "tribal",
        "hip-hop",
        "electronic",
    ],
    "mid": [
        "cinematic",
        "inspiring",
        "uplifting",
        "mysterious",
        "dramatic",
        "emotional",
        "atmospheric",
        "adventure",
        "movie",
        "retro",
        "cool",
        "modern",
        "corporate",
        "happy",
        "fun",
        "romantic",
        "nostalgic",
        "vintage",
        "vlog",
        "aesthetic",
        "night",
        "ambient",
        "piano",
        "calm",
        "peaceful",
        "chill",
        "lofi",
    ],
    "low": [
        "calm",
        "peaceful",
        "relaxing",
        "ambient",
        "piano",
        "chill",
        "soft",
        "gentle",
        "mellow",
        "lofi",
        "soothing",
        "meditation",
        "warm",
        "cozy",
        "dreamy",
        "acoustic",
        "classical",
        "smooth",
        "jazz",
        "lounge",
        "sad",
        "slow",
        "downtempo",
        "chillout",
        "healing",
        "nature",
        "spiritual",
        "soulful",
        "ballad",
        "instrumental",
        "orchestral",
        "guitar",
        "drone",
    ],
    "dark": [
        "dark",
        "haunting",
        "halloween",
        "night",
        "mysterious",
        "dramatic",
        "cinematic",
        "epic",
        "atmospheric",
    ],
}

# Mood words grouped by energy tier. The episode mood decides the
# tier first; the tier then decides which wide genre pool the
# random draw comes from.
HIGH_ENERGY_MOODS = frozenset(
    """
    energetic exciting action hype party fun funny comedy playful
    adventurous adventure powerful epic triumphant victorious
    upbeat joyful happy ecstatic wild crazy insane fast furious
    intense adrenaline workout sport heroic battle fight race chase
    """.split()
)

LOW_ENERGY_MOODS = frozenset(
    """
    calm peaceful relaxing relaxed soothing gentle soft quiet
    serene tranquil cozy warm tender dreamy nostalgic sad lonely
    melancholic slow mellow chill meditative healing
    romantic beautiful sleep sleepy night curious fascinating
    interesting mysterious wonder
    """.split()
)

DARK_ENERGY_MOODS = frozenset(
    """
    dark scary creepy horror haunted haunting eerie sinister
    dread fear terror nightmare ghost halloween
    """.split()
)

# Small obvious topic/narration keyword lists. They only nudge the
# energy estimate when the mood alone is neutral - the mood tags
# stay the primary signal.
HIGH_ENERGY_KEYWORDS = frozenset(
    """
    battle fight race chase explosion war army soldier survive
    survival attack chase fast speed run sprint danger extreme
    volcano earthquake storm shark predator hunt killer workout
    party dance celebration victory hero sport adrenaline wild
    crazy insane powerful strong fast furious
    """.split()
)

LOW_ENERGY_KEYWORDS = frozenset(
    """
    sleep dream calm peace peaceful relax relaxing quiet soft
    gentle slow cozy meditation ocean wave river lake forest
    night moon baby love tender healing heal soothing lullaby
    whisper warm kind mild
    """.split()
)

DARK_ENERGY_KEYWORDS = frozenset(
    """
    murder crime killer ghost horror scary creepy haunted death
    die dead grave dark nightmare monster demon curse poison
    fear terror mystery secret conspiracy detective shadow abyss
    deep
    """.split()
)


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
        destination_dir,
        content=None
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

        energy = self._detect_energy(mood_tags, content)

        genre_slugs = (
            self._genres_for_mood(
                mood_tags,
                energy,
                content
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
                mood_tags,
                energy,
                content
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
        mood_tags,
        energy=None,
        content=None
    ):

        """
        Builds the genre pages to fetch for this episode.

        No history is tracked on purpose: the same genre may come
        up back to back. Variety comes from shuffling a wide pool
        (mood genres + energy-tier genres + topic genres +
        configured fallback genres) and drawing max_genre_pages at
        random, so different episodes hit different pages while the
        energy tier keeps every draw appropriate for the video.
        """

        pool = []

        energy_pool = list(
            ENERGY_GENRE_POOL.get(
                energy or "mid",
                []
            )
        )

        topic_genres = list(
            self._topic_genres(
                content
            )
        )

        allowed = set(
            energy_pool
        )

        for mood in mood_tags:

            for genre in MOOD_GENRE_MAP.get(
                mood,
                []
            ):

                allowed.add(
                    genre
                )

        for genre in topic_genres:

            allowed.add(
                genre
            )

        for genre in self.genres:

            allowed.add(
                genre
            )

        mood_first = []

        for mood in mood_tags:

            for genre in MOOD_GENRE_MAP.get(
                mood,
                []
            ):

                if (
                    genre in energy_pool
                    and genre not in mood_first
                ):

                    mood_first.append(
                        genre
                    )

        leftover_mood = []

        for mood in mood_tags:

            for genre in MOOD_GENRE_MAP.get(
                mood,
                []
            ):

                if (
                    genre not in energy_pool
                    and genre not in leftover_mood
                ):

                    leftover_mood.append(
                        genre
                    )

        random.shuffle(
            leftover_mood
        )

        random.shuffle(
            mood_first
        )

        pool = list(
            mood_first
        )

        random.shuffle(
            energy_pool
        )

        for genre in energy_pool:

            if genre not in pool:

                pool.append(
                    genre
                )

        random.shuffle(
            topic_genres
        )

        for genre in topic_genres:

            if (
                genre not in pool
                and genre in allowed
            ):

                pool.append(
                    genre
                )

        for genre in self.genres:

            if genre not in pool:

                pool.append(
                    genre
                )

        for genre in leftover_mood:

            if genre not in pool:

                pool.append(
                    genre
                )

        random.shuffle(
            pool
        )

        return pool[
            :self.max_genre_pages
        ]

    def _detect_energy(
        self,
        mood_tags,
        content=None
    ):

        votes = {
            "high": 0,
            "mid": 0,
            "low": 0,
            "dark": 0,
        }

        for mood in (mood_tags or []):

            word = str(mood).strip().lower()

            if not word:

                continue

            if word in DARK_ENERGY_MOODS:

                votes["dark"] += 2

            elif word in HIGH_ENERGY_MOODS:

                votes["high"] += 2

            elif word in LOW_ENERGY_MOODS:

                votes["low"] += 2

            else:

                votes["mid"] += 1

        words = set(
            self._content_words(
                content
            )
        )

        if words:

            votes["high"] += len(
                words.intersection(
                    HIGH_ENERGY_KEYWORDS
                )
            )

            votes["low"] += len(
                words.intersection(
                    LOW_ENERGY_KEYWORDS
                )
            )

            votes["dark"] += (
                len(
                    words.intersection(
                        DARK_ENERGY_KEYWORDS
                    )
                ) * 2
            )

        ranked = sorted(
            votes.items(),
            key=lambda item: item[1],
            reverse=True
        )

        if ranked[0][1] <= 0:

            return "mid"

        return ranked[0][0]

    @staticmethod
    def _content_words(
        content
    ):

        if not isinstance(
            content,
            dict
        ):

            return []

        parts = []

        for key in (
            "title",
            "summary",
            "narration"
        ):

            value = content.get(
                key,
                ""
            )

            if value:

                parts.append(
                    str(
                        value
                    )
                )

        visuals = content.get(
            "visuals",
            []
        )

        if isinstance(
            visuals,
            list
        ):

            for visual in visuals:

                if not isinstance(
                    visual,
                    dict
                ):

                    continue

                query = visual.get(
                    "search_query",
                    ""
                )

                if query:

                    parts.append(
                        str(
                            query
                        )
                    )

        if not parts:

            return []

        return re.findall(
            r"[a-z]+",
            " ".join(
                parts
            ).lower()
        )

    def _topic_genres(
        self,
        content
    ):

        words = set(
            self._content_words(
                content
            )
        )

        if not words:

            return []

        topic_map = {
            "space": ["atmospheric", "ambient"],
            "ocean": ["ambient", "peaceful"],
            "forest": ["ambient", "acoustic"],
            "jungle": ["tribal", "nature"],
            "city": ["electronic", "modern"],
            "night": ["night", "lofi"],
            "sleep": ["meditation", "ambient"],
            "dream": ["dreamy", "ambient"],
            "love": ["romantic", "piano"],
            "party": ["party", "dance"],
            "sport": ["energetic", "hype"],
            "battle": ["epic", "action"],
            "mystery": ["mysterious", "dark"],
            "ghost": ["haunting", "dark"],
            "history": ["classical", "cinematic"],
            "jazz": ["jazz", "smooth"],
            "retro": ["retro", "vintage"],
            "summer": ["tropical", "summer"],
            "travel": ["vlog", "uplifting"],
            "tech": ["electronic", "modern"],
        }

        genres = []

        for word in words:

            for genre in topic_map.get(
                word,
                []
            ):

                if genre not in genres:

                    genres.append(
                        genre
                    )

        return genres

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
        mood_tags,
        energy=None,
        content=None
    ):

        """
        Picks the episode's track by total random draw.

        Every track fetched from the energy-compatible genre pages
        is already an appropriate match, so the final choice is a
        uniform random pick across all of them - no best-match
        narrowing, no history. Back-to-back repeats are allowed;
        variety comes from the wide genre pool, not from avoiding
        recent picks.
        """

        usable = [
            track
            for track in (tracks or [])
            if track.get("mp3_url")
            and track.get("title")
        ]

        if not usable:

            return None

        return random.choice(
            usable
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

