# Development

## Setup and checks

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
scripts/check.sh        # the whole gate: format, lint, types, dead code, tests
scripts/check.sh --fix  # the same, but format and fix what can be fixed
```

The gate runs the formatter, the linter, `mypy --strict`, the dead-code
check, and the offline test suite, and fails on the first gate that fails.
CI runs the same script on every push and pull request.

The `@pytest.mark.live` smoke checks are skipped by default. To opt
in, set `PYZILL_LIVE=1`. They hit a fixed Schenectady viewport for
sale/rent/sold search, resolve a zpid from the sale search for the home-detail
check, and exercise the live Nominatim geocode path once.

The rest of the suite never reaches the network, and `tests/conftest.py`
enforces it: a test that calls out without stubbing the transport fails
at the end of the test. Stub it with the `fake_http` fixture, or mark the
test `live`.

## Keeping up with Zillow's payload

Zillow's payloads are not committed: a capture holds real listing data and
stays local. What *is* committed is the **shape** of a capture -- the set of
key paths in it, with no values -- under `tests/shapes/`. The shapes are the
contract between Zillow and the parsers: a field Zillow renames fails
`tests/test_shapes.py` instead of quietly filling a column with nulls.

A capture stays on the machine that made it, so CI cannot run the drift check
against one. Instead, `scripts/build_synthetic_fixtures.py` turns each shape
back into a payload with exactly that shape under
`tests/fixtures/synthetic/`; every leaf is `null`, so the payloads carry no
listing data. A test keeps them in step with the baselines.

To refresh a shape, capture a payload, rebuild the synthetic payloads, and
commit both the shape and the rebuilt payload:

```bash
python scripts/capture_fixture.py search-sale --confirm
python scripts/build_synthetic_fixtures.py
git diff tests/shapes/ tests/fixtures/synthetic/   # read this: the payload changed
```

`python scripts/build_synthetic_fixtures.py --check` exits non-zero when the
committed synthetic payloads are stale.

