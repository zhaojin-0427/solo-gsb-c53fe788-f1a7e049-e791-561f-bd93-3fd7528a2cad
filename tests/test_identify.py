"""Click identify endpoint: hit adjudication, ordering, validation, 404s.

The endpoint resolves every source feature through the same project / clip /
simplify / quantise pipeline as the tile compiler and adjudicates the
*compiled* geometry against a sub-pixel click on the 4096 grid.
"""
import pytest
from shapely.geometry import Point, Polygon
from shapely.ops import transform as shp_transform

from app.identify import UNITS_PER_PX, click_to_units, hit_distance_px
from app.projection import make_lnglat_to_units_transformer
from app.tiler import compile_feature_geometry

z, x, y = 1, 1, 0  # northern/eastern quarter of the world at z=1


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _pxpy(lng: float, lat: float, z=z, x=x, y=y):
    """Click source-pixel coordinates of an EPSG:4326 position in a tile."""
    t = make_lnglat_to_units_transformer(z, x, y, 4096)
    p = shp_transform(t, Point(lng, lat))
    return p.x / UNITS_PER_PX, p.y / UNITS_PER_PX


def _ring(cx, cy, d):
    return [[[cx, cy], [cx + d, cy], [cx + d, cy + d], [cx, cy + d], [cx, cy]]]


# FeatureCollection exercising every identity / geometry case:
#   0 polygon (no id)            rectangle lng 10..30, lat 10..30
#   1 line with string id        along lat 50
#   2 numeric-id point           (50, 5)
#   3 no-id point                (40, 20)
#   4 no-id duplicate of #3      same geometry, different attributes
#   5 string-id point            (45, 25)
#   6 tiny polygon, collapses    near (20, 40) at this zoom
IDENTIFY_COLLECTION = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"name": "rect"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[10, 10], [30, 10], [30, 30], [10, 30], [10, 10]]],
            },
        },
        {
            "type": "Feature",
            "id": "L1",
            "properties": {"name": "line"},
            "geometry": {"type": "LineString", "coordinates": [[0, 50], [60, 50]]},
        },
        {
            "type": "Feature",
            "id": 7,
            "properties": {"name": "pt"},
            "geometry": {"type": "Point", "coordinates": [50.0, 5.0]},
        },
        {
            "type": "Feature",
            "properties": {"name": "no1"},
            "geometry": {"type": "Point", "coordinates": [40, 20]},
        },
        {
            "type": "Feature",
            "properties": {"name": "no2"},
            "geometry": {"type": "Point", "coordinates": [40, 20]},
        },
        {
            "type": "Feature",
            "id": "abc",
            "properties": {"name": "strid"},
            "geometry": {"type": "Point", "coordinates": [45, 25]},
        },
        {
            "type": "Feature",
            "id": 9,
            "properties": {"name": "tiny"},
            "geometry": {"type": "Polygon",
                         "coordinates": _ring(20.0, 40.0, 0.0000001)},
        },
    ],
}


