import copy
import pickle

import pytest

from pyzill.exceptions import (
    ZillowBlockedError,
    ZillowError,
    ZillowNetworkError,
    ZillowNotFoundError,
    ZillowParseError,
    ZillowServerError,
)


@pytest.mark.parametrize(
    "error",
    [
        ZillowError("base failure"),
        ZillowParseError("parse failure"),
        ZillowNotFoundError(404, "https://www.zillow.com/home/", "missing"),
        ZillowBlockedError(403, "https://www.zillow.com/search/", "blocked"),
        ZillowServerError(503, "https://www.zillow.com/search/", "unavailable"),
        ZillowNetworkError("https://www.zillow.com/search/"),
    ],
)
def test_zillow_exceptions_survive_copy_and_pickle(error):
    # The pickled payload is this test's own error object, never untrusted
    # input, so the deserialization warning does not apply.
    for cloned in (copy.copy(error), pickle.loads(pickle.dumps(error))):  # noqa: S301
        assert type(cloned) is type(error)
        assert str(cloned) == str(error)
        assert vars(cloned) == vars(error)
