"""
Doggy Dates Route Optimizer
Reads group assignments from Google Sheets, optimizes routes with OR-Tools,
and writes the optimized stop order back to Google Sheets.
"""

import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
import csv
import re
import io
import os
from solver import solve_simple_trip, solve_interleaved_trip

# =============================================================================
# CONFIG
# =============================================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_NAME = st.secrets.get("sheet_name", "Routing")
MATRIX_FILE_NAME = st.secrets.get("matrix_file_name", "matrix.csv")
OUTPUT_TAB_NAME = "Optimized Routes"
SNAPSHOT_TAB_NAME = "Route Snapshot"


# =============================================================================
# DATA LOADING
# =============================================================================

@st.cache_data(show_spinner="Loading distance matrix from Google Drive...", ttl=43200)
def load_matrix_from_drive(_client, file_name):
    """Download matrix CSV from Google Drive and parse it."""
    from google.oauth2.service_account import Credentials as Creds
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

    creds = Creds.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    drive_service = build("drive", "v3", credentials=creds)

    results = drive_service.files().list(
        q=f"name='{file_name}' and trashed=false",
        fields="files(id, name, size)"
    ).execute()
    files = results.get("files", [])

    if not files:
        st.error(f"Could not find '{file_name}' in Google Drive. "
                 "Make sure the file is shared with your service account email.")
        st.stop()

    file_id = files[0]["id"]

    request = drive_service.files().get_media(fileId=file_id)
    content = io.BytesIO()
    downloader = MediaIoBaseDownload(content, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    content.seek(0)
    text = content.read().decode("utf-8-sig")

    matrix = {}
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    col_ids = [h.strip().replace("\r", "") for h in header[1:] if h.strip()]

    for row in reader:
        row_id = row[0].strip().replace("\r", "")
        if not row_id:
            continue
        matrix[row_id] = {}
        for i, col_id in enumerate(col_ids):
            if i + 1 >= len(row):
                break
            val_str = row[i + 1].strip().replace("\r", "")
            if val_str:
                matrix[row_id][col_id] = float(val_str.replace(",", "."))
            else:
                matrix[row_id][col_id] = 9999
    return matrix


def get_gspread_client():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    return gspread.authorize(creds)


@st.cache_data(show_spinner="Loading Staff data...", ttl=300)
def load_staff_from_sheet(_client, sheet_name):
    sheet = _client.open(sheet_name)
    ws = sheet.worksheet("Staff")
    return ws.get_all_values()


@st.cache_data(show_spinner="Loading Schedule data...", ttl=300)
def load_schedule_sheet(_client, sheet_id):
    sheet = _client.open_by_key(sheet_id)
    ws = sheet.worksheet("Schedule")
    return ws.get_all_values()


def get_available_dates(schedule_data):
    """Read row 1 of Schedule tab and return future dates from columns K onward."""
    from datetime import datetime, date
    
    if not schedule_data:
        return {}
    header_row = schedule_data[0]
    today = date.today()
    
    dates = {}
    for col_idx in range(10, min(len(header_row), 53)):
        date_val = header_row[col_idx].strip()
        if not date_val:
            continue
        
        # Try to parse the date
        parsed = None
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m/%d", "%Y-%m-%d", "%b %d", "%B %d", "%b %d, %Y", "%B %d, %Y"):
            try:
                parsed = datetime.strptime(date_val, fmt).date()
                # If no year in format, assume current year
                if parsed.year == 1900:
                    parsed = parsed.replace(year=today.year)
                break
            except ValueError:
                continue
        
        if parsed is None:
            continue
        
        # Only include today and future
        if parsed < today:
            continue
        
        # Format nicely: "Monday July 20"
        display = parsed.strftime("%A %B %-d")
        dates[display] = {"col_idx": col_idx, "raw": date_val}
    
    return dates


def parse_schedule(schedule_data, date_col_idx):
    """Parse the Schedule tab for a specific date column."""
    assignments = []
    for row in schedule_data[1:]:
        if len(row) <= date_col_idx:
            continue

        customer_id = row[6].strip() if len(row) > 6 else ""
        email = row[4].strip() if len(row) > 4 else ""
        assignment_str = row[date_col_idx].strip() if len(row) > date_col_idx else ""
        # Dog count is in column BH (index 59)
        dog_count = int(row[59].strip()) if len(row) > 59 and row[59].strip() else 1
        dog_name = row[1].strip() if len(row) > 1 else ""
        address = row[0].strip()

        if not customer_id or not assignment_str:
            continue

        # Skip cancelled dogs
        if "cancel" in assignment_str.lower():
            continue

        if ":" not in assignment_str:
            continue

        parts = assignment_str.split(":")
        driver_name = parts[0].strip()
        code = parts[1].strip() if len(parts) > 1 else ""

        # Handle "!" split — dog goes home between groups (e.g., "1!3" = group 1, then group 3 separately)
        if "!" in code:
            sub_codes = code.split("!")
            for sub_code in sub_codes:
                digits = re.findall(r"\d", sub_code)
                if not digits:
                    continue
                assignments.append({
                    "customer_id": customer_id,
                    "driver": driver_name,
                    "pickup_group": int(digits[0]),
                    "dropoff_group": int(digits[-1]),
                    "dog_count": dog_count,
                    "is_staff_dog": (email == ""),
                    "dog_name": dog_name,
                    "address": address,
                    "raw": assignment_str,
                })
        else:
            digits = re.findall(r"\d", code)
            if not digits:
                continue
            assignments.append({
                "customer_id": customer_id,
                "driver": driver_name,
                "pickup_group": int(digits[0]),
                "dropoff_group": int(digits[-1]),
                "dog_count": dog_count,
                "is_staff_dog": (email == ""),
                "dog_name": dog_name,
                "address": address,
                "raw": assignment_str,
            })
    return assignments


# =============================================================================
# PARSING
# =============================================================================

def parse_staff(data):
    drivers = {}
    for row in data[1:]:
        if len(row) < 9:
            continue
        name = row[0].strip()
        status = row[1].strip()
        field_id = row[6].strip()
        parking_id = row[7].strip()
        capacity_str = row[8].strip()

        if status == "OFF" or not field_id or not capacity_str:
            continue

        capacity = int(capacity_str)

        drivers[name] = {
            "field_id": field_id,
            "parking_id": parking_id,
            "capacity": capacity,
            "field_address": row[2].strip() if len(row) > 2 else "",
            "parking_address": row[4].strip() if len(row) > 4 else "",
        }
    return drivers


def derive_groups(assignments, driver_name):
    """Determine a driver's groups from actual assignments in the Schedule tab."""
    driver_dogs = [a for a in assignments if a["driver"] == driver_name]
    pickup_groups = set(a["pickup_group"] for a in driver_dogs)
    return sorted(pickup_groups)
    return drivers


# =============================================================================
# ROUTE SOLVER
# =============================================================================

def solve_driver(matrix, driver_name, config, dogs, schedule_lookup):
    groups = config["groups"]
    field = config["field_id"]
    parking = config["parking_id"]
    capacity = config["capacity"]
    field_address = config.get("field_address", "")
    parking_address = config.get("parking_address", "")

    customer_dogs = [d for d in dogs if not d["is_staff_dog"]]
    staff_dogs = [d for d in dogs if d["is_staff_dog"]]
    dog_lookup = {d["customer_id"]: d for d in customer_dogs}

    # Build a set of all customer_ids that have a ! assignment (split groups)
    split_dogs = {}
    for d in customer_dogs:
        raw = d.get("raw", "")
        if "!" in raw:
            code = raw.split(":")[1] if ":" in raw else ""
            split_dogs[d["customer_id"]] = code

    def get_dog_display_name(loc_id, action):
        """Build dog name with symbols based on action and assignment."""
        d = dog_lookup.get(loc_id, {})
        if not d:
            return ""
        name = d.get("dog_name", loc_id)

        if action in ("DROP OFF",):
            return f"◼{name}"

        if action == "PICK UP":
            cid = d.get("customer_id", "")
            # Check for split groups (!)
            if cid in split_dogs:
                code = split_dogs[cid]
                parts = code.split("!")
                group_nums = []
                for p in parts:
                    digits = re.findall(r"\d", p)
                    if digits:
                        group_nums.append(digits[0])
                return f"{' & '.join(group_nums)} {name}"

            # Check how many groups the dog is staying for
            pickup = d.get("pickup_group", 0)
            dropoff = d.get("dropoff_group", 0)
            groups_staying = dropoff - pickup + 1

            if groups_staying == 3:
                return f"3️⃣{name}"
            elif groups_staying == 2:
                return f"2️⃣{name}"
            else:
                return name

        return name

    def get_extra_info(loc_id):
        """Get phone, customer name, instructions, breed, house desc from schedule lookup."""
        info = schedule_lookup.get(loc_id, {})
        return {
            "Phone": info.get("phone", ""),
            "Customer Name": info.get("customer_name", ""),
            "Instructions": info.get("instructions", ""),
            "Dog Breed": info.get("dog_breed", ""),
            "House Description": info.get("house_description", ""),
        }

    results = []

    for leg_num in range(len(groups) + 1):

        if leg_num == 0:
            current_group = groups[0]
            pickup_dogs = [
                d for d in customer_dogs
                if d["pickup_group"] == current_group and d["customer_id"] in matrix
            ]
            if not pickup_dogs:
                continue

            stop_ids = [d["customer_id"] for d in pickup_dogs]
            total_dogs = sum(d["dog_count"] for d in pickup_dogs)
            result = solve_simple_trip(matrix, stop_ids, parking, field)

            if result:
                route, dist = result
                for i, loc_id in enumerate(route):
                    d = dog_lookup.get(loc_id, {})
                    extra = get_extra_info(loc_id)
                    if loc_id == parking:
                        action = "START"
                        display_name = "Leave Parking"
                        addr = parking_address
                    elif loc_id == field:
                        action = "ARRIVE"
                        display_name = "Arrive at Field"
                        addr = field_address
                    else:
                        action = "PICK UP"
                        display_name = get_dog_display_name(loc_id, action)
                        addr = d.get("address", "")
                    results.append({
                        "Driver": driver_name, "Leg": leg_num + 1, "Stop": i + 1,
                        "Action": action, "Customer ID": loc_id,
                        "Dog Name": display_name, "Address": addr,
                        "Phone": extra.get("Phone", ""),
                        "Customer Name": extra.get("Customer Name", ""),
                        "Instructions": extra.get("Instructions", ""),
                        "Dog Breed": extra.get("Dog Breed", ""),
                        "House Description": extra.get("House Description", ""),
                        "Dogs at Stop": d.get("dog_count", ""),
                        "Dogs on Board": "", "Assignment": d.get("raw", "") or driver_name,
                        "Drive Min": round(dist, 1) if loc_id == field else "",
                    })
            else:
                results.append({
                    "Driver": driver_name, "Leg": leg_num + 1, "Stop": 0,
                    "Action": "⚠️ FAILED", "Customer ID": "",
                    "Dog Name": f"Leg FAILED: {len(pickup_dogs)} stops, {total_dogs} dogs, capacity {capacity}",
                    "Address": "", "Phone": "", "Customer Name": "",
                    "Instructions": "", "Dog Breed": "", "House Description": "",
                    "Dogs at Stop": "", "Dogs on Board": "",
                    "Assignment": driver_name, "Drive Min": "",
                })

        elif leg_num < len(groups):
            prev_group = groups[leg_num - 1]
            next_group = groups[leg_num]

            dropoffs = [
                (d["customer_id"], d["dog_count"])
                for d in customer_dogs
                if d["dropoff_group"] == prev_group and d["customer_id"] in matrix
            ]
            pickups = [
                (d["customer_id"], d["dog_count"])
                for d in customer_dogs
                if d["pickup_group"] == next_group and d["customer_id"] in matrix
            ]

            staying_customer = sum(
                d["dog_count"] for d in customer_dogs
                if d["pickup_group"] < next_group
                and d["dropoff_group"] > prev_group
                and d["pickup_group"] <= prev_group
            )
            staying_staff = sum(
                d["dog_count"] for d in staff_dogs
                if d["pickup_group"] <= prev_group and d["dropoff_group"] > prev_group
            )

            dogs_being_dropped = sum(cnt for _, cnt in dropoffs)
            dogs_being_picked = sum(cnt for _, cnt in pickups)
            initial_load = dogs_being_dropped + staying_customer + staying_staff

            if not dropoffs and not pickups:
                continue

            result = solve_interleaved_trip(
                matrix, dropoffs, pickups, field, field, capacity, initial_load
            )

            # If capacity is too tight, retry with relaxed limit
            if result is None:
                result = solve_interleaved_trip(
                    matrix, dropoffs, pickups, field, field, capacity + 4, initial_load
                )

            if result:
                route, dist = result
                for i, (loc_id, load, action_raw) in enumerate(route):
                    d = dog_lookup.get(loc_id, {})
                    extra = get_extra_info(loc_id)
                    if action_raw == "LEAVE FIELD":
                        display_name, addr = "Leave Field", field_address
                    elif action_raw == "ARRIVE FIELD":
                        display_name, addr = "Arrive at Field", field_address
                    else:
                        display_name = get_dog_display_name(loc_id, action_raw)
                        addr = d.get("address", "")
                    results.append({
                        "Driver": driver_name, "Leg": leg_num + 1, "Stop": i + 1,
                        "Action": action_raw, "Customer ID": loc_id,
                        "Dog Name": display_name, "Address": addr,
                        "Phone": extra.get("Phone", ""),
                        "Customer Name": extra.get("Customer Name", ""),
                        "Instructions": extra.get("Instructions", ""),
                        "Dog Breed": extra.get("Dog Breed", ""),
                        "House Description": extra.get("House Description", ""),
                        "Dogs at Stop": d.get("dog_count", ""),
                        "Dogs on Board": load, "Assignment": d.get("raw", "") or driver_name,
                        "Drive Min": round(dist, 1) if action_raw == "ARRIVE FIELD" else "",
                    })
            else:
                results.append({
                    "Driver": driver_name, "Leg": leg_num + 1, "Stop": 0,
                    "Action": "⚠️ FAILED", "Customer ID": "",
                    "Dog Name": f"Interleaved leg FAILED: drop {len(dropoffs)} ({dogs_being_dropped} dogs) + pick {len(pickups)} ({dogs_being_picked} dogs) + {staying_customer + staying_staff} staying = {initial_load} initial load, capacity {capacity}",
                    "Address": "", "Phone": "", "Customer Name": "",
                    "Instructions": "", "Dog Breed": "", "House Description": "",
                    "Dogs at Stop": "", "Dogs on Board": "",
                    "Assignment": driver_name, "Drive Min": "",
                })

        else:
            last_group = groups[-1]
            dropoff_dogs = [
                d for d in customer_dogs
                if d["dropoff_group"] >= last_group and d["customer_id"] in matrix
            ]
            if not dropoff_dogs:
                continue

            stop_ids = [d["customer_id"] for d in dropoff_dogs]
            result = solve_simple_trip(matrix, stop_ids, field, parking)

            if result:
                route, dist = result
                for i, loc_id in enumerate(route):
                    d = dog_lookup.get(loc_id, {})
                    extra = get_extra_info(loc_id)
                    if loc_id == field:
                        action = "LEAVE"
                        display_name, addr = "Leave Field", field_address
                    elif loc_id == parking:
                        action = "ARRIVE"
                        display_name, addr = "Arrive at Parking", parking_address
                    else:
                        action = "DROP OFF"
                        display_name = get_dog_display_name(loc_id, action)
                        addr = d.get("address", "")
                    results.append({
                        "Driver": driver_name, "Leg": leg_num + 1, "Stop": i + 1,
                        "Action": action, "Customer ID": loc_id,
                        "Dog Name": display_name, "Address": addr,
                        "Phone": extra.get("Phone", ""),
                        "Customer Name": extra.get("Customer Name", ""),
                        "Instructions": extra.get("Instructions", ""),
                        "Dog Breed": extra.get("Dog Breed", ""),
                        "House Description": extra.get("House Description", ""),
                        "Dogs at Stop": d.get("dog_count", ""),
                        "Dogs on Board": "", "Assignment": d.get("raw", "") or driver_name,
                        "Drive Min": round(dist, 1) if loc_id == parking else "",
                    })

    return results


# =============================================================================
# WRITE TO SHEET
# =============================================================================

def write_results_to_sheet(client, sheet_name, new_results, optimized_drivers):
    """Write routes to sheet with new column layout."""
    sheet = client.open(sheet_name)
    
    header = ["Drive Min", "Min to Next", "Dog Name", "Address", "Phone",
              "Customer Name", "Instructions", "Dog Breed", "House Description", "Assignment"]

    # Read existing results (if any) and keep rows for drivers NOT being re-optimized
    existing_rows = []
    try:
        existing_ws = sheet.worksheet(OUTPUT_TAB_NAME)
        existing_data = existing_ws.get_all_values()
        
        # For merging, check if partial run — use Assignment column (index 9) to find driver
        # Assignment format is "DriverName:code"
        if (len(existing_data) > 0
            and existing_data[0] == header
            and len(optimized_drivers) < len(set(
                row[9].split(":")[0] for row in existing_data[1:] 
                if row and len(row) > 9 and ":" in row[9]
            )) / 2):
            for row in existing_data[1:]:
                if len(row) > 9 and ":" in row[9]:
                    row_driver = row[9].split(":")[0]
                    if row_driver not in optimized_drivers:
                        existing_rows.append(row)
                elif len(row) > 0 and row[0]:
                    # Keep non-dog rows (field/parking) if driver not in optimized list
                    existing_rows.append(row)
        
        sheet.del_worksheet(existing_ws)
    except gspread.exceptions.WorksheetNotFound:
        pass

    # Build new rows
    new_rows = []
    for r in new_results:
        new_rows.append([
            r.get("Drive Min", ""),
            r.get("Min to Next", ""),
            r.get("Dog Name", ""),
            r.get("Address", ""),
            r.get("Phone", ""),
            r.get("Customer Name", ""),
            r.get("Instructions", ""),
            r.get("Dog Breed", ""),
            r.get("House Description", ""),
            r.get("Assignment", ""),
        ])

    # Combine: existing (unchanged drivers) + new (re-optimized drivers)
    all_rows = existing_rows + new_rows

    ws = sheet.add_worksheet(title=OUTPUT_TAB_NAME, rows=len(all_rows) + 1, cols=10)
    ws.update(range_name="A1", values=[header])

    if all_rows:
        ws.update(range_name="A2", values=all_rows)
    return len(all_rows)


# =============================================================================
# SNAPSHOT & CHANGE DETECTION
# =============================================================================

def save_snapshot(client, sheet_name, assignments):
    """Save current assignments for change detection."""
    sheet = client.open(sheet_name)
    try:
        existing = sheet.worksheet(SNAPSHOT_TAB_NAME)
        sheet.del_worksheet(existing)
    except gspread.exceptions.WorksheetNotFound:
        pass

    rows = [["Driver", "Customer ID", "Assignment", "Dog Count"]]
    for a in assignments:
        rows.append([a["driver"], a["customer_id"], a["raw"], a["dog_count"]])

    ws = sheet.add_worksheet(title=SNAPSHOT_TAB_NAME, rows=len(rows), cols=4)
    ws.update(range_name="A1", values=rows)


def load_snapshot(client, sheet_name):
    """Load last-optimized snapshot. Returns dict: driver -> set of (customer_id, assignment)."""
    try:
        sheet = client.open(sheet_name)
        ws = sheet.worksheet(SNAPSHOT_TAB_NAME)
        data = ws.get_all_values()
    except Exception:
        return None

    snapshot = {}
    for row in data[1:]:
        if len(row) < 3:
            continue
        driver = row[0].strip()
        cid = row[1].strip()
        assignment = row[2].strip()
        if driver not in snapshot:
            snapshot[driver] = set()
        snapshot[driver].add((cid, assignment))
    return snapshot


def detect_changes(assignments, snapshot):
    """Compare current assignments against snapshot. Returns dict of changes per driver."""
    if snapshot is None:
        return None

    current = {}
    for a in assignments:
        driver = a["driver"]
        if driver not in current:
            current[driver] = set()
        current[driver].add((a["customer_id"], a["raw"]))

    changes = {}
    all_drivers = set(list(current.keys()) + list(snapshot.keys()))
    for driver in all_drivers:
        cur = current.get(driver, set())
        prev = snapshot.get(driver, set())
        if cur != prev:
            added = cur - prev
            removed = prev - cur
            changes[driver] = {"added": added, "removed": removed}

    return changes


def auto_add_to_matrix(client, matrix, missing_dogs, schedule_data):
    """Automatically add missing dogs to the matrix using ORS API."""
    import requests
    import time as _time

    ors_key = st.secrets.get("ors_api_key", "")
    if not ors_key:
        st.warning("No ors_api_key in secrets — cannot auto-add dogs.")
        return matrix

    # Get lat/lngs for existing matrix entries from Schedule
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
        loc_sheet = client.open(SHEET_NAME)
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
    except Exception:
        pass  # Locations tab doesn't exist yet — skip

    existing_ids = list(existing_with_coords.keys())
    existing_coords = [[existing_with_coords[eid]["lng"], existing_with_coords[eid]["lat"]]
                       for eid in existing_ids]

    if not existing_ids:
        st.warning("No existing dogs have coordinates — cannot compute distances.")
        return matrix

    # Download matrix CSV from Drive
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
    from google.oauth2.service_account import Credentials as Creds

    creds = Creds.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    drive = build("drive", "v3", credentials=creds)

    matrix_file_name = st.secrets.get("matrix_file_name", "matrix.csv")
    file_list = drive.files().list(
        q=f"name='{matrix_file_name}' and trashed=false",
        fields="files(id)"
    ).execute().get("files", [])

    if not file_list:
        st.warning("Could not find matrix file in Drive.")
        return matrix

    file_id = file_list[0]["id"]

    req = drive.files().get_media(fileId=file_id)
    content = io.BytesIO()
    dl = MediaIoBaseDownload(content, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    content.seek(0)
    matrix_text = content.read().decode("utf-8-sig")

    reader_obj = csv.reader(io.StringIO(matrix_text))
    all_rows = list(reader_obj)
    header = all_rows[0]
    data_rows = all_rows[1:]

    progress = st.progress(0, text="Starting matrix update...")
    total = len(missing_dogs)
    completed = 0

    for new_id, new_coords in missing_dogs.items():
        completed += 1
        progress.progress(completed / total, text=f"Adding {new_id} ({completed}/{total})...")

        new_loc = [new_coords["lng"], new_coords["lat"]]
        new_to_existing = {}
        existing_to_new = {}
        batch_size = 25

        # Only compute ORS distances to dogs within 10 miles (haversine pre-filter)
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
            # Always include fields (F) and parking (P), filter dogs by distance
            if eid.endswith("F") or eid.endswith("P"):
                nearby_ids.append(eid)
                nearby_coords.append(ecoord)
            else:
                dist = haversine_miles(new_coords["lat"], new_coords["lng"], ecoord[1], ecoord[0])
                if dist <= 7:
                    nearby_ids.append(eid)
                    nearby_coords.append(ecoord)

        progress.progress(completed / total,
            text=f"Adding {new_id} ({completed}/{total}) — {len(nearby_ids)} nearby locations...")

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
            except Exception:
                pass
            _time.sleep(0.5)

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
            except Exception:
                pass
            _time.sleep(0.5)

        # Update CSV: add column to header
        header.append(new_id)

        # Add distance to each existing row
        for row in data_rows:
            row_id = row[0].strip()
            row.append(str(existing_to_new.get(row_id, 9999)))

        # Create new row
        new_row = [new_id]
        for col_id in header[1:]:
            if col_id == new_id:
                new_row.append("0")
            else:
                new_row.append(str(new_to_existing.get(col_id, 9999)))
        data_rows.append(new_row)

        # Update in-memory matrix
        matrix[new_id] = {}
        for cid, dist in new_to_existing.items():
            matrix[new_id][cid] = dist
        for cid, dist in existing_to_new.items():
            if cid in matrix:
                matrix[cid][new_id] = dist

        # Add to existing lists for subsequent dogs
        existing_ids.append(new_id)
        existing_coords.append(new_loc)

    # Upload updated CSV to Drive
    progress.progress(1.0, text="Uploading updated matrix...")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    for row in data_rows:
        writer.writerow(row)

    media = MediaIoBaseUpload(
        io.BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        resumable=True
    )
    request = drive.files().update(fileId=file_id, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()

    st.success(f"✅ Added {total} dog(s) to matrix automatically.")
    return matrix


# =============================================================================
# STREAMLIT UI
# =============================================================================

def main():
    st.set_page_config(page_title="Doggy Dates Route Optimizer", page_icon="🐕", layout="wide")

    st.title("🐕 Doggy Dates Route Optimizer")

    # Refresh button to reload data from Sheets
    if st.button("🔄 Refresh Data", help="Reload latest data from Google Sheets"):
        st.cache_data.clear()
        for key in list(st.session_state.keys()):
            if key.startswith("driver_") or key.startswith("defaults_applied"):
                del st.session_state[key]
        st.rerun()

    # ── Connect to Google Sheets ──
    try:
        client = get_gspread_client()
    except Exception as e:
        st.error(f"Could not connect to Google Sheets. Check your secrets. Error: {e}")
        st.stop()

    # ── Load matrix from Google Drive ──
    matrix = load_matrix_from_drive(client, MATRIX_FILE_NAME)
    st.sidebar.success(f"Matrix loaded: {len(matrix)} locations")

    # ── Load Staff from Routing sheet ──
    with st.spinner("Reading Staff data..."):
        try:
            staff_data = load_staff_from_sheet(client, SHEET_NAME)
        except Exception as e:
            st.error(f"Could not read Staff tab from '{SHEET_NAME}'. Error: {e}")
            st.stop()

    drivers = parse_staff(staff_data)

    # ── Load Schedule sheet and pick a date ──
    schedule_sheet_id = st.secrets.get("schedule_sheet_id", "")
    if not schedule_sheet_id:
        st.error("No schedule_sheet_id in secrets. Add it to your Streamlit secrets.")
        st.stop()

    with st.spinner("Reading Schedule data..."):
        try:
            schedule_data = load_schedule_sheet(client, schedule_sheet_id)
        except Exception as e:
            st.error(f"Could not read Schedule tab. Error: {e}")
            st.stop()

    available_dates = get_available_dates(schedule_data)

    if not available_dates:
        st.error("No dates found for today or later in the Schedule tab.")
        st.stop()

    selected_date = st.selectbox("Select date:", list(available_dates.keys()))
    date_col_idx = available_dates[selected_date]["col_idx"]

    # Reset checkboxes when date changes
    if st.session_state.get("last_date") != selected_date:
        for key in list(st.session_state.keys()):
            if key.startswith("driver_"):
                del st.session_state[key]
        st.session_state["last_date"] = selected_date
        st.session_state.pop("results", None)
        st.session_state.pop("errors", None)

    assignments = parse_schedule(schedule_data, date_col_idx)

    st.sidebar.markdown(f"**Active drivers:** {len(drivers)}")
    st.sidebar.markdown(f"**Dog assignments:** {len(assignments)}")

    # ── Build driver info ──
    active_drivers_with_dogs = []
    for name in sorted(drivers.keys()):
        config = drivers[name]
        # Derive groups from actual assignments, not Staff tab
        config["groups"] = derive_groups(assignments, name)
        dogs = [a for a in assignments if a["driver"] == name]
        if dogs and config["groups"]:
            dog_count = sum(d["dog_count"] for d in dogs)
            staff_count = sum(d["dog_count"] for d in dogs if d["is_staff_dog"])
            active_drivers_with_dogs.append({
                "name": name,
                "groups": config["groups"],
                "capacity": config["capacity"],
                "dogs": dog_count,
                "staff_dogs": staff_count,
            })

    # ── Auto-check for missing dogs and add them ──
    all_matrix_ids = set(matrix.keys())
    missing_dogs = {}
    for a in assignments:
        if a["customer_id"] not in all_matrix_ids and not a["is_staff_dog"]:
            # Get lat/lng from schedule
            for row in schedule_data[2:]:
                if len(row) > 9 and row[6].strip() == a["customer_id"]:
                    lat = row[8].strip()
                    lng = row[9].strip()
                    if lat and lng:
                        try:
                            missing_dogs[a["customer_id"]] = {"lat": float(lat), "lng": float(lng)}
                        except ValueError:
                            pass
                    break

    missing_fields_parking = set()
    for d in drivers.values():
        if d["field_id"] not in all_matrix_ids:
            missing_fields_parking.add(d["field_id"])
        if d["parking_id"] not in all_matrix_ids:
            missing_fields_parking.add(d["parking_id"])

    if missing_fields_parking:
        st.warning(f"⚠️ {len(missing_fields_parking)} field/parking IDs not in matrix: {sorted(missing_fields_parking)}. These need to be added manually.")

    if missing_dogs:
        st.warning(f"⚠️ {len(missing_dogs)} new dog(s) not in matrix: {', '.join(missing_dogs.keys())}")
        if st.button(f"➕ Add {len(missing_dogs)} dog(s) to matrix now", type="secondary"):
            matrix = auto_add_to_matrix(client, matrix, missing_dogs, schedule_data)
            all_matrix_ids = set(matrix.keys())

    # ── Load snapshot and detect changes ──
    snapshot = load_snapshot(client, SHEET_NAME)
    changes = detect_changes(assignments, snapshot)

    # ── Driver checklist ──
    st.subheader("Select Drivers to Optimize")

    if changes is not None:
        changed_drivers = set(changes.keys())
        if changed_drivers:
            changed_active = changed_drivers & set(d["name"] for d in active_drivers_with_dogs)
            if changed_active:
                # Count total changes
                total_changes = sum(len(changes[d]["added"]) + len(changes[d]["removed"]) for d in changed_active)

                # If too many changes, just show summary
                if len(changed_active) > 7 or total_changes > 30:
                    st.info(f"🔄 {len(changed_active)} driver(s) have changes since last optimization ({total_changes} total changes). Select All and re-optimize.")
                else:
                    # Build dog name lookup from schedule
                    dog_name_lookup = {}
                    for row in schedule_data[2:]:
                        if len(row) > 6:
                            cid = row[6].strip()
                            dname = row[1].strip() if len(row) > 1 else cid
                            dog_name_lookup[cid] = dname

                    # Check for cancelled dogs in current schedule
                    cancelled_dogs = set()
                    for row in schedule_data[2:]:
                        if len(row) > date_col_idx:
                            cid = row[6].strip() if len(row) > 6 else ""
                            val = row[date_col_idx].strip()
                            if cid and "cancel" in val.lower():
                                cancelled_dogs.add(cid)

                    # Build detail lines
                    change_details = []
                    for driver_name in sorted(changed_active):
                        c = changes[driver_name]
                        parts = []
                        if c["added"]:
                            added_items = []
                            for cid, assignment in c["added"]:
                                name = dog_name_lookup.get(cid, cid)[:25]
                                added_items.append(f"{name} ({assignment})")
                            parts.append(f"**added:** {', '.join(added_items)}")
                        if c["removed"]:
                            removed_items = []
                            for cid, assignment in c["removed"]:
                                name = dog_name_lookup.get(cid, cid)[:25]
                                if cid in cancelled_dogs:
                                    removed_items.append(f"~~{name}~~ CANCELLED")
                                else:
                                    removed_items.append(f"{name} ({assignment})")
                            parts.append(f"**removed:** {', '.join(removed_items)}")
                        change_details.append(f"• **{driver_name}** — {'; '.join(parts)}")
                    
                    st.info(f"🔄 {len(changed_active)} driver(s) have changes since last optimization:\n\n" + "\n".join(change_details))
        else:
            st.success("No changes detected since last optimization")
    else:
        changed_drivers = set()

    # Select All / Select None / Select Changed + Optimize button on same row
    btn_col1, btn_col2, btn_col3, btn_col4 = st.columns(4)
    with btn_col1:
        if st.button("Select All", use_container_width=True):
            for d in active_drivers_with_dogs:
                st.session_state[f"driver_{d['name']}"] = True
            st.rerun()
    with btn_col2:
        if st.button("Select None", use_container_width=True):
            for d in active_drivers_with_dogs:
                st.session_state[f"driver_{d['name']}"] = False
            st.rerun()
    with btn_col3:
        if changes and changed_drivers:
            if st.button("Select Changed", use_container_width=True):
                for d in active_drivers_with_dogs:
                    st.session_state[f"driver_{d['name']}"] = d["name"] in changed_drivers
                st.rerun()

    # Optimize button placeholder — renders here but triggered after checklist
    optimize_placeholder = st.empty()

    # Determine if most drivers changed — if so, just select all
    if changes is not None and len(active_drivers_with_dogs) > 0:
        pct_changed = len(changed_drivers & set(d["name"] for d in active_drivers_with_dogs)) / len(active_drivers_with_dogs)
        mostly_changed = pct_changed >= 0.70
    else:
        mostly_changed = False

    selected_drivers = []
    
    # Grid layout — 4 columns
    n_cols = 4
    driver_list = active_drivers_with_dogs
    rows_needed = (len(driver_list) + n_cols - 1) // n_cols

    for row_idx in range(rows_needed):
        cols = st.columns(n_cols)
        for col_idx in range(n_cols):
            d_idx = row_idx * n_cols + col_idx
            if d_idx >= len(driver_list):
                break
            d = driver_list[d_idx]
            name = d["name"]
            has_changes = name in changed_drivers

            # Default: only on first load for this date
            defaults_key = f"defaults_applied_{selected_date}"
            if not st.session_state.get(defaults_key, False):
                if changes is None or mostly_changed:
                    st.session_state[f"driver_{name}"] = True
                else:
                    st.session_state[f"driver_{name}"] = has_changes

            # Simple label — just name + change indicator
            change_tag = " 🔄" if has_changes else ""
            label = f"{name}{change_tag}"

            with cols[col_idx]:
                if st.checkbox(label, key=f"driver_{name}"):
                    selected_drivers.append(name)

    # Mark defaults as applied so they don't reset on every interaction
    st.session_state[f"defaults_applied_{selected_date}"] = True

    # Now render the optimize button in the placeholder above the checklist
    with optimize_placeholder:
        if selected_drivers:
            optimize_btn = st.button(
                f"🚀 Optimize {len(selected_drivers)} Driver{'s' if len(selected_drivers) != 1 else ''}",
                type="primary",
                use_container_width=True,
            )
        else:
            st.write("Select at least one driver to optimize.")
            optimize_btn = False

    if optimize_btn:
        all_results = []
        errors = []
        progress = st.progress(0, text="Starting...")

        # Build schedule lookup for extra columns
        schedule_lookup = {}
        for row in schedule_data[2:]:
            cid = row[6].strip() if len(row) > 6 else ""
            if cid:
                schedule_lookup[cid] = {
                    "phone": row[5].strip() if len(row) > 5 else "",
                    "customer_name": row[3].strip() if len(row) > 3 else "",
                    "instructions": row[62].strip() if len(row) > 62 else "",
                    "dog_breed": row[60].strip() if len(row) > 60 else "",
                    "house_description": row[61].strip() if len(row) > 61 else "",
                }

        # Prepare jobs
        driver_jobs = []
        for name in selected_drivers:
            config = drivers[name]
            dogs = [a for a in assignments if a["driver"] == name]
            driver_jobs.append((matrix, name, config, dogs, schedule_lookup))

        # Solve in parallel
        from concurrent.futures import ProcessPoolExecutor, as_completed
        import multiprocessing

        n_workers = min(4, multiprocessing.cpu_count(), len(driver_jobs))

        completed = 0
        with ProcessPoolExecutor(max_workers=n_workers) as executor:
            futures = {
                executor.submit(solve_driver, *job): job[1]
                for job in driver_jobs
            }
            for future in as_completed(futures):
                name = futures[future]
                completed += 1
                progress.progress(
                    completed / len(driver_jobs),
                    text=f"Solved {name} ({completed}/{len(driver_jobs)})..."
                )
                try:
                    results = future.result()
                    all_results.extend(results)
                except Exception as e:
                    errors.append(f"{name}: {str(e)}")

        progress.progress(1.0, text="Done!")

        # Add "Min to Next" column — drive time from each stop to the next
        OUTLIER_THRESHOLD = 10  # minutes — flag anything above this
        for i in range(len(all_results) - 1):
            curr = all_results[i]
            nxt = all_results[i + 1]
            # Only calculate within same driver and same leg
            if curr["Driver"] == nxt["Driver"] and curr["Leg"] == nxt["Leg"]:
                from_id = curr["Customer ID"]
                to_id = nxt["Customer ID"]
                if from_id in matrix and to_id in matrix.get(from_id, {}):
                    mins = round(matrix[from_id][to_id], 1)
                    all_results[i]["Min to Next"] = mins
                else:
                    all_results[i]["Min to Next"] = ""
            else:
                all_results[i]["Min to Next"] = ""
        all_results[-1]["Min to Next"] = ""  # last stop has no next

        st.session_state["results"] = all_results
        st.session_state["errors"] = errors
        st.session_state["optimized_drivers"] = selected_drivers

        # Auto-write to Google Sheet and save snapshot
        with st.spinner("Writing routes to Google Sheet..."):
            try:
                count = write_results_to_sheet(client, SHEET_NAME, all_results, selected_drivers)
                save_snapshot(client, SHEET_NAME, assignments)
                st.session_state["write_success"] = f"✅ Wrote {count} total rows to '{OUTPUT_TAB_NAME}' (updated {len(selected_drivers)} drivers, kept others)."
            except Exception as e:
                st.session_state["write_error"] = f"Failed to write: {e}"

    # ── Results ──
    if "results" in st.session_state and st.session_state["results"]:
        results = st.session_state["results"]
        errors = st.session_state.get("errors", [])
        optimized_drivers = st.session_state.get("optimized_drivers", [])

        if st.session_state.get("write_success"):
            st.success(st.session_state["write_success"])
        if st.session_state.get("write_error"):
            st.error(st.session_state["write_error"])

        if errors:
            st.error(f"Errors on {len(errors)} drivers: {errors}")

        # Validation
        validation_issues = []
        for driver_name in optimized_drivers:
            if driver_name not in drivers:
                continue

            expected_dogs = [
                a for a in assignments
                if a["driver"] == driver_name and not a["is_staff_dog"]
            ]
            expected_ids = set(a["customer_id"] for a in expected_dogs)

            driver_results = [r for r in results if r["Driver"] == driver_name]
            routed_ids = set(
                r["Customer ID"] for r in driver_results
                if r["Action"] in ("PICK UP", "DROP OFF")
            )

            missing_from_route = expected_ids - routed_ids
            if missing_from_route:
                for mid in missing_from_route:
                    dog_info = next((a for a in expected_dogs if a["customer_id"] == mid), {})
                    config = drivers[driver_name]
                    if mid not in matrix:
                        reason = "not in matrix"
                    else:
                        reason = "unknown"
                    validation_issues.append({
                        "Driver": driver_name,
                        "Missing ID": mid,
                        "Dog Name": dog_info.get("dog_name", "?"),
                        "Address": dog_info.get("address", "?"),
                        "Assignment": dog_info.get("raw", "?"),
                        "Reason": reason,
                    })

            pickup_ids = set(r["Customer ID"] for r in driver_results if r["Action"] == "PICK UP")
            dropoff_ids = set(r["Customer ID"] for r in driver_results if r["Action"] == "DROP OFF")

            for mid in pickup_ids - dropoff_ids:
                dog_info = next((a for a in expected_dogs if a["customer_id"] == mid), {})
                validation_issues.append({
                    "Driver": driver_name, "Missing ID": mid,
                    "Dog Name": dog_info.get("dog_name", "?"),
                    "Address": dog_info.get("address", "?"),
                    "Reason": "picked up but never dropped off",
                })
            for mid in dropoff_ids - pickup_ids:
                dog_info = next((a for a in expected_dogs if a["customer_id"] == mid), {})
                validation_issues.append({
                    "Driver": driver_name, "Missing ID": mid,
                    "Dog Name": dog_info.get("dog_name", "?"),
                    "Address": dog_info.get("address", "?"),
                    "Reason": "dropped off but never picked up",
                })

        if validation_issues:
            st.error(f"🚨 {len(validation_issues)} MISSING STOPS:")
            st.dataframe(pd.DataFrame(validation_issues), use_container_width=True, hide_index=True)
        else:
            st.success(f"✅ All dogs accounted for across {len(optimized_drivers)} drivers.")

        # Outlier check — only flag long gaps between dog stops in the middle of a leg
        # Skip first/last dog stops since they're near field or parking
        # Skip if the "far" dog is within 5 min of its next stop (it's in a cluster)
        outliers = []
        for i in range(len(results) - 1):
            r = results[i]
            nxt = results[i + 1]
            if (r.get("Min to Next") and r["Min to Next"] != ""
                and r["Min to Next"] > 10
                and r["Action"] in ("PICK UP", "DROP OFF")
                and nxt["Action"] in ("PICK UP", "DROP OFF")):
                # Check if this is an edge stop (first or last dog in the leg)
                prev_action = results[i - 1]["Action"] if i > 0 else ""
                next_next_action = results[i + 2]["Action"] if i + 2 < len(results) else ""
                if prev_action in ("START", "LEAVE", "LEAVE FIELD"):
                    continue
                if next_next_action in ("ARRIVE", "ARRIVE FIELD"):
                    continue
                # Check if the "to" dog is close to its next stop
                nxt_min = nxt.get("Min to Next", "")
                if nxt_min != "" and nxt_min <= 5:
                    continue  # dog is in a cluster, just far from previous stop
                outliers.append({
                    "Driver": r["Driver"],
                    "Leg": r["Leg"],
                    "From": r["Dog Name"],
                    "To": nxt["Dog Name"],
                    "Min Between": r["Min to Next"],
                })
        if outliers:
            st.warning(f"⚠️ {len(outliers)} long gaps between stops (over 10 min):")
            st.dataframe(pd.DataFrame(outliers), use_container_width=True, hide_index=True)

        # Capacity warning — flag drivers who exceed their nominal capacity
        over_capacity = []
        for driver_name in optimized_drivers:
            if driver_name not in drivers:
                continue
            cap = drivers[driver_name]["capacity"]
            driver_results = [r for r in results if r["Driver"] == driver_name]
            max_load = 0
            for r in driver_results:
                load = r.get("Dogs on Board", "")
                if load != "" and isinstance(load, (int, float)):
                    max_load = max(max_load, load)
            if max_load > cap:
                over_capacity.append({
                    "Driver": driver_name,
                    "Capacity": cap,
                    "Max Dogs on Board": int(max_load),
                    "Over By": int(max_load - cap),
                })
        if over_capacity:
            st.warning(f"🐕 {len(over_capacity)} driver(s) over capacity:")
            st.dataframe(pd.DataFrame(over_capacity), use_container_width=True, hide_index=True)



if __name__ == "__main__":
    main()
