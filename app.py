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

        if not name:
            continue

        capacity = int(capacity_str) if capacity_str.isdigit() else 0

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

def write_results_to_sheet(client, sheet_name, new_results, optimized_drivers, selected_date):
    """Write routes to sheet with date tracking."""
    sheet = client.open(sheet_name)
    
    header = ["Assignment", "Min to Next", "Dog Name", "Address", "Phone",
              "Customer Name", "Instructions", "Dog Breed", "House Description",
              "Driver Trip", "Customer ID"]

    # Read existing data for name preservation and merging
    existing_rows = []
    custom_names = {}
    try:
        existing_ws = sheet.worksheet(OUTPUT_TAB_NAME)
        existing_data = existing_ws.get_all_values()
        
        # Check if existing data is from the same date
        existing_date = existing_data[0][0] if existing_data and existing_data[0] else ""
        same_date = (existing_date == selected_date)
        
        if same_date and len(existing_data) > 1 and existing_data[1] == header:
            # Same date — check for manual name edits and merge
            for row in existing_data[2:]:  # skip date row and header
                if len(row) > 10 and row[10]:
                    cid = row[10].strip()
                    existing_name = row[2].strip() if len(row) > 2 else ""
                    if cid and existing_name:
                        custom_names[cid] = existing_name

                # Merge: keep rows for drivers NOT being re-optimized (partial runs only)
                total_drivers_in_sheet = len(set(
                    r[0].split(":")[0] for r in existing_data[2:]
                    if r and len(r) > 0 and ":" in r[0]
                ))
                if len(optimized_drivers) < total_drivers_in_sheet / 2:
                    if len(row) > 0 and ":" in row[0]:
                        row_driver = row[0].split(":")[0]
                        if row_driver not in optimized_drivers:
                            existing_rows.append(row)
                    elif len(row) > 9 and row[9]:
                        import re as _re
                        driver_match = _re.match(r'([A-Za-z]+)', row[9])
                        if driver_match and driver_match.group(1) not in optimized_drivers:
                            existing_rows.append(row)
        # Different date or no header match — full rewrite, no merge
        
        sheet.del_worksheet(existing_ws)
    except gspread.exceptions.WorksheetNotFound:
        pass

    # Build new rows with name preservation
    new_rows = []
    for r in new_results:
        driver_trip = f"{r.get('Driver', '')}{r.get('Leg', '')}"
        cid = r.get("Customer ID", "")
        
        dog_name = r.get("Dog Name", "")
        if cid in custom_names and custom_names[cid] != "":
            dog_name = custom_names[cid]

        new_rows.append([
            r.get("Assignment", ""),
            r.get("Min to Next", ""),
            dog_name,
            r.get("Address", ""),
            r.get("Phone", ""),
            r.get("Customer Name", ""),
            r.get("Instructions", ""),
            r.get("Dog Breed", ""),
            r.get("House Description", ""),
            driver_trip,
            cid,
        ])

    all_rows = existing_rows + new_rows

    # Build checklist for columns M, N, O — from the MERGED rows, so drivers that
    # weren't re-optimized keep their checklist entries (bug fix)
    checklist_rows = build_driver_checklist(rows_to_checklist_results(all_rows))
    
    max_rows = max(len(all_rows), len(checklist_rows)) + 2  # +2 for date row and header
    ws = sheet.add_worksheet(title=OUTPUT_TAB_NAME, rows=max_rows, cols=15)
    
    # Row 1: Date
    ws.update(range_name="A1", values=[[selected_date]])
    
    # Row 2: Header
    ws.update(range_name="A2", values=[header])
    
    # Row 3+: Data
    if all_rows:
        ws.update(range_name="A3", values=all_rows)

    # Checklist header and data (columns M-O)
    checklist_header = ["Dog", "Group", "Driver"]
    ws.update(range_name="M1", values=[[selected_date]])
    ws.update(range_name="M2", values=[checklist_header])
    if checklist_rows:
        ws.update(range_name="M3", values=checklist_rows)

    return len(all_rows)


