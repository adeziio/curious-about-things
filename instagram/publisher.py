import time

import requests

from instagram.auth import (
    _error_text
)

from instagram.config import (
    get_api_settings
)


TERMINAL_STATUSES = {
    "FINISHED",
    "ERROR"
}


class InstagramPublishError(RuntimeError):

    pass


def publish_reel(
    video_url,
    caption,
    access_token,
    ig_user_id,
    progress_callback=None
):
    """
    Publishes a publicly reachable video URL to Instagram as a Reel.

    Flow (Instagram Graph API):
      1. Create a media container pointing at the public video URL.
      2. Poll the container until Meta finishes processing it.
      3. Publish the finished container.
      4. Resolve the permalink of the published media.

    Connection settings (base URL, API version, poll interval,
    processing timeout) come from config/instagram.json.
    """

    if not video_url:

        raise InstagramPublishError(
            "A public video URL is required."
        )

    settings = get_api_settings()

    base = (
        f"{settings['graph_base_url']}/"
        f"{settings['api_version']}"
    )

    def notify(message):

        if progress_callback:

            progress_callback(message)

    # Step 1: create the media container.

    notify(
        "Creating Instagram media container…"
    )

    created = requests.post(
        f"{base}/{ig_user_id}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption or "",
            "share_to_feed": "true",
            "access_token": access_token
        },
        timeout=60
    )

    if created.status_code != 200:

        raise InstagramPublishError(
            _error_text(created)
        )

    container_id = str(
        (created.json() or {}).get("id") or ""
    ).strip()

    if not container_id:

        raise InstagramPublishError(
            "Instagram did not return a media container ID."
        )

    # Step 2: wait for Meta to finish processing the upload.

    notify(
        "Video uploaded to Instagram. Waiting for "
        "processing (this can take a few minutes)…"
    )

    deadline = (
        time.monotonic()
        + max(
            int(
                settings["processing_timeout_seconds"]
            ),
            30
        )
    )

    status = ""

    while time.monotonic() < deadline:

        polled = requests.get(
            f"{base}/{container_id}",
            params={
                "fields": "status_code,status",
                "access_token": access_token
            },
            timeout=30
        )

        if polled.status_code != 200:

            raise InstagramPublishError(
                _error_text(polled)
            )

        payload = polled.json() or {}

        status = str(
            payload.get("status_code") or ""
        ).strip().upper()

        if status in TERMINAL_STATUSES:

            break

        time.sleep(
            settings["poll_interval_seconds"]
        )

    else:

        raise InstagramPublishError(
            "Timed out waiting for Instagram to "
            "process the video."
        )

    if status != "FINISHED":

        detail = ""

        try:

            detail = str(
                (polled.json() or {}).get("status") or ""
            ).strip()

        except ValueError:

            detail = ""

        raise InstagramPublishError(
            "Instagram failed to process the video. "
            f"Final status: {status or 'unknown'}. "
            f"{detail} ".strip()
            + f"Video URL Instagram tried to fetch: {video_url}. "
            "Verify this URL opens the raw MP4 in a browser outside "
            "your machine (public tunnel up, HTTPS, direct file), and "
            "that the video is H.264/AAC MP4 between 3 seconds and "
            "15 minutes."
        )

    # Step 3: publish the processed container.

    notify("Publishing Reel…")

    published = requests.post(
        f"{base}/{ig_user_id}/media_publish",
        data={
            "creation_id": container_id,
            "access_token": access_token
        },
        timeout=60
    )

    if published.status_code != 200:

        raise InstagramPublishError(
            _error_text(published)
        )

    media_id = str(
        (published.json() or {}).get("id") or ""
    ).strip()

    if not media_id:

        raise InstagramPublishError(
            "Instagram did not return a published media ID."
        )

    # Step 4: resolve the public permalink.

    permalink = ""

    resolved = requests.get(
        f"{base}/{media_id}",
        params={
            "fields": "permalink",
            "access_token": access_token
        },
        timeout=30
    )

    if resolved.status_code == 200:

        permalink = str(
            (resolved.json() or {}).get("permalink") or ""
        ).strip()

    notify("Published.")

    return {
        "media_id": media_id,
        "container_id": container_id,
        "permalink": permalink
    }
