from pathlib import Path
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer
)
from urllib.parse import (
    urlparse,
    unquote,
    parse_qs
)
import json
import multiprocessing
import shutil
import threading
import traceback

from youtube import (
    config as youtube_config,
    metadata_generator,
    uploader
)

from instagram import (
    auth as instagram_auth,
    publisher as instagram_publisher
)

from instagram import (
    config as instagram_config_module
)


HOST = "0.0.0.0"
PORT = 8000

PROJECT_ROOT = (
    Path(__file__)
    .resolve()
    .parent
    .parent
)

MEDIA_ROOT = (
    PROJECT_ROOT
    /
    "media"
    /
    "output"
)

# Shorts are written under MEDIA_ROOT/shorts (e.g. media/output/shorts/<episode>/).
# Long-form videos/compilations will be written under MEDIA_ROOT/videos once
# implemented. Both directories are created at startup so the UI and discovery
# logic can treat them as known locations.
SHORTS_OUTPUT = (
    MEDIA_ROOT
    /
    "shorts"
)

VIDEOS_OUTPUT = (
    MEDIA_ROOT
    /
    "videos"
)

INDEX_FILE = (
    Path(__file__).resolve().parent
    /
    "index.html"
)


job_lock = threading.Lock()

job_state_lock = threading.Lock()

job_state = {
    "running": False,
    "type": None,

    "active_stage": None,

    "prompt_progress": 0,
    "prompt_status": "Waiting.",
    "prompt_message": "Waiting.",

    "video_progress": 0,
    "video_status": "Waiting.",
    "video_message": "Waiting.",

    "progress": 0,
    "message": "Ready.",

    "error": None,
    "result": None
}


"""
Snapshots of the episodes that already existed when a job
started. Episode directories created after this moment belong
to the running job, so they can be flagged as "generating".

This lets the web UI show exactly which episode is currently
being generated.
"""

job_baseline_episode_ids = None


def update_job_state(
    **updates
):

    with job_state_lock:

        job_state.update(
            updates
        )


def get_job_state():

    with job_state_lock:

        return dict(
            job_state
        )


def set_stage_progress(
    stage,
    percent,
    message
):

    if stage not in (
        "prompt",
        "video"
    ):

        return

    percent = int(
        max(
            0,
            min(
                100,
                percent
            )
        )
    )

    message = str(
        message
    )

    if stage == "prompt":

        update_job_state(
            prompt_progress=percent,
            prompt_message=message,
            prompt_status=(
                "Complete."
                if percent >= 100
                else "Running."
            ),
            active_stage=(
                "video"
                if percent >= 100
                else "prompt"
            ),
            progress=(
                percent
                if percent < 100
                else 0
            ),
            message=message
        )

        return

    if stage == "video":

        update_job_state(
            video_progress=percent,
            video_message=message,
            video_status=(
                "Complete."
                if percent >= 100
                else "Running."
            ),
            active_stage=(
                None
                if percent >= 100
                else "video"
            ),
            progress=percent,
            message=message
        )

        return


def set_stage_failed(
    stage,
    message
):

    message = str(
        message
    )

    if stage == "prompt":

        update_job_state(
            prompt_status="Failed.",
            prompt_message=message,
            active_stage=None,
            message="Prompt generation failed."
        )

        return

    if stage == "video":

        update_job_state(
            video_status="Failed.",
            video_message=message,
            active_stage=None,
            message="Video generation failed."
        )

        return


def set_stage_crashed(
    stage,
    message
):

    message = str(
        message
    )

    if stage == "prompt":

        update_job_state(
            prompt_status="Crashed.",
            prompt_message=message,
            active_stage=None,
            message="Prompt generation crashed."
        )

        return

    if stage == "video":

        update_job_state(
            video_status="Crashed.",
            video_message=message,
            active_stage=None,
            message="Video generation crashed."
        )

        return


def child_set_progress(
    progress_queue,
    stage,
    percent,
    message
):

    if stage not in (
        "prompt",
        "video"
    ):

        return

    percent = int(
        max(
            0,
            min(
                100,
                percent
            )
        )
    )

    try:

        progress_queue.put(
            {
                "type": "progress",
                "stage": stage,
                "percent": percent,
                "message": str(
                    message
                )
            }
        )

    except Exception:

        pass


def child_run_job(
    job_type,
    episode_id,
    prompt_item,
    progress_queue,
    result_queue,
    prompt_only=False,
    instruction=None
):

    """
    Runs the actual CuriousPipeline work inside a child process.

    The web server itself never loads the AI pipeline.

    This protects the HTTP server from native crashes caused by
    Selenium, Chrome, FFmpeg, or other native dependencies.
    """

    initial_stage = (
        "prompt"
        if job_type == "episode"
        else "video"
    )

    try:

        from core.pipeline import CuriousPipeline

        pipeline = CuriousPipeline()

        def progress_callback(
            percent,
            message,
            stage=None
        ):

            actual_stage = (
                stage
                if stage in (
                    "prompt",
                    "video"
                )
                else initial_stage
            )

            child_set_progress(
                progress_queue,
                actual_stage,
                percent,
                message
            )

        pipeline.set_progress_callback(
            progress_callback
        )

        if job_type == "episode":

            if episode_id:

                result = (
                    pipeline.create_prompt(
                        episode_id=episode_id,
                        instruction=instruction
                    )
                )

            else:

                result = (
                    pipeline.create_episode(
                        prompt_only=prompt_only,
                        instruction=instruction
                    )
                )

        elif job_type == "video":

            result = (
                pipeline.generate_video_from_prompt(
                    prompt_item,
                    episode_id
                )
            )

        else:

            raise ValueError(
                f"Unsupported job type: {job_type}"
            )

        result_queue.put(
            {
                "type": "success",
                "result": result
            }
        )

    except BaseException as error:

        error_text = str(
            error
        ).strip()

        if not error_text:

            error_text = (
                error.__class__.__name__
            )

        traceback_text = (
            traceback.format_exc()
        )

        try:

            result_queue.put(
                {
                    "type": "error",
                    "error": error_text,
                    "traceback": traceback_text
                }
            )

        except Exception:

            pass

        raise


