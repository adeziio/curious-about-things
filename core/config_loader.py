import json

from pathlib import Path


class ConfigLoader:

    def __init__(
        self,
        config_directory="config"
    ):

        self.config_directory = Path(
            config_directory
        )

    def load(
        self,
        filename
    ):

        file_path = (
            self.config_directory
            /
            filename
        )

        if not file_path.exists():

            raise FileNotFoundError(
                f"Config file missing: {file_path}"
            )

        with open(
            file_path,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(
                file
            )

    def load_optional(
        self,
        filename
    ):

        file_path = (
            self.config_directory
            /
            filename
        )

        if not file_path.exists():

            return None

        with open(
            file_path,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(
                file
            )

    def load_all(
        self
    ):

        config = {

            "content":
                self.load(
                    "content.json"
                ),

            "ai_models":
                self.load(
                    "ai_models.json"
                ),

            "app":
                self.load(
                    "app.json"
                ),

        }

        # Provider configs are optional - a provider that is not
        # installed/selected does not need a config file, but one
        # exists for every provider the project ships with.

        pexels = (
            self.load_optional(
                "pexels.json"
            )
        )

        if pexels is not None:

            config["pexels"] = (
                pexels
            )

        freesafemusic = (
            self.load_optional(
                "freesafemusic.json"
            )
        )

        if freesafemusic is not None:

            config["freesafemusic"] = (
                freesafemusic
            )

        return config