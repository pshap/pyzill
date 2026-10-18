_HTTP_TOO_MANY_REQUESTS = 429


class ZillowError(Exception):
    """Base class for Zillow request and response failures."""

    code = "other"
    retryable = False


class ZillowParseError(ZillowError):
    """A successful page response did not contain the expected data shape."""

    code = "schema_drift"


class ZillowNotFoundError(ZillowError):
    """Zillow returned HTTP 404 for a requested resource."""

    code = "not_found"
    retryable = False

    def __init__(self, status_code: int, url: str, body_snippet: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.body_snippet = body_snippet[:500]
        super().__init__(f"Zillow returned HTTP {status_code} for {url}")


class ZillowBlockedError(ZillowError):
    """Raised when Zillow responds with a non-2xx status (bot-block / PerimeterX)."""

    code = "blocked"

    def __init__(self, status_code: int, url: str, body_snippet: str = ""):
        self.status_code = status_code
        self.retryable = status_code == _HTTP_TOO_MANY_REQUESTS
        self.url = url
        self.body_snippet = body_snippet[:500]
        lowered = self.body_snippet.lower()
        hint = ""
        if "px-captcha" in lowered or "perimeterx" in lowered:
            hint = " Zillow PerimeterX detected the request. A residential proxy is required for search endpoints."
        super().__init__(f"Zillow returned HTTP {status_code} for {url}.{hint} Body starts with: {self.body_snippet!r}")


class ZillowServerError(ZillowError):
    """Zillow returned a server-side HTTP error after retries."""

    code = "server_error"
    retryable = True

    def __init__(self, status_code: int, url: str, body_snippet: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.body_snippet = body_snippet[:500]
        super().__init__(f"Zillow returned HTTP {status_code} for {url}. Body starts with: {self.body_snippet!r}")


class ZillowNetworkError(ZillowError):
    """A transport failure persisted after the configured request retries."""

    code = "network"
    retryable = True

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"Network request failed for {url}")
