import json

from pathlib import Path

from moviepy import (
    VideoFileClip
)


class VideoValidationError(
    RuntimeError
):

    pass


def validate_video(
    video_path,
    config
):

    """
    Validates the rendered Short against the configured
    requirements: duration window, resolution, and frame rate.

    Returns a result dict and writes it to validation.json next
    to the video. Raises VideoValidationError when the video does
    not meet the requirements - the actual rendered file is the
    authoritative source, so an invalid Short must be flagged,
    never silently accepted.
    """

    shorts_config = (
        config.get(
            "app",
            {}
        )
        .get(
            "shorts",
            {}
        )
    )

    min_duration = float(
        shorts_config.get(
            "min_duration_seconds",
            30
        )
    )

    max_duration = float(
        shorts_config.get(
            "max_duration_seconds",
            60
        )
    )

    resolution = (
        shorts_config.get(
            "resolution",
            {}
        )
    )

    expected_width = int(
        resolution.get(
            "width",
            1080
        )
    )

    expected_height = int(
        resolution.get(
            "height",
            1920
        )
    )

    expected_fps = float(
        shorts_config.get(
            "fps",
            30
        )
    )

    video_clip = None

    try:

        video_clip = VideoFileClip(
            str(
                video_path
            )
        )

        duration = float(
            video_clip.duration
        )

        width, height = (
            video_clip.size
        )

        fps = float(
            video_clip.fps
        )

    except Exception as error:

        raise VideoValidationError(
            f"The rendered video could not be read "
            f"for validation: {error}"
        )

    finally:

        if video_clip is not None:

            video_clip.close()

    errors = []

    if duration < min_duration:

        errors.append(
            f"Duration {duration:.1f}s is below the "
            f"minimum of {min_duration:g}s."
        )

    if duration > max_duration:

        errors.append(
            f"Duration {duration:.1f}s exceeds the "
            f"maximum of {max_duration:g}s."
        )

    if (
        width != expected_width
        or height != expected_height
    ):

        errors.append(
            f"Resolution {width}x{height} does not "
            f"match the required "
            f"{expected_width}x{expected_height}."
        )

    if abs(
        fps - expected_fps
    ) > 0.5:

        errors.append(
            f"Frame rate {fps:g} does not match the "
            f"required {expected_fps:g}."
        )

    result = {
        "ok": not errors,
        "duration_seconds": round(
            duration,
            2
        ),
        "width": width,
        "height": height,
        "fps": round(
            fps,
            2
        ),
        "requirements": {
            "min_duration_seconds": min_duration,
            "max_duration_seconds": max_duration,
            "width": expected_width,
            "height": expected_height,
            "fps": expected_fps
        },
        "errors": errors
    }

    validation_path = (
        Path(
            video_path
        )
        .parent
        /
        "validation.json"
    )

    with open(
        validation_path,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            result,
            file,
            indent=2,
            ensure_ascii=False
        )

    if errors:

        raise VideoValidationError(
            "Rendered Short is invalid: "
            + " ".join(
                errors
            )
        )

    return result