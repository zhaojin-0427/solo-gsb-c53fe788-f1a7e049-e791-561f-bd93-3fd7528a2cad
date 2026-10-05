"""Click identification ("point query") adjudicated on the compiled geometry.

Given the source features of one immutable revision and a click inside one
XYZ tile, :func:`identify_features` returns the features the click hits,
each with its 0-based source index, source ID, properties and pixel
distance.

Both the features and the click are resolved on the **exact** pipeline used
to compile the served MVT (see :mod:`app.tiler`): every source feature is
individually projected, clipped to the tile + 8 px buffer, simplified at
0.5 px and quantised to the integer 4096 grid.  Compiling per source --
rather than decoding the merged MVT -- preserves the original feature
identity: the 0-based :attr:`source_index` is the feature's position in the
published GeoJSON, and features without an ID, with string IDs or with
duplicated IDs are never merged, so every one of them is adjudicated on its
own.

Distance rules (Euclidean distance on the tile-local grid, in source
pixels of 16 grid units each):

* **area** geometries: a click anywhere inside the compiled polygon is a
  zero-distance hit;
* **line / point** geometries: the click must fall inside the circle of the
  requested ``radius_px`` (distance <= radius);
* geometry that disappears before encoding -- clipped away, downgraded to
  a lower dimension or collapsed onto one grid point by quantisation -- has
  no compiled geometry and therefore cannot hit;
* geometry survives only inside the 8 px buffer, so clicks on the buffered
  tile edges are adjudicated with the very same radius as the interior.

The click itself is **not** quantised: ``px`` / ``py`` in source pixels
(0 <= v < 256) map to the sub-pixel position on the 4096 grid
(``16 * px``) and stay floating point, so e.g. a click 0.25 px away from a
quantised point is reported at 0.25 px instead of snapping to zero.
"""
from __future__ import annotations

from typing import Any

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from .config import settings
from .tiler import EXTENT, compile_feature_geometry

# One source pixel covers this many units of the 4096 grid (4096 / 256 == 16).
UNITS_PER_PX: float = EXTENT / settings.tile_size_px

# Floating-point slack so a distance that is mathematically equal to the
# radius (both land on a 1/16 grid step) is not rejected by rounding noise.
_DISTANCE_EPS_PX = 1e-9


def click_to_units(px: float, py: float) -> Point:
    """Map source-pixel click coordinates to a sub-pixel 4096-grid point."""
    return Point(px * UNITS_PER_PX, py * UNITS_PER_PX)


def hit_distance_px(compiled: BaseGeometry, click: Point) -> float:
    """Euclidean distance between compiled geometry and the click, in pixels.

    A click inside an area geometry yields exactly ``0.0`` (GEOS measures
    distance to any point of the geometry, including its interior).
    """
    return compiled.distance(click) / UNITS_PER_PX


def identify_features(
    features: list[dict[str, Any]],
    z: int,
    x: int,
    y: int,
    px: float,
    py: float,
    radius_px: float,
) -> list[dict[str, Any]]:
    """Adjudicate one click against every source feature of a revision.

    Returns the hits sorted by distance (nearest first, area interiors at
    zero) and then by ascending 0-based source index for exact ties.
    """
    click = click_to_units(px, py)
    hits: list[dict[str, Any]] = []
    for source_index, feature in enumerate(features):
        # Same project -> clip -> simplify -> quantise -> clean path as the
        # tile compiler; ``None`` means this feature has no visible geometry
        # in this tile (or in its 8 px buffer) and cannot be picked.
        compiled = compile_feature_geometry(feature["geometry"], z, x, y)
        if compiled is None or compiled.is_empty:
            continue
        distance_px = hit_distance_px(compiled, click)
        if distance_px > radius_px + _DISTANCE_EPS_PX:
            continue
        hits.append(
            {
                "source_index": source_index,
                "source_id": feature.get("id"),
                "properties": feature.get("properties") or {},
                "distance_px": round(distance_px, 4),
                # Raw (unrounded) distance keeps the ordering exact even when
                # two hits round to the same reported value.
                "_distance": distance_px,
            }
        )
    hits.sort(key=lambda hit: (hit["_distance"], hit["source_index"]))
    for hit in hits:
        del hit["_distance"]
    return hits
