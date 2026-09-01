"""
update_matrix.py — Standalone script to check for new dogs and add them to the matrix.
Runs via GitHub Actions on a schedule. No Streamlit dependency.
"""

import csv
import io
import json
import os
import re
import time
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import gspread

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_credentials():
    """Load GCP credentials from environment variable."""
    creds_json = os.environ.get("GCP_SERVICE_ACCOUNT")
    if not creds_json:
        raise ValueError("GCP_SERVICE_ACCOUNT environment variable not set")
    creds_info = json.loads(creds_json)
    return Credentials.from_service_account_info(creds_info, scopes=SCOPES)


def load_matrix_from_drive(creds, matrix_file_name):
    """Download and parse the matrix CSV from Google Drive."""
    drive = build("drive", "v3", credentials=creds)
    file_list = drive.files().list(
        q=f"name='{matrix_file_name}' and trashed=false",
        fields="files(id, name)"
    ).execute().get("files", [])

    if len(file_list) > 1:
        raise ValueError(
            f"{len(file_list)} files named '{matrix_file_name}' found in Drive — refusing to "
            f"update any of them. Delete the duplicate(s) so exactly one remains."
        )
    if not file_list:
        raise ValueError(f"Matrix file '{matrix_file_name}' not found in Drive")

    file_id = file_list[0]["id"]
    print(f"Found matrix: {file_list[0]['name']}")

    req = drive.files().get_media(fileId=file_id)
    content = io.BytesIO()
    dl = MediaIoBaseDownload(content, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    content.seek(0)
    text = content.read().decode("utf-8-sig")

    matrix = {}
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    col_ids = [h.strip().replace("\r", "") for h in header[1:] if h.strip()]
    for row in reader:
        rid = row[0].strip().replace("\r", "")
        if not rid:
            continue
        matrix[rid] = {}
        for i, cid in enumerate(col_ids):
            if i + 1 >= len(row):
                break
            v = row[i + 1].strip().replace("\r", "")
            if v:
                matrix[rid][cid] = float(v.replace(",", "."))
            else:
                matrix[rid][cid] = 9999

    return matrix, file_id, text


def load_schedule(creds, sheet_id):
    """Load the Schedule tab from Google Sheets."""
    client = gspread.authorize(creds)
    sheet = client.open_by_key(sheet_id)
    ws = sheet.worksheet("Schedule")
    return ws.get_all_values()


def parse_lat_lng(lat_raw, lng_raw):
    """Tolerant coordinate parser: handles European comma decimals (42,3601),
    both values pasted into one cell (42.36, -71.05), degree symbols, stray
    spaces/quotes, and swapped lat/lng columns. Returns (lat, lng) or None."""
    def _clean(v):
        return (v or "").strip().replace("\u00b0", "").replace("'", "").replace('"', "")
    lat_s, lng_s = _clean(lat_raw), _clean(lng_raw)
    if lat_s and not lng_s and "," in lat_s:
        parts = [p.strip() for p in lat_s.split(",") if p.strip()]
        if len(parts) == 2:
            lat_s, lng_s = parts
    if not lat_s or not lng_s:
        return None
    def _to_f(s):
        s = s.replace(" ", "")
        if "," in s and "." not in s:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
        try:
            return float(s)
        except ValueError:
            return None
    lat, lng = _to_f(lat_s), _to_f(lng_s)
    if lat is None or lng is None:
        return None
    if abs(lat) > 90 and abs(lng) <= 90:
        lat, lng = lng, lat
    elif lat < 0 and lng > 0:
        # US coordinates: lat is positive, lng negative — columns were swapped
        lat, lng = lng, lat
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def ors_geocode(address, ors_key):
    """Geocode a street address via ORS. Returns (lat, lng) or None.
    TOWN-VERIFIED: the result's town must match the town written in the
    address ("..., Wayland, MA" must resolve to Wayland). An ambiguous
    street name resolved to another town is REJECTED, not silently used —
    that silent substitution was the root cause of the 2026 anchor bug."""
    import requests
    if not address or not ors_key:
        return None
    # expected town = the part right before the "MA" token
    exp_town = ""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    for i, p in enumerate(parts):
        if p.upper().replace(".", "").startswith("MA") and len(p) <= 8 and i > 0:
            exp_town = parts[i - 1].lower()
            break
    try:
        r = requests.get(
            "https://api.heigit.org/pelias/v1/search",
            params={"api_key": _ors_auth(ors_key), "text": address,
                    "boundary.country": "US", "size": 3},
            timeout=15,
        )
        if r.status_code == 403 and _ors_backup_key() and not _ORS_KEY_STATE["switched"]:
            _ORS_KEY_STATE["active"] = _ors_backup_key()
            _ORS_KEY_STATE["switched"] = True
            print("  ORS primary key quota exhausted on geocode — switching to backup key (ORS_API_KEY_2)")
            r = requests.get(
                "https://api.heigit.org/pelias/v1/search",
                params={"api_key": _ORS_KEY_STATE["active"], "text": address,
                        "boundary.country": "US", "size": 3},
                timeout=15,
            )
        feats = r.json().get("features", [])
        for f in feats:
            props = f.get("properties", {})
            towns = {str(props.get(k, "")).lower()
                     for k in ("locality", "localadmin", "county", "name")}
            if not exp_town or any(exp_town in t for t in towns if t):
                lng, lat = f["geometry"]["coordinates"]
                return float(lat), float(lng)
        if feats:
            _lbl = feats[0].get("properties", {}).get("label", "?")
            print(f"  Geocode REJECTED for '{address}': best hit was '{_lbl}' "
                  f"(wrong town) — not using it; will retry next run")
    except Exception as e:
        print(f"  Geocode failed for '{address}': {e}")
    return None


def geocode_missing_coords(creds, schedule_data, schedule_sheet_id, matrix, ors_key):
    """Dogs missing from the matrix AND missing lat/lng: geocode their address
    (col A) and write coords back into Schedule cols I/J so every downstream
    step (this run's add, the app, future runs) can see them."""
    matrix_ids = set(matrix.keys())
    targets = []
    for idx, row in enumerate(schedule_data):
        if idx < 2:
            continue
        cid = row[6].strip() if len(row) > 6 else ""
        # Staff dogs (no email in col E) are never routed — they don't need
        # coordinates or matrix entries.
        if not (row[4].strip() if len(row) > 4 else ""):
            continue
        lat = row[8].strip() if len(row) > 8 else ""
        lng = row[9].strip() if len(row) > 9 else ""
        addr = row[0].strip() if len(row) > 0 else ""
        if not cid or cid in matrix_ids or parse_lat_lng(lat, lng) is not None or not addr:
            continue
        has_assignment = False
        for col_idx in range(10, min(len(row), 53)):
            val = row[col_idx].strip()
            if val and ":" in val and "cancel" not in val.lower():
                has_assignment = True
                break
        if has_assignment:
            targets.append((idx, cid, addr))
    if not targets:
        return 0
    print(f"\nGeocoding {len(targets)} dog(s) with missing coordinates...")
    ws = None
    try:
        client = gspread.authorize(creds)
        ws = client.open_by_key(schedule_sheet_id).worksheet("Schedule")
    except Exception as e:
        print(f"  Warning: can't open Schedule for write-back: {e}")
    fixed = 0
    for idx, cid, addr in targets:
        res = ors_geocode(addr, ors_key)
        if res is None:
            print(f"  ✗ {cid}: could not geocode '{addr}'")
            continue
        lat, lng = res
        schedule_data[idx][8] = str(lat)
        schedule_data[idx][9] = str(lng)
        if ws is not None:
            try:
                ws.update_cell(idx + 1, 9, lat)
                ws.update_cell(idx + 1, 10, lng)
            except Exception as e:
                print(f"  Warning: write-back failed for {cid}: {e}")
        print(f"  ✓ {cid}: {addr} -> ({lat}, {lng})")
        fixed += 1
    return fixed


def find_missing_temp_addresses(creds, sheet_id, matrix, ors_key):
    """TempAddresses tab (A=orig ID, B=temp ID, C=temp address, D/E=dates).
    Any temp ID not yet in the matrix gets geocoded and queued for adding —
    regardless of dates, so addresses are ready ahead of time and reusable."""
    import gspread
    try:
        gc = gspread.authorize(creds)
        rows = gc.open_by_key(sheet_id).worksheet("TempAddresses").get_all_values()
    except Exception:
        return {}
    queued = {}
    # Layout: A=Customer Name, B=original ID, C=temp ID, D=temp address, E/F=dates
    _placeholder = {"new id", "new address", "id", "address", "temp id", "temp address", "example"}
    for row in rows[1:]:
        temp = row[2].strip() if len(row) > 2 else ""
        addr = row[3].strip() if len(row) > 3 else ""
        if not temp or not addr or temp in matrix or temp in queued:
            continue
        # skip template/placeholder rows ("New ID @ New Address" etc.)
        if temp.lower() in _placeholder or addr.lower() in _placeholder:
            continue
        res = ors_geocode(addr, ors_key)
        if not res:
            print(f"  ✗ temp address {temp}: could not geocode '{addr}'")
            continue
        lat, lng = res
        # region guard: service area is eastern Massachusetts — anything outside
        # is a bad geocode or bad row, never a real stop
        if not (41.5 <= lat <= 43.2 and -72.5 <= lng <= -70.0):
            print(f"  ✗ temp address {temp}: geocoded OUTSIDE the service region "
                  f"({lat:.3f},{lng:.3f}) — skipping, check the row/address")
            continue
        queued[temp] = {"lat": lat, "lng": lng}
        print(f"  + temp address {temp} @ '{addr[:40]}' queued for matrix add")
    return queued


def check_address_changes(creds, schedule_data, matrix, ors_key, schedule_sheet_id,
                          file_id, matrix_text, max_movers_per_run=3):
    """Detect dogs whose Schedule ADDRESS changed since their matrix distances
    were measured (AddressLog tab, Routing sheet: A=id, B=address-as-measured,
    C=coords-as-measured, D=date last measured — an audit trail of matrix adds).

    For each mover (capped per run):
      1. geocode the NEW address; update Schedule I/J and in-memory rows
      2. RESET the dog's entire matrix row+column to 9999 in the CSV
         (kills every stale old-house value, near and far)
      3. refill NEARBY pairs (7-mile rule, same as adds) from the new house
         via grouped ORS calls; far pairs stay 9999 by design
      4. upload the CSV, THEN update the log — so a quota death mid-refill
         leaves ordinary 9999s that the normal repair pass finishes later,
         and a totally failed mover keeps its OLD log entry and retriggers.

    First run seeds the log without re-measuring. Blank a dog's col-B cell to
    force a re-measure. Returns (matrix_text, moved_ids) — matrix_text MUST be
    reassigned by the caller so later steps see the reset."""
    import gspread, math, datetime
    try:
        gc = gspread.authorize(creds)
        routing_sheet_id = os.environ.get("ROUTING_SHEET_ID", "").strip()
        book = gc.open_by_key(routing_sheet_id) if routing_sheet_id else gc.open(
            os.environ.get("ROUTING_SHEET_NAME", "Routing"))
        try:
            ws = book.worksheet("AddressLog")
            if getattr(ws, "col_count", 4) < 4:
                ws.resize(cols=4)   # A=id B=address C=coords D=last-measured date
        except gspread.exceptions.WorksheetNotFound:
            ws = book.add_worksheet(title="AddressLog", rows=2500, cols=4)
    except Exception as e:
        print(f"  AddressLog unavailable ({e}) — skipping address-change check")
        return matrix_text, set()

    log_rows = ws.get_all_values()
    logged = {r[0].strip(): (r[1].strip() if len(r) > 1 else "")
              for r in log_rows if r and r[0].strip()}
    logged_ll = {r[0].strip(): (r[2].strip() if len(r) > 2 else "")
                 for r in log_rows if r and r[0].strip()}

    current = {}
    for row in schedule_data[1:]:
        if len(row) > 6 and row[6].strip() and row[0].strip():
            current[row[6].strip()] = " ".join(row[0].split())

    if not logged:
        # One-time: the Aug 2026 audit found exactly these dogs mislocated in the
        # original matrix build. Seeding them with a BLANK measured-address makes
        # the mover machinery re-geocode and re-measure them automatically over
        # the following runs — no manual cell-blanking needed. Harmless to leave
        # in place: it only applies when the AddressLog is first created.
        _force_remeasure = {"1203x", "1205x", "919x", "920x", "924x", "1454x",
                            "1931x", "2396x", "2340x", "2388x", "2409x", "2416x",
                            "1571x", "1733x", "2244x"}
        _cur_ll = {}
        for row in schedule_data[1:]:
            if len(row) > 9 and row[6].strip():
                _ll = parse_lat_lng(row[8], row[9])
                if _ll:
                    _cur_ll[row[6].strip()] = f"{_ll[0]},{_ll[1]}"
        seed = [[cid, ("" if cid in _force_remeasure else addr),
                 _cur_ll.get(cid, ""), ""]   # D=last-measured (blank = pre-existing/original)
                for cid, addr in sorted(current.items()) if cid in matrix]
        if seed:
            ws.update(range_name="A1", values=seed)
            print(f"  AddressLog seeded with {len(seed)} dog(s) — tracking ON from next run. "
                  f"(Blank a dog's col-B cell to force a re-measure.)")
        return matrix_text, set()

    changed = [cid for cid, addr in current.items()
               if cid in matrix and cid in logged and logged[cid] != addr]
    if changed:
        print(f"  {len(changed)} dog(s) with a CHANGED address: "
              f"{', '.join(changed[:12])}{'...' if len(changed) > 12 else ''}"
              + (f" — handling {max_movers_per_run} this run" if len(changed) > max_movers_per_run else ""))

    # coords for the 7-mile neighborhood test (Schedule + Locations)
    coords_lookup = {}
    for row in schedule_data[1:]:
        if len(row) > 9 and row[6].strip():
            _ll = parse_lat_lng(row[8], row[9])
            if _ll:
                coords_lookup[row[6].strip()] = {"lat": _ll[0], "lng": _ll[1]}
    try:
        loc_ws = book.worksheet("Locations")
        for row in loc_ws.get_all_values()[1:]:
            if len(row) >= 3 and row[0].strip():
                _ll = parse_lat_lng(row[1], row[2])
                if _ll:
                    coords_lookup[row[0].strip()] = {"lat": _ll[0], "lng": _ll[1]}
    except Exception:
        pass

    def _hav_mi(a, b):
        R = 3958.8
        p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
        dp = math.radians(b["lat"] - a["lat"]); dl = math.radians(b["lng"] - a["lng"])
        x = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return 2 * R * math.asin(math.sqrt(x))

    reader = csv.reader(io.StringIO(matrix_text))
    all_rows = list(reader)
    header = all_rows[0]
    data_rows = all_rows[1:]
    col_idx = {h.strip(): i for i, h in enumerate(header)}
    row_idx = {r[0].strip(): i for i, r in enumerate(data_rows)}

    moved_done = []
    for cid in changed[:max_movers_per_run]:
        if _ORS_QUOTA_HIT["v"]:
            print("    quota exhausted — remaining movers handled next run")
            break
        # COORDINATES FIRST: if the Schedule's lat/lng for this dog is fresh
        # (hand-updated — differs from the logged coords) or this is a forced
        # re-measure with usable coords, trust the Schedule's exact numbers
        # verbatim. Geocode only when the coords are absent or stale.
        _cur = None
        _srow = None
        for row in schedule_data[1:]:
            if len(row) > 6 and row[6].strip() == cid:
                _srow = row
                if len(row) > 9:
                    _cur = parse_lat_lng(row[8], row[9])
                break
        _cur_s = f"{_cur[0]},{_cur[1]}" if _cur else ""
        _fresh = bool(_cur) and (_cur_s != logged_ll.get(cid, "") or not logged[cid])
        if _fresh:
            new_coords = {"lat": _cur[0], "lng": _cur[1]}
            print(f"    {cid}: using Schedule's exact lat/lng (hand-updated) — no geocoding")
        else:
            _g = ors_geocode(current[cid], ors_key)
            if not _g:
                print(f"    {cid}: could not geocode new address — will retry next run")
                continue
            new_coords = {"lat": _g[0], "lng": _g[1]}
            try:
                sched_ws = gc.open_by_key(schedule_sheet_id).worksheet("Schedule")
                for ri, row in enumerate(schedule_data[1:], start=2):
                    if len(row) > 6 and row[6].strip() == cid:
                        sched_ws.update(range_name=f"I{ri}:J{ri}",
                                        values=[[new_coords["lat"], new_coords["lng"]]])
                        break
            except Exception as e:
                print(f"    {cid}: coord write failed ({e}) — will retry next run")
                continue
        if _srow is not None:
            while len(_srow) < 10:
                _srow.append("")
            _srow[8] = str(new_coords["lat"])
            _srow[9] = str(new_coords["lng"])
        coords_lookup[cid] = new_coords

        if cid not in row_idx or cid not in col_idx:
            print(f"    {cid}: not present in matrix CSV — the add path will handle it")
            continue
        ri = row_idx[cid]; ci = col_idx[cid]
        did_reset = False
        def _apply_reset():
            for j in range(1, len(data_rows[ri])):
                data_rows[ri][j] = "0" if j == ci else "9999"
            for r in data_rows:
                if len(r) > ci:
                    r[ci] = "0" if r[0].strip() == cid else "9999"
        # refill NEARBY pairs from the NEW house (7-mile rule, both directions)
        # NOTE: the stale row is wiped only after the FIRST successful ORS call,
        # so a dead-quota run leaves the dog usable (stale) instead of an empty
        # shell that the app then flags as a failed id.
        nearby = [oid for oid, c in coords_lookup.items()
                  if oid != cid and oid in col_idx and oid in row_idx
                  and (_hav_mi(new_coords, c) <= 7.0 or oid.endswith(("F", "P")))]
        filled = 0
        for direction in ("out", "in"):
            for k in range(0, len(nearby), 45):
                if _ORS_QUOTA_HIT["v"]:
                    break
                chunk = nearby[k:k + 45]
                if direction == "out":
                    locations = [[new_coords["lng"], new_coords["lat"]]] + [
                        [coords_lookup[d]["lng"], coords_lookup[d]["lat"]] for d in chunk]
                    payload = {"locations": locations, "sources": [0],
                               "destinations": list(range(1, len(chunk) + 1)),
                               "metrics": ["duration"]}
                else:
                    locations = [[coords_lookup[d]["lng"], coords_lookup[d]["lat"]] for d in chunk] + [
                        [new_coords["lng"], new_coords["lat"]]]
                    payload = {"locations": locations, "sources": list(range(len(chunk))),
                               "destinations": [len(chunk)], "metrics": ["duration"]}
                resp = _ors_matrix_call(
                    "https://api.heigit.org/openrouteservice/v2/matrix/driving-car",
                    {"Authorization": ors_key, "Content-Type": "application/json"},
                    payload, print)
                if resp is not None and resp.status_code == 200:
                    if not did_reset:
                        _apply_reset()
                        did_reset = True
                    durs = resp.json().get("durations", [])
                    for j, d in enumerate(chunk):
                        try:
                            v = durs[0][j] if direction == "out" else durs[j][0]
                        except Exception:
                            v = None
                        if v is None:
                            continue
                        val = str(round(v / 60, 1))
                        if direction == "out":
                            data_rows[row_idx[cid]][col_idx[d]] = val
                        else:
                            data_rows[row_idx[d]][col_idx[cid]] = val
                        filled += 1
                time.sleep(2.0)
        if not did_reset:
            print(f"    {cid}: quota dead before any measurement — left stale (usable), retry next run")
            continue
        print(f"    {cid}: reset stale row/col, refilled {filled}/{len(nearby) * 2} nearby "
              f"pairs from '{current[cid][:40]}'"
              + (" (rest finish via normal repair)" if filled < len(nearby) * 2 else ""))
        moved_done.append(cid)

    if not moved_done:
        return matrix_text, set()

    # upload the reset+refilled CSV, THEN update the log
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(data_rows)
    new_text = out.getvalue()
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload
        service = build("drive", "v3", credentials=creds)
        media = MediaIoBaseUpload(io.BytesIO(new_text.encode("utf-8")),
                                  mimetype="text/csv", resumable=True)
        service.files().update(fileId=file_id, media_body=media).execute()
        print(f"  Matrix uploaded with {len(moved_done)} re-measured mover(s)")
    except Exception as e:
        print(f"  Upload FAILED ({e}) — log NOT updated; movers retrigger next run")
        return matrix_text, set()

    # sync the in-memory dict so downstream steps (missing scan, repair) see truth
    def _f(s):
        try:
            return float(str(s).strip().replace(",", "."))
        except Exception:
            return 9999.0
    for cid in moved_done:
        for oid in list(matrix.get(cid, {})):
            matrix[cid][oid] = _f(data_rows[row_idx[cid]][col_idx[oid]]) if oid in col_idx else 9999.0
        for rid in matrix:
            if cid in matrix[rid] and rid in row_idx:
                matrix[rid][cid] = _f(data_rows[row_idx[rid]][col_idx[cid]])

    try:
        for i, r in enumerate(log_rows, start=1):
            cid = r[0].strip() if r else ""
            if cid in moved_done:
                _c = coords_lookup.get(cid, {})
                _cs = f"{_c.get('lat','')},{_c.get('lng','')}" if _c else ""
                _today = datetime.date.today().isoformat()
                ws.update(range_name=f"B{i}:D{i}", values=[[current[cid], _cs, _today]])
        new_dogs = [cid for cid in current if cid not in logged and cid in matrix]
        if new_dogs:
            _today = datetime.date.today().isoformat()
            ws.append_rows([[cid, current[cid], "", _today] for cid in sorted(new_dogs)])
    except Exception as e:
        # a failed log write only means the mover re-triggers next run — never
        # allowed to crash the run and block the repair pass behind it
        print(f"  AddressLog write failed ({e}) — movers will re-verify next run")
    return new_text, set(moved_done)


def find_missing_dogs(matrix, schedule_data):
    """Find dogs in the Schedule that aren't in the matrix."""
    matrix_ids = set(matrix.keys())
    missing = {}

    # Check ALL date columns for dog IDs that have assignments
    # We care about any dog that might be scheduled, not just today
    for row in schedule_data[2:]:  # skip header + sub-header
        cid = row[6].strip() if len(row) > 6 else ""
        # Staff dogs (no email in col E) are never routed — skip; no matrix entry needed.
        if not (row[4].strip() if len(row) > 4 else ""):
            continue
        lat = row[8].strip() if len(row) > 8 else ""
        lng = row[9].strip() if len(row) > 9 else ""

        if not cid or cid in matrix_ids or cid in missing:
            continue
        _ll = parse_lat_lng(lat, lng)
        if _ll is None:
            continue
        lat, lng = str(_ll[0]), str(_ll[1])

        # Check if this dog has any assignment in any date column
        has_assignment = False
        first_col = None
        for col_idx in range(10, min(len(row), 53)):
            val = row[col_idx].strip()
            if val and ":" in val and "cancel" not in val.lower():
                has_assignment = True
                first_col = col_idx
                break

        if has_assignment:
            try:
                missing[cid] = {"lat": float(lat), "lng": float(lng), "prio": first_col}
            except ValueError:
                continue

    return missing


_ORS_QUOTA_HIT = {"v": False}

# KEY FAILOVER: when the primary ORS key's daily quota dies (403), switch to
# the backup key in the ORS_API_KEY_2 secret (a second free ORS account) and
# keep working. "active" holds the backup key once switched; every ORS call
# funnels through _ors_matrix_call / ors_geocode, which check it.
_ORS_KEY_STATE = {"active": None, "switched": False}


def _ors_backup_key():
    return os.environ.get("ORS_API_KEY_2", "").strip()


def _ors_auth(primary_key):
    """The key every ORS call should use right now."""
    return _ORS_KEY_STATE["active"] or primary_key


def _osrm_table(new_loc, batch_coords, new_is_source, log):
    """QUOTA BACKUP measurement: real road driving times from the public OSRM
    server (router.project-osrm.org — no key, no daily quota, same OSM road
    network ORS routes on). Returns list of minutes (or None per slot), or None
    if the request failed entirely. new_loc/batch_coords are [lng, lat]."""
    import time as _t
    import requests as _rq
    coords = ";".join(f"{c[0]},{c[1]}" for c in [new_loc] + batch_coords)
    n = len(batch_coords)
    dest_idx = ";".join(str(i) for i in range(1, n + 1))
    if new_is_source:
        params = {"sources": "0", "destinations": dest_idx, "annotations": "duration"}
    else:
        params = {"sources": dest_idx, "destinations": "0", "annotations": "duration"}
    url = f"https://router.project-osrm.org/table/v1/driving/{coords}"
    for attempt in (1, 2, 3):
        try:
            resp = _rq.get(url, params=params, timeout=60)
            if resp.status_code == 200:
                dur = resp.json().get("durations") or []
                out = []
                for i in range(n):
                    v = dur[0][i] if new_is_source else (dur[i][0] if i < len(dur) and dur[i] else None)
                    out.append(round(v / 60, 1) if v is not None else None)
                _t.sleep(1.0)   # be polite to the public server
                return out
            log(f"    OSRM error {resp.status_code} (attempt {attempt}/3)")
        except Exception as e:
            log(f"    OSRM request failed ({e}) (attempt {attempt}/3)")
        _t.sleep(5 * attempt)
    return None


def _ors_matrix_call(url, headers, payload, log):
    """POST to ORS matrix API with one retry on rate-limit (429).
    On a 403 (daily quota dead), fails over ONCE to ORS_API_KEY_2 if set."""
    import time as _t
    import requests as _rq
    waits = {1: 10, 2: 30, 3: 60, 4: 120}
    headers = dict(headers)
    if _ORS_KEY_STATE["active"]:
        headers["Authorization"] = _ORS_KEY_STATE["active"]
    for attempt in (1, 2, 3, 4, 5):
        try:
            resp = _rq.post(url, headers=headers, json=payload, timeout=60)
            if resp.status_code == 403:
                _backup = _ors_backup_key()
                if _backup and not _ORS_KEY_STATE["switched"]:
                    _ORS_KEY_STATE["active"] = _backup
                    _ORS_KEY_STATE["switched"] = True
                    headers["Authorization"] = _backup
                    log("    ORS primary key quota exhausted (403) — SWITCHING to backup "
                        "key (ORS_API_KEY_2) and retrying...")
                    continue
                _ORS_QUOTA_HIT["v"] = True
                _rem = resp.headers.get("x-ratelimit-remaining", "?")
                _rst = resp.headers.get("x-ratelimit-reset", "?")
                log(f"    ORS DAILY quota exhausted (403"
                    f"{', backup key too' if _ORS_KEY_STATE['switched'] else ''}) — "
                    f"remaining: {_rem}, resets: {_rst}. "
                    f"Aborting remaining ORS work for this run.")
                return resp
            if resp.status_code == 429 and attempt < 4:
                _rem = resp.headers.get("x-ratelimit-remaining", "?")
                log(f"    ORS rate-limited (429, remaining: {_rem}) — backing off {waits[attempt]}s (retry {attempt}/4)...")
                _t.sleep(waits[attempt])
                continue
            return resp
        except Exception as e:
            if attempt < 4:
                log(f"    ORS request error ({e}) — retrying in {waits[attempt]}s...")
                _t.sleep(waits[attempt])
                continue
            raise
    return resp


def add_dogs_to_matrix(creds, matrix, missing_dogs, schedule_data, file_id, matrix_csv_text, ors_key):
    """Add missing dogs to the matrix using ORS API and upload to Drive."""
    # Get coords for existing matrix entries
    existing_with_coords = {}
    for row in schedule_data[2:]:
        cid = row[6].strip() if len(row) > 6 else ""
        lat = row[8].strip() if len(row) > 8 else ""
        lng = row[9].strip() if len(row) > 9 else ""
        if cid in matrix and lat and lng:
            _ll = parse_lat_lng(lat, lng)
            if _ll:
                existing_with_coords[cid] = {"lat": _ll[0], "lng": _ll[1]}

    # Also load field/parking coordinates from Locations tab
    try:
        client = gspread.authorize(creds)
        routing_sheet_id = os.environ.get("ROUTING_SHEET_ID", "").strip()
        if routing_sheet_id:
            loc_sheet = client.open_by_key(routing_sheet_id)
        else:
            sheet_name = os.environ.get("ROUTING_SHEET_NAME", "Routing")
            loc_sheet = client.open(sheet_name)
        loc_ws = loc_sheet.worksheet("Locations")
        loc_data = loc_ws.get_all_values()
        for row in loc_data[1:]:
            if len(row) >= 3:
                loc_id = row[0].strip()
                lat = row[1].strip()
                lng = row[2].strip()
                if loc_id and lat and lng and loc_id in matrix:
                    _ll = parse_lat_lng(lat, lng)
                    if _ll:
                        existing_with_coords[loc_id] = {"lat": _ll[0], "lng": _ll[1]}
        print(f"  Loaded {sum(1 for k in existing_with_coords if k.endswith('F') or k.endswith('P'))} field/parking coordinates from Locations tab")
    except Exception as e:
        print(f"  Warning: could not load Locations tab: {e}")

    _fp_loaded = sum(1 for k in existing_with_coords if k.endswith("F") or k.endswith("P"))
    if _fp_loaded == 0:
        print("  🛑 ZERO field/parking coordinates loaded — dogs added now would be "
              "unreachable from every field and parking lot. NOT adding any dogs this run. "
              "Fix the Locations tab access (set ROUTING_SHEET_ID or ROUTING_SHEET_NAME "
              "secret; share the Routing sheet with the service account).")
        return matrix, 0

    existing_ids = list(existing_with_coords.keys())
    existing_coords = [[existing_with_coords[eid]["lng"], existing_with_coords[eid]["lat"]]
                       for eid in existing_ids]

    if not existing_ids:
        print("No existing dogs have coordinates — cannot compute distances.")
        return

    # Parse current CSV
    reader_obj = csv.reader(io.StringIO(matrix_csv_text))
    all_rows = list(reader_obj)
    header = all_rows[0]
    data_rows = all_rows[1:]

    _ORS_QUOTA_HIT["v"] = False
    added_count = 0
    fallback_ids = []   # dogs measured via OSRM (quota backup) — re-measured with ORS next run
    # soonest-scheduled first (earliest date column wins; temps last)
    for new_id, new_coords in sorted(missing_dogs.items(),
                                     key=lambda kv: kv[1].get("prio", 999)):
        if _ORS_QUOTA_HIT["v"]:
            print(f"    Daily ORS quota exhausted — measuring {new_id} via OSRM "
                  f"backup (real road times; re-measured with ORS next run).")
        print(f"  Adding {new_id}...")
        new_loc = [new_coords["lng"], new_coords["lat"]]
        new_to_existing = {}
        existing_to_new = {}
        batch_size = 45  # ORS free tier caps ~50 locations per request

        # Haversine pre-filter: only compute ORS for nearby dogs + all fields/parking
        import math
        def haversine_miles(lat1, lon1, lat2, lon2):
            R = 3959
            dlat = math.radians(lat2 - lat1)
            dlon = math.radians(lon2 - lon1)
            a = (math.sin(dlat/2)**2 +
                 math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
                 math.sin(dlon/2)**2)
            return R * 2 * math.asin(math.sqrt(a))

        nearby_ids = []
        nearby_coords = []
        for eid, ecoord in zip(existing_ids, existing_coords):
            if eid.endswith("F") or eid.endswith("P"):
                nearby_ids.append(eid)
                nearby_coords.append(ecoord)
            else:
                dist = haversine_miles(new_coords["lat"], new_coords["lng"], ecoord[1], ecoord[0])
                if dist <= 7:
                    nearby_ids.append(eid)
                    nearby_coords.append(ecoord)

        print(f"    {len(nearby_ids)} nearby locations (of {len(existing_ids)} total)")

        for batch_start in range(0, len(nearby_ids), batch_size):
            if _ORS_QUOTA_HIT["v"]:
                break
            batch_ids = nearby_ids[batch_start:batch_start + batch_size]
            batch_coords = nearby_coords[batch_start:batch_start + batch_size]
            locations = [new_loc] + batch_coords
            destinations = list(range(1, len(batch_coords) + 1))

            # New → existing
            try:
                resp = _ors_matrix_call(
                    "https://api.heigit.org/openrouteservice/v2/matrix/driving-car",
                    {"Authorization": ors_key, "Content-Type": "application/json"},
                    {"locations": locations, "sources": [0],
                     "destinations": destinations, "metrics": ["duration"]},
                    print,
                )
                if resp.status_code == 200:
                    durations = resp.json().get("durations", [[]])[0]
                    for i, bid in enumerate(batch_ids):
                        if i < len(durations) and durations[i] is not None:
                            new_to_existing[bid] = round(durations[i] / 60, 1)
                else:
                    print(f"    ORS error (new→existing): {resp.status_code}")
            except Exception as e:
                print(f"    ORS request failed: {e}")
            time.sleep(2.0)

            # Existing → new
            try:
                resp = _ors_matrix_call(
                    "https://api.heigit.org/openrouteservice/v2/matrix/driving-car",
                    {"Authorization": ors_key, "Content-Type": "application/json"},
                    {"locations": locations, "sources": destinations,
                     "destinations": [0], "metrics": ["duration"]},
                    print,
                )
                if resp.status_code == 200:
                    dur_matrix = resp.json().get("durations", [])
                    for i, bid in enumerate(batch_ids):
                        if i < len(dur_matrix) and dur_matrix[i] and dur_matrix[i][0] is not None:
                            existing_to_new[bid] = round(dur_matrix[i][0] / 60, 1)
                else:
                    print(f"    ORS error (existing→new): {resp.status_code}")
            except Exception as e:
                print(f"    ORS request failed: {e}")
            time.sleep(2.0)

        _cov_out = len(new_to_existing)
        _cov_in = len(existing_to_new)
        if len(nearby_ids) and (_cov_out < len(nearby_ids) // 2 or _cov_in < len(nearby_ids) // 2):
            # QUOTA BACKUP: ORS quota died (or coverage came back too thin).
            # Measure the gaps with REAL road driving times from the public
            # OSRM server — same OpenStreetMap road network, no key, no daily
            # quota. Never a straight-line estimate. ORS numbers that DID come
            # back are kept; the dog is re-measured with ORS next run so the
            # matrix stays homogeneous.
            print(f"    {new_id}: ORS short ({_cov_out}/{len(nearby_ids)} out, "
                  f"{_cov_in}/{len(nearby_ids)} in) — filling gaps via OSRM backup...")
            for batch_start in range(0, len(nearby_ids), batch_size):
                batch_ids = nearby_ids[batch_start:batch_start + batch_size]
                batch_coords = nearby_coords[batch_start:batch_start + batch_size]
                if any(bid not in new_to_existing for bid in batch_ids):
                    vals = _osrm_table(new_loc, batch_coords, True, print)
                    if vals:
                        for i, bid in enumerate(batch_ids):
                            if bid not in new_to_existing and vals[i] is not None:
                                new_to_existing[bid] = vals[i]
                if any(bid not in existing_to_new for bid in batch_ids):
                    vals = _osrm_table(new_loc, batch_coords, False, print)
                    if vals:
                        for i, bid in enumerate(batch_ids):
                            if bid not in existing_to_new and vals[i] is not None:
                                existing_to_new[bid] = vals[i]
            _cov_out = len(new_to_existing)
            _cov_in = len(existing_to_new)
            if _cov_out < len(nearby_ids) // 2 or _cov_in < len(nearby_ids) // 2:
                print(f"    {new_id}: OSRM backup also short ({_cov_out}/{len(nearby_ids)} out, "
                      f"{_cov_in}/{len(nearby_ids)} in) — NOT added (stays missing for a "
                      f"future run). Never writing a mostly-9999 dog.")
                continue
            fallback_ids.append(new_id)
            print(f"    {new_id}: ✅ added with OSRM road times "
                  f"({_cov_out}/{len(nearby_ids)} out, {_cov_in}/{len(nearby_ids)} in); "
                  f"queued for ORS re-measure next run.")
        else:
            print(f"    {new_id}: computed {_cov_out}/{len(nearby_ids)} outbound, {_cov_in}/{len(nearby_ids)} inbound distances")

        # Update CSV
        header.append(new_id)
        for row in data_rows:
            row_id = row[0].strip()
            row.append(str(existing_to_new.get(row_id, 9999)))

        new_row = [new_id]
        for col_id in header[1:]:
            if col_id == new_id:
                new_row.append("0")
            else:
                new_row.append(str(new_to_existing.get(col_id, 9999)))
        data_rows.append(new_row)
        added_count += 1

        existing_ids.append(new_id)
        existing_coords.append(new_loc)

    if added_count == 0:
        print("No dogs were actually added — skipping upload (matrix unchanged).")
        return matrix, 0

    # Upload updated CSV
    print(f"Uploading updated matrix to Drive ({added_count} dog(s) added)...")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    for row in data_rows:
        writer.writerow(row)

    drive = build("drive", "v3", credentials=creds)
    media = MediaIoBaseUpload(
        io.BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv"
    )
    drive.files().update(fileId=file_id, media_body=media).execute()
    print("Done!")

    # QUOTA BACKUP bookkeeping: every OSRM-measured dog gets an AddressLog row
    # with a BLANK measured-address. Next run, the address-change (mover)
    # machinery sees blank != current address and re-measures the dog with ORS
    # — no new code path, the existing repair loop does the work.
    if fallback_ids:
        try:
            import datetime as _dt
            gc2 = gspread.authorize(creds)
            _rsid = os.environ.get("ROUTING_SHEET_ID", "").strip()
            _book = gc2.open_by_key(_rsid) if _rsid else gc2.open(
                os.environ.get("ROUTING_SHEET_NAME", "Routing"))
            _ws = _book.worksheet("AddressLog")
            _have = {r[0].strip() for r in _ws.get_all_values() if r and r[0].strip()}
            _rows = []
            for cid in fallback_ids:
                if cid in _have:
                    continue
                _c = missing_dogs.get(cid, {})
                _rows.append([cid, "", f"{_c.get('lat','')},{_c.get('lng','')}",
                              "OSRM " + _dt.date.today().isoformat()])
            if _rows:
                _ws.append_rows(_rows)
            print(f"  {len(fallback_ids)} OSRM-measured dog(s) queued for automatic "
                  f"re-measure next run: {', '.join(fallback_ids)}")
        except Exception as e:
            print(f"  ⚠️ Could not queue OSRM-measured dogs for re-measure ({e}) — "
                  f"blank their AddressLog col-B cells by hand to force it: "
                  f"{', '.join(fallback_ids)}")
    return matrix, added_count


def audit_matrix_health(creds, matrix, schedule_data, auto_queue=2):
    """Nightly self-audit: does every dog's matrix data agree with its coordinates?
    For each dog, compare matrix minutes to road-estimate for its ~12 nearest
    neighbors (within 7 mi). A dog whose near cells are systematically inflated
    (median +7 min AND x1.9 geometry) is concluded MISLOCATED; milder cases are
    WATCH. Conclusions are written to the MatrixHealth tab (Routing sheet), and
    the worst offenders are AUTO-QUEUED for re-measurement by blanking their
    AddressLog col-B cell — so mislocated dogs heal without anyone noticing them
    first. Never fatal: any failure just logs and the run continues."""
    import gspread, math, datetime, statistics
    coords = {}
    for row in schedule_data[1:]:
        if len(row) > 9 and row[6].strip():
            _ll = parse_lat_lng(row[8], row[9])
            if _ll:
                coords[row[6].strip()] = (_ll[0], _ll[1], row[0].strip())
    ids = [i for i in coords if i in matrix]
    if len(ids) < 20:
        return
    def _hav(a, b):
        R = 3958.8
        p1, p2 = math.radians(a[0]), math.radians(b[0])
        dp = math.radians(b[0] - a[0]); dl = math.radians(b[1] - a[1])
        x = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return 2 * R * math.asin(math.sqrt(x))
    findings = []
    pts = [(i, coords[i][0], coords[i][1]) for i in ids]
    for a, ala, alo in pts:
        near = sorted(((_hav((ala, alo), (bla, blo)), b)
                       for b, bla, blo in pts if b != a))[:12]
        near = [(d, b) for d, b in near if d <= 7.0]
        if len(near) < 5:
            continue
        excess, ratio, evid = [], [], None
        for d, b in near:
            m = matrix.get(a, {}).get(b)
            if m is None or m >= 9000:
                continue
            est = d / 28 * 60 + 1
            excess.append(m - est)
            ratio.append(m / est)
            if evid is None:
                evid = f"nearest {b} is {d:.1f} mi (~{est:.0f} min) but matrix says {m:.0f} min"
        if len(excess) >= 5:
            me, mr = statistics.median(excess), statistics.median(ratio)
            # conviction requires PHYSICAL impossibility, not just statistics:
            # >=2 near neighbors (<=0.6 mi) whose matrix minutes no road network
            # could produce. Statistical inflation alone is only WATCH (twisty
            # roads, rivers and conservation land legitimately inflate pairs).
            impossible = 0
            for d, b in near:
                if d <= 1.2:
                    m = matrix.get(a, {}).get(b)
                    est = d / 28 * 60 + 1
                    # impossible = at least triple the road estimate AND 6+ min over
                    if m is not None and m < 9000 and m >= max(est + 6, est * 3):
                        impossible += 1
                        if impossible == 1:
                            evid = (f"{b} is {d:.1f} mi away (~{est:.0f} min) "
                                    f"but matrix says {m:.0f} min — physically impossible")
            if impossible >= 2 and me >= 5:
                findings.append(("MISLOCATED", a, coords[a][2], me, mr, evid))
            elif me >= 4.5 and mr >= 1.6:
                findings.append(("WATCH", a, coords[a][2], me, mr, evid))
    findings.sort(key=lambda x: -x[3])
    strong = [f for f in findings if f[0] == "MISLOCATED"]
    print(f"  🩺 Matrix health: {len(strong)} mislocated, "
          f"{len(findings) - len(strong)} watch-list (of {len(ids)} dogs audited)")
    try:
        gc = gspread.authorize(creds)
        routing_sheet_id = os.environ.get("ROUTING_SHEET_ID", "").strip()
        book = gc.open_by_key(routing_sheet_id) if routing_sheet_id else gc.open(
            os.environ.get("ROUTING_SHEET_NAME", "Routing"))
        try:
            ws = book.worksheet("MatrixHealth")
        except gspread.exceptions.WorksheetNotFound:
            ws = book.add_worksheet(title="MatrixHealth", rows=60, cols=6)
        today = datetime.date.today().isoformat()
        rows = [["Verdict", "Dog ID", "Address", "How far off", "Evidence", f"Checked {today}"]]
        if not findings:
            rows.append(["✅ HEALTHY", "", "all dogs' distances agree with their coordinates", "", "", ""])
        for v, a, addr, me, mr, evid in findings[:40]:
            fix = "auto-queued for re-measure" if v == "MISLOCATED" else "monitoring"
            rows.append([v, a, addr[:45], f"+{me:.0f} min (x{mr:.1f})", evid or "", fix])
        ws.clear()
        ws.update(range_name="A1", values=rows)
    except Exception as e:
        print(f"  MatrixHealth tab write failed ({e}) — continuing")
    # auto-queue the worst offenders: blank AddressLog col B so the mover
    # machinery re-measures them from their coordinates on following runs
    if strong and auto_queue > 0:
        try:
            ws2 = book.worksheet("AddressLog")
            log_rows = ws2.get_all_values()
            queued = 0
            for v, a, addr, me, mr, evid in strong:
                if queued >= auto_queue:
                    break
                for i, r in enumerate(log_rows, start=1):
                    if r and r[0].strip() == a and (len(r) < 2 or r[1].strip()):
                        # LOOP GUARD: if this dog was ALREADY re-measured in the
                        # last 7 days (AddressLog col D) and still looks wrong,
                        # re-measuring again won't change anything — the problem
                        # is its coordinates or a real road-access quirk (dead-end
                        # street, no through road). Re-queueing it every run only
                        # hogs mover slots and quota (1x/1733x looped all day
                        # Aug 31, starving every other mover). Leave it listed on
                        # MatrixHealth for a human instead.
                        _last = (r[3].strip() if len(r) > 3 else "")
                        try:
                            _age = (datetime.date.today()
                                    - datetime.date.fromisoformat(_last[:10])).days
                        except Exception:
                            _age = 999
                        if _age <= 7:
                            print(f"    NOT re-queued {a}: already re-measured {_last[:10]} and still "
                                  f"flagged — check its coordinates/road access by hand ({evid})")
                            break
                        ws2.update(range_name=f"B{i}", values=[[""]])
                        print(f"    auto-queued {a} for re-measurement tonight ({evid})")
                        queued += 1
                        break
        except Exception as e:
            print(f"  auto-queue failed ({e}) — findings still listed in MatrixHealth")


def main():
    print("=" * 50)
    print("Matrix Update Check")
    print("=" * 50)

    # Load config from environment
    matrix_file_name = os.environ.get("MATRIX_FILE_NAME", "matrix.csv")
    schedule_sheet_id = os.environ.get("SCHEDULE_SHEET_ID", "")
    ors_key = os.environ.get("ORS_API_KEY", "")

    if not schedule_sheet_id:
        print("ERROR: SCHEDULE_SHEET_ID not set")
        return
    if not ors_key:
        print("ERROR: ORS_API_KEY not set")
        return

    # Connect
    creds = get_credentials()

    # Load matrix
    print("Loading matrix from Drive...")
    matrix, file_id, matrix_text = load_matrix_from_drive(creds, matrix_file_name)
    print(f"Matrix has {len(matrix)} locations")

    # Load schedule
    print("Loading Schedule tab...")
    schedule_data = load_schedule(creds, schedule_sheet_id)
    print(f"Schedule has {len(schedule_data)} rows")

    # Find missing
    geocode_missing_coords(creds, schedule_data, schedule_sheet_id, matrix, ors_key)
    print("Checking for changed addresses...")
    try:
        matrix_text, _moved = check_address_changes(creds, schedule_data, matrix, ors_key,
                                                    schedule_sheet_id, file_id, matrix_text)
        if _moved:
            print(f"  {len(_moved)} moved dog(s) re-measured from their NEW address")
    except Exception as e:
        print(f"  Address-change check crashed ({e}) — continuing; repair still runs")
    missing = find_missing_dogs(matrix, schedule_data)
    temp_missing = find_missing_temp_addresses(creds, schedule_sheet_id, matrix, ors_key)
    if temp_missing:
        print(f"TempAddresses: {len(temp_missing)} new temp ID(s) to add")
        missing.update(temp_missing)

    if not missing:
        print("✅ No new dogs to add.")
    else:
        print(f"⚠️ Found {len(missing)} new dog(s) to add:")
        for cid, coords in missing.items():
            print(f"  • {cid} — ({coords['lat']}, {coords['lng']})")
        _mx, _added = add_dogs_to_matrix(creds, matrix, missing, schedule_data, file_id, matrix_text, ors_key)
        if _added:
            print(f"✅ Added {_added} of {len(missing)} dog(s); the rest stay queued for a future run.")
        else:
            print(f"ℹ️ Added 0 of {len(missing)} dog(s) this run (quota or Locations issue above); all stay queued.")
        # Reload matrix after adding
        matrix, file_id, matrix_text = load_matrix_from_drive(creds, matrix_file_name)

    # ── Fix 9999 entries (batch of 50 pairs per run) ──
    print("\nChecking for 9999 entries to repair...")
    repair_9999s(creds, matrix, schedule_data, file_id, matrix_text, ors_key)

    print("Auditing matrix health...")
    try:
        audit_matrix_health(creds, matrix, schedule_data)
    except Exception as e:
        print(f"  health audit crashed ({e}) — run continues")
    print("✅ Matrix update complete.")


def repair_9999s(creds, matrix, schedule_data, file_id, matrix_text, ors_key):
    """Find 9999 entries in the matrix and fill them in via ORS. Processes 50 pairs per run."""
    import math

    # Build coordinate lookup from Schedule + Locations
    coords_lookup = {}
    for row in schedule_data[2:]:
        cid = row[6].strip() if len(row) > 6 else ""
        lat = row[8].strip() if len(row) > 8 else ""
        lng = row[9].strip() if len(row) > 9 else ""
        if cid and lat and lng:
            _ll = parse_lat_lng(lat, lng)
            if _ll:
                coords_lookup[cid] = {"lat": _ll[0], "lng": _ll[1]}

    # Load field/parking coords from Locations tab.
    # BY ID FIRST (matches every other function): opening by NAME failed on
    # every run ("could not load Locations tab") — which silently excluded all
    # field/parking anchors from repair, so broken anchor distances (e.g. a
    # driver's parking spot reading 9999 to her own drop-offs) never got fixed
    # and routes ignored parking. (Holly Trip 4 bug, Aug 31 2026.)
    try:
        client = gspread.authorize(creds)
        routing_sheet_id = os.environ.get("ROUTING_SHEET_ID", "").strip()
        if routing_sheet_id:
            loc_book = client.open_by_key(routing_sheet_id)
        else:
            loc_book = client.open(os.environ.get("ROUTING_SHEET_NAME", "Routing"))
        loc_ws = loc_book.worksheet("Locations")
        for row in loc_ws.get_all_values()[1:]:
            if len(row) >= 3:
                loc_id = row[0].strip()
                lat = row[1].strip()
                lng = row[2].strip()
                if loc_id and lat and lng:
                    _ll = parse_lat_lng(lat, lng)
                    if _ll:
                        coords_lookup[loc_id] = {"lat": _ll[0], "lng": _ll[1]}
    except Exception as e:
        print(f"  Warning: could not load Locations tab: {e}")

    # Find 9999 pairs where we have coordinates for both.
    #
    # 7-MILE RULE (matches the add path): dog-to-dog pairs farther than 7 miles
    # apart are 9999 ON PURPOSE — the add path never measures them because two
    # dogs that far apart never share a route. They are correct values, not
    # damage. Without this filter the repair pass counted every intentional
    # far-pair as broken (166k+ "stale" pairs), burned 500 real ORS calls per
    # run re-measuring pairs no route ever uses, and drained the daily quota
    # every single day while the backlog only grew. Field/parking pairs are
    # always kept — every trip touches the anchors regardless of distance.
    def _hav_mi7(a, b):
        p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
        dp = math.radians(b["lat"] - a["lat"]); dl = math.radians(b["lng"] - a["lng"])
        x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * 3958.8 * math.asin(math.sqrt(x))

    pairs_to_fix = []
    far_by_design = 0
    for from_id, dests in matrix.items():
        if from_id not in coords_lookup:
            continue
        for to_id, dist in dests.items():
            if dist >= 9999 and to_id in coords_lookup and from_id != to_id:
                _anchor = (from_id.endswith(("F", "P")) or to_id.endswith(("F", "P")))
                if not _anchor and _hav_mi7(coords_lookup[from_id], coords_lookup[to_id]) > 7:
                    far_by_design += 1
                    continue                    # intentional far-pair — leave it alone
                pairs_to_fix.append((from_id, to_id))
    if far_by_design:
        print(f"  ({far_by_design} far-apart 9999 pairs skipped — beyond 7 miles, correct by design)")

    if not pairs_to_fix:
        print("  No 9999 entries to repair.")
        return

    # Prioritize: field/parking pairs first, then dog-to-dog
    priority_pairs = [p for p in pairs_to_fix if p[0].endswith(('F', 'P')) or p[1].endswith(('F', 'P'))]
    other_pairs = [p for p in pairs_to_fix if p not in priority_pairs]
    sorted_pairs = priority_pairs + other_pairs

    # Fix up to 500 dog-dog pairs per run, plus a MUCH larger budget for
    # field/parking pairs (every trip touches them; after an anchor reset the
    # grouped calls make even thousands of anchor pairs only ~50-100 requests)
    batch = priority_pairs[:4000] + other_pairs[:500]
    print(f"  Found {len(pairs_to_fix)} pairs with 9999 ({len(priority_pairs)} involve fields/parking). Fixing {len(batch)} this run...")

    # Parse CSV for editing
    reader_obj = csv.reader(io.StringIO(matrix_text))
    all_rows = list(reader_obj)
    header = all_rows[0]
    data_rows = all_rows[1:]

    # Build column index lookup
    col_idx = {}
    for i, h in enumerate(header):
        col_idx[h.strip()] = i

    # Build row index lookup
    row_idx = {}
    for i, row in enumerate(data_rows):
        row_idx[row[0].strip()] = i

    # Process pairs via ORS — grouped by source so ONE call covers up to 500
    # pairs (the old per-pair version burned 2 API calls per pair — the quota
    # killer that capped adds at ~3 dogs/day)
    fixed = 0
    from collections import defaultdict
    _fwd = defaultdict(list)   # from_id -> [to_id, ...]   fills matrix[from][to]
    _rev = defaultdict(list)   # to_id   -> [from_id, ...] fills matrix[to][from]
    for from_id, to_id in batch:
        _fwd[from_id].append(to_id)
        _rev[to_id].append(from_id)

    def _fill_from_source(src_id, dest_ids):
        filled = 0
        for k in range(0, len(dest_ids), 45):
            if _ORS_QUOTA_HIT["v"]:
                return filled
            chunk = dest_ids[k:k + 45]
            locations = [[coords_lookup[src_id]["lng"], coords_lookup[src_id]["lat"]]] + [
                [coords_lookup[d]["lng"], coords_lookup[d]["lat"]] for d in chunk
            ]
            resp = _ors_matrix_call(
                "https://api.heigit.org/openrouteservice/v2/matrix/driving-car",
                {"Authorization": ors_key, "Content-Type": "application/json"},
                {"locations": locations, "sources": [0],
                 "destinations": list(range(1, len(chunk) + 1)), "metrics": ["duration"]},
                print,
            )
            if resp is not None and resp.status_code == 200:
                durs = resp.json().get("durations", [[]])[0]
                for j, d in enumerate(chunk):
                    v = durs[j] if j < len(durs) else None
                    if v is None:
                        continue
                    if src_id in row_idx and d in col_idx:
                        data_rows[row_idx[src_id]][col_idx[d]] = str(round(v / 60, 1))
                        filled += 1
            time.sleep(2.0)
        return filled

    for _src, _dests in _fwd.items():
        if _ORS_QUOTA_HIT["v"]:
            print("    Daily ORS quota exhausted — stopping repair pass for this run.")
            break
        fixed += _fill_from_source(_src, _dests)
    for _src, _dests in _rev.items():
        if _ORS_QUOTA_HIT["v"]:
            break
        fixed += _fill_from_source(_src, _dests)

    if fixed > 0:
        print(f"  Fixed {fixed} entries. Uploading...")
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(header)
        for row in data_rows:
            writer.writerow(row)

        drive = build("drive", "v3", credentials=creds)
        media = MediaIoBaseUpload(
            io.BytesIO(output.getvalue().encode("utf-8")),
            mimetype="text/csv",
            resumable=True
        )
        request = drive.files().update(fileId=file_id, media_body=media)
        response = None
        while response is None:
            _, response = request.next_chunk()
        print(f"  Uploaded. {len(pairs_to_fix) - len(batch)} pairs remaining for future runs.")
    else:
        print("  No fixes applied this run.")


if __name__ == "__main__":
    main()
