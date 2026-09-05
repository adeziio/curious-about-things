from production.footage.base import (
    VideoProvider,
    VideoProviderError
)


def create_video_provider(
    config,
    notify=None
):

    """
    Factory that resolves the active stock-video provider from
    the generic configuration value:

        { "video_provider": "pexels" }

    All provider-specific settings live under the provider's own
    config section (e.g. config/pexels.json). The video-generation
    pipeline only works with the generic VideoProvider interface,
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
            "video_provider",
            "pexels"
        )
    ).strip().lower()

    if name == "pexels":

        from production.footage.pexels import (
            PexelsVideoProvider
        )

        return PexelsVideoProvider(
            config,
            notify=notify
        )

    raise VideoProviderError(
        f"Unknown video provider: {name}. "
        "Check video_provider in config/app.json."
    )