def parse_prompt_file(
    path
):

    text = path.read_text(
        encoding="utf-8"
    )

    parts = text.split(
        "=" * 72
    )

    prompts = []

    # First try: find sections with both TITLE: and PROMPT: (new format)
    for part in parts:

        title_marker = "TITLE:"
        prompt_marker = "PROMPT:"
        summary_marker = "SUMMARY:"

        title_index = part.find(
            title_marker
        )

        prompt_index = part.find(
            prompt_marker
        )

        if (
            title_index == -1
            or
            prompt_index == -1
        ):

            continue

        summary_index = part.find(
            summary_marker,
            prompt_index
            +
            len(prompt_marker)
        )

        if summary_index == -1:

            # Older prompt files have no SUMMARY section.

            title = (
                part[
                    title_index
                    +
                    len(title_marker):
                    prompt_index
                ]
                .strip()
            )

            prompt = (
                part[
                    prompt_index
                    +
                    len(prompt_marker):
                ]
                .strip()
            )

            summary = ""

        else:

            title = (
                part[
                    title_index
                    +
                    len(title_marker):
                    prompt_index
                ]
                .strip()
            )

            prompt = (
                part[
                    prompt_index
                    +
                    len(prompt_marker):
                    summary_index
                ]
                .strip()
            )

            summary = (
                part[
                    summary_index
                    +
                    len(summary_marker):
                ]
                .strip()
            )

        if not title or not prompt:

            continue

        prompts.append(
            {
                "title": title,
                "prompt": prompt,
                "summary": summary
            }
        )

    if prompts:
        return prompts

    # Second try: handle old format where TITLE, PROMPT, SUMMARY
    # are each in their own section between dividers.
    title = ""
    prompt = ""
    summary = ""

    for part in parts:
        stripped = part.strip()
        if stripped.startswith("TITLE:"):
            title = stripped[len("TITLE:"):].strip()
        elif stripped.startswith("PROMPT:"):
            prompt = stripped[len("PROMPT:"):].strip()
        elif stripped.startswith("SUMMARY:"):
            summary = stripped[len("SUMMARY:"):].strip()

    if title and prompt:
        prompts.append(
            {
                "title": title,
                "prompt": prompt,
                "summary": summary
            }
        )

    return prompts


def scan_episode_ids():

    """
    Returns the set of episode ids currently discoverable under
    the shorts output root, without parsing prompt files.

    Used to snapshot which episodes existed when a job started.
    """

    ids = set()

    if not SHORTS_OUTPUT.exists():

        return ids

    for episode_directory in sorted(
        SHORTS_OUTPUT.iterdir()
    ):

        if not episode_directory.is_dir():

            continue

        if not episode_directory.name.isdigit():

            continue

        try:

            relative_directory = (
                episode_directory
                .relative_to(
                    PROJECT_ROOT
                )
            )

        except ValueError:

            continue

        ids.add(
            str(
                relative_directory
            ).replace(
                "\\",
                "/"
            )
        )

    return ids


def is_episode_generating(
    episode_id
):

    """
    A freshly created episode (one that did not exist when the
    current episode job started) is the one being generated.
    """

    state = (
        get_job_state()
    )

    if not state.get("running"):

        return False

    if state.get("type") != "episode":

        return False

    if job_baseline_episode_ids is None:

        return False

    return (
        episode_id
        not in
        job_baseline_episode_ids
    )


def discover_episodes():

    results = []

    if not SHORTS_OUTPUT.exists():

        return results

    for episode_directory in sorted(
        SHORTS_OUTPUT.iterdir(),
        reverse=True
    ):

        if not episode_directory.is_dir():

            continue

        if not episode_directory.name.isdigit():

            continue

        prompt_path = (
            episode_directory
            /
            "prompt.txt"
        )

        video_path = (
            episode_directory
            /
            "episode.mp4"
        )

        prompts = []

        if prompt_path.is_file():

            try:

                prompts = parse_prompt_file(
                    prompt_path
                )

            except Exception:

                prompts = []

        prompt_item = (
            prompts[0]
            if prompts
            else None
        )

        try:

            relative_directory = (
                episode_directory
                .relative_to(
                    PROJECT_ROOT
                )
            )

        except ValueError:

            continue

        episode_id = str(
            relative_directory
        ).replace(
            "\\",
            "/"
        )

        video_exists = (
            video_path.is_file()
        )

        video_relative_path = None

        if video_exists:

            try:

                video_relative_path = str(
                    video_path
                    .relative_to(
                        MEDIA_ROOT
                    )
                ).replace(
                    "\\",
                    "/"
                )

            except ValueError:

                video_relative_path = None

        results.append(
            {
                "id": episode_id,
                "number": episode_directory.name,
                "path": episode_id,
                "prompt_exists": bool(
                    prompt_item
                ),
                "title": (
                    prompt_item["title"]
                    if prompt_item
                    else ""
                ),
                "prompt": (
                    prompt_item["prompt"]
                    if prompt_item
                    else ""
                ),
                "summary": (
                    prompt_item.get(
                        "summary",
                        ""
                    )
                    if prompt_item
                    else ""
                ),
                "video_exists": video_exists,
                "video_path": video_relative_path,
                "uploaded": read_upload_status(
                    episode_directory
                ),
                "generating": is_episode_generating(
                    episode_id
                )
            }
        )

    results.sort(
        key=lambda item: int(
            item["number"]
        ),
        reverse=True
    )

    return results


