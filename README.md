# Tatari TV Campaign Performance Dashboard

A Streamlit dashboard for analyzing TV campaign performance data exported from
[Tatari](https://www.tatari.tv/) as CSV files. Uploads are stored persistently
in a Google Sheet so data survives restarts and re-deployments.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Open <http://localhost:8501>.

## Google Sheets setup (one-time, ~5 minutes)

### 1 — Create a Google Cloud service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. *APIs & Services → Credentials → Create Credentials → Service Account*
3. Open the new service account → *Keys → Add Key → JSON* → download the file

### 2 — Enable the Google Sheets API

*APIs & Services → Enable APIs & Services → search "Google Sheets API" → Enable*

### 3 — Create a Google Sheet and share it

1. Create a new blank sheet at [sheets.google.com](https://sheets.google.com)
2. *Share* → paste the service account email (e.g. `name@project.iam.gserviceaccount.com`) → **Editor**

The app auto-creates two worksheets the first time it writes:

| Worksheet | Purpose |
|---|---|
| `raw_data` | All campaign rows from every upload, tagged with week label and upload timestamp |
| `uploads_log` | One row per upload batch (timestamp, week label, filename, row count) |

### 4 — Configure credentials

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill in:

```toml
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit"

[GOOGLE_CREDENTIALS]
type = "service_account"
project_id = "your-project"
private_key_id = "..."
private_key = """-----BEGIN RSA PRIVATE KEY-----
...
-----END RSA PRIVATE KEY-----"""
client_email = "name@project.iam.gserviceaccount.com"
# ... (copy all fields from the downloaded JSON)
```

On **Streamlit Community Cloud**: paste the same content under
*App settings → Secrets* — no file needed.

## Features

| Feature | Detail |
|---|---|
| CSV upload | Sidebar uploader; rows appended to Google Sheet immediately |
| Week labelling | Pick the Mon–Sun week the export covers before uploading; stored as `_week_label` |
| Upload history | Sidebar picker lists every batch by week label + filename; select which to include |
| Delete batches | Remove an upload batch (rows + log entry) without touching other data |
| Column normalisation | Snake-cases names; synonym map collapses `campaign_name → campaign`, `imps → impressions`, etc. |
| Deduplication | Same row appearing in multiple exports is stored only once |
| KPI cards | Total spend, impressions, avg CPE, avg CPM for the most recent complete ISO week |
| WoW table | Current vs. prior ISO week with Δ and % change; green/red colour coding |
| Trend charts | Altair line charts for any numeric columns you select |
| Sidebar filters | Date range + dynamic multiselects for every categorical column |
| Resilience | Missing columns are skipped; app never crashes on absent metrics |

## Aggregation rules

- Metrics are bucketed by **ISO week** (Mon–Sun), labelled `YYYY-Www`.
- Total columns (`spend`, `impressions`, `clicks`, ...) are **summed**.
- Rate columns (`cpm`, `cpe`, `ctr`, `cvr`, `rate`, `avg`, ...) are
  **weighted-averaged by impressions** when present, otherwise a simple mean.
- "Most recent complete week" is the latest ISO week strictly before the
  current calendar week.

## File layout

```
app.py                            # Streamlit app
requirements.txt                  # Python dependencies
.streamlit/
    secrets.toml.example          # Credential template (copy → secrets.toml)
    secrets.toml                  # Your credentials (git-ignored)
data/
    .gitkeep                      # Keeps the folder in git (legacy, not used)
```
