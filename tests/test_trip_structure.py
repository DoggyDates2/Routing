"""
Trip-structure regression tests for app.py / solve_driver.

Run from the repo root:
    pip install -r requirements.txt pytest
    python -m pytest tests/ -v

These run the REAL OR-Tools solver on small synthetic worlds — no Google
Sheets, no Streamlit UI. Streamlit / gspread are stubbed so app.py imports.

The headline rule under test (see derive_groups in app.py):

    A driver's trips must be CONTIGUOUS from their first pickup group to
    their last. A driver who picks up in group 1 and group 3 (no group 2
    pickups) still gets a Trip 2, and every Group-1-only dog is dropped off
    in Trip 2 — never carried into Trip 3.
"""

import ast
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# Load app.py without running its Streamlit UI
# ---------------------------------------------------------------------------
def _stub_ui_modules():
    class _Deco:
        def __call__(self, *a, **k):
            if len(a) == 1 and callable(a[0]) and not k:
                return a[0]
            return lambda f: f

        def __getattr__(self, n):
            return _Deco()

    st = types.ModuleType("streamlit")
    st.secrets = {}
    st.__getattr__ = lambda n: _Deco()
    sys.modules.setdefault("streamlit", st)
    sys.modules.setdefault("gspread", types.ModuleType("gspread"))
    try:
        import google  # noqa: F401  (google.protobuf comes with ortools)
    except ImportError:
        sys.modules["google"] = types.ModuleType("google")
    oa = types.ModuleType("google.oauth2")
    sa = types.ModuleType("google.oauth2.service_account")
    sa.Credentials = object
    sys.modules.setdefault("google.oauth2", oa)
    sys.modules.setdefault("google.oauth2.service_account", sa)
    sys.modules["google"].oauth2 = oa
    try:
        import pandas  # noqa: F401
    except ImportError:
        sys.modules["pandas"] = types.ModuleType("pandas")


def _load_app():
    """Exec only imports/functions/classes/CONSTANTS from app.py."""
    _stub_ui_modules()
    src = (REPO / "app.py").read_text()
    tree = ast.parse(src)
    keep = [
        n for n in tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef))
        or (isinstance(n, ast.Assign)
            and all(isinstance(t, ast.Name) and t.id.isupper() for t in n.targets))
    ]
    mod = types.ModuleType("app_under_test")
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(REPO / "app.py"), "exec"),
         mod.__dict__)
    return mod


APP = _load_app()


# ---------------------------------------------------------------------------
# Synthetic world
# ---------------------------------------------------------------------------
_COORDS = {
    "P": (0, 0), "F": (10, 10),
    "H1": (1, 2), "H2": (2, 1), "H3": (3, 4), "H4": (12, 3),
    "H5": (13, 2), "H6": (4, 12), "H7": (11, 13), "H8": (2, 9),
}
MATRIX = {
    a: {b: abs(_COORDS[a][0] - _COORDS[b][0]) + abs(_COORDS[a][1] - _COORDS[b][1])
        for b in _COORDS}
    for a in _COORDS
}
CONFIG = {"field_id": "F", "parking_id": "P", "capacity": 8,
          "field_address": "Field Rd", "parking_address": "Park Rd"}
DRIVER = "TestDriver"


def dog(cid, name, pg, dg, cnt=1, staff=False, ride=False):
    return {
        "customer_id": cid, "dog_name": name, "driver": DRIVER,
        "pickup_group": pg, "dropoff_group": dg, "dog_count": cnt,
        "is_staff_dog": staff, "is_ride_along": ride,
        "raw": f"{DRIVER}:{pg}{dg}", "address": f"{cid} St",
        "email": "" if staff else "x@y.com", "no_dropoff": False,
    }


def route(dogs):
    groups = APP.derive_groups(dogs, DRIVER)
    rows = APP.solve_driver(MATRIX, DRIVER, dict(CONFIG, groups=groups), dogs, {})
    return groups, rows


def trip_of(rows, cid, action):
    return [r["Leg"] for r in rows if r["Action"] == action and r["Customer ID"] == cid]


def assert_every_dog_routed_once(rows, dogs):
    cust = [d for d in dogs if not d["is_staff_dog"] and not d["is_ride_along"]]
    for d in cust:
        assert len(trip_of(rows, d["customer_id"], "PICK UP")) == 1, d["dog_name"]
        assert len(trip_of(rows, d["customer_id"], "DROP OFF")) == 1, d["dog_name"]
    warns = [r for r in rows if str(r["Action"]).startswith("⚠")]
    assert not warns, [w["Dog Name"] for w in warns]