@pytest.fixture
def ds(client):
    resp = client.post(
        "/datasets",
        json=IDENTIFY_COLLECTION,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _get(client, ds, px, py, radius, **extra):
    q = f"px={px}&py={py}&radius_px={radius}"
    for k, v in extra.items():
        q += f"&{k}={v}"
    return client.get(f"/datasets/{ds}/tiles/{z}/{x}/{y}/identify?{q}")


def _features(client, ds, revision=1):
    from app import db
    from app.main import _load_revision_features

    with db.connection() as conn:
        return _load_revision_features(conn, ds, revision)


def _compiled(client, ds, index, revision=1):
    """The quantised geometry of one source feature in tile (z, x, y)."""
    return compile_feature_geometry(
        _features(client, ds, revision)[index]["geometry"], z, x, y
    )


def _compiled_click_pxpy(client, ds, index, revision=1):
    """Click (px, py) landing exactly on the compiled geometry (dist 0).

    Uses a point vertex for points/lines and an interior point for areas, so
    the distance to the *quantised* geometry is exactly zero (the click stays
    sub-pixel for all other positions via px offsets).
    """
    geom = _compiled(client, ds, index, revision)
    if geom.geom_type in ("MultiPoint", "MultiLineString", "MultiPolygon"):
        geom = geom.geoms[0]
    if geom.geom_type == "Point":
        u, v = geom.x, geom.y
    elif geom.geom_type == "LineString":
        u, v = geom.coords[0]
    else:
        rp = geom.representative_point()
        u, v = rp.x, rp.y
    return u / UNITS_PER_PX, v / UNITS_PER_PX


# ---------------------------------------------------------------------------
# Geometry family adjudication
# ---------------------------------------------------------------------------


def test_polygon_interior_is_zero_distance_hit(client, ds):
    # representative_point() of the compiled polygon is strictly inside it.
    px, py = _compiled_click_pxpy(client, ds, 0)
    resp = _get(client, ds, px, py, 2)
    assert resp.status_code == 200
    hits = resp.json()["hits"]
    assert [(h["source_index"], h["distance_px"]) for h in hits] == [(0, 0.0)]
    assert hits[0]["source_id"] is None
    assert hits[0]["properties"] == {"name": "rect"}


def test_polygon_outside_but_near_edge_uses_radius(client, ds):
    # Click just south of the rectangle's lat=10 edge: interior miss, but the
    # polygon boundary is within a generous radius so it still hits.
    px, py = _pxpy(20, 9.0)
    resp = _get(client, ds, px, py, 8)
    hits = resp.json()["hits"]
    assert hits and hits[0]["source_index"] == 0
    assert hits[0]["distance_px"] > 0
    # Tight radius misses: it is outside the area geometry.
    assert _get(client, ds, px, py, 0).json()["hits"] == []


def test_line_hit_on_the_line_is_zero(client, ds):
    px, py = _compiled_click_pxpy(client, ds, 1)
    hits = _get(client, ds, px, py, 2).json()["hits"]
    assert [(h["source_index"], h["source_id"], h["distance_px"]) for h in hits] == [
        (1, "L1", 0.0)
    ]


def test_line_within_radius_hits_and_reports_distance(client, ds):
    # Click parallel to the line, offset a few pixels south.
    px, on_line = _pxpy(30, 50)
    # offset py by 1.5 px; line is horizontal on the grid after projection
    hits = _get(client, ds, px, on_line + 1.5, 2).json()["hits"]
    assert len(hits) == 1
    assert hits[0]["source_index"] == 1
    assert hits[0]["distance_px"] == pytest.approx(1.5, abs=0.05)
    # Same click with a smaller radius misses.
    assert _get(client, ds, px, on_line + 1.5, 1).json()["hits"] == []


def test_point_click_slightly_off_reports_subpixel_distance(client, ds):
    px, py = _pxpy(50, 5)
    hits = _get(client, ds, px + 0.6, py, 1).json()["hits"]
    assert [(h["source_index"], h["source_id"]) for h in hits] == [(2, 7)]
    assert 0.5 < hits[0]["distance_px"] < 0.7
    assert hits[0]["properties"] == {"name": "pt"}


def test_point_outside_radius_misses(client, ds):
    px, py = _pxpy(50, 5)
    assert _get(client, ds, px + 0.6, py, 0).json()["hits"] == []
    assert _get(client, ds, px + 5, py, 2).json()["hits"] == []


def test_string_id_feature_is_independent(client, ds):
    px, py = _pxpy(45, 25)
    hits = _get(client, ds, px, py, 2).json()["hits"]
    assert [(h["source_index"], h["source_id"]) for h in hits] == [(5, "abc")]


# ---------------------------------------------------------------------------
# Identity: no merging for missing / string / duplicate IDs
# ---------------------------------------------------------------------------


def test_duplicate_idless_features_both_hit_and_keep_source_indices(client, ds):
    # Both no-id points quantise to the same grid vertex; clicking exactly on
    # that vertex returns both, with ties broken by source index.
    px, py = _compiled_click_pxpy(client, ds, 3)
    hits = _get(client, ds, px, py, 2).json()["hits"]
    assert [h["source_index"] for h in hits] == [3, 4]
    assert all(h["source_id"] is None for h in hits)
    assert [h["properties"]["name"] for h in hits] == ["no1", "no2"]
    # Both compile to the same grid point -> identical distance, tie broken by
    # source index.
    assert hits[0]["distance_px"] == hits[1]["distance_px"] == 0.0


def test_numeric_and_string_ids_are_not_merged(client, ds):
    # The string-id point is reported on its own (never merged with a numeric
    # feature), even at a radius large enough to also reach other features.
    px, py = _compiled_click_pxpy(client, ds, 5)
    hits = _get(client, ds, px, py, 8).json()["hits"]
    ids = [(h["source_index"], h["source_id"]) for h in hits]
    assert (5, "abc") in ids
    # Exactly one entry per source index -- nothing got merged.
    indices = [h["source_index"] for h in hits]
    assert len(indices) == len(set(indices))


def test_source_index_is_zero_based_position_in_source(client, ds):
    features = _features(client, ds)
    px, py = _pxpy(20, 20)
    hits = _get(client, ds, px, py, 2).json()["hits"]
    assert hits[0]["source_index"] == 0
    # Sanity: index really points at the polygon in the source feature list.
    assert features[0]["geometry"].geom_type == "Polygon"


# ---------------------------------------------------------------------------
# Quantised collapse and buffered edges
# ---------------------------------------------------------------------------


def test_geometry_collapsed_by_quantisation_does_not_hit(client, ds):
    # Feature 6 is a valid but tiny polygon whose ring rounds to one grid
    # point; it disappears from the tile and must never be picked.
    assert compile_feature_geometry(
        _features(client, ds)[6]["geometry"], z, x, y
    ) is None
    px, py = _pxpy(20.00000005, 40.00000005)
    hits = _get(client, ds, px, py, 8).json()["hits"]
    assert all(h["source_index"] != 6 for h in hits)


def test_buffered_edge_geometry_is_adjudicated_with_radius(client):
    # A line crosses the west tile edge (lng 0).  Its compiled portion inside
    # the 8 px buffer is clickable with the same radius rules as the interior.
    collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": 1,
                "properties": {"name": "edge"},
                "geometry": {"type": "LineString", "coordinates": [[-30, 40], [30, 40]]},
            }
        ],
    }
    d = client.post(
        "/datasets", json=collection, headers={"content-type": "application/json"}
    ).json()["id"]
    from app.main import _load_revision_features
    from app import db as _db

    with _db.connection() as conn:
        feats = _load_revision_features(conn, d, 1)
    compiled = compile_feature_geometry(feats[0]["geometry"], z, x, y)
    line = compiled.geoms[0] if compiled.geom_type.startswith("Multi") else compiled
    u, v = line.coords[0]
    assert u < 0, "the buffered line end sits outside the tile (negative units)"
    # Queries only accept 0<=px<256; click just inside the tile on the line row.
    on_row = v / UNITS_PER_PX
    hits = client.get(
        f"/datasets/{d}/tiles/{z}/{x}/{y}/identify?px=6&py={on_row}&radius_px=0"
    ).json()["hits"]
    assert [(h["source_index"], h["distance_px"]) for h in hits] == [(0, 0.0)]
    # 3 px off the line with radius 2 misses; radius 4 hits (same radius rule
    # applies at the buffered edge as in the interior).
    assert client.get(
        f"/datasets/{d}/tiles/{z}/{x}/{y}/identify?px=6&py={on_row + 3}&radius_px=2"
    ).json()["hits"] == []
    hits = client.get(
        f"/datasets/{d}/tiles/{z}/{x}/{y}/identify?px=6&py={on_row + 3}&radius_px=4"
    ).json()["hits"]
    assert hits[0]["source_index"] == 0


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_hits_ordered_by_distance_then_source_index(client, ds):
    # Click exactly on the coincident no-id points (dist 0); the radius 8 may
    # also reach the string-id point at positive distance.
    px, py = _compiled_click_pxpy(client, ds, 3)
    hits = _get(client, ds, px, py, 8).json()["hits"]
    distances = [h["distance_px"] for h in hits]
    assert distances == sorted(distances)
    # zero-distance entries ordered by source index
    zeros = [h["source_index"] for h in hits if h["distance_px"] == 0.0]
    assert zeros == sorted(zeros)
    assert zeros[:2] == [3, 4]


