"""The geocode command: resolve a place name to a search viewport.

A search geocodes its own query, so this command exists for the case the
search does not cover: getting the viewport for a place so it can be used
somewhere else -- as ``--north/--south/--east/--west`` on another command,
or in a script of your own.
"""

from typing import Annotated

import typer

from pyzill.cli.console import error, write_output
from pyzill.cli.options import FormatOpt, OutputOpt
from pyzill.cli.output import OutputFormat
from pyzill.geocode import GeocodeError, GeocodeUnavailableError
from pyzill.geocode import geocode as geocode_place


def geocode(
    place: Annotated[str, typer.Argument(help="Place name, ZIP code, or address")],
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Resolve a place name to the bounding box a Zillow search uses."""
    if output_format is not OutputFormat.json:
        error("The geocode command only supports JSON format", code=2)
    if not place.strip():
        error("Place name must not be empty", code=2)
    try:
        result = geocode_place(place)
    except GeocodeUnavailableError as exc:
        # The service could not be reached: trying again might work.
        error(str(exc), code=6, error_code="network", retryable=True)
    except GeocodeError as exc:
        error(str(exc), code=1)
    payload = {
        "place": place,
        "display_name": result.display_name,
        "north": result.bbox.north,
        "south": result.bbox.south,
        "east": result.bbox.east,
        "west": result.bbox.west,
        "synthetic_bbox": result.synthetic_bbox,
    }
    write_output(payload, OutputFormat.json, output)


def register(app: typer.Typer) -> None:
    """Register the geocode command on the root app."""
    app.command("geocode")(geocode)
