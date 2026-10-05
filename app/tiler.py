"""Offline vector-tile compilation pipeline.

Pipeline (applied per tile / per feature):

    EPSG:4326 GeoJSON
      -> project to Web Mercator and map onto the tile-local 4096 grid
      -> clip to the tile plus an 8-pixel buffer
      -> 0.5-pixel tolerance, topology-preserving simplification
      -> quantise to integer 4096 coordinates
      -> deterministic MVT encoding (stable layer / feature order)
"""
from __future__ import annotations

import hashlib
from typing import Any

import mapbox_vector_tile
import numpy as np
from shapely import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    remove_repeated_points,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from .config import BUFFER_UNITS, SIMPLIFY_UNITS, settings
from .projection import make_lnglat_to_units_transformer

EXTENT = settings.tile_extent
# Integer coordinate window covered by the buffered clipping box.
MIN_BUF = -BUFFER_UNITS
MAX_BUF = EXTENT + BUFFER_UNITS


def _geom_dimension(geom: BaseGeometry) -> int:
    """Topological dimension (point=0, line=1, area=2)."""
    if isinstance(geom, (Point, MultiPoint)):
        return 0
    if isinstance(geom, (LineString, MultiLineString)):
        return 1
    return 2  # Polygon / MultiPolygon (collections are exploded upstream)


def _explode(geom: BaseGeometry) -> list[BaseGeometry]:
    """Explode multi geometries / collections into atomic components."""
    if geom.is_empty:
        return []
    if isinstance(geom, (MultiPoint, MultiLineString, MultiPolygon, GeometryCollection)):
        parts: list[BaseGeometry] = []
        for g in geom.geoms:
            parts.extend(_explode(g))
        return parts
    return [geom]


def _keep_dimension(geom: BaseGeometry, dimension: int) -> BaseGeometry | None:
    """After a clip, retain only the components of the expected dimension.

    Intersection may turn a polygon boundary into a line, a line into a point,
    etc.  Such dimension drops are dropped from the feature.
    """
    kept = [
        g
        for g in _explode(geom)
        if not g.is_empty and _geom_dimension(g) == dimension
    ]
    if not kept:
        return None
    if dimension == 0:
        return MultiPoint(kept) if len(kept) > 1 else kept[0]
    if dimension == 1:
        return MultiLineString(kept) if len(kept) > 1 else kept[0]
    return MultiPolygon(kept) if len(kept) > 1 else kept[0]


def _dedupe_consecutive(coords: list[tuple]) -> list[tuple]:
    """Drop consecutive repeated vertices (pure Python, never raises).

    Mirrors what ``remove_repeated_points(..., tolerance=0)`` does to one
    ring, but works on the raw coordinate list so it can run *before* any
    LinearRing is reconstructed.  A closed input ring stays closed: the
    final coordinate always equals the first, so it is either appended or
    dropped because it duplicates the kept first vertex.
    """
    kept = [coords[0]]
    for pt in coords[1:]:
        if pt != kept[-1]:
            kept.append(pt)
    return kept


def _ring_coords_or_none(coords) -> list[tuple] | None:
    """Clean one quantised ring; ``None`` when it cannot enclose area.

    Quantisation can collapse every vertex of a small-but-valid ring onto
    the same grid point.  Once consecutive duplicates are removed such a
    ring has fewer than four positions, and rebuilding a GEOS LinearRing
    from it raises ``GEOSException`` (a two-point ring is illegal).  The
    ring is therefore adjudicated here -- before any polygon is rebuilt --
    by whether it can still form a valid area.
    """
    deduped = _dedupe_consecutive(list(coords))
    if len(deduped) < 4:
        return None
    if Polygon(deduped).area <= 0:
        return None
    return deduped


def _clean_quantized_polygon(geom: Polygon) -> BaseGeometry | None:
    """Rebuild one quantised polygon ring by ring.

    * an exterior ring that collapsed under quantisation drops only this
      polygon (its holes go with it),
    * a collapsed interior ring drops only that hole,
    * surviving rings are reassembled and made valid once more.
    """
    exterior = _ring_coords_or_none(geom.exterior.coords)
    if exterior is None:
        return None
    interiors: list[list[tuple]] = []
    for interior in geom.interiors:
        ring = _ring_coords_or_none(interior.coords)
        if ring is not None:
            interiors.append(ring)
    poly = Polygon(exterior, interiors)
    if not poly.is_valid:
        poly = poly.buffer(0)
        if poly.is_empty:
            return None
        if not isinstance(poly, Polygon):
            poly = _keep_dimension(poly, 2)
    return poly if (poly is not None and not poly.is_empty) else None