# ---------------------------------------------------------------------------
# Revision handling
# ---------------------------------------------------------------------------


def test_identify_defaults_to_head_revision(client, ds):
    resp = _get(client, ds, 0.0, 0.0, 0)
    assert resp.json()["revision"] == 1
    # Publish revision 2 (a single far-away point).
    rev2 = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": 1,
                "properties": {"v": 2},
                "geometry": {"type": "Point", "coordinates": [100.0, 70.0]},
            }
        ],
    }
    r = client.post(
        f"/datasets/{ds}/revisions?expected_revision=1",
        content=__import__("json").dumps(rev2).encode(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 201, r.text
    assert _get(client, ds, 0.0, 0.0, 0).json()["revision"] == 2
    pinned = _get(client, ds, 0.0, 0.0, 0, revision=1).json()
    assert pinned["revision"] == 1
    # Pinned revision 1 still resolves the old polygon.
    px, py = _pxpy(20, 20)
    pinned_hit = _get(client, ds, px, py, 2, revision=1).json()["hits"]
    assert pinned_hit and pinned_hit[0]["properties"] == {"name": "rect"}


def test_identify_missing_dataset_or_revision_404(client, ds):
    assert client.get(
        "/datasets/nope/tiles/0/0/0/identify?px=1&py=1&radius_px=1"
    ).status_code == 404
    resp = _get(client, ds, 1, 1, 1, revision=99)
    assert resp.status_code == 404
    assert resp.json()["latest_revision"] >= 1


# ---------------------------------------------------------------------------
# Parameter validation (400)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "px=10&py=10",                                  # missing radius_px
        "py=10&radius_px=1",                            # missing px
        "px=10&radius_px=1",                            # missing py
        "px=-1&py=10&radius_px=1",                      # px negative
        "px=256&py=10&radius_px=1",                    # px at exclusive upper bound
        "px=10&py=300&radius_px=1",                    # py >= 256
        "px=10&py=10&radius_px=9",                     # radius > 8
        "px=10&py=10&radius_px=-0.1",                 # radius negative
        "px=a&py=10&radius_px=1",                      # non-numeric px
        "px=10&py=10&radius_px=",                      # empty radius
        "px=10&py=10&radius_px=1&revision=0",         # revision < 1
        "px=10&py=10&radius_px=1&revision=x",         # non-integer revision
        "px=10&py=10&radius_px=1&revision=1.5",       # float revision
        "px=nan&py=10&radius_px=1",                    # NaN
        "px=10&py=inf&radius_px=1",                    # infinity
    ],
)
def test_invalid_parameters_return_400(client, ds, query):
    resp = client.get(f"/datasets/{ds}/tiles/{z}/{x}/{y}/identify?{query}")
    assert resp.status_code == 400, query
    assert "error" in resp.json()


