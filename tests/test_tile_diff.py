"""Incremental tile-diff manifest: GET /datasets/{id}/tile-diff.

The manifest must

* match features across the revisions (sort-key groups, source order inside
  a group, byte-level content signature) and enumerate candidates only from
  the *unmatched* features' geometry, so an unchanged world-covering polygon
  cannot trigger a global z=14 scan while ID-less / duplicate-ID features
  and 8 px buffer neighbours are never missed,
* adjudicate every candidate by the actually encoded MVT bytes through the
  exact same projection / clip / simplify / quantise / encode pipeline,
* report ETags identical to the pinned tile endpoint,
* stay empty for equal revisions and for source changes that compile away,
* validate parameters (400) and missing dataset / revision (404),
* touch only a handful of tiles for a localised z=14 update.
"""
import json

from shapely.geometry import shape

from app import tilediff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collection(features):
    return json.dumps(
        {"type": "FeatureCollection", "features": features}
    ).encode("utf-8")


def point(lng, lat, props=None, fid=None):
    f = {
        "type": "Feature",
        "properties": props or {},
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
    }
    if fid is not None:
        f["id"] = fid
    return f


def line(coords, props=None, fid=None):
    f = {
        "type": "Feature",
        "properties": props or {},
        "geometry": {"type": "LineString", "coordinates": coords},
    }
    if fid is not None:
        f["id"] = fid
    return f


def polygon(rings, props=None, fid=None):
    f = {
        "type": "Feature",
        "properties": props or {},
        "geometry": {"type": "Polygon", "coordinates": rings},
    }
    if fid is not None:
        f["id"] = fid
    return f


