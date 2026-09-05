import requests


CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


class YouTubeChannelError(RuntimeError):

    pass


def resolve_channel_by_name(
    access_token,
    channel_name,
    timeout=30
):

    """Resolve an exact human-readable channel name to its ID."""

    channel_name = str(channel_name or "").strip()

    if not channel_name:

        raise YouTubeChannelError(
            "Channel Name is required."
        )

    response = requests.get(
        CHANNELS_URL,
        params={
            "part": "id,snippet",
            "mine": "true"
        },
        headers={
            "Authorization": f"Bearer {access_token}"
        },
        timeout=timeout
    )

    if response.status_code != 200:

        if response.status_code == 403:

            raise YouTubeChannelError(
                "Could not look up the authenticated YouTube channel. "
                "The OAuth token is missing the youtube.readonly scope. "
                "Run python -m youtube.refresh_token again and approve the "
                "new permissions."
            )

        raise YouTubeChannelError(
            _error_message(
                response,
                "Could not look up the authenticated YouTube channel."
            )
        )

    payload = response.json()

    matches = [
        item
        for item in payload.get("items", [])
        if str(
            (item.get("snippet") or {}).get("title") or ""
        ).strip().casefold() == channel_name.casefold()
    ]

    if not matches:

        raise YouTubeChannelError(
            f"No authenticated YouTube channel named '{channel_name}' "
            "was found. Check the spelling and OAuth account."
        )

    if len(matches) > 1:

        raise YouTubeChannelError(
            f"More than one authenticated channel is named "
            f"'{channel_name}'. A unique Channel Name is required."
        )

    channel_id = str(
        matches[0].get("id") or ""
    ).strip()

    if not channel_id:

        raise YouTubeChannelError(
            "YouTube returned the channel name without a channel ID."
        )

    return {
        "channel_id": channel_id,
        "channel_name": str(
            (matches[0].get("snippet") or {}).get("title") or ""
        ).strip()
    }


def _error_message(
    response,
    context
):

    try:

        payload = response.json()

        error = payload.get("error") or {}

        detail = str(
            error.get("message") or ""
        ).strip()

    except Exception:

        detail = ""

    return (
        f"{context} [HTTP {response.status_code}] "
        f"{detail}"
    ).strip()
