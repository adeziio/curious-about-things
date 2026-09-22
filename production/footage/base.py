from pathlib import Path


class VideoProviderError(
    RuntimeError
):

    """
    Raised for any stock-footage provider failure. The message is
    always safe to surface directly in job state.
    """

    pass


class VideoProvider:

    """
    Base class for stock-video providers.

    A provider searches a stock footage source for a query and
    downloads candidate video files into a local directory:

        search(query)       -> candidate videos
        download(candidate) -> local footage

    Implementations must keep all provider-specific behavior
    (websites, APIs, Selenium flows, file naming) inside their own
    module. The rest of the pipeline only talks to this interface.
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
        query,
        destination_dir,
        max_videos=3,
        downloaded_ids=None,
        downloaded_hashes=None,
    ):

        """
        Searches the provider for `query`, downloads up to
        `max_videos` usable clips into `destination_dir` and
        returns the list of downloaded file paths.

        Implementations should be resume-aware: when
        `destination_dir` already contains `max_videos` complete
        clips from a previous run, nothing new is downloaded and
        the existing clips are returned instead.

        Implementations must raise VideoProviderError when
        `max_videos` clips cannot be delivered - callers fail the
        run instead of continuing with a partial result.

        Uniqueness: `downloaded_ids` (provider video ids) and
        `downloaded_hashes` (SHA-256 of downloaded bytes) are
        optional episode-wide mutable sets shared across queries.
        They are updated in place so later queries never reuse
        content fetched by earlier ones. Hash tracking is what
        catches identical footage served under different ids.
        """

        raise NotImplementedError

    @staticmethod
    def slugify(
        query
    ):

        slug = "".join(
            character
            if character.isalnum()
            else "-"
            for character in query.lower()
        )

        slug = "-".join(
            part
            for part in slug.split("-")
            if part
        )

        return (
            slug[:60]
            or "query"
        )

    @staticmethod
    def is_video_file(
        path
    ):

        return (
            Path(
                path
            )
            .suffix
            .lower()
            in (
                ".mp4",
                ".mov",
                ".webm",
                ".mkv"
            )
        )