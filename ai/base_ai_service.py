class BaseAIService:

    def __init__(
        self,
        config,
        log_prefix="SYSTEM"
    ):

        self.config = config

        self.log_prefix = log_prefix

    def log(
        self,
        message
    ):

        print(
            f"[{self.log_prefix}] {message}"
        )
