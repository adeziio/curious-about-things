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

    The narration is synthesized once at its natural pace. There
    is no hard duration limit and the audio is never trimmed,
    stretched, or re-synthesized - target_duration is used purely
    to report how close the narration landed to the target.
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

    # Natural pacing only. The narration is never slowed down,
    # sped up, or trimmed to fit a duration window - the video
    # timeline always follows the narration, so nothing can ever
    # be cut off. target_duration is informational only.

    if target_duration:

        report(
            f"Narration duration {duration:.1f}s vs. target "
            f"{float(target_duration):.1f}s (no limit applied)."
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