ANCHOR_ID_RE = re.compile(r"^\d+[FP]$")


def rows_to_checklist_results(rows):
    """Convert sheet-format rows (lists) into dicts build_driver_checklist understands.
    Fixes the bug where partial re-runs dropped non-optimized drivers from the checklist."""
    out = []
    for row in rows:
        if not row or len(row) < 11:
            continue
        cid = (row[10] or "").strip()
        if not cid or ANCHOR_ID_RE.match(cid):
            continue
        assign = (row[0] or "").strip()
        driver = assign.split(":")[0].strip() if ":" in assign else ""
        if not driver and len(row) > 9 and row[9]:
            m = re.match(r"([A-Za-z]+)", row[9])
            driver = m.group(1) if m else ""
        if not driver:
            continue
        out.append({
            "Driver": driver, "Customer ID": cid, "Action": "PICK UP",
            "Dog Name": row[2] or "", "Assignment": assign,
        })
    return out


def _surgical_compute(rows, matrix, driver_name, config, assignments, schedule_lookup, target_cid=None):
    """Pure computation: given current sheet rows (row 3+ as lists, padded to 11 cols),
    apply Schedule differences for one driver surgically. Returns (final_rows, report).
    Raises ValueError with a user-facing message on any refusal. Never touches the sheet."""
    capacity = config["capacity"]

    trip_re = re.compile(rf"^{re.escape(driver_name)}(\d+)$")
    trip_rows = {}
    for idx, row in enumerate(rows):
        m = trip_re.match((row[9] or "").strip()) if len(row) > 9 else None
        if m:
            trip_rows.setdefault(int(m.group(1)), []).append(idx)
    if not trip_rows:
        raise ValueError(
            f"No existing route rows found for {driver_name} — run a full optimization for this driver first."
        )

    my_assignments = [a for a in assignments if a["driver"] == driver_name and not a["is_staff_dog"]]
    staff_load = sum(a["dog_count"] for a in assignments
                     if a["driver"] == driver_name and a["is_staff_dog"])
    sched_by_cid = {}
    for a in my_assignments:
        sched_by_cid.setdefault(a["customer_id"], []).append(a)

    # ── Group→trip mapping derived from the SHEET itself (the sheet is the structure
    # of record; Staff/Schedule math is not consulted for trip structure) ──
    first_seen, last_seen = {}, {}
    for t in sorted(trip_rows):
        for i in trip_rows[t]:
            c = (rows[i][10] or "").strip()
            if c and not ANCHOR_ID_RE.match(c):
                first_seen.setdefault(c, t)
                last_seen[c] = t
    from collections import Counter as _Counter
    _pv, _dv = {}, {}
    for c, t in first_seen.items():
        al = sched_by_cid.get(c)
        if al:
            _pv.setdefault(al[0]["pickup_group"], _Counter())[t] += 1
    for c, t in last_seen.items():
        al = sched_by_cid.get(c)
        if al:
            _dv.setdefault(al[0]["dropoff_group"], _Counter())[t] += 1
    trip_of_pickup = {g: v.most_common(1)[0][0] for g, v in _pv.items()}
    trip_of_dropoff = {g: v.most_common(1)[0][0] for g, v in _dv.items()}

    def _clamped_trip(mapping, g, fallback):
        """Nearest real trip for a group the driver doesn't normally run — deliberate
        assignments (e.g. a group 1 dog on a 10AM driver) are honored, never refused."""
        if g in mapping:
            return mapping[g]
        known = sorted(mapping)
        if not known:
            return fallback
        if g < known[0]:
            return mapping[known[0]]
        if g > known[-1]:
            return mapping[known[-1]]
        return mapping[max(k for k in known if k < g)]

    sheet_cids = set()
    for idxs in trip_rows.values():
        for i in idxs:
            cid = (rows[i][10] or "").strip()
            if cid and not ANCHOR_ID_RE.match(cid):
                sheet_cids.add(cid)

    new_cids = [c for c in sched_by_cid if c not in sheet_cids]
    removed_cids = [c for c in sheet_cids if c not in sched_by_cid]
    if target_cid is not None:
        new_cids = [c for c in new_cids if c == target_cid]
        removed_cids = [c for c in removed_cids if c == target_cid]
    if not new_cids and not removed_cids:
        return None, []

    if len(new_cids) > 1:
        raise ValueError(
            "Multiple new dogs in one surgical pass isn't supported — apply changes "
            "one at a time (the checkbox list already does this automatically)."
        )

    for c in new_cids:
        if len(sched_by_cid[c]) > 1:
            nm = sched_by_cid[c][0]["dog_name"] or c
            raise ValueError(
                f"{nm} has a split ('!') assignment — surgical add can't handle split trips. "
                f"Run a full re-optimization for {driver_name}."
            )

    report = []
    touched_trips = set()
    removed_set = set(removed_cids)
    deleted = set()

    if removed_cids:
        removed_names = []
        for t, idxs in list(trip_rows.items()):
            keep = []
            for i in idxs:
                if (rows[i][10] or "").strip() in removed_set:
                    nm = (rows[i][2] or "").strip() or (rows[i][10] or "").strip()
                    if nm not in removed_names:
                        removed_names.append(nm)
                    deleted.add(i)
                    touched_trips.add(t)
                else:
                    keep.append(i)
            trip_rows[t] = keep
        report.append("Removed " + ", ".join(removed_names) + " — everyone else stays in the same order.")

    def dist(a, b):
        try:
            v = matrix[a][b]
            return float(v)
        except Exception:
            return 9999.0

    def row_delta(row, trip):
        cid = (row[10] or "").strip()
        if not cid or ANCHOR_ID_RE.match(cid):
            return 0
        alist = sched_by_cid.get(cid)
        if not alist:
            return 0
        a = alist[0]
        if trip_of_pickup.get(a["pickup_group"]) == trip:
            return a["dog_count"]
        if trip_of_dropoff.get(a["dropoff_group"]) == trip:
            return -a["dog_count"]
        return 0

    def entering_load(trip):
        load = staff_load
        for a in my_assignments:
            tp = trip_of_pickup.get(a["pickup_group"])
            td = trip_of_dropoff.get(a["dropoff_group"])
            if tp is not None and td is not None and tp < trip <= td:
                load += a["dog_count"]
        return load

    def trip_loads(trip):
        start = entering_load(trip)
        loads, cur = [], start
        for i in trip_rows.get(trip, []):
            cur += row_delta(rows[i], trip)
            loads.append(cur)
        return start, loads

    inserts = {}  # orig_index -> list of new rows placed AFTER that index

    def make_row(a, trip):
        extra = schedule_lookup.get(a["customer_id"], {})
        cnt = a["dog_count"]
        prefix = "2\u20e3 " if cnt == 2 else ("3\u20e3 " if cnt == 3 else "")
        return [
            a["raw"], "", prefix + (a["dog_name"] or a["customer_id"]), a["address"],
            extra.get("phone", ""), extra.get("customer_name", ""),
            extra.get("instructions", ""), extra.get("dog_breed", ""),
            extra.get("house_description", ""), f"{driver_name}{trip}", a["customer_id"],
        ]

    def insert(trip, new_row, cnt, is_pickup, label):
        idxs = trip_rows.get(trip)
        if not idxs or len(idxs) < 2:
            raise ValueError(
                f"Trip {driver_name}{trip} not found (or too short) in the sheet — run a full re-optimization."
            )
        start, loads = trip_loads(trip)
        seq_ids = [(rows[i][10] or "").strip() for i in idxs]
        new_id = new_row[10]
        best, best_any = None, None
        for k in range(len(idxs) - 1):
            a_id, b_id = seq_ids[k], seq_ids[k + 1]
            if not a_id or not b_id:
                continue
            cost = dist(a_id, new_id) + dist(new_id, b_id) - dist(a_id, b_id)
            if cost >= 9000:
                continue
            if best_any is None or cost < best_any[1]:
                best_any = (k, cost)
            if is_pickup:
                fits = max(loads[k:]) + cnt <= capacity
            else:
                fits = max([start] + loads[: k + 1]) <= capacity
            if fits and (best is None or cost < best[1]):
                best = (k, cost)
        over = False
        if best is None:
            if best_any is None:
                raise ValueError(
                    f"No usable position for {label} in {driver_name}{trip} — missing "
                    f"distances in the matrix. Run a full re-optimization."
                )
            best = best_any  # capacity is best-effort, never a refusal
            over = True
        k, cost = best
        inserts.setdefault(idxs[k], []).append(new_row)
        touched_trips.add(trip)
        prev_name = (rows[idxs[k]][2] or "").strip() or "the start of the trip"
        return prev_name, cost, over

    def _resolve_dropoff_trip(td, new_a):
        """Fully re-solve the drop-off trip (it hasn't started yet). Existing rows for
        that trip are reordered per the solver; the new dog's row is added."""
        idxs = trip_rows.get(td)
        if not idxs or len(idxs) < 2:
            raise ValueError(f"Trip {driver_name}{td} not found in the sheet — run a full re-optimization.")
        anchor_first, anchor_last = rows[idxs[0]], rows[idxs[-1]]
        # sheet-derived trip span per dog: first appearance = pickup trip, last = dropoff trip
        cid_trips = {}
        for t, ii in trip_rows.items():
            for i in ii:
                c = (rows[i][10] or "").strip()
                if c and not ANCHOR_ID_RE.match(c):
                    cid_trips.setdefault(c, set()).add(t)
        row_by_cid = {}
        for i in idxs[1:-1]:
            c = (rows[i][10] or "").strip()
            if c and not ANCHOR_ID_RE.match(c):
                row_by_cid[c] = rows[i]
        def cnt_of(c):
            al = sched_by_cid.get(c)
            if al:
                return al[0]["dog_count"]
            nm = (row_by_cid.get(c, ["", "", ""])[2] or "")
            if nm.startswith("2\u20e3"):
                return 2
            if nm.startswith("3\u20e3"):
                return 3
            return 1
        drops, picks = [], []
        for c in row_by_cid:
            span = cid_trips.get(c, {td})
            if min(span) == td and max(span) != td:
                picks.append((c, cnt_of(c)))
            else:
                drops.append((c, cnt_of(c)))
        drops.append((new_a["customer_id"], new_a["dog_count"]))
        load = staff_load + new_a["dog_count"]
        for c, span in cid_trips.items():
            if min(span) < td <= max(span):
                load += cnt_of(c)
        is_final = (td == max(trip_rows))
        def row_for(c):
            if c == new_a["customer_id"]:
                return make_row(new_a, td)
            return row_by_cid[c]
        if is_final and not picks:
            res = solve_simple_trip(matrix, [c for c, _ in drops],
                                    config["field_id"], config["parking_id"])
            if res is None:
                raise ValueError(f"Couldn't re-solve {driver_name}{td} — run a full re-optimization.")
            route_ids, _tot = res
            ordered = [c for c in route_ids if c in row_by_cid or c == new_a["customer_id"]]
        else:
            res = None
            for _cap in (max(capacity, load), max(capacity + 4, load), load + 99):
                res = solve_interleaved_trip(matrix, drops, picks, config["field_id"],
                                             config["field_id"], _cap, load)
                if res is not None:
                    break
            if res is None:
                raise ValueError(
                    f"Couldn't re-solve {driver_name}{td} — run a full re-optimization."
                )
            route, _tot = res
            ordered = [loc for loc, _l, act in route if act in ("DROP OFF", "PICK UP")]
        # replace the block: keep first anchor row in place, rebuild the rest after it
        for i in idxs[1:]:
            deleted.add(i)
        inserts.setdefault(idxs[0], [])
        inserts[idxs[0]] = [row_for(c) for c in ordered] + [anchor_last] + inserts[idxs[0]]
        touched_trips.add(td)

    for cid in new_cids:
        a = sched_by_cid[cid][0]
        pu, do = a["pickup_group"], a["dropoff_group"]
        tp = _clamped_trip(trip_of_pickup, pu, min(trip_rows))
        td = _clamped_trip(trip_of_dropoff, do, max(trip_rows))
        if td <= tp:
            td = min(tp + 1, max(trip_rows))
        if cid not in matrix:
            raise ValueError(
                f"{a['dog_name'] or cid} isn't in the distance matrix yet — wait for the "
                f"matrix update (or run it manually), then try again."
            )
        prev_name, cost, over = insert(tp, make_row(a, tp), a["dog_count"], True,
                                       a["dog_name"] or cid)
        _resolve_dropoff_trip(td, a)
        _note = " ⚠️ over capacity — best available spot used." if over else ""
        report.append(
            f"{a['dog_name'] or cid}: picked up right after {prev_name} in {driver_name}{tp} "
            f"(+{cost:.1f} min) — no other pickups moved. {driver_name}{td} drop-offs "
            f"re-optimized to fit the new drop.{_note}"
        )

    # ── assemble final rows ──
    final_rows = []
    for idx, row in enumerate(rows):
        if idx not in deleted:
            final_rows.append(row)
        for nr in inserts.get(idx, []):
            final_rows.append(nr)

    # ── recompute Min-to-Next for touched trips ──
    for t in touched_trips:
        label = f"{driver_name}{t}"
        t_idx = [i for i, r in enumerate(final_rows) if (r[9] or "").strip() == label]
        for pos, i in enumerate(t_idx):
            if pos == len(t_idx) - 1:
                final_rows[i][1] = ""
                continue
            a_id = (final_rows[i][10] or "").strip()
            b_id = (final_rows[t_idx[pos + 1]][10] or "").strip()
            d = dist(a_id, b_id) if a_id and b_id else 9999.0
            final_rows[i][1] = round(d, 1) if d < 9000 else ""

    return final_rows, report


