class ZillowBlockedError(Exception):
    """Raised when Zillow responds with a non-2xx status (bot-block / PerimeterX)."""

    def __init__(self, status_code: int, url: str, body_snippet: str = ""):
        self.status_code = status_code
        self.url = url
        self.body_snippet = body_snippet[:500]
        hint = ""
        if "px-captcha" in body_snippet.lower() or "perimeterx" in body_snippet.lower():
            hint = " Zillow PerimeterX detected the request. A residential proxy is required for search endpoints."
        super().__init__(f"Zillow returned HTTP {status_code} for {url}.{hint} Body starts with: {self.body_snippet!r}")
