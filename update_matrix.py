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


def find_missing_dogs(matrix, schedule_data):
    """Find dogs in the Schedule that aren't in the matrix."""
    matrix_ids = set(matrix.keys())
    missing = {}

    # Check ALL date columns for dog IDs that have assignments
    # We care about any dog that might be scheduled, not just today
    for row in schedule_data[2:]:  # skip header + sub-header
        cid = row[6].strip() if len(row) > 6 else ""
        lat = row[8].strip() if len(row) > 8 else ""
        lng = row[9].strip() if len(row) > 9 else ""

        if not cid or cid in matrix_ids or cid in missing:
            continue
        if not lat or not lng:
            continue

        # Check if this dog has any assignment in any date column
        has_assignment = False
        for col_idx in range(10, min(len(row), 53)):
            val = row[col_idx].strip()
            if val and ":" in val and "cancel" not in val.lower():
                has_assignment = True
                break

        if has_assignment:
            try:
                missing[cid] = {"lat": float(lat), "lng": float(lng)}
            except ValueError:
                continue

    return missing


def add_dogs_to_matrix(creds, matrix, missing_dogs, schedule_data, file_id, matrix_csv_text, ors_key):
    """Add missing dogs to the matrix using ORS API and upload to Drive."""
    # Get coords for existing matrix entries
    existing_with_coords = {}
    for row in schedule_data[2:]:
        cid = row[6].strip() if len(row) > 6 else ""
        lat = row[8].strip() if len(row) > 8 else ""
        lng = row[9].strip() if len(row) > 9 else ""
        if cid in matrix and lat and lng:
            try:
                existing_with_coords[cid] = {"lat": float(lat), "lng": float(lng)}
            except ValueError:
                continue

    # Also load field/parking coordinates from Locations tab
    try:
        sheet_name = os.environ.get("ROUTING_SHEET_NAME", "Routing")
        client = gspread.authorize(creds)
        loc_sheet = client.open(sheet_name)
        loc_ws = loc_sheet.worksheet("Locations")
        loc_data = loc_ws.get_all_values()
        for row in loc_data[1:]:
            if len(row) >= 3:
                loc_id = row[0].strip()
                lat = row[1].strip()
                lng = row[2].strip()
                if loc_id and lat and lng and loc_id in matrix:
                    try:
                        existing_with_coords[loc_id] = {"lat": float(lat), "lng": float(lng)}
                    except ValueError:
                        continue
        print(f"  Loaded {sum(1 for k in existing_with_coords if k.endswith('F') or k.endswith('P'))} field/parking coordinates from Locations tab")
    except Exception as e:
        print(f"  Warning: could not load Locations tab: {e}")

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

    for new_id, new_coords in missing_dogs.items():
        print(f"  Adding {new_id}...")
        new_loc = [new_coords["lng"], new_coords["lat"]]
        new_to_existing = {}
        existing_to_new = {}
        batch_size = 25

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
            batch_ids = nearby_ids[batch_start:batch_start + batch_size]
            batch_coords = nearby_coords[batch_start:batch_start + batch_size]
            locations = [new_loc] + batch_coords
            destinations = list(range(1, len(batch_coords) + 1))

            # New → existing
            try:
                resp = requests.post(
                    "https://api.openrouteservice.org/v2/matrix/driving-car",
                    headers={"Authorization": ors_key, "Content-Type": "application/json"},
                    json={"locations": locations, "sources": [0],
                          "destinations": destinations, "metrics": ["duration"]},
                    timeout=30
                )
                if resp.status_code == 200:
                    durations = resp.json().get("durations", [[]])[0]
                    for i, bid in enumerate(batch_ids):
                        new_to_existing[bid] = round(durations[i] / 60, 1)
                else:
                    print(f"    ORS error (new→existing): {resp.status_code}")
            except Exception as e:
                print(f"    ORS request failed: {e}")
            time.sleep(0.5)

            # Existing → new
            try:
                resp = requests.post(
                    "https://api.openrouteservice.org/v2/matrix/driving-car",
                    headers={"Authorization": ors_key, "Content-Type": "application/json"},
                    json={"locations": locations, "sources": destinations,
                          "destinations": [0], "metrics": ["duration"]},
                    timeout=30
                )
                if resp.status_code == 200:
                    dur_matrix = resp.json().get("durations", [])
                    for i, bid in enumerate(batch_ids):
                        existing_to_new[bid] = round(dur_matrix[i][0] / 60, 1)
                else:
                    print(f"    ORS error (existing→new): {resp.status_code}")
            except Exception as e:
                print(f"    ORS request failed: {e}")
            time.sleep(0.5)

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

        existing_ids.append(new_id)
        existing_coords.append(new_loc)

    # Upload updated CSV
    print("Uploading updated matrix to Drive...")
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
    missing = find_missing_dogs(matrix, schedule_data)

    if not missing:
        print("✅ No new dogs to add.")
    else:
        print(f"⚠️ Found {len(missing)} new dog(s) to add:")
        for cid, coords in missing.items():
            print(f"  • {cid} — ({coords['lat']}, {coords['lng']})")
        add_dogs_to_matrix(creds, matrix, missing, schedule_data, file_id, matrix_text, ors_key)
        print(f"✅ Added {len(missing)} dog(s).")
        # Reload matrix after adding
        matrix, file_id, matrix_text = load_matrix_from_drive(creds, matrix_file_name)

    # ── Fix 9999 entries (batch of 50 pairs per run) ──
    print("\nChecking for 9999 entries to repair...")
    repair_9999s(creds, matrix, schedule_data, file_id, matrix_text, ors_key)
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
            try:
                coords_lookup[cid] = {"lat": float(lat), "lng": float(lng)}
            except ValueError:
                continue

    # Load field/parking coords from Locations tab
    try:
        sheet_name = os.environ.get("ROUTING_SHEET_NAME", "Routing")
        client = gspread.authorize(creds)
        loc_ws = client.open(sheet_name).worksheet("Locations")
        for row in loc_ws.get_all_values()[1:]:
            if len(row) >= 3:
                loc_id = row[0].strip()
                lat = row[1].strip()
                lng = row[2].strip()
                if loc_id and lat and lng:
                    try:
                        coords_lookup[loc_id] = {"lat": float(lat), "lng": float(lng)}
                    except ValueError:
                        continue
    except Exception as e:
        print(f"  Warning: could not load Locations tab: {e}")

    # Find 9999 pairs where we have coordinates for both
    pairs_to_fix = []
    for from_id, dests in matrix.items():
        if from_id not in coords_lookup:
            continue
        for to_id, dist in dests.items():
            if dist >= 9999 and to_id in coords_lookup and from_id != to_id:
                pairs_to_fix.append((from_id, to_id))

    if not pairs_to_fix:
        print("  No 9999 entries to repair.")
        return

    # Limit to 50 pairs per run
    batch = pairs_to_fix[:50]
    print(f"  Found {len(pairs_to_fix)} pairs with 9999. Fixing {len(batch)} this run...")

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

    # Process pairs via ORS
    fixed = 0
    batch_size = 10  # ORS pairs per request

    for i in range(0, len(batch), batch_size):
        sub_batch = batch[i:i + batch_size]

        for from_id, to_id in sub_batch:
            from_coords = coords_lookup[from_id]
            to_coords = coords_lookup[to_id]

            locations = [
                [from_coords["lng"], from_coords["lat"]],
                [to_coords["lng"], to_coords["lat"]]
            ]

            # Forward: from → to
            try:
                resp = requests.post(
                    "https://api.openrouteservice.org/v2/matrix/driving-car",
                    headers={"Authorization": ors_key, "Content-Type": "application/json"},
                    json={"locations": locations, "sources": [0],
                          "destinations": [1], "metrics": ["duration"]},
                    timeout=30
                )
                if resp.status_code == 200:
                    dur = resp.json().get("durations", [[]])[0][0]
                    minutes = round(dur / 60, 1)

                    # Update CSV
                    if from_id in row_idx and to_id in col_idx:
                        data_rows[row_idx[from_id]][col_idx[to_id]] = str(minutes)
                        fixed += 1
            except Exception:
                pass

            # Reverse: to → from
            try:
                resp = requests.post(
                    "https://api.openrouteservice.org/v2/matrix/driving-car",
                    headers={"Authorization": ors_key, "Content-Type": "application/json"},
                    json={"locations": locations, "sources": [1],
                          "destinations": [0], "metrics": ["duration"]},
                    timeout=30
                )
                if resp.status_code == 200:
                    dur = resp.json().get("durations", [[]])[0][0]
                    minutes = round(dur / 60, 1)

                    if to_id in row_idx and from_id in col_idx:
                        data_rows[row_idx[to_id]][col_idx[from_id]] = str(minutes)
                        fixed += 1
            except Exception:
                pass

            time.sleep(0.5)

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
