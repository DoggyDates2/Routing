# Doggy Dates Route Optimizer

Optimizes daily driving routes for Doggy Dates playgroup operations using Google OR-Tools.

## What it does

- Reads today's group assignments from a Google Sheet (Staff + Today tabs)
- Uses a pre-built driving distance matrix (ORS-based, in minutes)
- Solves optimal stop order for each driver using OR-Tools
- Handles interleaved pickup/dropoff with vehicle capacity constraints
- Writes optimized routes back to the Google Sheet

## Setup

### 1. Create a GitHub repo

Upload these files to a new GitHub repository:
- `app.py`
- `solver.py`
- `requirements.txt`
- `Updated_Matrix_07_26_-_Minutes.csv` (your distance matrix)

### 2. Google Cloud service account

You need a Google Cloud service account with access to your Routing Google Sheet.

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project (or use an existing one)
3. Enable the **Google Sheets API** and **Google Drive API**
4. Create a **Service Account** under IAM & Admin
5. Create a JSON key for the service account
6. Share your "Routing" Google Sheet with the service account email

### 3. Deploy on Streamlit Community Cloud

1. Go to [share.streamlit.io](https://share.streamlit.io)
2. Click "New app" and connect your GitHub repo
3. Set the main file to `app.py`
4. Under **Advanced settings > Secrets**, paste:

```toml
sheet_name = "Routing"

[gcp_service_account]
type = "service_account"
project_id = "your-project-id"
private_key_id = "your-key-id"
private_key = "-----BEGIN PRIVATE KEY-----\nyour-private-key\n-----END PRIVATE KEY-----\n"
client_email = "your-service-account@your-project.iam.gserviceaccount.com"
client_id = "your-client-id"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "your-cert-url"
```

5. Click Deploy

### 4. Daily use

1. Staff fills in Today and Staff tabs in Google Sheets as usual
2. Open the Streamlit app
3. Click "Optimize All Routes"
4. Review results
5. Click "Write Routes to Google Sheet"
6. Drivers open the "Optimized Routes" tab to see their stop order

## Files

- `app.py` — Streamlit UI, Google Sheets integration, orchestration
- `solver.py` — OR-Tools optimization engine (simple trips + interleaved trips)
- `requirements.txt` — Python dependencies
- `Updated_Matrix_07_26_-_Minutes.csv` — Pre-computed driving distance matrix (minutes)
