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
import time
import os
from solver import solve_simple_trip, solve_interleaved_trip

# =============================================================================
# CONFIG
# =============================================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
# Set this to your Google Sheet name or ID
SHEET_NAME = st.secrets.get("sheet_name", "Routing")
MATRIX_FILE_NAME = st.secrets.get("matrix_file_name", "matrix.csv")
OUTPUT_TAB_NAME = "Optimized Routes"


# =============================================================================
# DATA LOADING
# =============================================================================

@st.cache_data(show_spinner="Loading distance matrix from Google Drive...")
def load_matrix_from_drive(_client, file_name):
    """Download matrix CSV from Google Drive and parse it."""
    # Find the file in Google Drive
    file_list = _client.list_spreadsheet_files()
    # That only lists spreadsheets — use raw Drive API instead
    import io
    from google.oauth2.service_account import Credentials as Creds
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

    creds = Creds.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    drive_service = build("drive", "v3", credentials=creds)

    # Search for the file by name
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
    st.sidebar.write(f"Found matrix: {files[0]['name']} ({files[0].get('size', '?')} bytes)")

    # Download the file
    request = drive_service.files().get_media(fileId=file_id)
    content = io.BytesIO()
    downloader = MediaIoBaseDownload(content, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    content.seek(0)
    text = content.read().decode("utf-8-sig")

    # Parse CSV
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
    """Connect to Google Sheets using service account credentials from Streamlit secrets."""
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    return gspread.authorize(creds)


def load_staff_from_sheet(client, sheet_name):
    """Read the Staff tab from Google Sheets."""
    sheet = client.open(sheet_name)
    ws = sheet.worksheet("Staff")
    data = ws.get_all_values()
    return data


def load_today_from_sheet(client, sheet_name):
    """Read the Today tab from Google Sheets."""
    sheet = client.open(sheet_name)
    ws = sheet.worksheet("Today")
    data = ws.get_all_values()
    return data


# =============================================================================
# PARSING
# =============================================================================

def parse_staff(data):
    """Parse Staff tab data into driver configs."""
    drivers = {}
    for row in data[1:]:  # skip header
        if len(row) < 9:
            continue
        name = row[0].strip()
        status = row[1].strip()
        notes = row[5].strip()
        field_id = row[6].strip()
        parking_id = row[7].strip()
        capacity_str = row[8].strip()

        if status == "OFF" or not field_id:
            continue
        if not capacity_str:
            continue

        capacity = int(capacity_str)

        if status == "10AM START" and notes == "NO THIRD":
            groups = [2]
        elif status == "10AM START":
            groups = [2, 3]
        elif notes == "NO THIRD":
            groups = [1, 2]
        else:
            groups = [1, 2, 3]

        drivers[name] = {
            "field_id": field_id,
            "parking_id": parking_id,
            "capacity": capacity,
            "groups": groups,
            "field_address": row[2].strip() if len(row) > 2 else "",
            "parking_address": row[4].strip() if len(row) > 4 else "",
        }
    return drivers


def parse_today(data):
    """Parse Today tab data into dog assignments."""
    assignments = []
    for row in data[1:]:  # skip header
        if len(row) < 11:
            continue

        customer_id = row[6].strip() if len(row) > 6 else ""
        email = row[4].strip() if len(row) > 4 else ""
        assignment_str = row[10].strip() if len(row) > 10 else ""
        dog_count = int(row[11].strip()) if len(row) > 11 and row[11].strip() else 1
        dog_name = row[1].strip() if len(row) > 1 else ""
        address = row[0].strip()

        if not customer_id or ":" not in assignment_str:
            continue

        parts = assignment_str.split(":")
        driver_name = parts[0].strip()
        code = parts[1].strip() if len(parts) > 1 else ""
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
# ROUTE SOLVER
# =============================================================================

def solve_driver(matrix, driver_name, config, dogs):
    """Solve all legs for a single driver. Returns list of route entries."""
    groups = config["groups"]
    field = config["field_id"]
    parking = config["parking_id"]
    capacity = config["capacity"]
    field_address = config.get("field_address", "")
    parking_address = config.get("parking_address", "")

    customer_dogs = [d for d in dogs if not d["is_staff_dog"]]
    staff_dogs = [d for d in dogs if d["is_staff_dog"]]
    dog_lookup = {d["customer_id"]: d for d in customer_dogs}

    results = []

    for leg_num in range(len(groups) + 1):

        if leg_num == 0:
            # ── First leg: pickup only ──
            current_group = groups[0]
            pickup_dogs = [
                d for d in customer_dogs
                if d["pickup_group"] == current_group and d["customer_id"] in matrix
            ]
            if not pickup_dogs:
                continue

            stop_ids = [d["customer_id"] for d in pickup_dogs]
            result = solve_simple_trip(matrix, stop_ids, parking, field)

            if result:
                route, dist = result
                for i, loc_id in enumerate(route):
                    d = dog_lookup.get(loc_id, {})
                    if loc_id == parking:
                        action = "START"
                        label = "Leave Parking"
                        addr = parking_address
                    elif loc_id == field:
                        action = "ARRIVE"
                        label = "Arrive at Field"
                        addr = field_address
                    else:
                        action = "PICK UP"
                        label = d.get("dog_name", loc_id)
                        addr = d.get("address", "")
                    results.append({
                        "Driver": driver_name,
                        "Leg": leg_num + 1,
                        "Stop": i + 1,
                        "Action": action,
                        "Customer ID": loc_id,
                        "Dog Name": label,
                        "Address": addr,
                        "Dogs at Stop": d.get("dog_count", ""),
                        "Dogs on Board": "",
                        "Assignment": d.get("raw", ""),
                        "Drive Min": round(dist, 1) if loc_id == field else "",
                    })

        elif leg_num < len(groups):
            # ── Middle leg: interleaved ──
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
            initial_load = dogs_being_dropped + staying_customer + staying_staff

            if not dropoffs and not pickups:
                continue

            result = solve_interleaved_trip(
                matrix, dropoffs, pickups, field, field, capacity, initial_load
            )

            if result:
                route, dist = result
                for i, (loc_id, load, action_raw) in enumerate(route):
                    d = dog_lookup.get(loc_id, {})
                    if action_raw == "LEAVE FIELD":
                        label = "Leave Field"
                        addr = field_address
                    elif action_raw == "ARRIVE FIELD":
                        label = "Arrive at Field"
                        addr = field_address
                    else:
                        label = d.get("dog_name", loc_id)
                        addr = d.get("address", "")
                    results.append({
                        "Driver": driver_name,
                        "Leg": leg_num + 1,
                        "Stop": i + 1,
                        "Action": action_raw,
                        "Customer ID": loc_id,
                        "Dog Name": label,
                        "Address": addr,
                        "Dogs at Stop": d.get("dog_count", ""),
                        "Dogs on Board": load,
                        "Assignment": d.get("raw", ""),
                        "Drive Min": round(dist, 1) if action_raw == "ARRIVE FIELD" else "",
                    })

        else:
            # ── Last leg: dropoff only ──
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
                    if loc_id == field:
                        action = "LEAVE"
                        label = "Leave Field"
                        addr = field_address
                    elif loc_id == parking:
                        action = "ARRIVE"
                        label = "Arrive at Parking"
                        addr = parking_address
                    else:
                        action = "DROP OFF"
                        label = d.get("dog_name", loc_id)
                        addr = d.get("address", "")
                    results.append({
                        "Driver": driver_name,
                        "Leg": leg_num + 1,
                        "Stop": i + 1,
                        "Action": action,
                        "Customer ID": loc_id,
                        "Dog Name": label,
                        "Address": addr,
                        "Dogs at Stop": d.get("dog_count", ""),
                        "Dogs on Board": "",
                        "Assignment": d.get("raw", ""),
                        "Drive Min": round(dist, 1) if loc_id == parking else "",
                    })

    return results


# =============================================================================
# WRITE TO SHEET
# =============================================================================

def write_results_to_sheet(client, sheet_name, all_results):
    """Write optimized routes to a new tab in the Google Sheet."""
    sheet = client.open(sheet_name)

    # Delete existing output tab if it exists
    try:
        existing = sheet.worksheet(OUTPUT_TAB_NAME)
        sheet.del_worksheet(existing)
    except gspread.exceptions.WorksheetNotFound:
        pass

    # Create new tab
    ws = sheet.add_worksheet(title=OUTPUT_TAB_NAME, rows=len(all_results) + 1, cols=11)

    # Write header
    header = ["Driver", "Leg", "Stop", "Action", "Customer ID",
              "Dog Name", "Address", "Dogs at Stop", "Dogs on Board", "Assignment", "Drive Min"]
    ws.update(range_name="A1", values=[header])

    # Write data
    rows = []
    for r in all_results:
        rows.append([
            r["Driver"], r["Leg"], r["Stop"], r["Action"],
            r["Customer ID"], r["Dog Name"], r["Address"],
            r["Dogs at Stop"], r["Dogs on Board"], r.get("Assignment", ""), r["Drive Min"],
        ])

    if rows:
        ws.update(range_name="A2", values=rows)

    return len(rows)


# =============================================================================
# STREAMLIT UI
# =============================================================================

def main():
    st.set_page_config(page_title="Doggy Dates Route Optimizer", page_icon="🐕", layout="wide")

    st.title("🐕 Doggy Dates Route Optimizer")

    # ── Connect to Google Sheets ──
    try:
        client = get_gspread_client()
    except Exception as e:
        st.error(f"Could not connect to Google Sheets. Check your secrets. Error: {e}")
        st.stop()

    # ── Load matrix from Google Drive ──
    matrix = load_matrix_from_drive(client, MATRIX_FILE_NAME)
    st.sidebar.success(f"Matrix loaded: {len(matrix)} locations")

    # ── Load data from Sheets ──
    with st.spinner("Reading from Google Sheets..."):
        try:
            staff_data = load_staff_from_sheet(client, SHEET_NAME)
            today_data = load_today_from_sheet(client, SHEET_NAME)
        except Exception as e:
            st.error(f"Could not read sheet '{SHEET_NAME}'. Error: {e}")
            st.stop()

    drivers = parse_staff(staff_data)
    assignments = parse_today(today_data)

    # ── Sidebar summary ──
    st.sidebar.markdown("---")
    st.sidebar.markdown(f"**Active drivers:** {len(drivers)}")
    st.sidebar.markdown(f"**Dog assignments:** {len(assignments)}")

    active_drivers_with_dogs = []
    for name in sorted(drivers.keys()):
        config = drivers[name]
        dogs = [a for a in assignments if a["driver"] == name
                and a["pickup_group"] in config["groups"]]
        if dogs:
            dog_count = sum(d["dog_count"] for d in dogs)
            staff_count = sum(d["dog_count"] for d in dogs if d["is_staff_dog"])
            active_drivers_with_dogs.append({
                "Driver": name,
                "Groups": str(config["groups"]),
                "Capacity": config["capacity"],
                "Dogs": dog_count,
                "Staff Dogs": staff_count,
                "Field": config["field_id"],
                "Parking": config["parking_id"],
            })

    st.subheader(f"Today's Drivers ({len(active_drivers_with_dogs)} active)")
    if active_drivers_with_dogs:
        st.dataframe(
            pd.DataFrame(active_drivers_with_dogs),
            use_container_width=True,
            hide_index=True,
        )

    # ── Check for missing IDs ──
    all_matrix_ids = set(matrix.keys())
    missing_ids = set()
    for a in assignments:
        if a["customer_id"] not in all_matrix_ids and not a["is_staff_dog"]:
            missing_ids.add(a["customer_id"])
    for d in drivers.values():
        if d["field_id"] not in all_matrix_ids:
            missing_ids.add(d["field_id"])
        if d["parking_id"] not in all_matrix_ids:
            missing_ids.add(d["parking_id"])

    if missing_ids:
        st.warning(f"⚠️ {len(missing_ids)} IDs not found in matrix: {sorted(missing_ids)}. "
                    "Routes involving these stops may fail.")

    # ── Optimize ──
    st.markdown("---")

    # Driver selection
    all_driver_names = sorted([d["Driver"] for d in active_drivers_with_dogs])
    
    col_select1, col_select2 = st.columns([1, 1])
    with col_select1:
        select_all = st.checkbox("Select all drivers", value=True)
    
    if select_all:
        selected_drivers = all_driver_names
    else:
        selected_drivers = st.multiselect(
            "Select drivers to optimize:",
            all_driver_names,
            default=[]
        )

    col1, col2 = st.columns([1, 3])
    with col1:
        optimize_btn = st.button(
            f"🚀 Optimize {len(selected_drivers)} Driver{'s' if len(selected_drivers) != 1 else ''}",
            type="primary",
            use_container_width=True,
            disabled=len(selected_drivers) == 0
        )

    if optimize_btn:
        all_results = []
        errors = []
        progress = st.progress(0, text="Starting...")

        driver_list = [d for d in sorted(active_drivers_with_dogs, key=lambda x: x["Driver"])
                       if d["Driver"] in selected_drivers]

        # Prepare all driver jobs
        driver_jobs = []
        for driver_info in driver_list:
            name = driver_info["Driver"]
            config = drivers[name]
            dogs = [a for a in assignments if a["driver"] == name
                    and a["pickup_group"] in config["groups"]]
            driver_jobs.append((matrix, name, config, dogs))

        # Solve in parallel
        from concurrent.futures import ProcessPoolExecutor, as_completed
        import multiprocessing

        # Use up to 4 workers (Streamlit Cloud typically has 2-4 cores)
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
                    completed / len(driver_list),
                    text=f"Solved {name} ({completed}/{len(driver_list)})..."
                )
                try:
                    results = future.result()
                    all_results.extend(results)
                except Exception as e:
                    errors.append(f"{name}: {str(e)}")

        progress.progress(1.0, text="Done!")
        st.session_state["results"] = all_results
        st.session_state["errors"] = errors
        st.session_state["selected_drivers_for_validation"] = selected_drivers

    # ── Display results ──
    if "results" in st.session_state and st.session_state["results"]:
        results = st.session_state["results"]
        errors = st.session_state.get("errors", [])
        validated_drivers = st.session_state.get("selected_drivers_for_validation", [])

        if errors:
            st.error(f"Errors on {len(errors)} drivers: {errors}")

        # ── VALIDATION: Check for missing stops ──
        validation_issues = []
        for driver_name in validated_drivers:
            if driver_name not in drivers:
                continue
            config = drivers[driver_name]
            
            # Expected: all non-staff dogs assigned to this driver
            expected_dogs = [
                a for a in assignments
                if a["driver"] == driver_name
                and not a["is_staff_dog"]
            ]
            expected_ids = set(a["customer_id"] for a in expected_dogs)
            
            # Actual: all customer stops in the results (exclude field/parking)
            driver_results = [r for r in results if r["Driver"] == driver_name]
            routed_ids = set(
                r["Customer ID"] for r in driver_results
                if r["Action"] in ("PICK UP", "DROP OFF")
            )
            
            # Check for missing pickups
            missing_from_route = expected_ids - routed_ids
            if missing_from_route:
                for mid in missing_from_route:
                    dog_info = next((a for a in expected_dogs if a["customer_id"] == mid), {})
                    reason = "not in matrix" if mid not in matrix else "unknown"
                    validation_issues.append({
                        "Driver": driver_name,
                        "Missing Customer ID": mid,
                        "Dog Name": dog_info.get("dog_name", "?"),
                        "Address": dog_info.get("address", "?"),
                        "Reason": reason,
                    })
            
            # Check each dog appears in both a pickup AND a dropoff
            pickup_ids = set(
                r["Customer ID"] for r in driver_results
                if r["Action"] == "PICK UP"
            )
            dropoff_ids = set(
                r["Customer ID"] for r in driver_results
                if r["Action"] == "DROP OFF"
            )
            picked_not_dropped = pickup_ids - dropoff_ids
            dropped_not_picked = dropoff_ids - pickup_ids
            
            for mid in picked_not_dropped:
                dog_info = next((a for a in expected_dogs if a["customer_id"] == mid), {})
                validation_issues.append({
                    "Driver": driver_name,
                    "Missing Customer ID": mid,
                    "Dog Name": dog_info.get("dog_name", "?"),
                    "Address": dog_info.get("address", "?"),
                    "Reason": "picked up but never dropped off",
                })
            for mid in dropped_not_picked:
                dog_info = next((a for a in expected_dogs if a["customer_id"] == mid), {})
                validation_issues.append({
                    "Driver": driver_name,
                    "Missing Customer ID": mid,
                    "Dog Name": dog_info.get("dog_name", "?"),
                    "Address": dog_info.get("address", "?"),
                    "Reason": "dropped off but never picked up",
                })

        if validation_issues:
            st.error(f"🚨 {len(validation_issues)} MISSING STOPS DETECTED:")
            st.dataframe(
                pd.DataFrame(validation_issues),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.success("✅ All dogs accounted for — every pickup has a dropoff.")

        st.subheader(f"Optimized Routes ({len(results)} total stops)")

        # Driver selector
        result_drivers = sorted(set(r["Driver"] for r in results))
        selected_driver = st.selectbox(
            "View driver:", ["All Drivers"] + result_drivers
        )

        if selected_driver == "All Drivers":
            display_results = results
        else:
            display_results = [r for r in results if r["Driver"] == selected_driver]

        df = pd.DataFrame(display_results)
        st.dataframe(df, use_container_width=True, hide_index=True, height=600)

        # ── Write to Sheet ──
        st.markdown("---")
        write_btn = st.button("📤 Write Routes to Google Sheet", type="secondary")
        if write_btn:
            with st.spinner("Writing to Google Sheet..."):
                try:
                    count = write_results_to_sheet(client, SHEET_NAME, results)
                    st.success(
                        f"✅ Wrote {count} rows to '{OUTPUT_TAB_NAME}' tab in '{SHEET_NAME}'"
                    )
                except Exception as e:
                    st.error(f"Failed to write: {e}")


if __name__ == "__main__":
    main()
