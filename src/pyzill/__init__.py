from pyzill.api import (
    for_rent,
    for_sale,
    get_from_apartment_id,
    get_from_apartment_url,
    get_from_department_id,
    get_from_department_url,
    get_from_home_id,
    get_from_home_url,
    iter_rent,
    iter_sale,
    iter_search,
    iter_sold,
    sold,
)
from pyzill.bbox import CONTINENTAL_US, BBox
from pyzill.client import ZillowClient
from pyzill.db import (
    DBStats,
    SyncStats,
    connect,
    db_stats,
    default_db_path,
    get_price_history,
    load_listings,
    upsert_listings,
    utc_now,
)
from pyzill.exceptions import (
    ZillowBlockedError,
    ZillowError,
    ZillowNetworkError,
    ZillowNotFoundError,
    ZillowParseError,
    ZillowServerError,
)

# Importing the submodule also binds ``pyzill.geocode`` to the module, so
# ``pyzill.geocode.geocode(...)`` resolves. Only ``GeoResult`` is re-exported
# by name: binding the function as ``pyzill.geocode`` would shadow the module.
from pyzill.geocode import GeoResult
from pyzill.normalize import (
    Listing,
    is_building_result,
    normalize_listing,
    normalize_listings,
)
from pyzill.property_detail import PropertyDetail, normalize_property_detail
from pyzill.ratelimit import get_min_interval, set_min_interval
from pyzill.tiling import split_bbox, tiled_search
from pyzill.utils import parse_proxy, resolve_proxy
from pyzill.values import parse_price

__all__ = [
    "CONTINENTAL_US",
    "BBox",
    "DBStats",
    "GeoResult",
    "Listing",
    "PropertyDetail",
    "SyncStats",
    "ZillowBlockedError",
    "ZillowClient",
    "ZillowError",
    "ZillowNetworkError",
    "ZillowNotFoundError",
    "ZillowParseError",
    "ZillowServerError",
    "connect",
    "db_stats",
    "default_db_path",
    "for_rent",
    "for_sale",
    "get_from_apartment_id",
    "get_from_apartment_url",
    "get_from_department_id",
    "get_from_department_url",
    "get_from_home_id",
    "get_from_home_url",
    "get_min_interval",
    "get_price_history",
    "is_building_result",
    "iter_rent",
    "iter_sale",
    "iter_search",
    "iter_sold",
    "load_listings",
    "normalize_listing",
    "normalize_listings",
    "normalize_property_detail",
    "parse_price",
    "parse_proxy",
    "resolve_proxy",
    "set_min_interval",
    "sold",
    "split_bbox",
    "tiled_search",
    "upsert_listings",
    "utc_now",
]