def get_prompt_by_id(
    prompt_id
):

    separator = "::"

    if separator not in prompt_id:

        raise ValueError(
            "Invalid prompt ID."
        )

    relative_path, index_text = (
        prompt_id.rsplit(
            separator,
            1
        )
    )

    index = int(
        index_text
    )

    prompt_path = (
        PROJECT_ROOT
        /
        Path(
            relative_path
        )
    ).resolve()

    allowed_root = (
        SHORTS_OUTPUT.resolve()
    )

    if (
        allowed_root
        not in prompt_path.parents
    ):

        raise ValueError(
            "Invalid prompt path."
        )

    if prompt_path.name != "prompt.txt":

        raise ValueError(
            "Invalid prompt file."
        )

    prompts = parse_prompt_file(
        prompt_path
    )

    if (
        index < 0
        or
        index >= len(prompts)
    ):

        raise ValueError(
            "Prompt no longer exists."
        )

    return prompts[index]


def get_episode_prompt(
    episode_id
):

    episode_path = (
        PROJECT_ROOT
        /
        Path(episode_id)
    ).resolve()

    allowed_root = (
        SHORTS_OUTPUT.resolve()
    )

    if (
        allowed_root
        not in episode_path.parents
    ):

        raise ValueError(
            "Invalid episode path."
        )

    if not episode_path.is_dir():

        raise ValueError(
            "Episode does not exist."
        )

    prompt_path = (
        episode_path
        /
        "prompt.txt"
    )

    if not prompt_path.is_file():

        raise ValueError(
            "Episode does not contain a prompt."
        )

    prompts = parse_prompt_file(
        prompt_path
    )

    if not prompts:

        raise ValueError(
            "Episode prompt is empty."
        )

    return prompts[0]


def resolve_episode_video_path(
    episode_id
):

    """
    Resolves and validates the episode.mp4 path for an episode id.
    """

    episode_path = (
        PROJECT_ROOT
        /
        Path(episode_id)
    ).resolve()

    allowed_root = (
        SHORTS_OUTPUT.resolve()
    )

    if (
        allowed_root
        not in episode_path.parents
    ):

        raise ValueError(
            "Invalid episode path."
        )

    if not episode_path.is_dir():

        raise ValueError(
            "Episode does not exist."
        )

    video_path = (
        episode_path
        /
        "episode.mp4"
    )

    if not video_path.is_file():

        raise ValueError(
            "Episode does not contain a video yet."
        )

    return video_path


def get_episode_identity(
    episode_id
):

    """
    Returns the episode number from an episode id.
    """

    episode_path = (
        PROJECT_ROOT
        /
        Path(episode_id)
    ).resolve()

    relative = (
        episode_path
        .relative_to(
            SHORTS_OUTPUT
        )
    )

    parts = relative.parts

    if len(parts) < 1:

        raise ValueError(
            "Invalid episode directory structure."
        )

    return {
        "number": parts[-1]
    }


UPLOAD_STATUS_FILE = "upload.txt"


def read_upload_status(
    episode_directory
):

    """
    Reads per-platform upload completion flags from an episode's
    upload.txt file. Missing file or keys mean "not uploaded".
    """

    status = {
        "youtube": False,
        "instagram": False
    }

    path = (
        episode_directory
        /
        UPLOAD_STATUS_FILE
    )

    if not path.is_file():

        return status

    try:

        text = path.read_text(
            encoding="utf-8"
        )

    except OSError:

        return status

    for line in text.splitlines():

        line = line.strip()

        if not line or "=" not in line:

            continue

        key, _, value = line.partition("=")

        key = key.strip().lower()

        if key in status:

            status[key] = (
                value.strip().lower()
                in ("true", "1", "yes", "done")
            )

    return status


def write_upload_status(
    episode_directory,
    platform,
    done
):

    """
    Updates one platform flag inside an episode's upload.txt while
    preserving the other platform's value.
    """

    platform = str(platform).strip().lower()

    if platform not in (
        "youtube",
        "instagram"
    ):

        raise ValueError(
            "Unknown upload platform: "
            f"{platform}"
        )

    status = read_upload_status(
        episode_directory
    )

    status[platform] = bool(done)

    newline = "\n"

    lines = [
        "youtube={}".format(
            "true"
            if status["youtube"]
            else "false"
        ),
        "instagram={}".format(
            "true"
            if status["instagram"]
            else "false"
        )
    ]

    (
        episode_directory
        / UPLOAD_STATUS_FILE
    ).write_text(
        newline.join(lines) + newline,
        encoding="utf-8"
    )

    return status


def resolve_episode_directory(
    episode_id
):

    """
    Resolves and validates an episode directory under media/output/shorts.
    """

    episode_path = (
        PROJECT_ROOT
        /
        Path(episode_id)
    ).resolve()

    media_root = (
        SHORTS_OUTPUT.resolve()
    )

    if media_root not in episode_path.parents:

        raise ValueError(
            "Invalid episode path."
        )

    if not episode_path.is_dir():

        raise ValueError(
            "Episode does not exist."
        )

    return episode_path


def _print_upload_progress(
    uploaded_bytes,
    total_bytes,
    message
):

    try:

        percent = int(
            (
                uploaded_bytes
                /
                total_bytes
            )
            * 100
        )

    except Exception:

        percent = 0

    percent = int(
        max(
            0,
            min(
                100,
                percent
            )
        )
    )

    print(
        f"[YOUTUBE] {percent}% - {message}"
    )