# ---------------------------------------------------------------------------
# The Otten case
# ---------------------------------------------------------------------------
def test_gap_driver_gets_trip_2_for_group1_only_dogs():
    """Pickups in groups 1 and 3, none in 2. Group-1 dogs go home in Trip 2."""
    dogs = [dog("H1", "Rex", 1, 1), dog("H2", "Bo", 1, 1), dog("H3", "Zed", 1, 1),
            dog("H4", "Max", 3, 3), dog("H5", "Lu", 3, 3)]
    groups, rows = route(dogs)
    assert groups == [1, 2, 3]
    for cid in ("H1", "H2", "H3"):
        assert trip_of(rows, cid, "DROP OFF") == [2], f"{cid} not dropped in Trip 2"
    assert trip_of(rows, "H4", "PICK UP") == [3]
    assert trip_of(rows, "H4", "DROP OFF") == [4]
    assert_every_dog_routed_once(rows, dogs)


def test_gap_driver_stay_through_dog_waits_for_trip_3():
    """Same gap, but Bo is 1-2 (stays through group 2): Rex Trip 2, Bo Trip 3."""
    dogs = [dog("H1", "Rex", 1, 1), dog("H2", "Bo", 1, 2), dog("H4", "Max", 3, 3)]
    groups, rows = route(dogs)
    assert groups == [1, 2, 3]
    assert trip_of(rows, "H1", "DROP OFF") == [2]
    assert trip_of(rows, "H2", "DROP OFF") == [3]
    assert_every_dog_routed_once(rows, dogs)


def test_staff_dog_in_missing_group_does_not_matter():
    """Staff dogs never create groups, but the gap is still filled."""
    dogs = [dog("H1", "Rex", 1, 1), dog("H4", "Max", 3, 3),
            dog("H6", "StaffPup", 2, 2, staff=True)]
    groups, rows = route(dogs)
    assert groups == [1, 2, 3]
    assert trip_of(rows, "H1", "DROP OFF") == [2]
    assert_every_dog_routed_once(rows, dogs)


# ---------------------------------------------------------------------------
# No-regression cases: drivers WITHOUT a gap keep the same trip structure
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label,dogs,expected_groups", [
    ("normal 1,2,3",
     [dog("H1", "Rex", 1, 1), dog("H2", "Bo", 1, 2), dog("H3", "Zed", 2, 2),
      dog("H4", "Max", 2, 3), dog("H5", "Lu", 3, 3), dog("H8", "Kai", 1, 3)],
     [1, 2, 3]),
    ("only group 1", [dog("H1", "Rex", 1, 1), dog("H2", "Bo", 1, 1)], [1]),
    ("only groups 2 & 3", [dog("H3", "Zed", 2, 2), dog("H4", "Max", 3, 3)], [2, 3]),
    ("pickups 1 & 2, one dog stays to 3",
     [dog("H1", "Rex", 1, 1), dog("H3", "Zed", 2, 3)], [1, 2]),
])
def test_no_gap_drivers_unchanged(label, dogs, expected_groups):
    groups, rows = route(dogs)
    assert groups == expected_groups, label
    assert_every_dog_routed_once(rows, dogs)


def test_normal_driver_trip_assignments():
    """Pin the exact trip each action lands in for a plain 1/2/3 driver."""
    dogs = [dog("H1", "Rex", 1, 1), dog("H2", "Bo", 1, 2), dog("H3", "Zed", 2, 2),
            dog("H4", "Max", 2, 3), dog("H5", "Lu", 3, 3), dog("H8", "Kai", 1, 3)]
    _, rows = route(dogs)
    assert trip_of(rows, "H1", "DROP OFF") == [2]   # 1-1 goes home Trip 2
    assert trip_of(rows, "H2", "DROP OFF") == [3]   # 1-2 goes home Trip 3
    assert trip_of(rows, "H3", "DROP OFF") == [3]   # 2-2 goes home Trip 3
    assert trip_of(rows, "H4", "DROP OFF") == [4]   # 2-3 goes home Trip 4
    assert trip_of(rows, "H8", "DROP OFF") == [4]   # 1-3 goes home Trip 4


def test_trailing_stay_dog_dropped_after_last_pickup_group():
    """Pickups only in 1 & 2; a 2-3 dog is dropped in Trip 3 (last leg), as before."""
    dogs = [dog("H1", "Rex", 1, 1), dog("H3", "Zed", 2, 3)]
    groups, rows = route(dogs)
    assert groups == [1, 2]
    assert trip_of(rows, "H3", "DROP OFF") == [3]