def test_boundary_values_are_accepted(client, ds):
    # px/py == 0 and just under 256; radius 0 and 8 are all legal.
    for q in ["px=0&py=0&radius_px=0", "px=255.999&py=255.999&radius_px=8",
              "px=0&py=0&radius_px=8"]:
        resp = client.get(f"/datasets/{ds}/tiles/{z}/{x}/{y}/identify?{q}")
        assert resp.status_code == 200, q


@pytest.mark.parametrize("path", ["15/0/0", "-1/0/0", "2/9/0", "2/0/4",
                                  "2/-1/0", "abc/0/0"])
def test_invalid_tile_coordinates_return_400(client, ds, path):
    resp = client.get(
        f"/datasets/{ds}/tiles/{path}/identify?px=1&py=1&radius_px=1"
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Response shape and tile-byte invariance
# ---------------------------------------------------------------------------


def test_response_shape(client, ds):
    px, py = _pxpy(20, 20)
    payload = _get(client, ds, px, py, 2).json()
    assert payload["dataset_id"] == ds
    assert payload["revision"] == 1
    assert (payload["z"], payload["x"], payload["y"]) == (z, x, y)
    assert payload["radius_px"] == 2
    hit = payload["hits"][0]
    assert set(hit) == {"source_index", "source_id", "properties", "distance_px"}


def test_identify_never_changes_tile_bytes_or_cache(client, publish):
    from app import db

    ds_id = publish()["id"]
    tile = client.get(f"/datasets/{ds_id}/tiles/0/0/0.mvt")
    etag, body = tile.headers["etag"], tile.content
    with db.connection() as conn:
        cached_before = conn.execute("SELECT COUNT(*) FROM tile_cache").fetchone()[0]
    assert cached_before == 1
    for i in range(3):
        client.get(
            f"/datasets/{ds_id}/tiles/0/0/0/identify?px={10 + i}&py=20&radius_px=8"
        )
    with db.connection() as conn:
        cached_after = conn.execute("SELECT COUNT(*) FROM tile_cache").fetchone()[0]
    assert cached_after == cached_before  # identify does not populate tile_cache
    again = client.get(f"/datasets/{ds_id}/tiles/0/0/0.mvt")
    assert again.headers["etag"] == etag
    assert again.content == body


def test_identify_empty_area_returns_empty_hits(client, ds):
    # Open ocean / empty region click with radius 0.
    resp = _get(client, ds, 5.0, 5.0, 0)
    assert resp.status_code == 200
    assert resp.json()["hits"] == []


# ---------------------------------------------------------------------------
# Pure helper unit tests (sub-pixel click on the 4096 grid)
# ---------------------------------------------------------------------------


def test_click_to_units_keeps_subpixel_position():
    # px values that are not multiples of 1/16 land between integer grid
    # vertices and must stay floating point (no snapping on quantisation).
    click = click_to_units(10.26, 20.31)
    assert click.x == pytest.approx(10.26 * 16)
    assert click.y == pytest.approx(20.31 * 16)
    assert click.x % 1 != 0  # not snapped to the integer grid
    assert click.y % 1 != 0
    # A grid-aligned click does land on an integer vertex.
    assert click_to_units(10.25, 20.5).x == 164.0


def test_hit_distance_zero_for_area_interior_and_point_coincidence():
    poly = Polygon(
        [(100, 100), (200, 100), (200, 200), (100, 200), (100, 100)])
    assert hit_distance_px(poly, Point(150, 150)) == 0.0
    assert hit_distance_px(Point(500, 500), Point(500, 500)) == 0.0
    # 32 grid units off == 2 source pixels.
    assert hit_distance_px(Point(500, 500), Point(532, 500)) == pytest.approx(2.0)


def test_identify_features_adjudicates_in_tile_units():
    # Geometry expressed directly in the z=0 tile-local 4096 grid by feeding
    # lng/lat that compile there is done elsewhere; here use the pure helper
    # to confirm the distance/sort contract for a point and an area together.
    feats = [
        {"id": None, "properties": {"k": "area"},
         "geometry": Polygon([(100, 100), (200, 100), (200, 200),
                              (100, 200), (100, 100)])},
        {"id": 9, "properties": {"k": "pt"}, "geometry": Point(260, 150)},
    ]
    # identify_features runs the full geographic compiler, so this direct test
    # only covers the distance helpers; end-to-end coverage lives above.
    click = Point(232, 150)  # 2 px right of the area, ~1.75 px left of point
    d_area = hit_distance_px(feats[0]["geometry"], click)
    d_point = hit_distance_px(feats[1]["geometry"], click)
    assert d_area == pytest.approx(2.0)
    assert d_point == pytest.approx(1.75, abs=0.01)
