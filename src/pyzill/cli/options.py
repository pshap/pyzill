"""Shared Typer option/argument types, declared once and reused by commands."""

from typing import Annotated

import typer

from pyzill.cli.output import OutputFormat

Query = Annotated[str, typer.Argument(help="Search query: place name (geocoded), ZIP, or address")]
North = Annotated[float | None, typer.Option("--north", help="Bounding box north latitude")]
South = Annotated[float | None, typer.Option("--south", help="Bounding box south latitude")]
East = Annotated[float | None, typer.Option("--east", help="Bounding box east longitude")]
West = Annotated[float | None, typer.Option("--west", help="Bounding box west longitude")]
Zoom = Annotated[int | None, typer.Option("--zoom", help="Map zoom level")]
MinBeds = Annotated[int | None, typer.Option("--min-beds", help="Minimum bedrooms")]
MaxBeds = Annotated[int | None, typer.Option("--max-beds", help="Maximum bedrooms")]
MinBaths = Annotated[int | None, typer.Option("--min-bathrooms", help="Minimum bathrooms")]
MaxBaths = Annotated[int | None, typer.Option("--max-bathrooms", help="Maximum bathrooms")]
MinPrice = Annotated[int | None, typer.Option("--min-price", help="Minimum price")]
MaxPrice = Annotated[int | None, typer.Option("--max-price", help="Maximum price")]
Tile = Annotated[bool, typer.Option("--tile", help="Recursively split capped map results")]
MaxDepth = Annotated[
    int,
    typer.Option("--max-depth", min=0, max=6, help="Maximum recursive tile depth (default: 4, max: 6)"),
]
MaxRequests = Annotated[
    int,
    typer.Option(
        "--max-requests",
        min=1,
        help="Maximum search requests when tiling (default: 50; capped by PYZILL_MAX_REQUESTS, unset = no cap)",
    ),
]
Raw = Annotated[bool, typer.Option("--raw", help="Return raw search response (not normalized)")]
IsRoom = Annotated[bool, typer.Option("--room", help="Filter for rooms for rent")]
IsEntirePlace = Annotated[bool, typer.Option("--entire-place", help="Filter for entire places for rent")]
FormatOpt = Annotated[
    OutputFormat,
    typer.Option("--format", "-f", help="Output format (default: json)"),
]
SummaryFormatOpt = Annotated[OutputFormat | None, typer.Option("--format", "-f")]
OutputOpt = Annotated[
    str | None,
    typer.Option("--output", "-o", help="Output file (default: stdout)"),
]
WhereOpt = Annotated[
    list[str] | None,
    typer.Option("--where", help="Filter: FIELD OP VALUE (repeatable; OP: >= <= > < == != ~)"),
]
WhereJSONOpt = Annotated[
    str | None,
    typer.Option("--where-json", help="Filter using a JSON array of {field,op,value} objects"),
]
SortOpt = Annotated[str | None, typer.Option("--sort", help="Sort by FIELD or FIELD:asc / FIELD:desc")]
ColsOpt = Annotated[str | None, typer.Option("--cols", help="Comma-separated columns to output")]
LimitOpt = Annotated[int | None, typer.Option("--limit", min=1, help="Maximum rows to output")]
MaxAgeOpt = Annotated[
    int | None,
    typer.Option("--max-age", help="Only enrich rows not enriched within this many days (default: 7)"),
]
RefreshOpt = Annotated[
    bool,
    typer.Option("--refresh", help="Ignore --max-age and re-enrich every shortlisted listing"),
]
PlanOpt = Annotated[
    bool,
    typer.Option("--plan", help="Show what the command would do without executing it"),
]
KeysOpt = Annotated[bool, typer.Option("--keys", help="List available fields instead of dumping the record")]
PathOpt = Annotated[
    str | None,
    typer.Option("--path", help="Extract one field by dotted path, e.g. 'address.city' or 'priceHistory[0].price'"),
]
DbPathOpt = Annotated[
    str | None,
    typer.Option("--db", help="Database path (default: $XDG_DATA_HOME/pyzill/pyzill.db or PYZILL_DB env)"),
]