def drain_progress_queue(
    progress_queue
):

    """
    Consume all currently available progress messages.

    The child process sends progress here.
    The parent process owns the actual web-server job state.
    """

    while True:

        try:

            message = (
                progress_queue.get_nowait()
            )

        except Exception:

            break

        if not isinstance(
            message,
            dict
        ):

            continue

        if (
            message.get(
                "type"
            )
            !=
            "progress"
        ):

            continue

        stage = (
            message.get(
                "stage"
            )
        )

        set_stage_progress(
            stage,
            message.get(
                "percent",
                0
            ),
            message.get(
                "message",
                ""
            )
        )


def initialize_job_state(
    job_type
):

    if job_type == "episode":

        update_job_state(
            running=True,
            type="episode",
            active_stage="prompt",

            prompt_progress=0,
            prompt_status="Running.",
            prompt_message="Starting prompt generation.",

            video_progress=0,
            video_status="Waiting.",
            video_message="Waiting for prompt generation.",

            progress=0,
            message="Starting prompt generation.",

            error=None,
            result=None
        )

        return

    if job_type == "video":

        update_job_state(
            running=True,
            type="video",
            active_stage="video",

            prompt_progress=100,
            prompt_status="Complete.",
            prompt_message="Prompt already generated.",

            video_progress=0,
            video_status="Running.",
            video_message="Starting video generation.",

            progress=0,
            message="Starting video generation.",

            error=None,
            result=None
        )

        return


def run_job(
    job_type,
    episode_id=None,
    prompt_item=None,
    prompt_only=False,
    instruction=None
):

    if not job_lock.acquire(
        blocking=False
    ):

        return False

    initialize_job_state(
        job_type
    )

    global job_baseline_episode_ids

    job_baseline_episode_ids = (
        scan_episode_ids()
    )

    def worker():

        multiprocessing_context = None
        progress_queue = None
        result_queue = None
        process = None

        received_result = None
        received_error = None

        stage = (
            "prompt"
            if job_type == "episode"
            else "video"
        )

        try:

            multiprocessing_context = (
                multiprocessing.get_context(
                    "spawn"
                )
            )

            progress_queue = (
                multiprocessing_context.Queue()
            )

            result_queue = (
                multiprocessing_context.Queue()
            )

            process = (
                multiprocessing_context.Process(
                    target=child_run_job,
                    args=(
                        job_type,
                        episode_id,
                        prompt_item,
                        progress_queue,
                        result_queue,
                        prompt_only,
                        instruction
                    ),
                    daemon=False
                )
            )

            print()
            print(
                "[JOB] Starting isolated "
                f"{job_type} generation process."
            )

            process.start()

            while process.is_alive():

                try:

                    message = (
                        result_queue.get(
                            timeout=0.25
                        )
                    )

                    if not isinstance(
                        message,
                        dict
                    ):

                        continue

                    message_type = (
                        message.get(
                            "type"
                        )
                    )

                    if message_type == "success":

                        received_result = (
                            message.get(
                                "result"
                            )
                        )

                    elif message_type == "error":

                        received_error = (
                            message
                        )

                except Exception:

                    pass

                drain_progress_queue(
                    progress_queue
                )

                # Refresh local stage according to the shared job state so
                # failures or crashes are attributed to the correct stage
                try:
                    state = get_job_state()
                    if job_type == "episode":
                        # If prompt is complete, next stage is video
                        # (unless running prompt-only).
                        if (
                            int(state.get("prompt_progress", 0)) >= 100
                            and not prompt_only
                        ):
                            stage = "video"
                        else:
                            stage = "prompt"
                    else:
                        stage = "video"
                except Exception:
                    # If reading state fails, leave stage unchanged
                    pass

            process.join(
                timeout=5
            )

            drain_progress_queue(
                progress_queue
            )

            # Refresh local stage according to the shared job state so
            # failures or crashes are attributed to the correct stage
            try:
                state = get_job_state()
                if job_type == "episode":
                    if (
                        int(state.get("prompt_progress", 0)) >= 100
                        and not prompt_only
                    ):
                        stage = "video"
                    else:
                        stage = "prompt"
                else:
                    stage = "video"
            except Exception:
                pass

            while True:

                try:

                    message = (
                        result_queue.get_nowait()
                    )

                except Exception:

                    break

                if not isinstance(
                    message,
                    dict
                ):

                    continue

                message_type = (
                    message.get(
                        "type"
                    )
                )

                if message_type == "success":

                    received_result = (
                        message.get(
                            "result"
                        )
                    )

                elif message_type == "error":

                    received_error = (
                        message
                    )

            exit_code = (
                process.exitcode
            )

            if (
                received_error
                is not None
            ):

                error_text = str(
                    received_error.get(
                        "error",
                        "Generation failed."
                    )
                )

                traceback_text = (
                    received_error.get(
                        "traceback",
                        ""
                    )
                )

                print()
                print(
                    "[JOB] =================================="
                )
                print(
                    "[JOB] Generation failed."
                )
                print(
                    f"[JOB] {error_text}"
                )

                if traceback_text:

                    print(
                        traceback_text
                    )

                print(
                    "[JOB] The web server is still running."
                )
                print(
                    "[JOB] =================================="
                )

                set_stage_failed(
                    stage,
                    error_text
                )

                update_job_state(
                    error=error_text,
                    message=(
                        "Prompt generation failed."
                        if stage == "prompt"
                        else
                        "Video generation failed."
                    )
                )

            elif (
                exit_code == 0
                and
                received_result is not None
            ):

                if stage == "prompt":

                    set_stage_progress(
                        "prompt",
                        100,
                        "Prompt generation complete."
                    )

                else:

                    set_stage_progress(
                        "video",
                        100,
                        "Video generation complete."
                    )

                update_job_state(
                    result=received_result,
                    progress=100,
                    message=(
                        "Prompt generation complete."
                        if stage == "prompt"
                        else
                        "Video generation complete."
                    )
                )

                print(
                    "[JOB] Generation completed successfully."
                )

            elif exit_code != 0:

                error_message = (
                    "Generation process terminated "
                    "unexpectedly."
                )

                if exit_code is not None:

                    error_message += (
                        f" Exit code: {exit_code}"
                    )

                print()
                print(
                    "[JOB] =================================="
                )
                print(
                    "[JOB] GENERATION PROCESS CRASHED"
                )
                print(
                    f"[JOB] {error_message}"
                )
                print(
                    "[JOB] The web server is still running."
                )
                print(
                    "[JOB] The job lock will be released."
                )
                print(
                    "[JOB] Another generation can be started."
                )
                print(
                    "[JOB] =================================="
                )

                set_stage_crashed(
                    stage,
                    error_message
                )

                update_job_state(
                    error=error_message,
                    message=(
                        "Prompt generation crashed."
                        if stage == "prompt"
                        else
                        "Video generation crashed."
                    )
                )

            else:

                error_message = (
                    "Generation process ended "
                    "without returning a result."
                )

                print(
                    f"[JOB] {error_message}"
                )

                set_stage_failed(
                    stage,
                    error_message
                )

                update_job_state(
                    error=error_message,
                    message=(
                        "Prompt generation failed."
                        if stage == "prompt"
                        else
                        "Video generation failed."
                    )
                )

        except Exception as error:

            traceback.print_exc()

            error_message = str(
                error
            )

            set_stage_failed(
                stage,
                error_message
            )

            update_job_state(
                error=error_message,
                message=(
                    "Prompt generation failed."
                    if stage == "prompt"
                    else
                    "Video generation failed."
                )
            )

        finally:

            if process is not None:

                try:

                    if process.is_alive():

                        process.terminate()

                        process.join(
                            timeout=5
                        )

                except Exception:

                    pass

            if progress_queue is not None:

                try:

                    progress_queue.close()

                except Exception:

                    pass

            if result_queue is not None:

                try:

                    result_queue.close()

                except Exception:

                    pass

            update_job_state(
                running=False,
                type=None,
                active_stage=None
            )

            global job_baseline_episode_ids

            job_baseline_episode_ids = None

            job_lock.release()

            print(
                "[JOB] Job lock released."
            )

    thread = threading.Thread(
        target=worker,
        daemon=True
    )

    thread.start()

    return True


