import json
from pathlib import Path

from production.narration import (
    generate_narration,
    load_word_timings
)

from production.captions import (
    build_caption_cues,
    build_word_cues,
    write_srt
)

from production.footage import (
    create_video_provider,
    VideoProviderError
)

from production.music import (
    create_music_provider,
    MusicProviderError
)

from production.composer import (
    Composer,
    CompositionError
)


class ProductionError(
    RuntimeError
):

    pass


class ProductionPipeline:

    """
    The Generate Video stage. Turns AI-generated episode content
    into a finished, validated vertical Short:

        1. Generate narration audio (with word timings)
        2. Retrieve stock footage via the configured video provider
        3. Generate captions synchronized with the narration
        4. Compose footage, narration, music, and captions
        5. Render and validate the final video

    The narration is the primary timeline; footage supports it
    visually.
    """

    def __init__(
        self,
        config,
        progress_callback=None
    ):

        self.config = config

        self.app_config = (
            config.get(
                "app",
                {}
            )
        )

        self.shorts_config = (
            self.app_config.get(
                "shorts",
                {}
            )
        )

        self.audio_config = (
            self.app_config.get(
                "audio",
                {}
            )
        )

        self.progress_callback = (
            progress_callback
        )

        self._last_progress = 0

        self.composer = Composer(
            config
        )

    def set_progress_callback(
        self,
        callback
    ):

        self.progress_callback = (
            callback
        )

    def update_progress(
        self,
        percent,
        message
    ):

        percent = int(
            max(
                0,
                min(
                    100,
                    percent
                )
            )
        )

        # The final rendered state is authoritative, so the video
        # progress must never regress. Intermediate notifications
        # from sub-steps (providers, narration, rendering) can
        # otherwise pull the bar backwards.

        if (
            percent < 100
            and percent
            <= self._last_progress
        ):

            percent = (
                self._last_progress
            )

        else:

            self._last_progress = (
                percent
            )

        if self.progress_callback is None:

            print(
                f"[VIDEO {percent}%] {message}"
            )

            return

        try:

            self.progress_callback(
                percent,
                str(
                    message
                ),
                "video"
            )

        except Exception:

            pass
    def _progress_between(
        self,
        start_percent,
        end_percent,
        index,
        total
    ):

        """
        Interpolates a progress value between two stage percents
        for item `index` of `total`, so sub-step notifications map
        onto the overall stage range.
        """

        total = max(
            1,
            int(
                total
            )
        )

        index = max(
            0,
            min(
                index,
                total
            )
        )

        fraction = (
            index
            /
            total
        )

        return int(
            round(
                start_percent
                + (
                    end_percent
                    - start_percent
                )
                * fraction
            )
        )

    def run(
        self,
        episode_directory,
        content
    ):

        episode_directory = Path(
            episode_directory
        )

        episode_directory.mkdir(
            parents=True,
            exist_ok=True
        )

        visuals = (
            content.get(
                "visuals",
                []
            )
        )

        narration_text = str(
            content.get(
                "narration",
                ""
            )
        ).strip()

        if not narration_text:

            raise ProductionError(
                "Episode content has no narration."
            )

        # 1. Narration audio

        self.update_progress(
            5,
            "Generating narration audio..."
        )

        narration = (
            generate_narration(
                narration_text,
                self.audio_config,
                episode_directory
                /
                "audio",
                min_duration=(
                    self.shorts_config.get(
                        "min_duration_seconds",
                        30
                    )
                ),
                max_duration=(
                    self.shorts_config.get(
                        "max_duration_seconds",
                        60
                    )
                ),
                target_duration=(
                    self.shorts_config.get(
                        "target_duration_seconds",
                        58
                    )
                ),
                notify=(
                    lambda message:
                    self.update_progress(
                        self._progress_between(
                            5,
                            10,
                            1,
                            2
                        ),
                        message
                    )
                )
            )
        )

        # 2. Stock footage through the configured provider

        self.update_progress(
            15,
            "Searching for stock footage..."
        )

        footage_groups = (
            self._collect_footage(
                episode_directory,
                visuals,
            )
        )

        # 3. Background music via the configured music provider

        music_metadata = (
            self._collect_music(
                episode_directory,
                content
            )
        )

        # 4. Captions synchronized with the narration

        self.update_progress(
            68,
            "Generating captions..."
        )

        words = (
            load_word_timings(
                narration["words_path"]
            )
        )

        cues = (
            build_caption_cues(
                words,
                max_words_per_line=int(
                    self.app_config.get(
                        "captions",
                        {}
                    )
                    .get(
                        "max_words_per_line",
                        4
                    )
                )
            )
        )

        word_cues = (
            build_word_cues(
                words
            )
        )

        captions_path = (
            write_srt(
                cues,
                episode_directory
                /
                "captions"
                /
                "captions.srt"
            )
        )

        # 5. Compose and render

        self.update_progress(
            72,
            "Composing the final video..."
        )

        # Compute per-sentence durations from the word timings so the
        # composer can align visual segments with what the narration is
        # actually saying. Each sentence gets a segment whose length
        # matches its spoken duration, and clips from that sentence's
        # visual group are combined (long sentence) or cut (short
        # sentence) to fit — keeping visuals and narration in sync.
        sentence_durations = self._compute_sentence_durations(
            words, content
        )

        try:

            output_path = (
                self.composer.compose(
                    episode_directory,
                    narration,
                    word_cues,
                    footage_groups,
                    visuals=visuals,
                    segment_count=len(
                        visuals
                    )
                    if visuals
                    else None,
                    sentence_durations=sentence_durations,
                )
            )

            # 6. Render complete
            self.update_progress(
                96,
                "Rendering complete."
            )

        finally:

            # The music track was only rented for this render -
            # delete the temporary file and keep the metadata.

            self._cleanup_music(
                music_metadata
            )

        if output_path.exists():
            self.update_progress(
                100,
                "Render complete."
            )
            return {
                "video_path": str(output_path),
                "captions_path": str(captions_path),
            }

    def _collect_footage(
        self,
        episode_directory,
        visuals,
    ):

        footage_directory = (
            episode_directory
            / "footage"
        )

        provider = (
            create_video_provider(
                self.config,
                notify=(
                    lambda message:
                    self.update_progress(
                        query_percent["value"],
                        message,
                    )
                ),
            )
        )

        query_percent = {"value": 15}

        candidates_per_query = int(
            self._provider_setting(
                "candidates_per_query",
                3,
            )
        )

        # One sublist per visual query, in the same order as `visuals`.
        # Each sublist holds the downloaded clip paths for that query.
        footage_groups = []
        failed_queries = []
        total_queries = len(visuals)
        # Track Pexels video IDs already downloaded this episode so
        # different search queries do not produce duplicate clips.
        downloaded_ids = set()

        for index, visual in enumerate(visuals, start=1):
            query = str(visual.get("search_query", "")).strip()

            if not query:
                footage_groups.append([])
                continue

            self.update_progress(
                self._progress_between(
                    20, 58, index - 1, total_queries
                ),
                f"Footage {index}/{total_queries}: {query}",
            )

            query_percent["value"] = self._progress_between(
                20, 58, index - 1, total_queries
            )

            query_directory = (
                footage_directory
                / provider.slugify(query)
            )

            try:
                downloaded = provider.fetch(
                    query,
                    query_directory,
                    max_videos=candidates_per_query,
                    downloaded_ids=downloaded_ids,
                )
            except Exception as error:
                self.update_progress(
                    40,
                    f"Footage search failed for "
                    f"'{query}': {error}",
                )
                failed_queries.append(query)
                footage_groups.append([])
                continue

            footage_groups.append(list(downloaded))

        total_available = sum(len(g) for g in footage_groups)

        if total_available == 0:
            raise VideoProviderError(
                "No stock footage could be downloaded "
                "for any visual search query."
            )

        if failed_queries:
            self.update_progress(
                45,
                f"Some footage searches failed "
                f"({len(failed_queries)}/{total_queries}); "
                f"continuing with the clips that were downloaded.",
            )

        return footage_groups

    def _collect_music(
        self,
        episode_directory,
        content
    ):

        """
        Retrieves background music through the configured music
        provider. Music is optional: any failure to find or
        download a track is reported and the episode continues
        without music. On success the track file is downloaded
        into the episode's music/ directory and its metadata is
        saved next to it (the file itself is deleted after the
        render - the metadata stays for reference).
        """

        music_config = (
            self.audio_config.get(
                "music",
                {}
            )
        )

        if not music_config.get(
            "enabled",
            True
        ):

            self.notify(
                "Background music is disabled."
            )

            return None

        self.update_progress(
            62,
            "Selecting background music..."
        )

        mood_tags = (
            content.get(
                "mood"
            )
            if isinstance(
                content,
                dict
            )
            else []
        )

        if not isinstance(
            mood_tags,
            list
        ):

            mood_tags = []

        try:

            provider = (
                create_music_provider(
                    self.config,
                    notify=(
                        lambda message:
                        self.update_progress(
                            64,
                            message
                        )
                    )
                )
            )

            metadata = provider.fetch(
                mood_tags,
                episode_directory
                /
                "music"
            )

        except Exception as error:

            # Music is optional - a provider failure must
            # never stop the episode from being produced.

            self.update_progress(
                66,
                f"Background music unavailable: {error}"
            )

            return None

        if metadata is None:

            self.update_progress(
                66,
                "No background music found; "
                "continuing without music."
            )

            return None

        metadata_path = (
            episode_directory
            /
            "music"
            /
            "metadata.json"
        )

        try:

            with open(
                metadata_path,
                "w",
                encoding="utf-8"
            ) as file:

                json.dump(
                    metadata,
                    file,
                    indent=2,
                    ensure_ascii=False
                )

        except Exception:

            pass

        self.update_progress(
            66,
            "Background music ready: "
            f"{metadata.get('title', 'unknown')}"
        )

        return metadata

    def _cleanup_music(
        self,
        music_metadata
    ):

        """
        Deletes the temporarily downloaded music file after the
        render. The metadata.json next to it is kept so every
        episode records which track was used and under which
        license.
        """

        if not isinstance(
            music_metadata,
            dict
        ):

            return

        track_path = music_metadata.get(
            "file"
        )

        if not track_path:

            return

        try:

            Path(
                track_path
            ).unlink(
                missing_ok=True
            )

        except Exception:

            pass

    def _compute_sentence_durations(self, words, content):
        """
        Computes the spoken duration of each narration sentence by
        partitioning word timings based on each sentence's word count.
        Returns a list of durations (seconds) in sentence order.
        Falls back to even distribution when mapping cannot be done.
        """
        if not words:
            return []

        narration_sentences = []
        if isinstance(content, dict):
            narration_sentences = content.get("narration_sentences", [])
        if not narration_sentences:
            return []

        total_duration = float(words[-1].get("end", 0.0)) if words else 0.0
        if total_duration <= 0:
            return []

        # Count words per sentence. edge-tts splits on whitespace, so we
        # use the same rule here to get a matching word count per sentence.
        sentence_word_counts = []
        for s in narration_sentences:
            sentence_word_counts.append(len(str(s).split()))

        # Partition the word timings by sentence. Words are in order, so
        # the first N words belong to sentence 0, the next M to sentence 1,
        # etc. Sentence duration = last word end - first word start.
        durations = []
        word_idx = 0
        for count in sentence_word_counts:
            if count <= 0 or word_idx >= len(words):
                durations.append(0.0)
                continue
            start_time = float(words[word_idx].get("start", 0.0))
            end_idx = min(word_idx + count, len(words)) - 1
            end_time = float(words[end_idx].get("end", 0.0))
            durations.append(max(0.0, end_time - start_time))
            word_idx += count

        # Assign any remaining words to the last sentence.
        if word_idx < len(words) and durations:
            extra_end = float(words[-1].get("end", 0.0))
            last_start = float(words[word_idx].get("start", 0.0)) if word_idx < len(words) else 0.0
            durations[-1] = max(durations[-1], extra_end - last_start)

        # If all zero, fall back to even distribution.
        if not durations or sum(durations) <= 0:
            per_sentence = total_duration / len(narration_sentences)
            return [per_sentence] * len(narration_sentences)

        return durations

    def _provider_setting(
        self,
        name,
        default
    ):

        value = (
            self.config.get(
                "pexels",
                {}
            )
            .get(
                name,
                default
            )
        )

        try:

            return int(
                value
            )

        except (
            TypeError,
            ValueError
        ):

            return default
