import argparse

import threading

import webbrowser

from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer
)

from pathlib import Path

from urllib.parse import (
    parse_qs,
    urlencode,
    urlparse
)

import requests

from dotenv import (
    load_dotenv,
    set_key
)

from youtube.config import (
    load_youtube_config
)


TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"

DEFAULT_SCOPE = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)

DEFAULT_PORT = 8080

WAIT_TIMEOUT_SECONDS = 240


def project_root():

    return Path(__file__).resolve().parent.parent


class AuthRedirectHandler(
    BaseHTTPRequestHandler
):

    def do_GET(
        self
    ):

        parsed = urlparse(
            self.path
        )

        params = parse_qs(
            parsed.query
        )

        self.server.auth_code = (
            params.get(
                "code",
                [None]
            )[0]
        )

        self.server.auth_error = (
            params.get(
                "error",
                [None]
            )[0]
        )

        self.server.done.set()

        message = (
            "Authorization received. You can close this tab "
            "and return to the terminal."
        )

        body = (
            f"<html><body><h3>{message}</h3></body></html>"
        ).encode(
            "utf-8"
        )

        self.send_response(200)

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

    def log_message(
        self,
        format,
        *args
    ):

        return


def build_authorization_url(
    client_id,
    redirect_uri,
    scope
):

    return (
        AUTH_ENDPOINT
        +
        "?"
        +
        urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": scope,
                "access_type": "offline",
                "prompt": "consent"
            }
        )
    )


def run_authorization_flow(
    client_id,
    client_secret,
    port,
    scope
):

    redirect_uri = (
        f"http://localhost:{port}/"
    )

    auth_url = build_authorization_url(
        client_id,
        redirect_uri,
        scope
    )

    done = threading.Event()

    try:

        server = ThreadingHTTPServer(
            (
                "127.0.0.1",
                port
            ),
            AuthRedirectHandler
        )

    except OSError as error:

        raise SystemExit(
            f"[OAUTH] Could not bind port {port}: {error}"
        )

    server.auth_code = None

    server.auth_error = None

    server.done = done

    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True
    )

    server_thread.start()

    print(
        "[OAUTH] Opening the browser to authorize the channel..."
    )

    print(
        f"[OAUTH] {auth_url}"
    )

    webbrowser.open(
        auth_url
    )

    done.wait(
        timeout=WAIT_TIMEOUT_SECONDS
    )

    server.shutdown()

    server.server_close()

    server_thread.join(
        timeout=5
    )

    if server.auth_error:

        raise SystemExit(
            f"[OAUTH] Authorization failed: {server.auth_error}"
        )

    if not server.auth_code:

        raise SystemExit(
            "[OAUTH] No authorization code was received "
            "(timed out after "
            f"{WAIT_TIMEOUT_SECONDS} seconds?)."
        )

    return exchange_code(
        client_id,
        client_secret,
        redirect_uri,
        server.auth_code
    )


def exchange_code(
    client_id,
    client_secret,
    redirect_uri,
    code
):

    response = requests.post(
        TOKEN_ENDPOINT,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"
        },
        timeout=30
    )

    if response.status_code != 200:

        raise SystemExit(
            "[OAUTH] Token exchange failed: "
            f"HTTP {response.status_code} "
            f"{response.text[:300]}"
        )

    return response.json()


def save_account_to_config(
    client_id,
    client_secret,
    refresh_token
):

    env_path = (
        Path(__file__)
        .resolve()
        .parent
        .parent
        /
        ".env"
    )

    set_key(
        str(env_path),
        "youtube_client_id",
        client_id
    )

    set_key(
        str(env_path),
        "youtube_client_secret",
        client_secret
    )

    set_key(
        str(env_path),
        "youtube_refresh_token",
        refresh_token
    )

    print(
        "[OAUTH] Saved OAuth credentials "
        f"in {env_path}"
    )


def main():

    env_path = project_root() / ".env"

    load_dotenv(
        env_path,
        override=False
    )

    parser = argparse.ArgumentParser(
        description=(
            "Grab YouTube OAuth tokens (access + refresh) for the "
            "upload feature."
        )
    )

    parser.add_argument(
        "--client-id",
        default="",
        help="OAuth 2.0 client id (xxx.apps.googleusercontent.com)."
    )

    parser.add_argument(
        "--client-secret",
        default="",
        help="OAuth 2.0 client secret."
    )

    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=(
            "Local port the browser redirects to "
            f"(default {DEFAULT_PORT})."
        )
    )

    parser.add_argument(
        "--scope",
        default="",
        help=(
            "OAuth scopes, space separated (default: the scopes in "
            "config/youtube.json)."
        )
    )

    args = parser.parse_args()

    config = load_youtube_config()

    account = config.get("account") or {}

    client_id = str(
        args.client_id
        or account.get("client_id")
        or ""
    ).strip()

    client_secret = str(
        args.client_secret
        or account.get("client_secret")
        or ""
    ).strip()

    api = config.get("api") or {}

    scopes = list(
        api.get("scope")
        or []
    )

    scope = str(
        args.scope
        or " ".join(scopes)
    ).strip() or DEFAULT_SCOPE

    if not client_id or not client_secret:

        raise SystemExit(
            "[OAUTH] Client ID and Client Secret were not found. "
            "Add youtube_client_id and youtube_client_secret to "
            "your .env, put them in config/youtube.json, "
            "or pass --client-id/--client-secret."
        )

    tokens = run_authorization_flow(
        client_id,
        client_secret,
        args.port,
        scope
    )

    access_token = str(
        tokens.get(
            "access_token",
            ""
        )
    )

    refresh_token = str(
        tokens.get(
            "refresh_token",
            ""
        )
    )

    expires_in = tokens.get(
        "expires_in",
        "?"
    )

    print()

    print(
        "=============== ACCOUNT CREDENTIALS ==============="
    )

    print(
        f"Access Token (valid ~{expires_in} seconds):"
    )

    print(
        access_token
    )

    print(
        "Refresh Token:"
    )

    print(
        refresh_token
    )

    print(
        "==================================================="
    )

    if not refresh_token:

        print(
            "[OAUTH] No refresh token was returned. Make sure the "
            "OAuth client uses loopback (Desktop/Web with a "
            "localhost redirect) and consent was granted."
        )

    save_account_to_config(
            client_id,
            client_secret,
            refresh_token
    )


if __name__ == "__main__":

    main()
