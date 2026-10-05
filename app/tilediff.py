"""Candidate tile enumeration for incremental tile diffs.

Given two immutable revisions of an EPSG:4326 GeoJSON layer and a zoom ``z``,
``candidate_tiles`` returns the *small* set of XYZ tile coordinates at that
zoom whose encoded MVT bytes could possibly differ between the revisions.

A feature is emitted into a tile only when its geometry intersects the tile
box expanded by the 8 px clipping buffer (``BUFFER_UNITS = 128`` of the 4096
tile units -- see :func:`app.tiler.compile_feature_geometry`).  Therefore one
feature can only affect tiles whose own (un-buffered) bounds cover its
geometry expanded by the very same 8 px.

The crucial point is that **unchanged features must not contribute any
candidates**.  When both revisions contain a polygon covering the whole globe
and only one local point moves, feeding *every* feature bbox to the enumerator
would walk the entire grid at high zoom (2**28 tiles at z=14).  Instead we
first determine the cross-revision feature correspondence
(:func:`changed_features`) and enumerate the 8 px buffered tile range of the
unmatched features only -- both the old copy (in ``features_a``) and the new
copy (in ``features_b``), so a moved point covers its old *and* new
neighbourhood, an addition covers the new one and a deletion the old one.

Cross-revision correspondence has to match the encoder exactly, because
``encode_tile`` emits features in a deterministic order
(:func:`app.tiler._sort_key`) and swapping that order changes the MVT bytes:

* features are grouped by the very same sort key the encoder uses
  (non-negative integer ID -> oversized/negative integer / string ID ->
  content hash for ID-less features), so features without an ID are matched
  by content and duplicated IDs never collapse into one feature;
* features sharing one sort key (duplicate numeric/string IDs, or identical
  ID-less content) are paired *positionally in encoder order* -- a reorder of
  such features changes the encoded bytes and must never be falsely
  cancelled;
* a pair is "the same feature" only when geometry and typed properties are
  equal (``1`` and ``true`` encode differently and never compare equal);
* group size differences (added / deleted features) leave the surplus
  features unmatched.

Every unmatched feature is then projected with the exact tile projection /
latitude clamp, its mercator bounding box is grown by 8 px
(128/4096 of one tile span) and mapped to the inclusive tile index range;
all ranges are unioned.  The endpoint still adjudicates each candidate by
the actually encoded MVT bytes, so the conservative correspondence can only
over-report, never miss.  A localised z=14 update enumerates only a handful
of neighbouring tiles -- never the whole ``2**14`` grid (which would be
268 million tiles).
"""
from __future__ import annotations

import math
from itertools import groupby
from typing import Iterable

import numpy as np
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from .config import BUFFER_UNITS
from .projection import ORIGIN_SHIFT, lnglat_to_merc
from .tiler import EXTENT, _sort_key

# Fraction of one tile span covered by the 8 px buffer (128 / 4096 == 1/32).
_BUFFER_FRACTION = BUFFER_UNITS / EXTENT
# Floating point slack when a buffered edge lands (almost) exactly on a tile
# boundary.  Candidate sets must be supersets, so erring towards the extra
# tile is always safe; a real margin of 1/32 of a tile span dwarfs the slack.
_EPS = 1e-9


def _to_mercator(geom: BaseGeometry) -> BaseGeometry:
    """Reproject one 4326 geometry to Web Mercator metres (tile projection)."""

    def project(xs, ys, zs=None):
        return lnglat_to_merc(np.asarray(xs, dtype=np.float64),
                              np.asarray(ys, dtype=np.float64))

    return shp_transform(project, geom)


def _tile_range_for_geometry(
    geom_4326: BaseGeometry, z: int
) -> tuple[int, int, int, int]:
    """Return the inclusive ``(xmin, xmax, ymin, ymax)`` tile range at ``z``.

    The geometry's mercator bounding box is grown by the 8 px buffer on every
    side, so every tile the compiled feature could be clipped into is covered.
    """
    minx, miny, maxx, maxy = _to_mercator(geom_4326).bounds

    n = 1 << z
    span = 2.0 * ORIGIN_SHIFT / n
    # 8 px buffer plus a microscopic slack (1e-9 of a tile span) so a buffered
    # edge that mathematically crosses a tile boundary can never be rounded
    # back inside it by floating point error.
    grow = (_BUFFER_FRACTION + _EPS) * span
    minx, miny = minx - grow, miny - grow
    maxx, maxy = maxx + grow, maxy + grow

    # Keep the box inside the world (latitude is clamped to the mercator
    # limit by the projection itself; the buffer could only overshoot here).
    minx = max(minx, -ORIGIN_SHIFT)
    maxx = min(maxx, ORIGIN_SHIFT)
    miny = max(miny, -ORIGIN_SHIFT)
    maxy = min(maxy, ORIGIN_SHIFT)

    def tile_x(mx: float) -> int:
        return int(math.floor((mx + ORIGIN_SHIFT) / span))

    def tile_y(my: float) -> int:
        # XYZ: y grows southward (mercator Y is largest at the north edge).
        return int(math.floor((ORIGIN_SHIFT - my) / span))

    xmin = max(0, tile_x(minx))
    xmax = min(n - 1, tile_x(maxx))
    ymin = max(0, tile_y(maxy))
    ymax = min(n - 1, tile_y(miny))

    return xmin, xmax, ymin, ymax


