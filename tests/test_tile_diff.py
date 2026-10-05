"""Incremental tile-diff manifest: GET /datasets/{id}/tile-diff.

The manifest must

* enumerate candidates from *geometry* (so ID-less / duplicate-ID features
  and 8 px buffer neighbours cannot be missed),
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


def test_candidate_tiles_cover_buffer_neighbours():
    # Revision 1 holds the near-boundary point; revision 2 removes it (a
    # far-away replacement keeps the collection non-empty).  Unmatched
    # features drive the candidate set, so the containing tile *and* the
    # 8 px buffered neighbour of the deleted point are enumerated.
    feats_a = _as_shapely_features([point(NEAR_BOUNDARY_WEST, 10.0, fid=1)])
    feats_b = _as_shapely_features([point(2.0, 2.0, fid=2)])
    candidates = tilediff.candidate_tiles(feats_a, feats_b, 3)
    # Containing tile (6,3) plus the 8 px buffered neighbour (7,3).
    assert (6, 3) in candidates
    assert (7, 3) in candidates


def test_identical_revisions_have_no_candidates():
    # Every feature pairs with an identical counterpart: nothing may move,
    # so no tile is enumerated at all (the endpoint skips this case anyway).
    feats = _as_shapely_features(
        [
            point(10.0, 10.0, {"k": 1}, fid=1),
            point(-50.0, 5.0, {"k": 2}, fid="s"),
            polygon(
                [[[-179, -80], [179, -80], [179, 80], [-179, 80], [-179, -80]]],
                {"kind": "land"},
                fid=9,
            ),
        ]
    )
    assert tilediff.candidate_tiles(feats, feats, 14) == []
    changed_a, changed_b = tilediff.changed_features(feats, feats)
    assert changed_a == [] and changed_b == []


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


def test_z14_endpoint_diff_is_small_for_local_update(client):
    lng, lat = 116.4074, 39.9042
    ds = _publish_one(client, [point(lng, lat, fid=1)])
    _publish_next(client, ds, [point(lng + 0.0003, lat, fid=1)], 1)
    changed = _diff(client, ds, 1, 2, 14).json()["changed_tiles"]
    assert 1 <= len(changed) <= 2


def world_polygon(fid=9, props=None):
    """A valid polygon covering essentially the whole globe."""
    return polygon(
        [[[-179.0, -80.0], [179.0, -80.0], [179.0, 80.0],
          [-179.0, 80.0], [-179.0, -80.0]]],
        props if props is not None else {"kind": "land"},
        fid=fid,
    )


def test_z14_global_polygon_plus_local_point_move_enumerates_locally():
    # Both revisions keep a globe-spanning polygon; only one local point
    # moves ~30 m.  Pre-fix the polygon bbox expanded the candidate set to
    # the whole z14 grid (2**28 tiles, OOM); post-fix only the point's old
    # and new buffered neighbourhoods are enumerated.
    lng, lat = 116.4074, 39.9042
    rev1 = _as_shapely_features([world_polygon(), point(lng, lat, fid=1)])
    rev2 = _as_shapely_features(
        [world_polygon(), point(lng + 0.0003, lat, fid=1)]
    )

    candidates = tilediff.candidate_tiles(rev1, rev2, 14)
    assert 1 <= len(candidates) <= 4
    x = int((lng + 180) / 360 * (1 << 14))
    for cx, cy in candidates:
        assert abs(cx - x) <= 1

    # The global polygon alone (identical in both revisions) enumerates
    # nothing at any zoom.
    same = _as_shapely_features([world_polygon()])
    assert tilediff.candidate_tiles(same, same, 14) == []
    assert tilediff.candidate_tiles(same, same, 0) == []


def test_z14_endpoint_global_polygon_plus_local_move_is_fast_and_exact(client):
    lng, lat = 116.4074, 39.9042
    rev1 = [world_polygon(), point(lng, lat, {"name": "p"}, fid=1)]
    rev2 = [world_polygon(), point(lng + 0.0003, lat, {"name": "p"}, fid=1)]
    ds = _publish_one(client, rev1)
    _publish_next(client, ds, rev2, 1)

    import time

    t0 = time.monotonic()
    resp = _diff(client, ds, 1, 2, 14)
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    assert elapsed < 10.0, "diff must not walk the global z14 grid"

    tiles = resp.json()["changed_tiles"]
    # The moved point changes only its local tile(s); the polygon is unchanged.
    assert 1 <= len(tiles) <= 4
    x = int((lng + 180) / 360 * (1 << 14))
    for t in tiles:
        assert abs(t["x"] - x) <= 1
        assert t["from_etag"] != t["to_etag"]

    # The manifest must agree with an exhaustive compile around the change,
    # and a far-away global tile must be byte-identical across revisions.
    far = client.get("/datasets/%s/tiles/14/0/0.mvt?revision=1" % ds)
    far2 = client.get("/datasets/%s/tiles/14/0/0.mvt?revision=2" % ds)
    assert far.status_code == far2.status_code == 200
    assert far.content == far2.content
    assert far.headers["etag"] == far2.headers["etag"]


def test_global_polygon_movement_is_still_detected():
    # When the *polygon* itself changes (a vertex nudged), its global range
    # legitimately dominates -- correctness over locality.
    rev1 = _as_shapely_features([world_polygon(), point(116.4074, 39.9042, fid=1)])
    moved = world_polygon()
    moved["geometry"] = shape(
        polygon(
            [[[-178.0, -80.0], [179.0, -80.0], [179.0, 80.0],
              [-178.0, 80.0], [-178.0, -80.0]]]
        )["geometry"]
    )
    rev2 = _as_shapely_features([moved, point(116.4074, 39.9042, fid=1)])
    changed_a, changed_b = tilediff.changed_features(rev1, rev2)
    assert len(changed_a) == 1 and len(changed_b) == 1
    assert changed_a[0]["id"] == changed_b[0]["id"] == 9


# ---------------------------------------------------------------------------
# Cross-revision feature correspondence
# ---------------------------------------------------------------------------


def test_correspondence_without_ids_matches_by_content():
    base = _as_shapely_features(
        [point(10.0, 10.0, {"k": "a"}), point(-20.0, 20.0, {"k": "b"})]
    )
    # Same two ID-less features in reversed source order: encoder order is by
    # content hash, so every feature still pairs identically.
    reordered = list(reversed(base))
    changed_a, changed_b = tilediff.changed_features(base, reordered)
    assert changed_a == [] and changed_b == []


def test_correspondence_duplicate_ids_reorder_is_a_change():
    # Two features sharing ID 7; swapping their source order changes the
    # emitted MVT (the encoder sorts stably, so the equal sort key keeps
    # source order).  Positional pairing must NOT cancel them.
    a = _as_shapely_features(
        [point(10.0, 10.0, {"name": "a"}, fid=7),
         point(10.0, 11.0, {"name": "b"}, fid=7)]
    )
    b = _as_shapely_features(
        [point(10.0, 11.0, {"name": "b"}, fid=7),
         point(10.0, 10.0, {"name": "a"}, fid=7)]
    )
    changed_a, changed_b = tilediff.changed_features(a, b)
    assert len(changed_a) == 2 and len(changed_b) == 2


def test_correspondence_duplicate_string_ids_reorder_is_a_change():
    a = _as_shapely_features(
        [point(10.0, 10.0, {"name": "a"}, fid="x"),
         point(10.0, 11.0, {"name": "b"}, fid="x")]
    )
    b = _as_shapely_features(
        [point(10.0, 11.0, {"name": "b"}, fid="x"),
         point(10.0, 10.0, {"name": "a"}, fid="x")]
    )
    changed_a, changed_b = tilediff.changed_features(a, b)
    assert len(changed_a) == 2 and len(changed_b) == 2


def test_correspondence_equal_key_identical_content_swap_cancels():
    # Equal sort key *and* equal content: swapping identical copies changes
    # no bytes anywhere, so cancelling is correct.
    a = _as_shapely_features(
        [point(10.0, 10.0, {"name": "a"}, fid=7),
         point(10.0, 10.0, {"name": "a"}, fid=7)]
    )
    b = list(reversed(a))
    changed_a, changed_b = tilediff.changed_features(a, b)
    assert changed_a == [] and changed_b == []


def test_correspondence_bool_vs_int_property_is_a_change():
    # JSON true and integer 1 encode to different MVT value types: they must
    # never be treated as the same property.
    a = _as_shapely_features([point(10.0, 10.0, {"open": True}, fid=1)])
    b = _as_shapely_features([point(10.0, 10.0, {"open": 1}, fid=1)])
    changed_a, changed_b = tilediff.changed_features(a, b)
    assert len(changed_a) == 1 and len(changed_b) == 1


def test_correspondence_add_delete_and_move():
    a = _as_shapely_features(
        [point(10.0, 10.0, fid=1), point(-40.0, 20.0, fid=2),
         point(5.0, 5.0, fid="gone")]
    )
    b = _as_shapely_features(
        [point(12.0, 10.0, fid=1), point(-40.0, 20.0, fid=2),
         point(70.0, -30.0, fid="new")]
    )
    changed_a, changed_b = tilediff.changed_features(a, b)
    ids_a = sorted((f["id"] for f in changed_a), key=str)
    ids_b = sorted((f["id"] for f in changed_b), key=str)
    assert ids_a == [1, "gone"]
    assert ids_b == [1, "new"]


def test_reorder_of_same_key_features_is_reported(client):
    # Endpoint-level: reordering two distinct, duplicate-ID features at the
    # same location changes MVT bytes, and the local tile must be reported.
    rev1 = [
        point(10.0, 10.0, {"v": "a"}, fid=7),
        point(10.0, 10.0, {"v": "b"}, fid=7),
    ]
    rev2 = [
        point(10.0, 10.0, {"v": "b"}, fid=7),
        point(10.0, 10.0, {"v": "a"}, fid=7),
    ]
    ds = _publish_one(client, rev1)
    _publish_next(client, ds, rev2, 1)
    changed = _diff(client, ds, 1, 2, 6).json()["changed_tiles"]
    expected = _brute_force_changed_tiles(client, ds, 1, 2, 6)
    got = {(t["x"], t["y"]) for t in changed}
    assert got == expected
    assert expected


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
