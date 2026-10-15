import pytest

from pyzill import normalize
from pyzill.normalize import Listing


def test_normalize_listing_keys_match_typeddict():
    out = normalize.normalize_listing({"zpid": "1"})
    assert set(out) == set(Listing.__annotations__)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("$329,900", 329900),
        ("$1,200,000", 1200000),
        (329900, 329900),
        (329900.0, 329900),
        ("$329,900.00", 329900),
        (None, None),
        ("", None),
        ("$0", None),
        (0, None),
        ("N/A", None),
        ("$550K", 550000),
        ("$1.2M", 1200000),
        ("$1.5B", 1500000000),
        ("$2T", None),
    ],
)
def test_parse_price(value, expected):
    assert normalize.parse_price(value) == expected


def test_normalize_listing_full():
    raw = {
        "zpid": "32462766",
        "detailUrl": "/homedetails/225-Green-St/32462766_zpid/",
        "statusType": "FOR_SALE",
        "statusText": "Active",
        "price": "$329,900",
        "priceLabel": "$330K",
        "address": "225 Green Street, Schenectady, NY 12305",
        "addressState": "NY",
        "beds": 6,
        "baths": 3.0,
        "area": 2544,
        "imgSrc": "https://photos.zillowstatic.com/fp/x-p_e.jpg",
        "latLong": {"latitude": 42.818394, "longitude": -73.94406},
        "hdpData": {
            "homeInfo": {
                "zipcode": "12305",
                "city": "Schenectady",
                "state": "NY",
                "homeType": "MULTI_FAMILY",
                "zestimate": 532300,
                "rentZestimate": 1414,
            }
        },
    }
    out = normalize.normalize_listing(raw)

    assert out["zpid"] == 32462766
    assert out["price"] == 329900
    assert out["beds"] == 6
    assert out["baths"] == 3.0
    assert out["area"] == 2544
    assert out["city"] == "Schenectady"
    assert out["zipcode"] == "12305"
    assert out["detail_url"] == "https://www.zillow.com/homedetails/225-Green-St/32462766_zpid/"
    assert out["zestimate"] == 532300
    assert out["rent_zestimate"] == 1414
    assert out["latitude"] == 42.818394


def test_normalize_listing_missing_fields_are_none():
    raw = {"zpid": "462055986", "address": "somewhere", "baths": 0.0}
    out = normalize.normalize_listing(raw)

    assert out["price"] is None
    assert out["beds"] is None
    assert out["area"] is None
    assert out["city"] is None
    assert out["zestimate"] is None
    assert out["detail_url"] is None
    assert out["status_type"] is None


def test_normalize_listing_falls_back_to_hdp_home_info():
    raw = {
        "zpid": "1",
        "hdpData": {
            "homeInfo": {
                "price": 425000.0,
                "bedrooms": 4.0,
                "bathrooms": 2.0,
                "livingArea": 1800.0,
                "streetAddress": "1 Main St",
                "city": "Schenectady",
                "state": "NY",
                "zipcode": "12305",
                "homeType": "SINGLE_FAMILY",
                "homeStatus": "FOR_SALE",
            }
        },
    }
    out = normalize.normalize_listing(raw)

    assert out["price"] == 425000
    assert out["beds"] == 4
    assert out["baths"] == 2.0
    assert out["area"] == 1800
    assert out["street"] == "1 Main St"
    assert out["city"] == "Schenectady"
    assert out["status"] == "FOR_SALE"


def test_normalize_listing_coerces_coordinate_strings():
    out = normalize.normalize_listing(
        {"zpid": "1", "latLong": {"latitude": "42.5", "longitude": "-73.9"}}
    )

    assert out["latitude"] == 42.5
    assert out["longitude"] == -73.9


def test_normalize_listings_skips_non_dicts():
    out = normalize.normalize_listings([{"zpid": "1"}, None, "junk", {"zpid": "2"}])
    assert len(out) == 2
    assert out[0]["zpid"] == 1
    assert out[1]["zpid"] == 2


def test_normalize_listings_empty():
    assert normalize.normalize_listings(None) == []
    assert normalize.normalize_listings([]) == []


def test_normalize_listing_skips_non_dict():
    assert normalize.normalize_listing(["not a dict"]) is None
