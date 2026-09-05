import json
import sys
import traceback

from core.pipeline import CuriousPipeline


PROGRESS_PREFIX = "__CURIOUS_PROGRESS__:"
RESULT_PREFIX = "__CURIOUS_RESULT__:"
ERROR_PREFIX = "__CURIOUS_ERROR__:"


def emit(
    prefix,
    data
):

    print(
        prefix
        +
        json.dumps(
            data
        ),
        flush=True
    )


def emit_progress(
    percent,
    message,
    stage=None
):

    emit(
        PROGRESS_PREFIX,
        {
            "type": "progress",
            "percent": int(
                percent
            ),
            "message": str(
                message
            ),
            "stage": stage
        }
    )


def main():

    if len(sys.argv) < 2:

        emit(
            ERROR_PREFIX,
            {
                "error":
                "No job payload was provided."
            }
        )

        return 1

    try:

        payload = json.loads(
            sys.argv[1]
        )

    except Exception as error:

        emit(
            ERROR_PREFIX,
            {
                "error":
                f"Invalid job payload: {error}"
            }
        )

        return 1

    try:

        pipeline = CuriousPipeline()

        pipeline.set_progress_callback(
            emit_progress
        )

        job_type = str(
            payload.get(
                "job_type",
                ""
            )
        ).strip()

        if job_type == "episode":

            prompt_only = bool(
                payload.get(
                    "prompt_only",
                    False
                )
            )

            episode_id = str(
                payload.get(
                    "episode_id",
                    ""
                )
            ).strip()

            if episode_id:

                result = (
                    pipeline.create_prompt(
                        episode_id=episode_id
                    )
                )

            else:

                result = (
                    pipeline.create_episode(
                        prompt_only=prompt_only
                    )
                )

        elif job_type == "video":

            prompt_item = (
                payload.get(
                    "prompt_item"
                )
            )

            episode_id = str(
                payload.get(
                    "episode_id",
                    ""
                )
            ).strip()

            if not isinstance(
                prompt_item,
                dict
            ):

                raise ValueError(
                    "Prompt item must be a dictionary."
                )

            if not episode_id:

                raise ValueError(
                    "Episode ID is required."
                )

            result = (
                pipeline.generate_video_from_prompt(
                    prompt_item,
                    episode_id
                )
            )

        else:

            raise ValueError(
                "Unsupported job type: "
                f"{job_type}"
            )

        emit(
            RESULT_PREFIX,
            {
                "success": True,
                "result": result
            }
        )

        return 0

    except Exception as error:

        traceback.print_exc()

        emit(
            ERROR_PREFIX,
            {
                "error": str(
                    error
                )
            }
        )

        return 1


if __name__ == "__main__":

    sys.exit(
        main()
    )