# ---------------------------------------------------------------------------
# Universal invariant: every Group-N-only dog is dropped in Trip N+1
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("pickup_groups", [
    (1,), (2,), (3,), (1, 2), (2, 3), (1, 3), (1, 2, 3),
])
def test_single_group_dogs_always_dropped_next_trip(pickup_groups):
    houses = iter(["H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8"])
    dogs = [dog(next(houses), f"D{g}", g, g) for g in pickup_groups]
    _, rows = route(dogs)
    for d in dogs:
        g = d["pickup_group"]
        assert trip_of(rows, d["customer_id"], "PICK UP") == [g]
        assert trip_of(rows, d["customer_id"], "DROP OFF") == [g + 1], \
            f"group-{g}-only dog {d['dog_name']} not dropped in Trip {g + 1}"
    assert_every_dog_routed_once(rows, dogs)


# ---------------------------------------------------------------------------
# XX-code rules (Elizabeth, Sep 2026)
# ---------------------------------------------------------------------------
def xxdog(cid, name, code, addr_house="H1"):
    """Build an assignment dict via the real parser semantics by calling route()
    inputs directly — pickup/dropoff/no_pickup mirror parse_schedule's XX logic."""
    import re as _re
    m = _re.search(r"(\d?)\s*[Xx]{2}\s*(\d*)", code)
    digits = _re.findall(r"\d", code)
    is_ra = "xx" in code.lower() and len(digits) >= 3
    if m and not is_ra:
        pre, post = m.group(1), m.group(2)
        pg = int(pre) if pre else 1
        dg = int(post[-1]) if post else 3
        np = (pre == "" and post != "")
    else:
        pg, dg, np = int(digits[0]), int(digits[-1]), False
    d = dog(addr_house, name, pg, dg)
    d["customer_id"] = cid
    d["no_pickup"] = np
    d["no_dropoff"] = bool(code.strip()) and not code.strip()[-1].isdigit()
    d["is_ride_along"] = is_ra
    d["raw"] = f"{DRIVER}:{code}"
    return d


def test_parser_xx_fields():
    """The real parser must produce the XX fields this suite assumes."""
    sched = [["h"] * 11, ["h"] * 11]
    hdr = ["addr", "dog", "", "", "email", "ph", "id", "", "lat", "lng", "code"]
    cases = {"1XX": (1, 3, False, False), "XX2": (1, 2, True, False),
             "2XX": (2, 3, False, False), "XX1": (1, 1, True, False),
             "1XX2": (1, 2, False, False), "1XX3": (1, 3, False, False),
             "1XX1": (1, 1, False, False), "1XX23": (1, 3, False, True)}
    rows = [["h"] * 11, ["h"] * 11]
    ids = []
    for i, code in enumerate(cases):
        cid = f"90{i}x"
        ids.append((cid, code))
        rows.append([f"{i} Xx St", "Dog" + cid, "", "", "x@y.com", "5", cid, "",
                     "42.3", "-71.3", f"{DRIVER}:{code}"])
    parsed = APP.parse_schedule(rows, 10)
    by_id = {a["customer_id"]: a for a in parsed}
    for cid, code in ids:
        pg, dg, np, ra = cases[code]
        a = by_id[cid]
        assert a["pickup_group"] == pg, (code, a["pickup_group"])
        assert a["dropoff_group"] == dg, (code, a["dropoff_group"])
        assert a.get("no_pickup", False) == np, code
        assert a.get("is_ride_along", False) == ra, code
    # legacy: 3+ digit XX stays ride-along; ends-in-letters stays no_dropoff
    assert by_id[[c for c, k in ids if k == "1XX"][0]]["no_dropoff"] is True


def _acts_for(rows, cid):
    return sorted((r["Action"], r["Leg"]) for r in rows if r["Customer ID"] == cid
                  and r["Action"] in ("PICK UP", "DROP OFF", "RIDE-ALONG"))


def test_xx_one_sided_codes():
    """PXX: pickup only, seat to end of day. XXD: drop only, seat from start."""
    dogs = [dog("H1", "Rex", 1, 1), dog("H3", "Zed", 2, 2), dog("H5", "Lu", 3, 3),
            xxdog("H2", "SleepIn", "XX2"),    # no pickup, drop in trip 3
            xxdog("H4", "TakeHome", "1XX")]   # pickup trip 1, no drop, rides all day
    groups, rows = route(dogs)
    assert groups == [1, 2, 3]
    assert _acts_for(rows, "H2") == [("DROP OFF", 3)], _acts_for(rows, "H2")
    assert _acts_for(rows, "H4") == [("PICK UP", 1)], _acts_for(rows, "H4")


