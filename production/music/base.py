class MusicProviderError(RuntimeError):
    """
    Raised for any background-music provider failure. The message is
    always safe to surface directly in job state.
    """

    pass


class MusicProvider:
    """
    Base class for background-music providers.

    A provider selects a suitable track for the episode's mood,
    downloads it into the episode's music/ directory and returns
    the track metadata. The track file is temporary - the caller
    is responsible for deleting it after the video is rendered.

    Implementations must keep all provider-specific behavior
    (websites, APIs, licensing checks, file naming) inside their
    own module. The rest of the pipeline only talks to this
    interface, so additional providers can be added later without
    touching the video pipeline.
    """

    name = "base"

    def __init__(
        self,
        config,
        notify=None
    ):

        self.config = config

        self._notify_callback = notify

    def notify(
        self,
        message
    ):

        if self._notify_callback is None:

            return

        try:

            self._notify_callback(
                str(
                    message
                )
            )

        except Exception:

            pass

    def fetch(
        self,
        mood_tags,
        destination_dir
    ):

        """
        Selects and downloads a track suitable for the given mood
        tags (a list of lowercase words, possibly empty) into
        destination_dir. Returns a metadata dict:

            {
                "provider": "...",
                "title": "...",
                "artist": "...",
                "source_url": "...",
                "license": "...",
                "tags": [...],
                "file": "path/to/track.mp3"
            }

        Returns None when no suitable track was found - callers
        must handle that gracefully (music is optional).
        """

        raise NotImplementedError
