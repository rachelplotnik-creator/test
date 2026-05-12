# Tatari TV Campaign Performance Dashboard

A Streamlit dashboard for analyzing TV campaign performance data exported from
[Tatari](https://www.tatari.tv/) as CSV files. Uploads are stored persistently
in [Airtable](https://airtable.com/) so data survives restarts and re-deployments.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Open <http://localhost:8501>.

## Airtable setup (one-time, ~5 minutes)

### 1. Create an Airtable base

1. Sign up at [airtable.com](https://airtable.com) (free)
2. Click **+ Create a base** → **Start from scratch** → name it `Tatari`

### 2. Create the `raw_data` table

Rename the default `Table 1` to `raw_data` and set up these fields (delete any defaults):

| Field name | Type |
|---|---|
| `upload_ts` | Single line text |
| `week_label` | Single line text |
| `source_file` | Single line text |
| `row_data` | Long text |

### 3. Create the `uploads_log` table

Click **+ Add or import** → **Create empty table** → name it `uploads_log`.

| Field name | Type |
|---|---|
| `upload_ts` | Single line text |
| `week_label` | Single line text |
| `original_filename` | Single line text |
| `row_count` | Number (Integer) |

### 4. Get a Personal Access Token

1. Go to [airtable.com/create/tokens](https://airtable.com/create/tokens)
2. Click **Create new token**
3. Name: `tatari-dashboard`
4. **Scopes**: `data.records:read`, `data.records:write`
5. **Access**: add your `Tatari` base
6. Create → copy the token (starts with `pat`)

### 5. Get your Base ID

1. Go to [airtable.com/developers/web/api/introduction](https://airtable.com/developers/web/api/introduction)
2. Click your `Tatari` base — the URL will contain `appXXXXXXXX`

### 6. Configure credentials

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill in:

```toml
AIRTABLE_BASE_ID = "appXXXXXXXXXXXXXX"
AIRTABLE_PAT     = "patXXXXXXXXXXXXX.YYYYYYYYYYYYYYYYY"
```

On **Streamlit Community Cloud**: paste the same content under
*App settings → Secrets* — no file needed.

## Free tier note

Airtable's free plan allows **1,000 records per base**. Each row of your CSV
becomes one record, so depending on data granularity you'll eventually want
to either upgrade to Plus ($10/mo for 5k records), prune older batches via
**Manage uploads → Delete batches**, or switch to a different backend.

## Features

| Feature | Detail |
|---|---|
| CSV upload | Sidebar uploader; rows pushed to Airtable in batches |
| Week labelling | Pick the Mon–Sun week the export covers; stored as `week_label` |
| Upload history | Sidebar picker lists each batch by week label + filename + row count |
| Delete batches | Removes all records for an upload batch (and the log entry) |
| Column normalisation | Snake-cases names; synonym map collapses `campaign_name → campaign`, `imps → impressions`, etc. |
| Deduplication | Same row appearing in multiple exports is stored only once |
| KPI cards | Total spend, impressions, avg CPE, avg CPM for the most recent complete ISO week |
| WoW table | Current vs. prior ISO week with Δ and % change; green/red colour coding |
| Trend charts | Altair line charts for any numeric columns you select |
| Sidebar filters | Date range + dynamic multiselects for every categorical column |
| Resilience | Missing columns are skipped; app never crashes on absent metrics |

## How storage works

The Airtable schema is fixed regardless of which Tatari columns appear in
your exports. Each row is JSON-encoded into the `row_data` long-text field,
and the app expands it back into a wide DataFrame on load. This means you
can upload exports with different column sets and the app handles them
seamlessly.

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
```