def _clean_quantized(geom: BaseGeometry) -> BaseGeometry | None:
    """Tidy geometry after rounding so the encoder never sees degenerate rings.

    * collapse repeated consecutive vertices (a side effect of quantisation),
    * drop zero-length lines / points and too-small polygon rings,
    * make polygons valid once more.

    Polygons are cleaned ring by ring *before* reconstruction (see
    :func:`_clean_quantized_polygon`): a ring whose vertices all rounded to
    the same grid point is discarded instead of reaching
    ``remove_repeated_points``, which would raise ``GEOSException`` on the
    resulting two-point LinearRing.  Points and lines cannot degenerate
    below GEOS limits that way, so they keep the whole-geometry call.
    """
    if geom.is_empty:
        return None

    if isinstance(geom, Polygon):
        return _clean_quantized_polygon(geom)

    if isinstance(geom, MultiPolygon):
        # Each part is adjudicated on its own: one collapsed polygon never
        # drags the surviving parts of the feature down with it.
        polys: list[Polygon] = []
        for part in geom.geoms:
            cleaned = _clean_quantized_polygon(part)
            if cleaned is not None:
                polys.extend(_explode(cleaned))
        if not polys:
            return None
        return MultiPolygon(polys) if len(polys) > 1 else polys[0]

    if isinstance(geom, GeometryCollection):
        # GeometryCollection should never survive this far; defensively
        # explode and clean each atomic part on its own.
        cleaned = [c for g in _explode(geom) if (c := _clean_quantized(g)) is not None]
        if not cleaned:
            return None
        return (
            MultiPolygon(cleaned)
            if isinstance(cleaned[0], Polygon)
            else (MultiLineString(cleaned) if isinstance(cleaned[0], LineString) else MultiPoint(cleaned))
        )

    geom = remove_repeated_points(geom, tolerance=0)
    if geom.is_empty:
        return None

    if isinstance(geom, Point):
        return geom
    if isinstance(geom, MultiPoint):
        pts = [p for p in geom.geoms if not p.is_empty]
        return MultiPoint(pts) if len(pts) > 1 else (pts[0] if pts else None)

    if isinstance(geom, LineString):
        coords = list(geom.coords)
        return LineString(coords) if len(set(coords)) >= 2 else None
    if isinstance(geom, MultiLineString):
        lines = [
            LineString(list(line.coords))
            for line in geom.geoms
            if len(set(line.coords)) >= 2
        ]
        if not lines:
            return None
        return MultiLineString(lines) if len(lines) > 1 else lines[0]

    # Only Point / MultiPoint / LineString / MultiLineString reach the
    # whole-geometry ``remove_repeated_points`` above; every container type
    # has been dispatched already.
    return None


def _quantize(geom: BaseGeometry) -> BaseGeometry:
    """Round to the integer grid and clamp inside the buffered window."""

    def round_xy(xs, ys, zs=None):
        xi = np.rint(np.asarray(xs, dtype=np.float64)).astype(np.int64)
        yi = np.rint(np.asarray(ys, dtype=np.float64)).astype(np.int64)
        np.clip(xi, MIN_BUF, MAX_BUF, out=xi)
        np.clip(yi, MIN_BUF, MAX_BUF, out=yi)
        return xi.tolist(), yi.tolist()

    return shp_transform(round_xy, geom)


def _filter_non_degenerate(geom: BaseGeometry, dimension: int) -> BaseGeometry | None:
    """Drop zero-length line parts and zero-area polygon parts."""
    kept: list[BaseGeometry] = []
    for g in _explode(geom):
        if g.is_empty:
            continue
        if dimension == 1 and g.length == 0:
            continue
        if dimension == 2 and g.area == 0:
            continue
        kept.append(g)
    if not kept:
        return None
    if dimension == 0:
        return MultiPoint(kept) if len(kept) > 1 else kept[0]
    if dimension == 1:
        return MultiLineString(kept) if len(kept) > 1 else kept[0]
    return MultiPolygon(kept) if len(kept) > 1 else kept[0]


