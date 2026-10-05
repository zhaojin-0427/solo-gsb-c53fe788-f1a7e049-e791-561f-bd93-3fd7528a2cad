"""Candidate tile enumeration for incremental tile diffs.

Given two immutable revisions of an EPSG:4326 GeoJSON layer and a zoom ``z``,
``candidate_tiles`` returns the *small* set of XYZ tile coordinates at that
zoom whose encoded MVT bytes could possibly differ between the revisions.

Why not simply union every feature footprint?  A layer that keeps a
world-covering background polygon in both revisions would then enumerate the
whole ``2**z`` x ``2**z`` grid (268 million tiles at z=14) even when only one
local point moved.  Instead the two revisions are first **matched feature by
feature** and only features that cannot be paired off contribute their
footprints: an unchanged world polygon cancels, so a local z=14 update
enumerates only a handful of neighbouring tiles.

Cross-revision correspondence
-----------------------------

The encoded bytes of one tile depend on the *sequence* of features compiled
into it: :func:`app.tiler.encode_tile` sorts by ``_sort_key`` and Python's
stable sort keeps the **source order** of features with equal sort keys, so
reordering same-key features changes the MVT bytes.  The correspondence
between the two revisions is therefore established like this:

1.  Features of each revision are grouped by their exact encode-time sort
    key (:func:`app.tiler._sort_key` -- imported, not duplicated, so the
    grouping can never drift away from the encoder).  Features without an ID
    land in their content-hash bucket; features sharing a duplicate ID share
    one bucket.
2.  Inside each group the members of the two revisions are paired **in
    source order** (first with first, second with second, ...).  A pair
    cancels only when the *content signature* -- effective MVT id, canonical
    properties and exact geometry WKB -- is identical, which guarantees the
    two features compile to the same bytes in every tile.
3.  Everything else stays *unmatched*: pairs whose signatures differ, and
    the leftover members when one revision's group is longer.

Matched pairs carry identical sort keys and the pairing preserves source
order inside each group, so the matching is monotone in the global
(sort key, source index) order that determines the encoded feature sequence.
Any tile that avoids the footprints of all unmatched features therefore sees
exactly the same ordered feature sequence from both revisions and encodes to
identical bytes.  Enumerating the union of the unmatched footprints (each
grown by the 8 px buffer) can hence never miss a changed tile, while a false
cancellation -- which would silently drop a real change -- is impossible by
construction: cancelling requires byte-level identical content at an
order-preserving position.
"""
from __future__ import annotations

import json
import math
from typing import Iterable

import numpy as np
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from .config import BUFFER_UNITS
from .projection import ORIGIN_SHIFT, lnglat_to_merc
# The grouping must use the encoder's own ordering key; importing the private
# helper (same package) guarantees the two can never diverge.
from .tiler import _UINT64_MAX, EXTENT, _sort_key

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
# Cross-revision feature matching
# ---------------------------------------------------------------------------


def _content_signature(feature: dict) -> tuple:
    """Fingerprint of everything a feature contributes to any tile's bytes.

    Two features with equal signatures compile to byte-identical MVT feature
    records in every tile: the effective id is exactly what
    :func:`app.tiler.encode_tile` emits as the protobuf feature id, the
    properties JSON is canonical (key order is normalised at encode time),
    and the WKB pins the geometry's structure and coordinates bit for bit.
    """
    fid = feature.get("id")
    effective_id = (
        fid
        if (
            not isinstance(fid, bool)
            and isinstance(fid, int)
            and 0 <= fid <= _UINT64_MAX
        )
        else None
    )
    props = feature.get("properties") or {}
    props_json = json.dumps(
        {str(k): v for k, v in props.items()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    geom = feature.get("geometry")
    return (effective_id, props_json, None if geom is None else geom.wkb)


def _group_features(features: Iterable[dict]) -> dict[tuple, list[dict]]:
    """Bucket features by their exact encode-time sort key, in source order.

    Features with no geometry (or an empty one) never survive into any tile,
    so they are left out entirely: they cannot change a single byte.
    """
    groups: dict[tuple, list[dict]] = {}
    for feature in features:
        geom = feature.get("geometry")
        if geom is None or geom.is_empty:
            continue
        groups.setdefault(_sort_key(feature), []).append(feature)
    return groups


def _unmatched_features(
    features_a: Iterable[dict], features_b: Iterable[dict]
) -> list[dict]:
    """Features of both revisions that cannot be cancelled pairwise.

    The result covers every feature whose encoded presence in some tile could
    differ between the revisions; everything not returned is proven to
    contribute byte-identical, order-identical records to every tile.
    """
    groups_a = _group_features(features_a)
    groups_b = _group_features(features_b)

    unmatched: list[dict] = []
    for key in groups_a.keys() | groups_b.keys():
        members_a = groups_a.get(key, [])
        members_b = groups_b.get(key, [])
        # Pair members in source order: with equal sort keys the encoder
        # keeps the source order, so position i in one revision corresponds
        # to position i in the other.  Cancelling across positions could
        # hide a reorder that does change the MVT bytes.
        for fa, fb in zip(members_a, members_b):
            if _content_signature(fa) != _content_signature(fb):
                unmatched.append(fa)
                unmatched.append(fb)
        # A group that grew or shrank between revisions leaves unpaired
        # members on the longer side (added / removed duplicates).
        longer, shorter = (
            (members_a, members_b)
            if len(members_a) >= len(members_b)
            else (members_b, members_a)
        )
        unmatched.extend(longer[len(shorter):])
    return unmatched


def candidate_tiles(
    features_a: Iterable[dict], features_b: Iterable[dict], z: int
) -> list[tuple[int, int]]:
    """Union of possibly-affected ``(x, y)`` tiles at zoom ``z``.

    Only features that cannot be cancelled across the two revisions (added,
    removed, moved, attribute-changed, or reordered within one sort-key
    group) contribute their 8 px-buffered footprints -- including both the
    old and the new position of a moved feature.  Unchanged features cancel
    no matter how large their geometry is, so a kept world-covering polygon
    never turns a local update into a global scan.  The result is sorted by
    ``x`` then ``y`` and contains no duplicates.
    """
    coords: set[tuple[int, int]] = set()
    for feature in _unmatched_features(features_a, features_b):
        geom = feature["geometry"]
        xmin, xmax, ymin, ymax = _tile_range_for_geometry(geom, z)
        for x in range(xmin, xmax + 1):
            for y in range(ymin, ymax + 1):
                coords.add((x, y))
    return sorted(coords)