def _publish_one(client, features, layer="demo"):
    resp = client.post(
        f"/datasets?layer={layer}",
        content=_collection(features),
        headers={"content-type": "application/geo+json"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _publish_next(client, ds, features, expected):
    resp = client.post(
        f"/datasets/{ds}/revisions",
        params={"expected_revision": str(expected)},
        content=_collection(features),
        headers={"content-type": "application/geo+json"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["revision"]


def _diff(client, ds, from_rev, to_rev, z):
    return client.get(
        f"/datasets/{ds}/tile-diff",
        params={"from_revision": from_rev, "to_revision": to_rev, "z": z},
    )


def _brute_force_changed_tiles(client, ds, from_rev, to_rev, z):
    """Compile *every* tile at zoom z for both revisions and compare bytes."""
    n = 1 << z
    changed = set()
    for x in range(n):
        for y in range(n):
            a = client.get(
                f"/datasets/{ds}/tiles/{z}/{x}/{y}.mvt?revision={from_rev}"
            )
            b = client.get(
                f"/datasets/{ds}/tiles/{z}/{x}/{y}.mvt?revision={to_rev}"
            )
            if a.content != b.content:
                changed.add((x, y))
    return changed


def _as_shapely_features(features):
    return [{"id": f.get("id"), "properties": f.get("properties") or {},
             "geometry": shape(f["geometry"])} for f in features]


# A boundary-crossing fixture: z=3 tile width is 45 degrees, the 8 px buffer
# spans 45 * 128/4096 == 1.40625 degrees.  A point 1.2 degrees west of the
# x=6/x=7 boundary (lng 135) therefore still leaks into tile x=7's buffer.
NEAR_BOUNDARY_WEST = 133.8
NEAR_BOUNDARY_EAST = 136.2


# ---------------------------------------------------------------------------
# Parameter / existence validation
# ---------------------------------------------------------------------------


def test_same_revision_returns_empty_manifest(client):
    ds = _publish_one(client, [point(10.0, 10.0, {"k": "v"}, 1)])
    _publish_next(client, ds, [point(11.0, 11.0, {"k": "w"}, 1)], 1)
    resp = _diff(client, ds, 1, 1, 5)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["changed_tiles"] == []
    assert payload["from_revision"] == payload["to_revision"] == 1
    assert payload["z"] == 5
    # Equal non-head revisions behave the same.
    assert _diff(client, ds, 2, 2, 5).json()["changed_tiles"] == []


def test_unknown_dataset_is_404(client):
    resp = _diff(client, "nope", 1, 2, 0)
    assert resp.status_code == 404
    assert resp.json()["dataset_id"] == "nope"


def test_missing_revision_is_404(client):
    ds = _publish_one(client, [point(0, 0, fid=1)])
    resp = _diff(client, ds, 1, 99, 0)
    assert resp.status_code == 404
    assert "99" in resp.json()["error"]
    assert resp.json()["latest_revision"] == 1

    resp = _diff(client, ds, 99, 1, 0)
    assert resp.status_code == 404


def test_invalid_revision_numbers_are_400(client):
    ds = _publish_one(client, [point(0, 0, fid=1)])
    base = f"/datasets/{ds}/tile-diff"
    for query in [
        "to_revision=1&z=0",          # missing from_revision
        "from_revision=1&z=0",        # missing to_revision
        "from_revision=1&to_revision=1",  # missing z
        "from_revision=abc&to_revision=1&z=0",
        "from_revision=1&to_revision=1.5&z=0",
        "from_revision=0&to_revision=1&z=0",
        "from_revision=-2&to_revision=1&z=0",
    ]:
        resp = client.get(f"{base}?{query}")
        assert resp.status_code == 400, query


def test_invalid_zoom_is_400(client):
    ds = _publish_one(client, [point(0, 0, fid=1)])
    base = f"/datasets/{ds}/tile-diff"
    for query, _label in [
        ("from_revision=1&to_revision=1&z=15", "above max"),
        ("from_revision=1&to_revision=1&z=-1", "negative"),
        ("from_revision=1&to_revision=1&z=abc", "non integer"),
    ]:
        resp = client.get(f"{base}?{query}")
        assert resp.status_code == 400, query


# ---------------------------------------------------------------------------
# Byte-adjudicated manifest
# ---------------------------------------------------------------------------


def test_diff_matches_exhaustive_tile_enumeration(client):
    # A deliberately mixed revision pair: numeric / string / missing /
    # duplicate IDs, a point that moves *across* a z=3 tile boundary, a
    # deleted point, an added point, a changed line and an unchanged polygon.
    rev1 = [
        point(10.0, 10.0, {"name": "alpha"}, fid=1),
        point(100.0, 50.0, {"name": "deleted"}, fid="gone"),
        point(NEAR_BOUNDARY_WEST, 10.0, {"name": "dup-a"}, fid=7),
        point(-50.0, -30.0, {"name": "dup-b"}, fid=7),
        line([[10.0, 0.0], [20.0, 10.0]], {"scalerank": 5}, fid=20),
        polygon(
            [[[55.0, 35.0], [65.0, 35.0], [65.0, 45.0], [55.0, 45.0],
              [55.0, 35.0]]],
            {"name": "stable rect"},
            fid=30,
        ),
    ]
    rev2 = [
        # moved + attribute changed
        point(12.0, 10.0, {"name": "alpha2"}, fid=1),
        # point "gone" deleted
        # first duplicate-id point crosses into tile x=7 (buffer neighbour)
        point(NEAR_BOUNDARY_EAST, 10.0, {"name": "dup-a"}, fid=7),
        point(-50.0, -30.0, {"name": "dup-b"}, fid=7),
        line([[10.0, 0.0], [22.0, 12.0]], {"scalerank": 5}, fid=20),
        polygon(
            [[[55.0, 35.0], [65.0, 35.0], [65.0, 45.0], [55.0, 45.0],
              [55.0, 35.0]]],
            {"name": "stable rect"},
            fid=30,
        ),
        point(30.0, 30.0, {"name": "brand new"}),  # no ID, newly added
    ]
    ds = _publish_one(client, rev1)
    _publish_next(client, ds, rev2, 1)

    z = 3
    expected = _brute_force_changed_tiles(client, ds, 1, 2, z)
    resp = _diff(client, ds, 1, 2, z)
    assert resp.status_code == 200
    tiles = resp.json()["changed_tiles"]
    got = {(t["x"], t["y"]) for t in tiles}

    # Exact agreement with compiling all 64 tiles of the level.
    assert got == expected
    assert expected, "fixture should produce at least one changed tile"

    # Sorted by x then y.
    assert [(t["x"], t["y"]) for t in tiles] == sorted(got)

    # Every entry echoes z and both revisions, and carries two ETag strings.
    for t in tiles:
        assert t["z"] == z
        assert t["from_etag"].startswith('"') and t["from_etag"].endswith('"')
        assert t["to_etag"].startswith('"') and t["to_etag"].endswith('"')
        assert t["from_etag"] != t["to_etag"]

    # ETags are exactly those of the pinned tile endpoint.
    for x, y in got:
        a = client.get(f"/datasets/{ds}/tiles/{z}/{x}/{y}.mvt?revision=1")
        b = client.get(f"/datasets/{ds}/tiles/{z}/{x}/{y}.mvt?revision=2")
        entry = next(t for t in tiles if (t["x"], t["y"]) == (x, y))
        assert entry["from_etag"] == a.headers["etag"]
        assert entry["to_etag"] == b.headers["etag"]


def test_diff_is_symmetric_when_revisions_swapped(client):
    rev1 = [point(10.0, 10.0, fid=1)]
    rev2 = [point(40.0, 40.0, fid=1)]
    ds = _publish_one(client, rev1)
    _publish_next(client, ds, rev2, 1)

    forward = _diff(client, ds, 1, 2, 4).json()["changed_tiles"]
    backward = _diff(client, ds, 2, 1, 4).json()["changed_tiles"]
    assert [(t["x"], t["y"]) for t in forward] == [
        (t["x"], t["y"]) for t in backward
    ]
    for a, b in zip(forward, backward):
        assert a["from_etag"] == b["to_etag"]
        assert a["to_etag"] == b["from_etag"]


def test_buffer_only_neighbour_is_reported_on_delete(client):
    # The point leaks into tile x=7 at z=3 only via the 8 px buffer.
    rev1 = [point(NEAR_BOUNDARY_WEST, 10.0, fid=1)]
    # Revision 2 deletes it (kept collection non-empty with a far-away point).
    rev2 = [point(2.0, 2.0, fid=2)]
    ds = _publish_one(client, rev1)
    _publish_next(client, ds, rev2, 1)

    tiles = {(t["x"], t["y"]): t for t in _diff(client, ds, 1, 2, 3).json()["changed_tiles"]}
    # Containing tile (6,3) loses the interior point ...
    assert (6, 3) in tiles
    # ... and the buffer-only neighbour (7,3) must not be missed.
    assert (7, 3) in tiles


def test_source_difference_that_quantises_away_is_not_reported(client):
    # A sub-unit nudge (~3.6e-7 tile units at z=5) rounds to the same integer
    # 4096 coordinate, so the source GeoJSON differs but the MVT is identical.
    rev1 = [point(10.0, 10.0, {"k": "v"}, fid=1)]
    rev2 = [point(10.0 + 1e-9, 10.0, {"k": "v"}, fid=1)]
    ds = _publish_one(client, rev1)
    _publish_next(client, ds, rev2, 1)

    resp = _diff(client, ds, 1, 2, 5)
    assert resp.status_code == 200
    assert resp.json()["changed_tiles"] == []


def test_feature_reordering_compiles_to_empty_diff(client):
    feats = [
        point(10.0, 10.0, {"k": "a"}, fid=3),
        point(-20.0, 20.0, {"k": "b"}, fid=1),
        point(0.0, 0.0, {"k": "c"}, fid=2),
    ]
    ds = _publish_one(client, feats)
    _publish_next(client, ds, list(reversed(feats)), 1)
    assert _diff(client, ds, 1, 2, 3).json()["changed_tiles"] == []


def test_same_sort_key_reorder_is_not_falsely_cancelled(client):
    # Two features share id=7, hence the same sort key; the encoder's stable
    # sort keeps their *source* order, so swapping them changes the MVT byte
    # order in tiles where they co-occur.  Matching must not cancel them
    # across positions -- that would silently drop a real change.
    f = point(10.0, 10.0, {"name": "f"}, fid=7)
    g = point(10.4, 10.4, {"name": "g"}, fid=7)
    ds = _publish_one(client, [f, g])
    _publish_next(client, ds, [g, f], 1)

    z = 4
    expected = _brute_force_changed_tiles(client, ds, 1, 2, z)
    got = {(t["x"], t["y"]) for t in _diff(client, ds, 1, 2, z).json()["changed_tiles"]}
    assert got == expected
    assert expected, "reordering same-key features must change some tile bytes"


def test_removed_duplicate_idless_feature_is_reported(client):
    # Two byte-identical ID-less points: one cancels against the kept copy,
    # the other is unmatched, so dropping the duplicate is reported.
    f = point(10.0, 10.0, {"k": "v"})
    ds = _publish_one(client, [f, f])
    _publish_next(client, ds, [f], 1)

    z = 4
    expected = _brute_force_changed_tiles(client, ds, 1, 2, z)
    got = {(t["x"], t["y"]) for t in _diff(client, ds, 1, 2, z).json()["changed_tiles"]}
    assert got == expected
    assert expected, "removing one of two identical features changes bytes"


def test_added_and_removed_features_appear(client):
    rev1 = [point(10.0, 10.0, fid=1), point(-40.0, 20.0, fid=2)]
    rev2 = [point(10.0, 10.0, fid=1), point(70.0, -30.0, fid=3)]
    ds = _publish_one(client, rev1)
    _publish_next(client, ds, rev2, 1)

    expected = _brute_force_changed_tiles(client, ds, 1, 2, 4)
    got = {(t["x"], t["y"]) for t in _diff(client, ds, 1, 2, 4).json()["changed_tiles"]}
    assert got == expected
    # Old point tile and new point tile are both present (>=2 changed tiles).
    assert len(got) >= 2


def test_property_only_change_is_reported(client):
    rev1 = [point(10.0, 10.0, {"status": "open"}, fid=1)]
    rev2 = [point(10.0, 10.0, {"status": "closed"}, fid=1)]
    ds = _publish_one(client, rev1)
    _publish_next(client, ds, rev2, 1)

    changed = _diff(client, ds, 1, 2, 6).json()["changed_tiles"]
    assert [(t["x"], t["y"]) for t in changed] and len(changed) >= 1


def test_features_without_or_with_duplicate_ids_are_not_missed(client):
    # Two ID-less points and two points sharing id 7; move one of each group.
    rev1 = [
        point(10.0, 10.0),
        point(11.0, 11.0),
        point(-80.0, 5.0, fid=7),
        point(-81.0, 6.0, fid=7),
    ]
    rev2 = [
        point(13.0, 10.0),          # first no-id point moves east
        point(11.0, 11.0),
        point(-77.0, 5.0, fid=7),   # first dup-id point moves east
        point(-81.0, 6.0, fid=7),
    ]
    ds = _publish_one(client, rev1)
    _publish_next(client, ds, rev2, 1)

    z = 3
    expected = _brute_force_changed_tiles(client, ds, 1, 2, z)
    got = {(t["x"], t["y"]) for t in _diff(client, ds, 1, 2, z).json()["changed_tiles"]}
    assert got == expected
    assert expected


def test_tiny_polygon_revision_compiles_away_and_diff_survives(client):
    # A valid ~0.1 mm triangle collapses under quantisation at every
    # supported zoom.  Adding it changes no tile bytes, so the manifest
    # stays empty -- and computing it through the shared compile path must
    # not raise (the pre-fix pipeline threw GEOSException here).
    tiny = polygon(
        [[[116.4074, 39.9042], [116.407401, 39.9042],
          [116.407401, 39.904201], [116.4074, 39.9042]]],
        {"name": "tiny"},
        fid=9,
    )
    rev1 = [point(116.4074, 39.9042, {"name": "bj"}, fid=1)]
    rev2 = rev1 + [tiny]
    ds = _publish_one(client, rev1)
    _publish_next(client, ds, rev2, 1)

    for z in (0, 7, 14):
        resp = _diff(client, ds, 1, 2, z)
        assert resp.status_code == 200
        assert resp.json()["changed_tiles"] == []

    # A renderable change alongside the tiny polygon is still reported:
    # revision 3 also moves the point ~3 km east.
    _publish_next(client, ds, [point(116.44, 39.9042, {"name": "bj"}, fid=1), tiny], 2)
    changed = _diff(client, ds, 2, 3, 14).json()["changed_tiles"]
    assert changed, "a real geometry change must produce a manifest entry"
    for entry in changed:
        assert entry["from_etag"] != entry["to_etag"]


# ---------------------------------------------------------------------------
# Candidate enumeration
# ---------------------------------------------------------------------------


def test_candidate_superset_exhaustive_across_zooms():
    """Every tile whose bytes differ must be in the candidate set.

    Compiles the *whole* grid for z=0..6 for the mixed fixture and confirms
    the geometry-derived candidate set is never missing a changed tile (no
    false negatives), independently of the HTTP layer.
    """
    from app.tiler import compile_tile

    rev1 = [
        {"id": 1, "properties": {"v": 1}, "geometry": shape(point(10.0, 10.0)["geometry"])},
        {"id": "gone", "properties": {}, "geometry": shape(point(100.0, 50.0)["geometry"])},
        {"id": 7, "properties": {}, "geometry": shape(point(NEAR_BOUNDARY_WEST, 10.0)["geometry"])},
        {"id": 7, "properties": {}, "geometry": shape(point(-50.0, -30.0)["geometry"])},
        {"id": 20, "properties": {},
         "geometry": shape(line([[10.0, 0.0], [20.0, 10.0]])["geometry"])},
        {"id": 30, "properties": {},
         "geometry": shape(polygon([[[55.0, 35.0], [65.0, 35.0], [65.0, 45.0],
                                     [55.0, 45.0], [55.0, 35.0]]])["geometry"])},
    ]
    rev2 = [
        {"id": 1, "properties": {"v": 2}, "geometry": shape(point(12.0, 10.0)["geometry"])},
        {"id": 7, "properties": {}, "geometry": shape(point(NEAR_BOUNDARY_EAST, 10.0)["geometry"])},
        {"id": 7, "properties": {}, "geometry": shape(point(-50.0, -30.0)["geometry"])},
        {"id": 20, "properties": {},
         "geometry": shape(line([[10.0, 0.0], [22.0, 12.0]])["geometry"])},
        {"id": 30, "properties": {},
         "geometry": shape(polygon([[[55.0, 35.0], [65.0, 35.0], [65.0, 45.0],
                                     [55.0, 45.0], [55.0, 35.0]]])["geometry"])},
        {"id": None, "properties": {}, "geometry": shape(point(30.0, 30.0)["geometry"])},
    ]

    for z in range(0, 7):
        candidates = set(tilediff.candidate_tiles(rev1, rev2, z))
        missed = []
        for x in range(1 << z):
            for y in range(1 << z):
                a = compile_tile({"demo": rev1}, z, x, y)
                b = compile_tile({"demo": rev2}, z, x, y)
                if a != b and (x, y) not in candidates:
                    missed.append((x, y))
        assert missed == [], f"z={z}: candidate set missed changed tiles {missed}"


def test_candidate_superset_with_same_key_reorder():
    """Swapping two same-sort-key features changes tile bytes; the candidate
    set must still cover every changed tile (no false cancellation)."""
    from app.tiler import compile_tile

    f = {"id": 7, "properties": {"name": "f"},
         "geometry": shape(point(10.0, 10.0)["geometry"])}
    g = {"id": 7, "properties": {"name": "g"},
         "geometry": shape(point(10.4, 10.4)["geometry"])}
    rev1 = [f, g]
    rev2 = [g, f]

    changed_any = False
    for z in range(0, 7):
        candidates = set(tilediff.candidate_tiles(rev1, rev2, z))
        for x in range(1 << z):
            for y in range(1 << z):
                a = compile_tile({"demo": rev1}, z, x, y)
                b = compile_tile({"demo": rev2}, z, x, y)
                if a != b:
                    changed_any = True
                    assert (x, y) in candidates, f"z={z}: missed {(x, y)}"
    assert changed_any


def test_candidate_tiles_cover_buffer_neighbours():
    feats = _as_shapely_features([point(NEAR_BOUNDARY_WEST, 10.0, fid=1)])
    # Deleting the point leaves it unmatched, so its footprint is enumerated:
    # containing tile (6,3) plus the 8 px buffered neighbour (7,3).
    candidates = tilediff.candidate_tiles(feats, [], 3)
    assert (6, 3) in candidates
    assert (7, 3) in candidates


def test_identical_revisions_yield_no_candidates():
    # Byte-identical revisions cancel completely: nothing may be enumerated,
    # not even a world-covering polygon's footprint.
    feats = _as_shapely_features([
        polygon([[[-180.0, -85.0], [180.0, -85.0], [180.0, 85.0],
                  [-180.0, 85.0], [-180.0, -85.0]]], {"name": "bg"}, fid=1),
        point(116.4074, 39.9042, {"name": "bj"}, fid=2),
    ])
    assert tilediff.candidate_tiles(feats, feats, 14) == []


def test_candidate_tiles_union_both_revisions_and_sorted():
    a = _as_shapely_features([point(10.0, 10.0, fid=1)])
    b = _as_shapely_features([point(-70.0, -20.0, fid=2)])
    candidates = tilediff.candidate_tiles(a, b, 3)
    assert candidates == sorted(set(candidates))
    assert len(candidates) >= 2


def test_z14_local_update_enumerates_only_local_neighbourhood(client):
    # ~30 m move inside Beijing at z=14 must not enumerate the global grid
    # (268 million tiles): a point's buffered footprint is local.
    lng, lat = 116.4074, 39.9042
    rev1 = _as_shapely_features([point(lng, lat, fid=1)])
    rev2 = _as_shapely_features([point(lng + 0.0003, lat, fid=1)])

    candidates = tilediff.candidate_tiles(rev1, rev2, 14)
    n = 1 << 14
    assert n * n > 10**8  # sanity: global enumeration would be absurd
    assert 1 <= len(candidates) <= 4
    # And all candidates are actually adjacent around the moved point.
    import math

    x = int((lng + 180) / 360 * n)
    for cx, cy in candidates:
        assert abs(cx - x) <= 1


# A world-covering background polygon, kept byte-identical in both revisions.
WORLD_POLYGON = polygon(
    [[[-180.0, -85.0], [180.0, -85.0], [180.0, 85.0],
      [-180.0, 85.0], [-180.0, -85.0]]],
    {"name": "background"},
    fid=1,
)


def test_z14_world_polygon_cancels_and_local_move_stays_local(client):
    # Regression for the global scan: both revisions keep a world-covering
    # polygon and move one local point.  The unchanged polygon must cancel
    # in the cross-revision match, so only the moved point's old/new
    # neighbourhoods are enumerated -- never the 2^34-tile global grid.
    lng, lat = 116.4074, 39.9042
    rev1 = _as_shapely_features([WORLD_POLYGON, point(lng, lat, {"name": "bj"}, fid=2)])
    rev2 = _as_shapely_features(
        [WORLD_POLYGON, point(lng + 0.0003, lat, {"name": "bj"}, fid=2)]
    )

    candidates = tilediff.candidate_tiles(rev1, rev2, 14)
    n = 1 << 14
    assert n * n > 10**8
    assert 1 <= len(candidates) <= 4
    x = int((lng + 180) / 360 * n)
    for cx, cy in candidates:
        assert abs(cx - x) <= 1


def test_z14_endpoint_with_world_polygon_returns_local_manifest(client):
    # End-to-end over HTTP: the diff of the world-polygon pair above must
    # come back immediately with only the moved point's tile(s).
    lng, lat = 116.4074, 39.9042
    ds = _publish_one(client, [WORLD_POLYGON, point(lng, lat, {"name": "bj"}, fid=2)])
    _publish_next(
        client, ds, [WORLD_POLYGON, point(lng + 0.0003, lat, {"name": "bj"}, fid=2)], 1
    )

    resp = _diff(client, ds, 1, 2, 14)
    assert resp.status_code == 200
    changed = resp.json()["changed_tiles"]
    assert 1 <= len(changed) <= 2
    assert [(t["x"], t["y"]) for t in changed] == sorted(
        (t["x"], t["y"]) for t in changed
    )
    # Both ETags are exactly those of the pinned tile endpoint.
    for t in changed:
        a = client.get(f"/datasets/{ds}/tiles/14/{t['x']}/{t['y']}.mvt?revision=1")
        b = client.get(f"/datasets/{ds}/tiles/14/{t['x']}/{t['y']}.mvt?revision=2")
        assert t["from_etag"] == a.headers["etag"]
        assert t["to_etag"] == b.headers["etag"]


def test_z14_endpoint_diff_is_small_for_local_update(client):
    lng, lat = 116.4074, 39.9042
    ds = _publish_one(client, [point(lng, lat, fid=1)])
    _publish_next(client, ds, [point(lng + 0.0003, lat, fid=1)], 1)
    changed = _diff(client, ds, 1, 2, 14).json()["changed_tiles"]
    assert 1 <= len(changed) <= 2


# ---------------------------------------------------------------------------
# Old tile endpoint remains fully usable
# ---------------------------------------------------------------------------


def test_tile_endpoint_still_serves_both_revisions(client):
    rev1 = [point(10.0, 10.0, {"v": 1}, fid=1)]
    rev2 = [point(10.0, 10.0, {"v": 2}, fid=1)]
    ds = _publish_one(client, rev1)
    _publish_next(client, ds, rev2, 1)

    t1 = client.get(f"/datasets/{ds}/tiles/6/33/30.mvt?revision=1")
    t2 = client.get(f"/datasets/{ds}/tiles/6/33/30.mvt?revision=2")
    head = client.get(f"/datasets/{ds}/tiles/6/33/30.mvt")
    assert t1.status_code == t2.status_code == head.status_code == 200
    assert t1.headers["x-revision"] == "1"
    assert t2.headers["x-revision"] == "2"
    assert head.headers["x-revision"] == "2"
    assert "immutable" in t1.headers["cache-control"]
    assert "immutable" not in head.headers["cache-control"]
    assert t1.content != t2.content