def surgical_apply(client, sheet_name, matrix, driver_name, config, assignments,
                   schedule_lookup, selected_date, target_cid=None):
    """Sheet wrapper around _surgical_compute. All validation happens BEFORE the tab
    is rewritten, so refusals never destroy existing routes."""
    sheet = client.open(sheet_name)
    try:
        ws = sheet.worksheet(OUTPUT_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        raise ValueError("No Routes tab found — run a full optimization first.")
    data = ws.get_all_values()
    if not data or not data[0] or (data[0][0] or "").strip() != selected_date:
        raise ValueError("The Routes tab is for a different date — run a full optimization first.")
    rows = [list(r) + [""] * (11 - len(r)) for r in data[2:]]

    final_rows, report = _surgical_compute(
        rows, matrix, driver_name, config, assignments, schedule_lookup, target_cid=target_cid
    )
    if final_rows is None:
        return []

    header = ["Assignment", "Min to Next", "Dog Name", "Address", "Phone",
              "Customer Name", "Instructions", "Dog Breed", "House Description",
              "Driver Trip", "Customer ID"]
    checklist_rows = build_driver_checklist(rows_to_checklist_results(final_rows))
    max_rows = max(len(final_rows), len(checklist_rows)) + 2
    sheet.del_worksheet(ws)
    ws = sheet.add_worksheet(title=OUTPUT_TAB_NAME, rows=max_rows, cols=15)
    ws.update(range_name="A1", values=[[selected_date]])
    ws.update(range_name="A2", values=[header])
    if final_rows:
        ws.update(range_name="A3", values=final_rows)
    ws.update(range_name="M1", values=[[selected_date]])
    ws.update(range_name="M2", values=[["Dog", "Group", "Driver"]])
    if checklist_rows:
        ws.update(range_name="M3", values=checklist_rows)
    return report


def build_driver_checklist(results):
    """Build a flat checklist of all dogs organized by driver and group."""
    # Collect all dogs by driver and the groups they participate in
    # Use the results to figure out which drivers and dogs exist
    dog_groups = {}  # (driver, customer_id, dog_name) → set of groups
    
    for r in results:
        driver = r.get("Driver", "")
        cid = r.get("Customer ID", "")
        action = r.get("Action", "")
        raw_name = r.get("Dog Name", "")
        
        if not cid or not driver or action not in ("PICK UP", "DROP OFF"):
            continue
        
        # Strip symbols from dog name to get clean name
        clean_name = raw_name.replace("◼", "").replace("2️⃣", "").replace("3️⃣", "").strip()
        # Remove "X & Y" prefix
        import re as _re
        clean_name = _re.sub(r'^\d+\s*&\s*\d+\s*', '', clean_name).strip()
        
        key = (driver, cid, clean_name)
        if key not in dog_groups:
            dog_groups[key] = set()
        
        # Derive groups from the Assignment string (e.g. "Ali:2&3" -> groups 2,3).
        # Never use Leg numbers: legs do not equal groups for 10AM drivers.
        raw = r.get("Assignment", "")
        if ":" in raw:
            code = raw.split(":")[1]
            parts = code.split("!") if "!" in code else [code]
            for part in parts:
                digits = _re.findall(r"\d", part)
                if digits:
                    for g in range(int(digits[0]), int(digits[-1]) + 1):
                        dog_groups[key].add(g)
    
    # If dog_groups is empty, try building from assignments in results
    if not dog_groups:
        # Fallback: use Assignment column to determine groups
        for r in results:
            driver = r.get("Driver", "")
            cid = r.get("Customer ID", "")
            raw = r.get("Assignment", "")
            action = r.get("Action", "")
            raw_name = r.get("Dog Name", "")
            
            if not cid or not driver or action not in ("PICK UP",):
                continue
            
            clean_name = raw_name.replace("2️⃣", "").replace("3️⃣", "").strip()
            clean_name = _re.sub(r'^\d+\s*&\s*\d+\s*', '', clean_name).strip()
            
            key = (driver, cid, clean_name)
            if key not in dog_groups:
                dog_groups[key] = set()
            
            if ":" in raw:
                code = raw.split(":")[1]
                if "!" in code:
                    for part in code.split("!"):
                        digits = _re.findall(r'\d', part)
                        if digits:
                            for g in range(int(digits[0]), int(digits[-1]) + 1):
                                dog_groups[key].add(g)
                else:
                    digits = _re.findall(r'\d', code)
                    if digits:
                        for g in range(int(digits[0]), int(digits[-1]) + 1):
                            dog_groups[key].add(g)

    # Build checklist rows
    group_emoji = {1: "✅ ", 2: "💛 ", 3: "🔴 "}
    checklist = []
    
    for (driver, cid, dog_name), groups_set in sorted(dog_groups.items()):
        for g in sorted(groups_set):
            emoji = group_emoji.get(g, "")
            checklist.append([
                f"{emoji}{dog_name}",
                g,
                driver,
            ])
    
    # Sort by driver, then group
    checklist.sort(key=lambda x: (x[2], x[1], x[0]))
    
    return checklist


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


def update_snapshot_for_driver(client, sheet_name, assignments, driver_name):
    """Refresh the snapshot for ONE driver (after a surgical update) so change
    detection stops flagging changes that were already handled surgically,
    while other drivers' pending changes stay visible."""
    sheet = client.open(sheet_name)
    try:
        ws = sheet.worksheet(SNAPSHOT_TAB_NAME)
        data = ws.get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        save_snapshot(client, sheet_name, assignments)
        return
    rows = [["Driver", "Customer ID", "Assignment", "Dog Count"]]
    for row in data[1:]:
        if row and row[0].strip() and row[0].strip() != driver_name:
            rows.append(list(row)[:4] + [""] * max(0, 4 - len(row)))
    for a in assignments:
        if a["driver"] == driver_name:
            rows.append([a["driver"], a["customer_id"], a["raw"], a["dog_count"]])
    sheet.del_worksheet(ws)
    ws = sheet.add_worksheet(title=SNAPSHOT_TAB_NAME, rows=len(rows), cols=4)
    ws.update(range_name="A1", values=rows)


def update_snapshot_for_dog(client, sheet_name, assignments, driver_name, cid):
    """Refresh the snapshot for ONE dog after a targeted surgical change, so only that
    change clears from the banner and the driver's other pending changes stay visible."""
    sheet = client.open(sheet_name)
    try:
        ws = sheet.worksheet(SNAPSHOT_TAB_NAME)
        data = ws.get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        save_snapshot(client, sheet_name, assignments)
        return
    rows = [["Driver", "Customer ID", "Assignment", "Dog Count"]]
    for row in data[1:]:
        if not row or not row[0].strip():
            continue
        if row[0].strip() == driver_name and len(row) > 1 and row[1].strip() == cid:
            continue  # drop the old snapshot line for this dog
        rows.append(list(row)[:4] + [""] * max(0, 4 - len(row)))
    for a in assignments:
        if a["driver"] == driver_name and a["customer_id"] == cid:
            rows.append([a["driver"], a["customer_id"], a["raw"], a["dog_count"]])
    sheet.del_worksheet(ws)
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

    scheduled_names = sorted(set(a["driver"] for a in assignments if a.get("driver")))
    st.sidebar.markdown(f"**Drivers on schedule:** {len(scheduled_names)}")
    st.sidebar.markdown(f"**Dog assignments:** {len(assignments)}")

    # ── Build driver info (Schedule tab is the source of truth) ──
    active_drivers_with_dogs = []
    missing_staff_info = []
    for name in scheduled_names:
        config = drivers.get(name)
        if not config or not config["field_id"] or not config["capacity"]:
            missing_staff_info.append(name)
            continue
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

    if missing_staff_info:
        st.warning(
            "These drivers are on the Schedule for this date but have no usable Staff row "
            "(missing field, parking, or capacity) and were skipped: "
            + ", ".join(missing_staff_info)
        )

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

    # ── Self-heal: drop "changes" that the Routes tab already reflects ──
    # A removal is handled if the dog isn't in the routes; an addition is handled if it
    # is. Group-changes (same dog in both added and removed) are never auto-cleared.
    if changes:
        try:
            _rt_ws = client.open(SHEET_NAME).worksheet(OUTPUT_TAB_NAME)
            _rt = _rt_ws.get_all_values()
            if _rt and _rt[0] and (_rt[0][0] or "").strip() == selected_date:
                _sheet_cids = {}
                for _r in _rt[2:]:
                    if len(_r) > 10 and _r[10].strip() and not ANCHOR_ID_RE.match(_r[10].strip()):
                        _asn = (_r[0] or "").strip()
                        _d = _asn.split(":")[0].strip() if ":" in _asn else ""
                        if _d:
                            _sheet_cids.setdefault(_d, set()).add(_r[10].strip())
                for _drv in list(changes.keys()):
                    _have = _sheet_cids.get(_drv, set())
                    _added = changes[_drv].get("added", set())
                    _removed = changes[_drv].get("removed", set())
                    _added_cids = {c for c, _ in _added}
                    _removed_cids = {c for c, _ in _removed}
                    _both = _added_cids & _removed_cids  # group change: keep pending
                    _done = []
                    for _cid, _raw in list(_added):
                        if _cid not in _both and _cid in _have:
                            _done.append((_cid, _raw, "added"))
                    for _cid, _raw in list(_removed):
                        if _cid not in _both and _cid not in _have:
                            _done.append((_cid, _raw, "removed"))
                    for _cid, _raw, _kind in _done:
                        update_snapshot_for_dog(client, SHEET_NAME, assignments, _drv, _cid)
                        changes[_drv][_kind].discard((_cid, _raw))
                    if not changes[_drv].get("added") and not changes[_drv].get("removed"):
                        del changes[_drv]
        except Exception:
            pass  # reconciliation is best-effort; never block the app on it

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
                count = write_results_to_sheet(client, SHEET_NAME, all_results, selected_drivers, selected_date)
                save_snapshot(client, SHEET_NAME, assignments)
                st.session_state["write_success"] = f"✅ Wrote {count} total rows to '{OUTPUT_TAB_NAME}' (updated {len(selected_drivers)} drivers, kept others)."
            except Exception as e:
                import traceback
                st.session_state["write_error"] = f"Failed to write: {e}\n\n{traceback.format_exc()}"

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

    # ── Surgical Add ──
    st.divider()
    st.subheader("🔧 Surgical Add")
    st.caption(
        "Apply pending Schedule changes without reshuffling routes in progress. "
        "Check the changes to apply: pickups get inserted at the cheapest capacity-safe spot "
        "(no other pickups move); drop-off trips are re-optimized since they haven't started. "
        "Use the main Optimize button when a full reshuffle is fine."
    )
    _name_by_cid = {}
    for _row in schedule_data[1:]:
        _c = _row[6].strip() if len(_row) > 6 else ""
        if _c:
            _name_by_cid[_c] = _row[1].strip() if len(_row) > 1 else _c
    _opts = []
    if changes:
        for _drv in sorted(changes.keys()):
            for _cid, _raw in sorted(changes[_drv].get("added", set())):
                _nm = _name_by_cid.get(_cid, _cid)[:10]
                _opts.append((f"{_raw}  Add {_nm}", _drv, _cid))
            for _cid, _raw in sorted(changes[_drv].get("removed", set())):
                _nm = _name_by_cid.get(_cid, _cid)[:10]
                _opts.append((f"{_raw}  Remove {_nm}", _drv, _cid))
    if not _opts:
        st.info("No pending schedule changes — nothing to apply surgically.")
    else:
        _checked = []
        for _label, _drv, _cid in _opts:
            if st.checkbox(_label, key=f"surg_{_drv}_{_cid}"):
                _checked.append((_label, _drv, _cid))
        if st.button(f"🪡 Apply {len(_checked)} change(s) surgically", key="surgical_btn",
                     disabled=(len(_checked) == 0)):
            _surg_lookup = {}
            for _row in schedule_data[2:]:
                _c = _row[6].strip() if len(_row) > 6 else ""
                if _c:
                    _surg_lookup[_c] = {
                        "phone": _row[5].strip() if len(_row) > 5 else "",
                        "customer_name": _row[3].strip() if len(_row) > 3 else "",
                        "instructions": _row[62].strip() if len(_row) > 62 else "",
                        "dog_breed": _row[60].strip() if len(_row) > 60 else "",
                        "house_description": _row[61].strip() if len(_row) > 61 else "",
                    }
            for _label, _drv, _cid in _checked:
                _cfg = drivers.get(_drv)
                if not _cfg or not _cfg["field_id"] or not _cfg["capacity"]:
                    st.error(f"{_label}: {_drv} has no usable Staff row — skipped.")
                    continue
                with st.spinner(f"Applying: {_label}..."):
                    try:
                        _rep = surgical_apply(
                            client, SHEET_NAME, matrix, _drv, _cfg,
                            assignments, _surg_lookup, selected_date, target_cid=_cid,
                        )
                        # Sync the snapshot in BOTH outcomes: routes changed now, or routes
                        # already matched (handled earlier but never recorded). Either way this
                        # change is done and must stop appearing in the pending list.
                        update_snapshot_for_dog(client, SHEET_NAME, assignments, _drv, _cid)
                        if _rep:
                            for _line in _rep:
                                st.success(_line)
                        else:
                            st.success(f"{_label}: routes already matched — marked as handled. "
                                       f"It won't appear in this list after the next refresh.")
                    except ValueError as _e:
                        st.error(f"{_label}: {_e}")
                    except Exception as _e:
                        import traceback
                        st.error(f"{_label}: failed — routes NOT rewritten for this change: {_e}\n\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
