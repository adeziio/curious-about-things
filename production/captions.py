from pathlib import Path


def build_caption_cues(
    words,
    max_words_per_line=4,
    max_gap=1.0,
    min_cue_duration=0.65
):

    """
    Groups word-level narration timings into caption cues.

    words: list of {"word", "start", "end"} dicts from the TTS
    stream.

    Returns a list of {"start", "end", "text"} cues, each covering
    a small group of words. A pause longer than max_gap seconds
    always starts a new cue. Cues shorter than min_cue_duration are
    merged into the following cue so captions never flash on screen
    for a split second (which viewers perceive as the caption being
    cut off).
    """

    cues = []

    current_words = []

    for word in words:

        text = str(
            word.get(
                "word",
                ""
            )
        ).strip()

        if not text:

            continue

        if current_words:

            previous = current_words[-1]

            gap = (
                float(
                    word["start"]
                )
                - float(
                    previous["end"]
                )
            )

            full = (
                len(current_words)
                >= max_words_per_line
            )

            ends_sentence = (
                text.endswith(
                    (".", "!", "?")
                )
            )

            if (
                gap > max_gap
                or full
                or (
                    ends_sentence
                    and len(current_words)
                    >= max(2, max_words_per_line // 2)
                )
            ):

                cues.append(
                    _make_cue(
                        current_words
                    )
                )

                current_words = []

        current_words.append(
            {
                "word": text,
                "start": float(
                    word["start"]
                ),
                "end": float(
                    word["end"]
                )
            }
        )

    if current_words:

        cues.append(
            _make_cue(
                current_words
            )
        )

    # Merge blink-short cues into the next cue so every caption is
    # visible long enough to read.
    merged = []

    for cue in cues:

        merged.append(
            cue
        )

        while (
            len(merged) >= 2
            and float(
                merged[-2]["end"]
            )
            - float(
                merged[-2]["start"]
            )
            < min_cue_duration
        ):

            previous = merged.pop(-2)

            current = merged[-1]

            merged[-1] = {
                "start": previous["start"],
                "end": current["end"],
                "text": (
                    previous["text"]
                    + " "
                    + current["text"]
                )
            }

    return merged


def _make_cue(
    words
):

    text = " ".join(
        word["word"]
        for word in words
    ).upper()

    return {
        "start": round(
            words[0]["start"],
            3
        ),
        "end": round(
            words[-1]["end"],
            3
        ),
        "text": text,
        "words": [
            {
                "word": str(
                    word.get("word", "")
                ).strip().upper(),
                "start": float(
                    word["start"]
                ),
                "end": float(
                    word["end"]
                )
            }
            for word in words
            if str(
                word.get("word", "")
            ).strip()
        ]
    }


def write_srt(
    cues,
    output_path
):

    """
    Writes cues as a standard .srt subtitle file. Returns the
    path written.
    """

    output_path = Path(
        output_path
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    blocks = []

    for index, cue in enumerate(
        cues,
        start=1
    ):

        blocks.append(
            f"{index}\n"
            f"{_srt_time(cue['start'])} --> "
            f"{_srt_time(cue['end'])}\n"
            f"{cue['text']}\n"
        )

    output_path.write_text(
        "\n".join(
            blocks
        ),
        encoding="utf-8"
    )

    return output_path


def _srt_time(
    seconds
):

    milliseconds = int(
        round(
            seconds * 1000
        )
    )

    hours, remainder = divmod(
        milliseconds,
        3_600_000
    )

    minutes, remainder = divmod(
        remainder,
        60_000
    )

    secs, millis = divmod(
        remainder,
        1000
    )

    return (
        f"{hours:02d}:{minutes:02d}:"
        f"{secs:02d},{millis:03d}"
    )