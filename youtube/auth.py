import requests


TOKEN_URL = "https://oauth2.googleapis.com/token"

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class YouTubeAuthError(RuntimeError):

    pass


def refresh_access_token(
    client_id,
    client_secret,
    refresh_token,
    timeout=30
):

    """
    Exchanges a long-lived refresh token for a short-lived access
    token using the Google OAuth token endpoint.
    """

    if not client_id or not client_secret or not refresh_token:

        raise YouTubeAuthError(
            "Client ID, Client Secret and Refresh Token "
            "are all required to obtain an access token."
        )

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token
        },
        timeout=timeout
    )

    if response.status_code != 200:

        raise YouTubeAuthError(
            _token_error_text(response)
        )

    payload = response.json()

    access_token = (
        payload.get("access_token") or ""
    ).strip()

    if not access_token:

        raise YouTubeAuthError(
            "OAuth token response did not contain an access token."
        )

    return access_token


def resolve_access_token(
    account,
    timeout=30
):

    """
    Always obtains a fresh short-lived access token from the required
    client credentials and refresh token.
    """

    account = account or {}

    return refresh_access_token(
        str(account.get("client_id") or "").strip(),
        str(account.get("client_secret") or "").strip(),
        str(account.get("refresh_token") or "").strip(),
        timeout=timeout
    )


def _token_error_text(response):

    try:

        payload = response.json()

        detail = (
            payload.get("error_description")
            or
            payload.get("error")
            or
            ""
        )

    except Exception:

        detail = ""

    text = (
        "Token endpoint returned HTTP "
        f"{response.status_code}. {detail}"
    )

    return text.strip()