def compile_feature_geometry(
    geom_4326: BaseGeometry, z: int, x: int, y: int
) -> BaseGeometry | None:
    """Run one source geometry through the whole tile pipeline.

    Returns ``None`` when the feature does not survive to this tile.
    """
    transformer = make_lnglat_to_units_transformer(z, x, y, EXTENT)
    geom = shp_transform(transformer, geom_4326)

    # 1) Clip to the tile + 8 px (128 units) buffer.
    clip_box = Polygon(
        [
            (MIN_BUF, MIN_BUF),
            (MAX_BUF, MIN_BUF),
            (MAX_BUF, MAX_BUF),
            (MIN_BUF, MAX_BUF),
        ]
    )
    if not geom.intersects(clip_box):
        return None
    geom = geom.intersection(clip_box)

    # Determine dimensional family of the *source* geometry.
    dimension = _geom_dimension(geom_4326)
    geom = _keep_dimension(geom, dimension)
    if geom is None or geom.is_empty:
        return None
    geom = _filter_non_degenerate(geom, dimension)
    if geom is None:
        return None

    # 2) Topology-preserving simplification, 0.5 px (8 units) tolerance.
    #    preserve_topology avoids collapsing polygons into invalid forms.
    if dimension > 0:
        geom = geom.simplify(SIMPLIFY_UNITS, preserve_topology=True)
        if geom.is_empty:
            return None
        geom = _keep_dimension(geom, dimension)
        if geom is None or geom.is_empty:
            return None
        geom = _filter_non_degenerate(geom, dimension)
        if geom is None:
            return None

    # 3) Quantise to the integer 4096 grid and clean degeneracies.
    geom = _quantize(geom)
    geom = _clean_quantized(geom)
    return geom


# ---------------------------------------------------------------------------
# Stable feature ordering
# ---------------------------------------------------------------------------

_UINT64_MAX = (1 << 64) - 1


def _sort_key(feature: dict[str, Any]) -> tuple:
    """Deterministic ordering for features inside one layer.

    Positive integer IDs come first (numeric order), then string IDs
    (lexicographic), then features without an ID (by stable content hash).
    """
    fid = feature.get("id")
    if isinstance(fid, bool):  # bool is an int subclass; treat as no numeric id
        fid = None
    if isinstance(fid, int) and 0 <= fid <= _UINT64_MAX:
        return (0, fid, "")
    if isinstance(fid, (int, str)):
        return (1, 0, str(fid))
    digest = hashlib.sha256(
        (
            repr(feature["geometry"])
            + "|"
            + repr(sorted((feature.get("properties") or {}).items()))
        ).encode("utf-8")
    ).hexdigest()
    return (2, 0, digest)


def _normalise_properties(props: dict[str, Any]) -> dict[str, Any]:
    """Return a plain dict with sorted keys (encoding order must be stable)."""
    return {str(k): v for k, v in sorted(props.items(), key=lambda kv: str(kv[0]))}


# ---------------------------------------------------------------------------
# Tile encoding
# ---------------------------------------------------------------------------

from mapbox_vector_tile.encoder import on_invalid_geometry_raise

_ENCODE_OPTIONS = {
    "y_coord_down": True,    # our local Y already points downward
    "extents": EXTENT,
    "quantize_bounds": None,  # coordinates are pre-quantised
    "check_winding_order": True,
    # Never silently swallow invalid geometry: our pipeline validates input
    # and cleans after clipping, so an invalid shape here is a real error.
    "on_invalid_geometry": on_invalid_geometry_raise,
}


def encode_tile(layers: dict[str, list[dict[str, Any]]]) -> bytes:
    """Encode ``{layer_name: [feature, ...]}`` into deterministic MVT bytes."""
    encoded_layers = []
    for layer_name in sorted(layers):
        features = sorted(layers[layer_name], key=_sort_key)
        out_features = []
        for f in features:
            geom = f["geometry"]
            if geom is None or geom.is_empty:
                continue
            fid = f.get("id")
            mvt_feature: dict[str, Any] = {
                "geometry": geom,
                "properties": _normalise_properties(f.get("properties") or {}),
            }
            # MVT feature IDs must be non-negative uint64; anything else is
            # still ordered deterministically, just not emitted as an ID.
            if (
                not isinstance(fid, bool)
                and isinstance(fid, int)
                and 0 <= fid <= _UINT64_MAX
            ):
                mvt_feature["id"] = fid
            out_features.append(mvt_feature)
        encoded_layers.append({"name": layer_name, "features": out_features})

    return mapbox_vector_tile.encode(
        encoded_layers, default_options=_ENCODE_OPTIONS
    )


def compile_tile(
    layers: dict[str, list[dict[str, Any]]], z: int, x: int, y: int
) -> bytes:
    """Compile all layers for one tile and return encoded MVT bytes."""
    compiled: dict[str, list[dict[str, Any]]] = {}
    for layer_name, features in layers.items():
        layer_features = []
        for feature in features:
            geom = compile_feature_geometry(feature["geometry"], z, x, y)
            if geom is None:
                continue
            layer_features.append(
                {
                    "id": feature.get("id"),
                    "properties": feature.get("properties") or {},
                    "geometry": geom,
                }
            )
        # Always keep the layer slot so the layer/feature ordering used to
        # compute byte identity is independent of tile contents.
        compiled[layer_name] = layer_features
    return encode_tile(compiled)


def tile_etag(data: bytes) -> str:
    """Strong validator for the exact MVT byte payload."""
    return '"' + hashlib.sha256(data).hexdigest() + '"'
