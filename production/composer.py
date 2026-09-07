from pathlib import Path

import numpy as np

from PIL import (
    Image,
    ImageDraw,
    ImageFont
)

from moviepy import (
    VideoFileClip,
    AudioFileClip,
    TextClip,
    CompositeVideoClip,
    CompositeAudioClip,
    AudioClip,
    vfx,
    afx
)


class CompositionError(
    RuntimeError
):

    pass


FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/seguisb.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf"
]


class Composer:

    """
    Composes the final vertical Short.

    The narration audio is the primary timeline. The downloaded
    stock footage is cut into segments that follow the narration,
    captions are burned in, and background music (plus optional
    SFX placed in the episode's sfx/ folder) is mixed underneath
    the narration.
    """

    def __init__(
        self,
        config
    ):

        self.config = config

        app_config = (
            config.get(
                "app",
                {}
            )
        )

        self.shorts_config = (
            app_config.get(
                "shorts",
                {}
            )
        )

        self.audio_config = (
            app_config.get(
                "audio",
                {}
            )
        )

        self.captions_config = (
            app_config.get(
                "captions",
                {}
            )
        )

        self.project_root = (
            Path(
                __file__
            )
            .resolve()
            .parents[1]
        )

    def compose(
        self,
        episode_directory,
        narration,
        cues,
        footage_paths,
        segment_count=None
    ):

        """
        Renders episode.mp4 into the episode directory.

        narration : result dict from production.narration
        cues      : caption cues from production.captions
        footage_paths : downloaded footage files
        segment_count : how many visual segments the timeline
                        should be split into (defaults to the
                        number of downloaded footage files)
        """

        if not footage_paths:

            raise CompositionError(
                "No stock footage was downloaded, so the "
                "video cannot be composed."
            )

        width = int(
            self.shorts_config.get(
                "resolution",
                {}
            ).get(
                "width",
                1080
            )
        )

        height = int(
            self.shorts_config.get(
                "resolution",
                {}
            ).get(
                "height",
                1920
            )
        )

        fps = int(
            self.shorts_config.get(
                "fps",
                30
            )
        )

        max_duration = float(
            self.shorts_config.get(
                "max_duration_seconds",
                60
            )
        )

        narration_duration = float(
            narration["duration"]
        )

        # A short tail after the last spoken word keeps the
        # ending from feeling cut off, while never exceeding
        # the Shorts duration limit.

        video_duration = min(
            narration_duration + 0.5,
            max_duration
        )

        output_path = (
            Path(
                episode_directory
            )
            /
            "episode.mp4"
        )

        self._opened_sources = []

        try:

            frame_clips = (
                self._build_frame_clips(
                    footage_paths,
                    video_duration,
                    width,
                    height,
                    segment_count
                )
            )

            caption_clips = (
                self._build_caption_clips(
                    cues,
                    width,
                    height
                )
            )

            audio = (
                self._build_audio(
                    narration,
                    video_duration,
                    Path(
                        episode_directory
                    )
                )
            )

            final = CompositeVideoClip(
                frame_clips + caption_clips,
                size=(
                    width,
                    height
                )
            ).with_duration(
                video_duration
            )

            if audio is not None:

                final = final.with_audio(
                    audio
                )

            try:

                final.write_videofile(
                    str(
                        output_path
                    ),
                    fps=fps,
                    codec="libx264",
                    audio_codec="aac",
                    audio_bitrate="192k",
                    preset="medium",
                    threads=4,
                    logger=None
                )

            except Exception as error:

                raise CompositionError(
                    f"Video rendering failed: {error}"
                )

            finally:

                self._close(
                    final
                )

                self._close(
                    audio
                )

        finally:

            for source in (
                self._opened_sources
            ):

                self._close(
                    source
                )

        return output_path

    def _close(
        self,
        clip
    ):

        try:

            clip.close()

        except Exception:

            pass
    def _build_frame_clips(
        self,
        footage_paths,
        video_duration,
        width,
        height,
        segment_count
    ):

        if segment_count is None or segment_count < 1:

            segment_count = len(
                footage_paths
            )

        segment_count = min(
            segment_count,
            len(
                footage_paths
            )
            * 4
        )

        segment_duration = (
            video_duration
            / segment_count
        )

        frame_clips = []

        for index in range(
            segment_count
        ):

            start = (
                index
                * segment_duration
            )

            needed = min(
                segment_duration,
                video_duration - start
            )

            if needed <= 0:

                break

            source_path = (
                footage_paths[
                    index % len(footage_paths)
                ]
            )

            clip = VideoFileClip(
                str(
                    source_path
                )
            )

            self._opened_sources.append(
                clip
            )

            try:

                if clip.duration > needed + 0.05:

                    clip = clip.subclipped(
                        0,
                        needed
                    )

                elif clip.duration < needed - 0.05:

                    clip = clip.with_effects(
                        [
                            vfx.Loop(
                                duration=needed
                            )
                        ]
                    )

                clip = (
                    self._fit_to_frame(
                        clip,
                        width,
                        height
                    )
                )

                clip = (
                    clip.without_audio()
                    .with_start(
                        start
                    )
                    .with_duration(
                        needed
                    )
                )

                frame_clips.append(
                    clip
                )

            except Exception:

                self._close(
                    clip
                )

                raise

        if not frame_clips:

            raise CompositionError(
                "No usable footage segments could be "
                "prepared."
            )

        return frame_clips

    def _fit_to_frame(
        self,
        clip,
        width,
        height
    ):

        """
        Center-crops the footage to the Shorts aspect ratio and
        scales it to the output resolution.
        """

        clip_width, clip_height = (
            clip.size
        )

        target_ratio = (
            width
            / height
        )

        source_ratio = (
            clip_width
            / clip_height
        )

        if source_ratio > target_ratio:

            crop_width = int(
                clip_height
                * target_ratio
            )

            clip = clip.cropped(
                width=crop_width
            )

        elif source_ratio < target_ratio:

            crop_height = int(
                clip_width
                / target_ratio
            )

            clip = clip.cropped(
                height=crop_height
            )

        if (
            clip.size[0] != width
            or clip.size[1] != height
        ):

            clip = clip.resized(
                (
                    width,
                    height
                )
            )

        return clip

    def _build_caption_clips(
        self,
        cues,
        width,
        height
    ):

        if not self.captions_config.get(
            "enabled",
            True
        ):

            return []

        if not cues:

            return []

        font_size = int(
            self.captions_config.get(
                "font_size",
                62
            )
        )

        text_color = str(
            self.captions_config.get(
                "text_color",
                "white"
            )
        )

        stroke_color = str(
            self.captions_config.get(
                "stroke_color",
                "black"
            )
        )

        stroke_width = int(
            self.captions_config.get(
                "stroke_width",
                3
            )
        )

        vertical_position = float(
            self.captions_config.get(
                "vertical_position",
                0.74
            )
        )

        font = (
            self._resolve_font()
        )

        origin_y = int(
            height
            * vertical_position
        )

        caption_clips = []

        for cue in cues:

            cue_start = float(
                cue["start"]
            )

            cue_end = float(
                cue["end"]
            )

            if cue_end <= cue_start:

                continue

            text = str(
                cue.get(
                    "text",
                    ""
                )
            ).strip()

            if not text:

                continue

            clip = TextClip(
                font=font,
                text=text,
                font_size=font_size,
                color=text_color,
                stroke_color=stroke_color,
                stroke_width=stroke_width,
                method="label",
                text_align="center"
            )

            caption_clips.append(
                clip
                .with_position(
                    (
                        "center",
                        origin_y
                    )
                )
                .with_start(
                    cue_start
                )
                .with_end(
                    cue_end
                )
            )

        return caption_clips

    def _make_caption_text_clip(
        self,
        text,
        font,
        font_size,
        stroke_width,
        text_color,
        stroke_color,
        caption_width,
        frame_width
    ):

        """
        Builds one caption TextClip with the text wrapped to the safe
        caption area using real font measurements.

        The final clip width is verified against the video frame, so
        caption text can never be cut off at the horizontal edges of
        the video. If the text renderer produces an image wider than
        the frame (measurement differences between the wrap step and
        the renderer), the font size is reduced and the text is
        re-wrapped until it fits.
        """

        # Small safety margin so the rendered stroke never touches
        # the edge of the text image.

        safe_text_width = max(
            100,
            caption_width
            - 2 * stroke_width
            - 16
        )

        current_font_size = font_size

        wrapped_text, current_font_size = (
            self._wrap_caption_text(
                text,
                font,
                current_font_size,
                stroke_width,
                safe_text_width
            )
        )

        for _attempt in range(6):

            clip = TextClip(
                font=font,
                text=wrapped_text + "\n",
                font_size=current_font_size,
                color=text_color,
                stroke_color=stroke_color,
                stroke_width=stroke_width,
                method="caption",
                size=(
                    caption_width,
                    None
                ),
                text_align="center"
            )

            if clip.size[0] <= frame_width:

                return clip

            clip.close()

            current_font_size = max(
                20,
                current_font_size - 4
            )

            safe_text_width = max(
                100,
                int(
                    caption_width
                    * current_font_size
                    / max(1, font_size)
                )
            )

            wrapped_text, current_font_size = (
                self._wrap_caption_text(
                    text,
                    font,
                    current_font_size,
                    stroke_width,
                    safe_text_width
                )
            )

        # Very defensive fallback - the smallest safe size.
        return TextClip(
            font=font,
            text=wrapped_text + "\n",
            font_size=current_font_size,
            color=text_color,
            stroke_color=stroke_color,
            stroke_width=stroke_width,
            method="caption",
            size=(
                caption_width,
                None
            ),
            text_align="center"
        )

    def _wrap_caption_text(
        self,
        text,
        font_path,
        font_size,
        stroke_width,
        max_width
    ):

        """
        Word-wraps caption text with real font measurements so
        every line fits inside the safe horizontal area. Words are
        never split: if a single word is wider than the safe area,
        the font size is reduced until it fits (full words are
        always visible, nothing is cut off horizontally).
        """

        words = str(
            text
        ).split()

        if not words:

            return text, font_size

        image = Image.new(
            "RGB",
            (1, 1)
        )

        draw = ImageDraw.Draw(
            image
        )

        def make_font(
            size
        ):

            if font_path:

                return ImageFont.truetype(
                    font_path,
                    size
                )

            return ImageFont.load_default(
                size
            )

        def text_width(
            value,
            font_pil
        ):

            left, top, right, bottom = (
                draw.textbbox(
                    (0, 0),
                    value,
                    font=font_pil,
                    stroke_width=stroke_width
                )
            )

            return right - left

        font_pil = make_font(
            font_size
        )

        effective_size = font_size

        longest_word = max(
            words,
            key=lambda word: text_width(
                word,
                font_pil
            )
        )

        while (
            effective_size > 20
            and text_width(
                longest_word,
                font_pil
            )
            > max_width
        ):

            effective_size -= 2

            font_pil = make_font(
                effective_size
            )

        lines = []

        current_line = ""

        for word in words:

            candidate = (
                word
                if not current_line
                else current_line + " " + word
            )

            if (
                text_width(
                    candidate,
                    font_pil
                )
                <= max_width
                or not current_line
            ):

                current_line = candidate

            else:

                lines.append(
                    current_line
                )

                current_line = word

        if current_line:

            lines.append(
                current_line
            )

        return (
            "\n".join(
                lines
            ),
            effective_size
        )

    def _resolve_font(
        self
    ):

        configured = str(
            self.captions_config.get(
                "font",
                ""
            )
        ).strip()

        candidates = []

        if configured:

            candidates.append(
                configured
            )

        candidates.extend(
            FONT_CANDIDATES
        )

        for candidate in candidates:

            if candidate and Path(
                candidate
            ).is_file():

                return candidate

        return None

    def _build_audio(
        self,
        narration,
        video_duration,
        episode_directory
    ):

        audio_tracks = []

        narration_clip = AudioFileClip(
            narration["audio_path"]
        ).with_volume_scaled(
            float(
                self.audio_config.get(
                    "narration",
                    {}
                ).get(
                    "volume",
                    1.0
                )
            )
        )

        audio_tracks.append(
            narration_clip
        )

        music_clip = (
            self._build_music(
                video_duration,
                episode_directory
            )
        )

        if music_clip is not None:

            audio_tracks.append(
                music_clip
            )

        sfx_clips = (
            self._build_sfx(
                episode_directory,
                video_duration
            )
        )

        audio_tracks.extend(
            sfx_clips
        )

        mixed = CompositeAudioClip(
            audio_tracks
        ).with_duration(
            video_duration
        )

        return mixed

    def _build_music(
        self,
        video_duration,
        episode_directory
    ):

        """
        Mixes the background music downloaded for this episode by
        the music provider into the episode's music/ directory.
        The track file is temporary - the production pipeline
        deletes it after the render.
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

            return None

        music_directory = (
            Path(
                episode_directory
            )
            /
            "music"
        )

        music_files = (
            self._list_audio_files(
                music_directory
            )
        )

        if not music_files:

            return None

        music_path = (
            music_files[0]
        )

        music_clip = AudioFileClip(
            str(
                music_path
            )
        )

        try:

            looped = music_clip.with_effects(
                [
                    afx.AudioLoop(
                        duration=video_duration
                    )
                ]
            ).with_volume_scaled(
                float(
                    music_config.get(
                        "volume",
                        0.12
                    )
                )
            )

            return looped

        except Exception:

            self._close(
                music_clip
            )

            raise

    def _build_sfx(
        self,
        episode_directory,
        video_duration
    ):

        """
        Optional sound effects. Any audio file placed in the
        episode sfx/ directory is mixed into the video. A numeric
        prefix in the file name sets the offset:

            sfx/1.5__whoosh.mp3  -> plays at 1.5 seconds

        Files without a prefix play at 0 seconds.
        """

        sfx_config = (
            self.audio_config.get(
                "sfx",
                {}
            )
        )

        if not sfx_config.get(
            "enabled",
            False
        ):

            return []

        sfx_directory = (
            Path(
                episode_directory
            )
            /
            "sfx"
        )

        sfx_files = (
            self._list_audio_files(
                sfx_directory
            )
        )

        volume = float(
            sfx_config.get(
                "volume",
                0.6
            )
        )

        clips = []

        for sfx_path in sfx_files:

            offset = (
                self._offset_from_name(
                    sfx_path
                )
            )

            if offset >= video_duration:

                continue

            clip = AudioFileClip(
                str(
                    sfx_path
                )
            )

            try:

                clips.append(
                    clip.with_start(
                        offset
                    ).with_volume_scaled(
                        volume
                    )
                )

            except Exception:

                self._close(
                    clip
                )

                raise

        return clips

    def _offset_from_name(
        self,
        path
    ):

        name = Path(
            path
        ).name

        prefix = name.split(
            "__",
            1
        )[0]

        try:

            return max(
                0.0,
                float(
                    prefix
                )
            )

        except ValueError:

            return 0.0

    def _list_audio_files(
        self,
        directory
    ):

        directory = Path(
            directory
        )

        if not directory.is_absolute():

            directory = (
                self.project_root
                /
                directory
            )

        if not directory.is_dir():

            return []

        extensions = (
            ".mp3",
            ".wav",
            ".m4a",
            ".ogg"
        )

        files = [
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower()
            in extensions
        ]

        return sorted(
            files
        )

    @staticmethod
    def _silence(
        duration,
        fps=44100
    ):

        return AudioClip(
            lambda t: np.zeros(
                (
                    np.size(t),
                    2
                ),
                dtype=np.float32
            ),
            duration=duration,
            fps=fps
        )
