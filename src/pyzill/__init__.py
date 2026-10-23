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
    PruneStats,
    SyncRun,
    SyncStats,
    connect,
    db_stats,
    default_db_path,
    get_price_history,
    list_runs,
    load_listings,
    prune_database,
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
# ``pyzill.geocode.geocode(...)`` resolves. Only the names below are
# re-exported by name: binding the function as ``pyzill.geocode`` would
# shadow the module.
from pyzill.geocode import GeocodeError, GeocodeUnavailableError, GeoResult
from pyzill.normalize import (
    Listing,
    is_building_result,
    normalize_listing,
    normalize_listings,
)
from pyzill.property_detail import PropertyDetail, normalize_property_detail
from pyzill.proxy import parse_proxy, resolve_proxy
from pyzill.ratelimit import get_min_interval, set_min_interval
from pyzill.tiling import TiledSearchError, split_bbox, tiled_search
from pyzill.values import parse_price

__all__ = [
    "CONTINENTAL_US",
    "BBox",
    "DBStats",
    "GeoResult",
    "GeocodeError",
    "GeocodeUnavailableError",
    "Listing",
    "PropertyDetail",
    "PruneStats",
    "SyncRun",
    "SyncStats",
    "TiledSearchError",
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
    "list_runs",
    "load_listings",
    "normalize_listing",
    "normalize_listings",
    "normalize_property_detail",
    "parse_price",
    "parse_proxy",
    "prune_database",
    "resolve_proxy",
    "set_min_interval",
    "sold",
    "split_bbox",
    "tiled_search",
    "upsert_listings",
    "utc_now",
]
