import requests


class OllamaProvider:

    def __init__(
        self,
        config
    ):

        language_config = (
            config["ai_models"]
            ["models"]
            ["language_model"]
        )

        self.url = (
            language_config.get(
                "url",
                "http://localhost:11434"
            )
        )

        self.model = (
            language_config.get(
                "model",
                ""
            )
        )

        self.timeout = (
            language_config.get(
                "timeout",
                900
            )
        )

        self.thinking = (
            language_config.get(
                "thinking",
                False
            )
        )

        self.options = (
            language_config.get(
                "options",
                {}
            )
        )

    def generate(
        self,
        prompt,
        response_format=None
    ):

        self.log(
            f"Sending prompt to Ollama: "
            f"{self.model}"
        )

        request_data = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "think": self.thinking
        }

        if isinstance(
            self.options,
            dict
        ) and self.options:

            request_data["options"] = dict(
                self.options
            )

        if response_format:

            request_data["format"] = (
                response_format
            )

        try:

            response = requests.post(

                f"{self.url}/api/generate",

                json=request_data,

                timeout=self.timeout

            )

            response.raise_for_status()

        except requests.exceptions.Timeout:

            self.log(
                "Ollama request timed out."
            )

            raise RuntimeError(
                "Ollama request timed out after "
                f"{self.timeout} seconds."
            )

        except requests.exceptions.RequestException as error:

            self.log(
                f"Ollama request failed: {error}"
            )

            raise RuntimeError(
                f"Ollama request failed: {error}"
            )

        data = response.json()

        result = (
            data.get(
                "response",
                ""
            )
        )

        if not isinstance(
            result,
            str
        ):

            return ""

        result = result.strip()

        self.log(
            "Ollama response received."
        )

        return result

    def log(
        self,
        message
    ):

        print(
            f"[OLLAMA] {message}"
        )
