import requests

from instagram.config import (
    get_account,
    get_api_settings,
    load_instagram_config
)


class InstagramAuthError(RuntimeError):

    pass


def _graph_request(
    method,
    path,
    params,
    timeout=30
):
    """
    Performs a Graph API request using the connection settings from
    config/instagram.json.
    """

    settings = get_api_settings()

    url = (
        f"{settings['graph_base_url']}/"
        f"{settings['api_version']}/{path}"
    )

    if method == "GET":

        return requests.get(
            url,
            params=params,
            timeout=timeout
        )

    return requests.post(
        url,
        data=params,
        timeout=timeout
    )


def _uses_instagram_login_host():

    """
    True when the configured Graph host is graph.instagram.com (the
    Instagram API with Instagram Login), whose token lifecycle differs
    from the Facebook-Login flow served by graph.facebook.com.
    """

    settings = get_api_settings()

    return "graph.instagram" in settings["graph_base_url"]


def validate_account(
    access_token,
    ig_user_id,
    timeout=30
):
    """
    Confirms the token is valid and can reach the given Instagram
    account. The `username` field is deprecated on user nodes under
    newer Graph API versions, so the primary check uses the stable
    `id` field; the username is fetched only as a best-effort and
    never raises if unavailable.
    """

    if not access_token:

        raise InstagramAuthError(
            "Instagram access token is required."
        )

    if not ig_user_id:

        raise InstagramAuthError(
            "Instagram User ID is required."
        )

    node = str(ig_user_id).lstrip("/")

    response = _graph_request(
        "GET",
        node,
        {
            "fields": "id",
            "access_token": access_token
        },
        timeout=timeout
    )

    if response.status_code != 200:

        raise InstagramAuthError(
            _error_text(response)
        )

    account_id = str(
        (response.json() or {}).get("id")
        or node
    ).strip()

    # Best-effort username for friendly logging. Some graph versions /
    # node types no longer expose `username`, which must not fail
    # the whole validation.

    username = ""

    try:

        uname_response = _graph_request(
            "GET",
            node,
            {
                "fields": "username",
                "access_token": access_token
            },
            timeout=timeout
        )

        if uname_response.status_code == 200:

            username = str(
                (uname_response.json() or {}).get("username") or ""
            ).strip()

    except (
        InstagramAuthError,
        requests.RequestException
    ):

        username = ""

    return username or account_id


def refresh_long_lived_token(
    app_id,
    app_secret,
    long_lived_token,
    timeout=30
):
    """
    Exchanges a still-valid long-lived token for a fresh one (~60
    more days). Meta only allows refreshing a token that has not
    expired yet.

    The grant used depends on the configured Graph host:

      - graph.instagram.com (Instagram Login): the dedicated
        refresh_access_token endpoint, which needs no app credentials.
      - graph.facebook.com (Facebook Login): fb_exchange_token, which
        requires the app ID and secret.
    """

    if not long_lived_token:

        raise InstagramAuthError(
            "The current access token is required to refresh."
        )

    if _uses_instagram_login_host():

        response = _graph_request(
            "GET",
            "refresh_access_token",
            {
                "grant_type": "ig_refresh_token",
                "access_token": long_lived_token
            },
            timeout=timeout
        )

    else:

        if not app_id or not app_secret:

            raise InstagramAuthError(
                "App ID and App Secret are required to refresh "
                "when using the Facebook Graph host "
                "(graph.facebook.com)."
            )

        response = _graph_request(
            "GET",
            "oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": long_lived_token
            },
            timeout=timeout
        )

    if response.status_code != 200:

        raise InstagramAuthError(
            _error_text(response)
        )

    token = str(
        (response.json() or {}).get("access_token") or ""
    ).strip()

    if not token:

        raise InstagramAuthError(
            "Token refresh response did not contain "
            "an access token."
        )

    return token


def _error_text(response):

    try:

        payload = response.json()

    except ValueError:

        payload = {}

    error = (
        payload.get("error")
        if isinstance(payload, dict)
        else None
    ) or {}

    message = (
        error.get("message")
        or f"HTTP {response.status_code}"
    )

    code = error.get("code")

    subcode = error.get("error_subcode")

    suffix = ""

    if code:

        suffix += f" [code {code}]"

    if subcode:

        suffix += f" [subcode {subcode}]"

    return (
        f"Instagram API error: {message}{suffix}"
    )


def resolve_account(account=None, timeout=30):
    """
    Resolves credentials from an explicit account dictionary or from
    config/instagram.json + .env, then validates them. Returns
    (access_token, user_id, username).
    """

    config = get_account(
        load_instagram_config()
    )

    account = account or {}

    access_token = str(
        account.get("access_token")
        or config.get("access_token")
        or ""
    ).strip()

    user_id = str(
        account.get("ig_user_id")
        or account.get("user_id")
        or config.get("user_id")
        or ""
    ).strip()

    username = validate_account(
        access_token,
        user_id,
        timeout=timeout
    )

    return (
        access_token,
        user_id,
        username
    )
