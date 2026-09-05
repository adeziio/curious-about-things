import os

from pathlib import Path

from core.config_loader import ConfigLoader

from dotenv import (
    load_dotenv
)


def _config_directory():
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "config"


def _project_root():

    return Path(__file__).resolve().parent.parent


def _environment_value(*names):

    for name in names:

        value = os.getenv(name)

        if value:

            return value.strip()

    return ""


def load_youtube_config():

    load_dotenv(
        _project_root() / ".env",
        override=False
    )

    loader = ConfigLoader(_config_directory())
    config = loader.load("youtube.json")

    account = config.setdefault(
        "account",
        {}
    )

    environment_values = {
        "client_id": _environment_value(
            "YOUTUBE_CLIENT_ID",
            "youtube_client_id"
        ),
        "client_secret": _environment_value(
            "YOUTUBE_CLIENT_SECRET",
            "youtube_client_secret"
        ),
        "refresh_token": _environment_value(
            "YOUTUBE_REFRESH_TOKEN",
            "youtube_refresh_token"
        )
    }

    for key, value in environment_values.items():

        if value:

            account[key] = value

    return config


def get_account(config=None):
    config = config if config is not None else load_youtube_config()
    account = config.get("account") or {}
    return account if isinstance(account, dict) else {}


def get_metadata_defaults(config=None):
    config = config if config is not None else load_youtube_config()
    defaults = (config.get("metadata") or {}).get("defaults") or {}
    return dict(defaults)