class RequestHandler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        format,
        *args
    ):

        return

    def send_json(
        self,
        data,
        status=200
    ):

        body = json.dumps(
            data
        ).encode(
            "utf-8"
        )

        self.send_response(
            status
        )

        self.send_header(
            "Content-Type",
            "application/json"
        )

        self.send_header(
            "Content-Length",
            str(
                len(body)
            )
        )

        self.end_headers()

        self.wfile.write(
            body
        )

    def read_json(
        self
    ):

        length = int(
            self.headers.get(
                "Content-Length",
                "0"
            )
        )

        if length <= 0:

            return {}

        body = self.rfile.read(
            length
        )

        return json.loads(
            body.decode(
                "utf-8"
            )
        )

    def do_GET(
        self
    ):

        parsed = urlparse(
            self.path
        )

        path = parsed.path

        if path == "/":

            self.serve_index()

            return

        if path == "/api/status":

            self.send_json(
                get_job_state()
            )

            return

        if path == "/api/episodes":

            self.send_json(
                discover_episodes()
            )

            return

        if path == "/api/prompts":

            episodes = discover_episodes()

            prompts = []

            for episode in episodes:

                if not episode[
                    "prompt_exists"
                ]:

                    continue

                prompts.append(
                    {
                        "id": (
                            f"{episode['path']}"
                            "::0"
                        ),
                        "path": (
                            f"{episode['path']}"
                            "/prompt.txt"
                        ),
                        "index": 0,
                        "title": episode[
                            "title"
                        ],
                        "prompt": episode[
                            "prompt"
                        ]
                    }
                )

            self.send_json(
                prompts
            )

            return

        if path == "/api/youtube/form":

            params = parse_qs(
                parsed.query
            )

            episode_ids = (
                params.get(
                    "episode_id",
                    []
                )
            )

            if not episode_ids:

                self.send_json(
                    {
                        "error":
                        "Missing episode ID."
                    },
                    400
                )

                return

            episode_id = (
                episode_ids[0]
            )

            try:

                prompt_item = (
                    get_episode_prompt(
                        episode_id
                    )
                )

                identity = (
                    get_episode_identity(
                        episode_id
                    )
                )

                config = (
                    youtube_config.load_youtube_config()
                )

            except Exception as error:

                self.send_json(
                    {
                        "error": str(
                            error
                        )
                    },
                    400
                )

                return

            metadata = (
                metadata_generator.generate_metadata_from_prompt(
                    prompt_item,
                    episode_number=identity["number"],
                    config=config
                )
            )

            self.send_json(
                {
                    "config": config,
                    "episode": {
                        "id": episode_id,
                        "number": identity["number"],
                        "uploaded": read_upload_status(
                            resolve_episode_directory(
                                episode_id
                            )
                        ),
                        "title": prompt_item.get(
                            "title",
                            ""
                        ),
                        "prompt": prompt_item.get(
                            "prompt",
                            ""
                        )
                    },
                    "metadata": metadata
                }
            )

            return

        if path == "/api/instagram/form":

            params = parse_qs(
                parsed.query
            )

            episode_ids = (
                params.get(
                    "episode_id",
                    []
                )
            )

            if not episode_ids:

                self.send_json(
                    {
                        "error":
                        "Missing episode ID."
                    },
                    400
                )

                return

            episode_id = (
                episode_ids[0]
            )

            try:

                prompt_item = (
                    get_episode_prompt(
                        episode_id
                    )
                )

                identity = (
                    get_episode_identity(
                        episode_id
                    )
                )

                instagram_config = (
                    instagram_config_module.get_account(
                        instagram_config_module.load_instagram_config()
                    )
                )

                caption_defaults = (
                    instagram_config_module.get_caption_defaults(
                        instagram_config_module.load_instagram_config()
                    )
                )

            except Exception as error:

                self.send_json(
                    {
                        "error": str(
                            error
                        )
                    },
                    400
                )

                return

            title = str(
                prompt_item.get(
                    "title",
                    ""
                )
            ).strip()

            summary = str(
                prompt_item.get(
                    "summary",
                    ""
                ).strip()
                or prompt_item.get(
                    "prompt",
                    ""
                ).strip()
            )

            tags = list(
                caption_defaults.get(
                    "default_hashtags"
                )
                or ["#Shorts", "#AI"]
            )

            caption_parts = [
                part
                for part in (
                    title,
                    summary,
                    " ".join(tags)
                )
                if part.strip()
            ]

            self.send_json(
                {
                    "config": {
                        "account": {
                            "access_token": instagram_config.get(
                                "access_token",
                                ""
                            ),
                            "user_id": instagram_config.get(
                                "user_id",
                                ""
                            )
                        }
                    },
                    "episode": {
                        "id": episode_id,
                        "number": identity[
                            "number"
                        ],
                        "uploaded": read_upload_status(
                            resolve_episode_directory(
                                episode_id
                            )
                        ),
                        "title": title
                    },
                    "caption": "\n\n".join(
                        caption_parts
                    )
                }
            )

            return

        if path.startswith(
            "/media/"
        ):

            self.serve_media(
                path[
                    len("/media/"):
                ]
            )

            return

        self.send_json(
            {
                "error": "Not found."
            },
            404
        )

    def do_POST(
        self
    ):

        parsed = urlparse(
            self.path
        )

        if parsed.path == "/api/upload-status":

            try:

                data = self.read_json()

                episode_id = str(
                    data.get("episode_id", "")
                ).strip()

                if not episode_id:

                    raise ValueError(
                        "Missing episode ID."
                    )

                platform = str(
                    data.get("platform", "")
                ).strip().lower()

                done = bool(
                    data.get("done")
                )

                episode_directory = (
                    resolve_episode_directory(
                        episode_id
                    )
                )

                status = write_upload_status(
                    episode_directory,
                    platform,
                    done
                )

            except Exception as error:

                self.send_json(
                    {
                        "error": str(error)
                    },
                    400
                )

                return

            print(
                f"[UPLOAD-STATUS] {episode_id} "
                f"{platform}="
                f"{'done' if status.get(platform) else 'not done'}"
            )

            self.send_json(
                {
                    "success": True,
                    "episode_id": episode_id,
                    "platform": platform,
                    "done": status.get(platform, False),
                    "uploaded": status
                }
            )

            return

        if parsed.path == "/api/episode/delete":

            try:

                if get_job_state().get(
                    "running"
                ):

                    raise ValueError(
                        "Another job is already running."
                    )

                data = self.read_json()

                episode_id = str(
                    data.get(
                        "episode_id",
                        ""
                    )
                ).strip()

                if not episode_id:

                    raise ValueError(
                        "Missing episode ID."
                    )

                episode_directory = (
                    resolve_episode_directory(
                        episode_id
                    )
                )

                # Delete everything inside the episode folder
                # (videos, content, upload status, etc.). The
                # folder itself is kept so it still resolves as
                # an existing (but now empty) episode.

                for entry in episode_directory.iterdir():

                    if (
                        entry.is_dir()
                        and not entry.is_symlink()
                    ):

                        shutil.rmtree(
                            entry
                        )

                    else:

                        entry.unlink()

            except Exception as error:

                self.send_json(
                    {
                        "error": str(
                            error
                        )
                    },
                    400
                )

                return

            print(
                f"[EPISODE] Deleted {episode_id}: "
                "removed all episode contents."
            )

            self.send_json(
                {
                    "success": True,
                    "episode_id": episode_id
                }
            )

            return

        if parsed.path == "/api/episode/prompt":

            try:

                data = self.read_json()

                episode_id = str(
                    data.get(
                        "episode_id",
                        ""
                    )
                ).strip()

                if not episode_id:

                    raise ValueError(
                        "Missing episode ID."
                    )

                instruction = str(
                    data.get(
                        "instruction",
                        ""
                    )
                ).strip() or None

                # Validate the episode directory exists before
                # starting the regeneration job.
                resolve_episode_directory(
                    episode_id
                )

            except Exception as error:

                self.send_json(
                    {
                        "error": str(
                            error
                        )
                    },
                    400
                )

                return

            started = run_job(
                "episode",
                episode_id=episode_id,
                prompt_only=True,
                instruction=instruction
            )

            if not started:

                self.send_json(
                    {
                        "error":
                        "Another job is already running."
                    },
                    409
                )

                return

            self.send_json(
                {
                    "started": True
                }
            )

            return

        if parsed.path == "/api/episode":

            data = self.read_json()

            prompt_only = bool(
                data.get(
                    "prompt_only",
                    False
                )
            )

            instruction = str(
                data.get(
                    "instruction",
                    ""
                )
            ).strip() or None

            started = run_job(
                "episode",
                prompt_only=prompt_only,
                instruction=instruction
            )

            if not started:

                self.send_json(
                    {
                        "error":
                        "Another job is already running."
                    },
                    409
                )

                return

            self.send_json(
                {
                    "started": True
                }
            )

            return

        if parsed.path == "/api/video":

            try:

                data = self.read_json()

                episode_id = str(
                    data.get(
                        "episode_id",
                        ""
                    )
                ).strip()

                if not episode_id:

                    raise ValueError(
                        "Missing episode ID."
                    )

                prompt_item = (
                    get_episode_prompt(
                        episode_id
                    )
                )

            except Exception as error:

                self.send_json(
                    {
                        "error": str(
                            error
                        )
                    },
                    400
                )

                return

            started = run_job(
                "video",
                episode_id=episode_id,
                prompt_item=prompt_item
            )

            if not started:

                self.send_json(
                    {
                        "error":
                        "Another job is already running."
                    },
                    409
                )

                return

            self.send_json(
                {
                    "started": True
                }
            )

            return

        if parsed.path == "/api/youtube/upload":

            try:

                data = self.read_json()

                episode_id = str(
                    data.get(
                        "episode_id",
                        ""
                    )
                ).strip()

                if not episode_id:

                    raise ValueError(
                        "Missing episode ID."
                    )

                video_path = (
                    resolve_episode_video_path(
                        episode_id
                    )
                )

                account = data.get("account") or {}

                if not isinstance(account, dict):

                    raise ValueError(
                        "Account must be a dictionary."
                    )

                account = {
                    key: str(
                        account.get(key) or ""
                    ).strip()
                    for key in (
                        "client_id",
                        "client_secret",
                        "refresh_token",
                        "channel_name"
                    )
                }

                metadata = (
                    metadata_generator.normalize_upload_metadata(
                        data.get("metadata")
                        or
                        {}
                    )
                )

                result = (
                    uploader.upload_short(
                        video_path,
                        metadata,
                        account,
                        progress_callback=(
                            _print_upload_progress
                        )
                    )
                )

            except Exception as error:

                self.send_json(
                    {
                        "error": str(
                            error
                        )
                    },
                    400
                )

                return

            print(
                f"[YOUTUBE] Uploaded: {result.video_url}"
            )

            try:

                write_upload_status(
                    resolve_episode_directory(
                        episode_id
                    ),
                    "youtube",
                    True
                )

                print(
                    f"[YOUTUBE] Marked {episode_id} "
                    "as uploaded."
                )

            except Exception as mark_error:

                print(
                    "[YOUTUBE] Could not write upload "
                    f"status: {mark_error}"
                )

            self.send_json(
                {
                    "success": True,
                    "video_id": result.video_id,
                    "video_url": result.video_url,
                    "title": result.title
                }
            )

            return

        if parsed.path == "/api/instagram/publish":

            try:

                data = self.read_json()

                episode_id = str(
                    data.get(
                        "episode_id",
                        ""
                    )
                ).strip()

                if not episode_id:

                    raise ValueError(
                        "Missing episode ID."
                    )

                video_path = (
                    resolve_episode_video_path(
                        episode_id
                    )
                )

                account = (
                    data.get("account")
                    or {}
                )

                if not isinstance(account, dict):

                    raise ValueError(
                        "Account must be a dictionary."
                    )

                caption = str(
                    data.get("caption") or ""
                ).strip()

                if not caption:

                    raise ValueError(
                        "Caption is required."
                    )

                access_token = str(
                    account.get(
                        "access_token"
                    ) or ""
                ).strip()

                ig_user_id = str(
                    account.get(
                        "ig_user_id"
                    ) or account.get(
                        "user_id"
                    ) or ""
                ).strip()

                # Derive the publicly reachable base URL from the
                # incoming request itself (e.g. a cloudflared/ngrok
                # tunnel that the user opened the app through), so it
                # does not need to be configured anywhere.

                host = str(
                    self.headers.get("Host") or ""
                ).strip()

                if not host:

                    host = f"localhost:{PORT}"

                forwarded_proto = str(
                    self.headers.get(
                        "X-Forwarded-Proto"
                    ) or ""
                ).strip().lower()

                if forwarded_proto in (
                    "http",
                    "https"
                ):

                    scheme = forwarded_proto

                elif (
                    host.startswith("localhost")
                    or host.startswith("127.0.0.1")
                ):

                    scheme = "http"

                else:

                    scheme = "https"

                public_base_url = (
                    f"{scheme}://{host}"
                )

                relative_media = str(
                    video_path.relative_to(
                        MEDIA_ROOT
                    )
                ).replace(
                    "\\",
                    "/"
                )

                public_video_url = (
                    public_base_url.rstrip("/")
                    + "/media/"
                    + relative_media
                )

                # Instagram's servers fetch the video themselves, so
                # they cannot reach loopback addresses. Warn early and
                # loudly instead of failing later with a cryptic
                # processing error.

                is_loopback = (
                    host.startswith("localhost")
                    or host.startswith("127.0.0.1")
                )

                if is_loopback:

                    print(
                        "[INSTAGRAM] WARNING: The video URL uses "
                        f"'{host}', which Instagram's servers cannot "
                        "reach. Open this app through your public "
                        "tunnel (cloudflared/ngrok) and publish from "
                        "that URL instead."
                    )

                (
                    resolved_token,
                    resolved_user_id,
                    username
                ) = instagram_auth.resolve_account(
                    {
                        "access_token": access_token,
                        "ig_user_id": ig_user_id
                    }
                )

                print(
                    f"[INSTAGRAM] Publishing as @{username}: "
                    f"{public_video_url}"
                )

                def progress_callback(message):
                    print(
                        f"[INSTAGRAM] {message}"
                    )

                result = (
                    instagram_publisher.publish_reel(
                        public_video_url,
                        caption,
                        resolved_token,
                        resolved_user_id,
                        progress_callback=(
                            progress_callback
                        )
                    )
                )

            except Exception as error:

                self.send_json(
                    {
                        "error": str(
                            error
                        )
                    },
                    400
                )

                return

            permalink = (
                result.get("permalink")
                or ""
            )

            print(
                f"[INSTAGRAM] Published: {permalink}"
            )

            try:

                write_upload_status(
                    resolve_episode_directory(
                        episode_id
                    ),
                    "instagram",
                    True
                )

                print(
                    f"[INSTAGRAM] Marked {episode_id} "
                    "as uploaded."
                )

            except Exception as mark_error:

                print(
                    "[INSTAGRAM] Could not write upload "
                    f"status: {mark_error}"
                )

            self.send_json(
                {
                    "success": True,
                    "permalink": permalink,
                    "media_id": result.get(
                        "media_id",
                        ""
                    )
                }
            )

            return

        self.send_json(
            {
                "error": "Not found."
            },
            404
        )

    def serve_index(
        self
    ):

        try:

            body = INDEX_FILE.read_bytes()

        except OSError:

            self.send_json(
                {
                    "error":
                    "UI file not found."
                },
                500
            )

            return

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(
                len(body)
            )
        )

        self.end_headers()

        self.wfile.write(
            body
        )

    def serve_media(
        self,
        relative_path
    ):

        relative_path = unquote(
            relative_path
        )

        file_path = (
            MEDIA_ROOT
            /
            relative_path
        ).resolve()

        media_root = (
            MEDIA_ROOT.resolve()
        )

        if (
            media_root
            not in file_path.parents
        ):

            self.send_json(
                {
                    "error": "Invalid path."
                },
                403
            )

            return

        if not file_path.is_file():

            self.send_json(
                {
                    "error": "File not found."
                },
                404
            )

            return

        if file_path.suffix.lower() == ".mp4":

            content_type = "video/mp4"

        else:

            content_type = (
                "application/octet-stream"
            )

        try:

            file_size = file_path.stat().st_size

        except OSError:

            self.send_json(
                {
                    "error": "Could not read file."
                },
                500
            )

            return

        range_header = self.headers.get(
            "Range"
        )

        start = 0
        end = file_size - 1

        if range_header:

            try:

                if not range_header.startswith(
                    "bytes="
                ):

                    raise ValueError(
                        "Invalid range."
                    )

                range_value = (
                    range_header[
                        len("bytes="):
                    ]
                    .split(
                        ",",
                        1
                    )[0]
                    .strip()
                )

                if "-" not in range_value:

                    raise ValueError(
                        "Invalid range."
                    )

                start_text, end_text = (
                    range_value.split(
                        "-",
                        1
                    )
                )

                if start_text:

                    start = int(
                        start_text
                    )

                    if end_text:

                        end = int(
                            end_text
                        )

                    else:

                        end = (
                            file_size - 1
                        )

                else:

                    suffix_length = int(
                        end_text
                    )

                    if suffix_length <= 0:

                        raise ValueError(
                            "Invalid range."
                        )

                    start = max(
                        0,
                        file_size
                        -
                        suffix_length
                    )

                    end = (
                        file_size - 1
                    )

                if (
                    start < 0
                    or
                    start >= file_size
                    or
                    end < start
                ):

                    raise ValueError(
                        "Invalid range."
                    )

                end = min(
                    end,
                    file_size - 1
                )

            except (
                ValueError,
                TypeError
            ):

                self.send_response(
                    416
                )

                self.send_header(
                    "Content-Range",
                    f"bytes */{file_size}"
                )

                self.end_headers()

                return

            content_length = (
                end
                -
                start
                +
                1
            )

            self.send_response(
                206
            )

            self.send_header(
                "Content-Type",
                content_type
            )

            self.send_header(
                "Content-Length",
                str(
                    content_length
                )
            )

            self.send_header(
                "Content-Range",
                f"bytes {start}-{end}/{file_size}"
            )

            self.send_header(
                "Accept-Ranges",
                "bytes"
            )

            self.send_header(
                "Cache-Control",
                "no-cache"
            )

            self.end_headers()

            try:

                with file_path.open(
                    "rb"
                ) as file:

                    file.seek(
                        start
                    )

                    remaining = (
                        content_length
                    )

                    while remaining > 0:

                        chunk_size = min(
                            1024 * 1024,
                            remaining
                        )

                        chunk = file.read(
                            chunk_size
                        )

                        if not chunk:

                            break

                        self.wfile.write(
                            chunk
                        )

                        remaining -= len(
                            chunk
                        )

            except (
                BrokenPipeError,
                ConnectionResetError
            ):

                return

            return

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            content_type
        )

        self.send_header(
            "Content-Length",
            str(
                file_size
            )
        )

        self.send_header(
            "Accept-Ranges",
            "bytes"
        )

        self.send_header(
            "Cache-Control",
            "no-cache"
        )

        self.end_headers()

        try:

            with file_path.open(
                "rb"
            ) as file:

                while True:

                    chunk = file.read(
                        1024 * 1024
                    )

                    if not chunk:

                        break

                    self.wfile.write(
                        chunk
                    )

        except (
            BrokenPipeError,
            ConnectionResetError
        ):

            return


def main():

    multiprocessing.freeze_support()

    # Ensure both output destinations exist. Shorts are generated
    # today; videos/ is reserved for future long-form output.
    SHORTS_OUTPUT.mkdir(
        parents=True,
        exist_ok=True
    )

    VIDEOS_OUTPUT.mkdir(
        parents=True,
        exist_ok=True
    )

    server = ThreadingHTTPServer(
        (
            HOST,
            PORT
        ),
        RequestHandler
    )

    print()
    print(
        "======================================"
    )
    print(
        "    CURIOUS ABOUT THINGS WEB UI"
    )
    print(
        "======================================"
    )
    print(
        f"Running on http://{HOST}:{PORT}"
    )
    print(
        "Open http://localhost:8000"
    )
    print(
        "======================================"
    )
    print()

    try:

        server.serve_forever()

    except KeyboardInterrupt:

        print(
            "\n[SYSTEM] Shutting down."
        )

    finally:

        server.server_close()


if __name__ == "__main__":

    main()
