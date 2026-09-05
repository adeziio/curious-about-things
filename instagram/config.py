import os

from pathlib import Path

from core.config_loader import ConfigLoader

from dotenv import (
    load_dotenv
)


def _project_root():

    return Path(__file__).resolve().parent.parent


def _environment_value(*names):

    for name in names:

        value = os.getenv(name)

        if value:

            return value.strip()

    return ""


def load_instagram_config():
    """
    Loads the full Instagram configuration from
    config/instagram.json. Secret account values are overridden by
    matching .env variables when present, so credentials never need
    to live in the JSON file.
    """

    load_dotenv(
        _project_root() / ".env",
        override=False
    )

    loader = ConfigLoader(
        _project_root() / "config"
    )

    config = loader.load("instagram.json")

    account = config.setdefault(
        "account",
        {}
    )

    environment_values = {
        "access_token": _environment_value(
            "INSTAGRAM_ACCESS_TOKEN",
            "instagram_access_token"
        ),
        "user_id": _environment_value(
            "INSTAGRAM_USER_ID",
            "instagram_user_id"
        ),
        "app_id": _environment_value(
            "INSTAGRAM_APP_ID",
            "instagram_app_id"
        ),
        "app_secret": _environment_value(
            "INSTAGRAM_APP_SECRET",
            "instagram_app_secret"
        )
    }

    for key, value in environment_values.items():

        if value:

            account[key] = value

    return config


def get_account(config=None):

    config = (
        config
        if config is not None
        else load_instagram_config()
    )

    account = config.get("account") or {}

    if not isinstance(account, dict):

        return {}

    return {
        key: str(account.get(key) or "").strip()
        for key in (
            "access_token",
            "user_id",
            "app_id",
            "app_secret"
        )
    }


def get_api_settings(config=None):
    """
    Returns Graph API connection settings from the api block of the
    config: base URL, API version, polling interval and processing
    timeout.
    """

    config = (
        config
        if config is not None
        else load_instagram_config()
    )

    api = config.get("api") or {}

    env_version = _environment_value(
        "INSTAGRAM_GRAPH_VERSION",
        "instagram_graph_version"
    )

    return {
        "graph_base_url": str(
            api.get("graph_base_url")
            or "https://graph.instagram.com"
        ).strip().rstrip("/"),
        "api_version": str(
            env_version
            or api.get("api_version")
            or "v21.0"
        ).strip(),
        "poll_interval_seconds": float(
            api.get("poll_interval_seconds")
            or 5
        ),
        "processing_timeout_seconds": int(
            api.get("processing_timeout_seconds")
            or 600
        )
    }


def get_caption_defaults(config=None):
    """
    Returns the caption defaults (hashtags) used to prefill the post
    caption for new uploads.
    """

    config = (
        config
        if config is not None
        else load_instagram_config()
    )

    defaults = (
        (
            config.get("metadata")
            or {}
        ).get("defaults")
        or {}
    )

    hashtags = [
        str(tag).strip()
        for tag in (
            defaults.get("default_hashtags")
            or []
        )
        if str(tag).strip()
    ]

    return {
        "default_hashtags": hashtags
    }
