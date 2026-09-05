from production.music.base import (
    MusicProvider,
    MusicProviderError
)


def create_music_provider(
    config,
    notify=None
):

    """
    Factory that resolves the active background-music provider
    from the generic configuration value:

        { "music_provider": "freesafemusic" }

    All provider-specific settings live under the provider's own
    config section (e.g. config/freesafemusic.json). The video
    pipeline only works with the generic MusicProvider interface,
    so additional providers can be added without touching it.
    """

    app_config = (
        config.get(
            "app",
            {}
        )
    )

    name = str(
        app_config.get(
            "music_provider",
            "freesafemusic"
        )
    ).strip().lower()

    if name == "freesafemusic":

        from production.music.freesafemusic import (
            FreeSafeMusicProvider
        )

        return FreeSafeMusicProvider(
            config,
            notify=notify
        )

    raise MusicProviderError(
        f"Unknown music provider: {name}. "
        "Check music_provider in config/app.json."
    )