# ---------------------------------------------------------------------------
# Cross-revision feature correspondence
# ---------------------------------------------------------------------------


def _property_values_equal(a: object, b: object) -> bool:
    """Compare two MVT-encodable property values as the encoder would.

    ``bool`` is an ``int`` subclass but protobuf encodes ``true`` and ``1``
    with different value types, so equal numeric value across the two types
    must not count as identical.  Every other pair has to share its exact
    type (string / int / float) and value; mismatches are "changed" (which
    only adds candidates, it never removes them).
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if type(a) is not type(b):
        return False
    return bool(a == b)


def _features_equal(feature_a: dict, feature_b: dict) -> bool:
    """Whether two features are the same feature in both revisions.

    Only an exact match of (typed) properties and geometry counts; a
    mismatch anywhere leaves both features unmatched so their buffered tile
    ranges enter the candidate set.
    """
    props_a = feature_a.get("properties") or {}
    props_b = feature_b.get("properties") or {}
    if set(props_a) != set(props_b):
        return False
    if not all(_property_values_equal(props_a[k], props_b[k]) for k in props_a):
        return False
    geom_a = feature_a.get("geometry")
    geom_b = feature_b.get("geometry")
    if geom_a is None or geom_b is None:
        return geom_a is geom_b
    # Vertex-exact equality: different geometry type or any different
    # coordinate is a real change.  Tolerance zero intentionally does not
    # snap, matching the encoder which keeps the raw geometry until its own
    # per-tile quantisation.
    return bool(geom_a.equals_exact(geom_b, 0.0))


def _keyed_groups(features: Iterable[dict]) -> dict[tuple, list[dict]]:
    """Group features by the encoder sort key, in encoder order.

    Each group holds the features sharing one key (duplicate IDs / identical
    ID-less content) ordered exactly as ``encode_tile`` emits them.
    """
    ordered = sorted(features, key=_sort_key)
    return {
        key: list(group)
        for key, group in groupby(ordered, key=_sort_key)
    }


def changed_features(
    features_a: Iterable[dict], features_b: Iterable[dict]
) -> tuple[list[dict], list[dict]]:
    """Return the unmatched features ``(changed_in_a, changed_in_b)``.

    Features are paired exactly as the encoder orders them: same sort key,
    same position inside that key's group.  Pairing positional copies of
    equal features is safe (they occupy the same encoder slot and compile
    identically everywhere); a reorder inside a shared-key group -- which
    does change MVT bytes -- leaves both copies unmatched instead of falsely
    cancelling them.  Features with no ID (matched by content hash),
    duplicated IDs and added / deleted features are all covered.
    """
    groups_a = _keyed_groups(features_a)
    groups_b = _keyed_groups(features_b)

    changed_a: list[dict] = []
    changed_b: list[dict] = []
    for key in sorted(set(groups_a) | set(groups_b)):
        group_a = groups_a.get(key, [])
        group_b = groups_b.get(key, [])
        for i in range(max(len(group_a), len(group_b))):
            feature_a = group_a[i] if i < len(group_a) else None
            feature_b = group_b[i] if i < len(group_b) else None
            if feature_a is not None and feature_b is not None:
                if _features_equal(feature_a, feature_b):
                    continue
                changed_a.append(feature_a)
                changed_b.append(feature_b)
            elif feature_a is not None:
                changed_a.append(feature_a)
            else:
                changed_b.append(feature_b)  # type: ignore[union-attr]
    return changed_a, changed_b


def candidate_tiles(
    features_a: Iterable[dict], features_b: Iterable[dict], z: int
) -> list[tuple[int, int]]:
    """Union of possibly-affected ``(x, y)`` tiles at zoom ``z``.

    Only features that do not have an identical counterpart in the other
    revision contribute (see :func:`changed_features`): old and new position
    of a moved feature, plus additions and deletions.  Each contributing
    geometry's mercator bounds are grown by the same 8 px buffer the tile
    pipeline clips with, so buffer-only neighbours cannot be missed.  The
    result is sorted by ``x`` then ``y`` and contains no duplicates.
    """
    changed_a, changed_b = changed_features(features_a, features_b)

    coords: set[tuple[int, int]] = set()
    for feature in [*changed_a, *changed_b]:
        geom = feature["geometry"]
        if geom is None or geom.is_empty:
            continue
        xmin, xmax, ymin, ymax = _tile_range_for_geometry(geom, z)
        for x in range(xmin, xmax + 1):
            for y in range(ymin, ymax + 1):
                coords.add((x, y))
    return sorted(coords)
