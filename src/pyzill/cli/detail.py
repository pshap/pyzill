"""Detail commands and the validation helpers they use."""

import re
from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer

from pyzill import get_from_apartment_id, get_from_apartment_url, get_from_home_id, get_from_home_url
from pyzill.cli.console import context, debug, emit, error, network_errors, write_output
from pyzill.cli.options import FormatOpt, KeysOpt, OutputOpt, PathOpt
from pyzill.cli.output import OutputFormat

detail_app = typer.Typer(name="detail", help="Fetch property details")


def home_detail_target(value: str) -> tuple[str, int | str]:
    """Return the target kind and normalized value for a home detail command."""
    cleaned = value.strip()
    match = re.fullmatch(r"(\d+)(?:_zpid)?", cleaned)
    if match:
        return "id", int(match.group(1))
    try:
        parsed = urlsplit(cleaned)
        host = parsed.hostname or ""
    except ValueError as exc:
        raise ValueError("Home detail input must be a zpid or an HTTPS Zillow URL") from exc
    if parsed.scheme == "https" and (host == "zillow.com" or host.endswith(".zillow.com")):
        return "url", cleaned
    raise ValueError("Home detail input must be a zpid or an HTTPS Zillow URL")


def _path_segments(path: str) -> list[str | int]:
    """Split a dotted path into keys and list indices ('a.b[0].c' -> a, b, 0, c)."""
    segments: list[str | int] = []
    for part in path.split("."):
        name, _, rest = part.partition("[")
        if name:
            segments.append(name)
        while rest:
            rest = rest.removeprefix("[")
            index, close, rest = rest.partition("]")
            if not close or not index.isdigit():
                error(f"Bad --path {path!r}: expected [n] list indices", code=2)
            segments.append(int(index))
    if not segments:
        error("--path must not be empty", code=2)
    return segments


def _extract_path(data: Any, path: str) -> Any:
    """Walk a dotted path through nested dicts/lists; error when it goes missing."""
    current = data
    walked: list[str] = []
    for segment in _path_segments(path):
        label = f"[{segment}]" if isinstance(segment, int) else segment
        if isinstance(segment, int):
            ok = isinstance(current, list) and segment < len(current)
        else:
            ok = isinstance(current, dict) and segment in current
        if not ok:
            here = ".".join(walked) or "(root)"
            error(f"--path {path!r} not found: no {label!r} at {here}", code=1)
        current = current[segment]
        walked.append(str(label))
    return current


def _emit_keys(data: Any, output: str | None = None) -> None:
    """Print 'key: type' lines for a detail record (or the dict at --path)."""
    if not isinstance(data, dict):
        error(f"--keys needs an object (got {type(data).__name__}); try --path to reach one", code=2)
    width = max((len(key) for key in data), default=0)
    lines = [f"{key.ljust(width)}  {type(value).__name__}" for key, value in data.items()]
    emit("\n".join(lines), output)


def run_detail(  # noqa: PLR0913
    fetch: Callable[..., Any],
    value: str | int,
    output_format: OutputFormat,
    output: str | None,
    keys: bool = False,
    path: str | None = None,
) -> None:
    """Everything the home/apartment detail commands have in common."""
    if output_format is not OutputFormat.json:
        error("Detail commands only support JSON format", code=2)
    if keys and path:
        error("--keys and --path can't be used together", code=2)

    http_kwargs = context().http_kwargs
    debug(f"{fetch.__name__}: {value!r}")

    with network_errors("Detail fetch failed"):
        result = fetch(value, **http_kwargs)

    if path:
        write_output(_extract_path(result, path), output_format, output)
    elif keys:
        _emit_keys(result, output)
    else:
        write_output(result, output_format, output)


@detail_app.command("home")
def detail_home(
    url_or_id: Annotated[str, typer.Argument(help="Zillow home URL or zpid")],
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
    keys: KeysOpt = False,
    path: PathOpt = None,
) -> None:
    """Fetch home details by URL or zpid."""
    try:
        target_kind, target = home_detail_target(url_or_id)
    except ValueError as exc:
        error(str(exc), code=2)
    fetch = get_from_home_id if target_kind == "id" else get_from_home_url
    run_detail(fetch, target, output_format, output, keys=keys, path=path)


@detail_app.command("apartment")
def detail_apartment(
    url_or_id: Annotated[str, typer.Argument(help="Zillow apartment URL or apartment ID")],
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
    keys: KeysOpt = False,
    path: PathOpt = None,
) -> None:
    """Fetch apartment details by URL or apartment ID."""
    value = url_or_id.strip()
    fetch = get_from_apartment_url if value.startswith("http") else get_from_apartment_id
    run_detail(fetch, value, output_format, output, keys=keys, path=path)
