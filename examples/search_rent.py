import json
import os
import sys
from pathlib import Path

import pyzill


def main(output_path: str) -> None:
    """Run a room search and write the raw response to ``output_path``."""
    results = pyzill.for_rent(
        search_value="12305",
        is_entire_place=False,
        is_room=True,
        min_beds=1,
        proxy_url=os.environ.get("ZILLOW_PROXY"),
    )
    Path(output_path).write_text(json.dumps(results))


if __name__ == "__main__":
    output_args = sys.argv[1:]
    if not output_args:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} OUTPUT_PATH")
    main(output_args[0])