def test_xx_two_digit_unchanged():
    """1XX3 etc. stay routed exactly like before: pickup first digit, drop last+1."""
    dogs = [dog("H1", "Rex", 1, 1), dog("H3", "Zed", 2, 2), dog("H5", "Lu", 3, 3),
            xxdog("H2", "Span", "1XX3"), xxdog("H4", "Tight", "2XX2")]
    groups, rows = route(dogs)
    assert _acts_for(rows, "H2") == [("DROP OFF", 4), ("PICK UP", 1)]
    assert _acts_for(rows, "H4") == [("DROP OFF", 3), ("PICK UP", 2)]


def test_xx_sleepover_sentinel_row():
    """1XX23: no real stops; ONE pinned display row, Trip 1 Stop 0, with address."""
    ra = xxdog("H6", "Snoozer", "1XX23")
    dogs = [dog("H1", "Rex", 1, 1), dog("H3", "Zed", 2, 2), dog("H5", "Lu", 3, 3), ra]
    groups, rows = route(dogs)
    acts = _acts_for(rows, "H6")
    assert acts == [("RIDE-ALONG", 1)], acts
    ra_rows = [r for r in rows if r["Customer ID"] == "H6"]
    assert len(ra_rows) == 1 and ra_rows[0]["Stop"] == 0
    assert ra_rows[0]["Address"] == ra["address"]          # home address shown
    assert "RIDE-ALONG" in ra_rows[0]["Dog Name"]
    # every real dog still routed once each way
    assert_every_dog_routed_once(rows, [d for d in dogs if d["customer_id"] != "H6"])


def test_checklist_xx_spans():
    spans = {"1XX": [1, 2, 3], "1XX1": [1], "XX1": [1], "XX2": [1, 2],
             "2XX2": [2], "2XX": [2, 3], "1XX23": [1, 2, 3], "1XX3": [1, 2, 3],
             "2&3": [2, 3], "123": [1, 2, 3], "2": [2]}
    for code, want in spans.items():
        got = sorted(APP._checklist_groups(code))
        assert got == want, (code, got, want)


# ---------------------------------------------------------------------------
# Potty codes (Elizabeth, Sep 2026): NPotty = routed visit during trip N,
# zero capacity, no group/checklist membership. N may be 1-4.
# ---------------------------------------------------------------------------
def pottydog(cid, trip, house="H6"):
    d = dog(house, f"Potty{trip}", trip, trip)
    d["customer_id"] = cid
    d["is_potty"] = True
    d["no_dropoff"] = True
    d["raw"] = f"{DRIVER}:{trip}Potty"
    return d


BASE = lambda: [dog("H1", "Rex", 1, 1), dog("H2", "Bo", 1, 2), dog("H3", "Zed", 2, 2),
                dog("H4", "Max", 2, 3), dog("H5", "Lu", 3, 3)]


@pytest.mark.parametrize("trip", [1, 2, 3, 4])
def test_potty_lands_in_its_trip(trip):
    dogs = BASE() + [pottydog("H6", trip)]
    groups, rows = route(dogs)
    assert groups == [1, 2, 3], f"potty must never change trip structure (got {groups})"
    acts = [(r["Action"], r["Leg"]) for r in rows if r["Customer ID"] == "H6"]
    assert acts == [("POTTY", trip)], acts
    prow = [r for r in rows if r["Customer ID"] == "H6"][0]
    assert "POTTY BREAK" in prow["Dog Name"] and "◼" not in prow["Dog Name"]
    assert_every_dog_routed_once(rows, [d for d in dogs if not d.get("is_potty")])


def test_potty_takes_no_capacity():
    """Adding a potty visit must not change any other stop's Dogs on Board."""
    base_rows = route(BASE())[1]
    with_potty = route(BASE() + [pottydog("H6", 2)])[1]
    def loads(rows):
        return {(r["Customer ID"], r["Action"]): r["Dogs on Board"]
                for r in rows if r["Customer ID"] not in ("H6",) and
                r["Action"] in ("PICK UP", "DROP OFF") and r["Dogs on Board"] != ""}
    assert loads(base_rows) == loads(with_potty)


def test_potty_not_in_checklist():
    assert list(APP._checklist_groups("1Potty")) == []
    assert list(APP._checklist_groups("4Potty")) == []
    rows = route(BASE() + [pottydog("H6", 2)])[1]
    ck = APP.build_driver_checklist(rows)
    assert not any("Potty2" in row[0] for row in ck), ck
