import json
import asyncio

from pathlib import Path

import edge_tts

from moviepy import (
    AudioFileClip
)


class NarrationError(
    RuntimeError
):

    pass


async def _synthesize(
    text,
    voice,
    rate,
    pitch,
    output_path,
    words_path=None
):

    """
    Runs edge-tts once. Writes the narration audio to
    output_path and, when words_path is given, a JSON file of
    word-level timings (seconds) captured from the stream.
    """

    communicate = edge_tts.Communicate(
        text,
        voice,
        rate=rate,
        pitch=pitch,
        boundary="WordBoundary"
    )

    words = []

    with open(
        output_path,
        "wb"
    ) as audio_file:

        async for chunk in communicate.stream():

            if chunk["type"] == "audio":

                audio_file.write(
                    chunk["data"]
                )

            elif (
                chunk["type"] == "WordBoundary"
                and words_path is not None
            ):

                # Offsets and durations come in 100-nanosecond
                # units.

                start = (
                    chunk["offset"]
                    / 10_000_000
                )

                duration = (
                    chunk["duration"]
                    / 10_000_000
                )

                words.append(
                    {
                        "word": str(
                            chunk["text"]
                        ),
                        "start": round(
                            start,
                            3
                        ),
                        "end": round(
                            start + duration,
                            3
                        )
                    }
                )

    if words_path is not None:

        with open(
            words_path,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                words,
                file,
                indent=2,
                ensure_ascii=False
            )

    return words


def _rate_value(
    rate
):

    """
    Normalizes an edge-tts rate string like "+10%" / "-5%" / "+0%"
    to the signed integer percent.
    """

    text = str(
        rate or "+0%"
    ).strip().replace(
        "%",
        ""
    )

    try:

        return int(
            text
        )

    except ValueError:

        return 0


def generate_narration(
    text,
    audio_config,
    output_directory,
    min_duration=None,
    max_duration=None,
    target_duration=None,
    notify=None
):

    """
    Generates the narration audio for an episode.

    Returns a dict with the narration mp3 path, its measured
    duration, and the word timings used for captions and footage
    segmentation:

        {
            "audio_path": ...,
            "words_path": ...,
            "duration": 57.3,
            "voice": "..."
        }

    If the first synthesis lands outside the allowed duration
    window, the narration is re-synthesized once with an adjusted
    speaking rate.
    """

    def report(
        message
    ):

        if notify is not None:

            notify(
                str(
                    message
                )
            )

    text = str(
        text or ""
    ).strip()

    if not text:

        raise NarrationError(
            "Narration text is empty."
        )

    audio_config = (
        audio_config or {}
    )

    narration_config = (
        audio_config.get(
            "narration",
            {}
        )
    )

    voice = str(
        narration_config.get(
            "voice",
            "en-US-AndrewMultilingualNeural"
        )
    )

    base_rate = _rate_value(
        narration_config.get(
            "rate",
            "+0%"
        )
    )

    pitch = str(
        narration_config.get(
            "pitch",
            "+0Hz"
        )
    )

    output_directory = Path(
        output_directory
    )

    output_directory.mkdir(
        parents=True,
        exist_ok=True
    )

    audio_path = (
        output_directory
        /
        "narration.mp3"
    )

    words_path = (
        output_directory
        /
        "words.json"
    )

    report(
        f"Generating narration audio ({voice})..."
    )

    asyncio.run(
        _synthesize(
            text,
            voice,
            f"{base_rate:+d}%",
            pitch,
            audio_path,
            words_path
        )
    )

    duration = _audio_duration(
        audio_path
    )

    # Natural pacing first. We NEVER slow the narration down to hit
    # a duration target - stretching the voice is what produced the
    # dragged-out, unnatural delivery and the "cut off at one minute"
    # videos (the stretched audio exceeded the Shorts cap and the
    # renderer trimmed the ending). If the content is naturally too
    # short, the video validator catches it instead.
    #
    # The only re-synthesis we do is a modest speed-up when the
    # narration is clearly too long for the Shorts window.

    if (
        max_duration
        and target_duration
        and duration > max_duration + 2
    ):

        factor = target_duration / duration

        delta = int(
            round(
                (1.0 - factor) * 100
            )
        )

        # Speed the too-long audio up, but never by more than a
        # comfortable amount relative to the configured base rate.
        final_rate = min(
            base_rate + 8,
            base_rate + delta
        )

        if final_rate != base_rate:

            report(
                "Narration duration "
                f"{duration:.1f}s exceeds the Shorts window; "
                f"retrying with rate {final_rate:+d}% "
                "(natural pacing preserved)."
            )

            asyncio.run(
                _synthesize(
                    text,
                    voice,
                    f"{final_rate:+d}%",
                    pitch,
                    audio_path,
                    words_path
                )
            )

            duration = _audio_duration(
                audio_path
            )

    elif (
        min_duration
        and duration < min_duration
    ):

        # Too short: report it so the caller knows (and so the
        # video validator's minimum duration can flag the episode)
        # instead of stretching the voice.
        report(
            f"Narration duration {duration:.1f}s is below the "
            f"{min_duration:.1f}s target; the narration script "
            "itself needs more content."
        )

    report(
        f"Narration audio ready ({duration:.1f}s)."
    )

    return {
        "audio_path": str(
            audio_path
        ),
        "words_path": str(
            words_path
        ),
        "duration": duration,
        "voice": voice
    }


def _audio_duration(
    audio_path
):

    audio_clip = None

    try:

        audio_clip = AudioFileClip(
            str(
                audio_path
            )
        )

        return float(
            audio_clip.duration
        )

    except Exception as error:

        raise NarrationError(
            f"Generated narration audio could not be "
            f"read: {error}"
        )

    finally:

        if audio_clip is not None:

            audio_clip.close()


def load_word_timings(
    words_path
):

    words_path = Path(
        words_path
    )

    if not words_path.is_file():

        return []

    with open(
        words_path,
        "r",
        encoding="utf-8"
    ) as file:

        words = json.load(
            file
        )

    if not isinstance(
        words,
        list
    ):

        return []

    return [
        word
        for word in words
        if isinstance(
            word,
            dict
        )
        and "start" in word
        and "end" in word
    ]