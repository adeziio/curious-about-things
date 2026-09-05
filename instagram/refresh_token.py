"""
Instagram token helper.

One-time setup (Meta developer console):
  1. Create an app (Business type) at developers.facebook.com and add
     the Instagram Graph API product.
  2. Link your Instagram Business/Creator account to a Facebook Page.
  3. Generate a long-lived user token (Graph API Explorer -> generate
     access token with instagram_basic + instagram_content_publish +
     pages_show_list, then exchange it for a long-lived token).
  4. Put the values into .env:
       instagram_access_token=...
       instagram_user_id=...        (the IG professional account ID)

   App credentials are NOT needed when config/instagram.json points
   at graph.instagram.com (Instagram Login). They are only required
   if you switch to graph.facebook.com (Facebook Login):
       instagram_app_id=...
       instagram_app_secret=...

Rotating the token before it expires (~60 days):
  python -m instagram.refresh_token

This exchanges the current still-valid long-lived token for a fresh
one and writes it back to .env automatically.
"""

import argparse

import json

import sys

from pathlib import Path

from instagram.auth import (
    InstagramAuthError,
    refresh_long_lived_token,
    validate_account
)

from instagram.config import (
    get_account,
    load_instagram_config
)

LF = chr(10)


def _update_env_value(
    env_path,
    key,
    value
):
    text = (
        env_path.read_text(encoding="utf-8")
        if env_path.exists()
        else ""
    )

    lines = text.splitlines()

    updated = False

    new_lines = []

    for line in lines:

        stripped = line.strip()

        if stripped.startswith(
            f"{key}="
        ) or stripped.startswith(
            f"{key} ="
        ):

            new_lines.append(
                f"{key}={value}"
            )

            updated = True

        else:

            new_lines.append(line)

    if not updated:

        if new_lines and new_lines[-1].strip():

            new_lines.append("")

        new_lines.append(
            f"{key}={value}"
        )

    env_path.write_text(
        LF.join(new_lines) + LF,
        encoding="utf-8"
    )


def refresh_and_save():

    account = get_account(
        load_instagram_config()
    )

    env_path = (
        Path(__file__)
        .resolve()
        .parent.parent
        / ".env"
    )

    print("Refreshing Instagram long-lived token…")

    token = refresh_long_lived_token(
        account.get("app_id"),
        account.get("app_secret"),
        account.get("access_token")
    )

    _update_env_value(
        env_path,
        "instagram_access_token",
        token
    )

    print(
        "New token saved to .env "
        "(instagram_access_token)."
    )

    user_id = account.get("user_id")

    if user_id:

        identity = str(
            validate_account(
                token,
                user_id
            )
        )

        # validate_account falls back to the numeric account ID when
        # the graph version no longer exposes usernames; only prefix
        # an @ when it is an actual username.

        if identity.isdigit():

            print(f"Token verified for Instagram account {identity}.")

        else:

            print(f"Token verified for Instagram account: @{identity}")


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Refresh the Instagram long-lived access token and save "
            "it to .env. Uses the Instagram refresh endpoint when "
            "config points at graph.instagram.com; otherwise "
            "requires instagram_app_id and instagram_app_secret in "
            ".env. The current token must not have expired yet."
        )
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable output."
    )

    args = parser.parse_args()

    try:

        refresh_and_save()

        if args.json:

            print(
                json.dumps(
                    {
                        "success": True
                    }
                )
            )

        return 0

    except (
        InstagramAuthError,
        RuntimeError
    ) as error:

        message = str(error)

        if args.json:

            print(
                json.dumps(
                    {
                        "success": False,
                        "error": message
                    }
                ),
                file=sys.stderr
            )

        else:

            print(f"ERROR: {message}", file=sys.stderr)

        return 1


if __name__ == "__main__":

    raise SystemExit(main())
