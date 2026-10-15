import os


def read_env(name: str) -> str | None:
    """Read a pyzill environment setting."""
    return os.environ.get(name)
