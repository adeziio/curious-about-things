import json

from pathlib import Path

import requests

from youtube.auth import (
    resolve_access_token
)

from youtube.channel import (
    resolve_channel_by_name
)


UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024

DEFAULT_TIMEOUT = 300


class YouTubeUploadError(RuntimeError):

    pass


class UploadResult:

    def __init__(
        self,
        video_id,
        video_url,
        title
    ):

        self.video_id = video_id

        self.video_url = video_url

        self.title = title

    def to_dict(self):

        return {
            "video_id": self.video_id,
            "video_url": self.video_url,
            "title": self.title
        }


def upload_short(
    video_path,
    metadata,
    account,
    progress_callback=None,
    chunk_size=DEFAULT_CHUNK_SIZE,
    timeout=DEFAULT_TIMEOUT
):

    """
    Uploads a vertical short-form video using the YouTube Data API v3
    resumable upload protocol.

    No third-party Google libraries are required; the upload is done
    with plain HTTP requests (requests).
    """

    video_path = Path(video_path)

    if not video_path.is_file():

        raise YouTubeUploadError(
            f"Video file not found: {video_path}"
        )

    file_size = video_path.stat().st_size

    if file_size <= 0:

        raise YouTubeUploadError(
            "Video file is empty."
        )

    metadata = dict(metadata or {})

    title = metadata.get("title") or ""

    description = metadata.get("description") or ""

    tags = list(
        metadata.get("tags") or []
    )

    category_id = metadata.get("category_id") or ""

    privacy_status = metadata.get("privacy_status") or ""

    made_for_kids = bool(
        metadata.get("made_for_kids", False)
    )

    access_token = resolve_access_token(account)

    resolve_channel_by_name(
        access_token,
        account.get("channel_name"),
        timeout=30
    )

    upload_uri = _start_upload_session(
        access_token,
        title,
        description,
        tags,
        category_id,
        privacy_status,
        made_for_kids,
        file_size,
        timeout
    )

    """
    Resumable chunked upload. Each non-final chunk gets HTTP 308
    and the next byte range is uploaded until the video is done.
    """

    offset = 0

    with video_path.open("rb") as file:

        while offset < file_size:

            file.seek(offset)

            chunk = file.read(chunk_size)

            if not chunk:

                break

            chunk_end = offset + len(chunk) - 1

            if progress_callback:

                try:

                    progress_callback(
                        offset + len(chunk),
                        file_size,
                        "Uploading video."
                    )

                except Exception:

                    pass

            put_response = requests.put(
                upload_uri,
                data=chunk,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": (
                        f"bytes {offset}-{chunk_end}/{file_size}"
                    )
                },
                timeout=timeout
            )

            if put_response.status_code in (200, 201):

                if progress_callback:

                    try:

                        progress_callback(
                            file_size,
                            file_size,
                            "Upload complete."
                        )

                    except Exception:

                        pass

                return _parse_upload_result(
                    put_response,
                    title
                )

            if put_response.status_code != 308:

                raise YouTubeUploadError(
                    _error_message(
                        put_response,
                        "Upload was interrupted."
                    )
                )

            offset = _next_offset(
                put_response,
                chunk_end + 1
            )

    raise YouTubeUploadError(
        "Upload ended without a final response from YouTube."
    )


def _start_upload_session(
    access_token,
    title,
    description,
    tags,
    category_id,
    privacy_status,
    made_for_kids,
    file_size,
    timeout
):

    """
    Opens the resumable upload session and returns the Location URI
    that receives the video chunks.
    """

    body = json.dumps({
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": made_for_kids
        }
    })

    response = requests.post(
        f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status",
        data=body.encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(file_size)
        },
        timeout=timeout
    )

    if response.status_code != 200:

        raise YouTubeUploadError(
            _error_message(
                response,
                "Could not start the upload session."
            )
        )

    upload_uri = response.headers.get("Location", "")

    if not upload_uri:

        raise YouTubeUploadError(
            "YouTube did not return an upload session URL."
        )

    return upload_uri


def _next_offset(
    response,
    fallback
):

    """
    Follow the server-reported Range header when available so the
    upload resumes from the exact byte YouTube has received.
    """

    range_header = response.headers.get("Range", "")

    if not range_header.startswith("bytes="):

        return fallback

    try:

        received_end = int(
            range_header
            .split("=", 1)[1]
            .split("-", 1)[1]
        )

        return received_end + 1

    except (IndexError, ValueError):

        return fallback


def _parse_upload_result(
    response,
    title
):

    try:

        payload = response.json()

    except Exception:

        raise YouTubeUploadError(
            "YouTube accepted the upload but did not return "
            "the video resource. Check the channel uploads."
        )

    payload = dict(payload or {})

    video_id = str(
        payload.get("id") or ""
    ).strip()

    if not video_id:

        raise YouTubeUploadError(
            "YouTube accepted the upload but did not return "
            "the video ID. Check the channel uploads."
        )

    return UploadResult(
        video_id,
        f"https://youtu.be/{video_id}",
        title
    )


def _error_message(
    response,
    context
):

    try:

        payload = response.json()

        error = dict(payload.get("error") or {})

        messages = []

        code = str(
            error.get("code") or response.status_code
        )

        message = str(
            error.get("message") or ""
        ).strip()

        if message:

            messages.append(message)

        for detail in (error.get("errors") or []):

            reason = str(
                detail.get("reason") or ""
            ).strip()

            if reason:

                messages.append(
                    f"reason: {reason}"
                )

        if messages:

            return (
                f"{context} [HTTP {code}] "
                + " — ".join(messages)
            ).strip()

    except Exception:

        pass

    return (
        f"{context} [HTTP {response.status_code}]"
    )
