# Tatari TV Campaign Performance Dashboard

A Streamlit dashboard for analyzing TV campaign performance data exported
from [Tatari](https://www.tatari.tv/) as CSV files. Upload one or more CSV
exports through the UI; the app stores them locally in `data/`, merges them
into a single deduplicated dataset, and surfaces week-over-week comparisons,
KPIs, and trend charts.

## Features

- **CSV upload & history** – uploaded files are saved to `data/` with a
  timestamp prefix so you build up a history. Pick which previously-uploaded
  files to include from the sidebar, or delete old ones.
- **Auto-parsing** – column names are normalized and date columns are
  auto-detected so slightly different exports still line up. Duplicate rows
  across exports are dropped.
- **KPI cards** – total spend, total impressions, average CPE, and average
  CPM for the most recent complete ISO week (when those columns are present).
- **Week-over-week table** – current vs. prior ISO week with raw values, Δ,
  and percent change. Positive deltas are green, negative are red.
- **Trend charts** – Altair line charts for any numeric columns you select.
- **Filters** – sidebar date-range filter plus dynamic multiselect filters
  for every categorical column found in the data (network, campaign,
  creative, etc.).
- **Resilient** – missing columns are skipped rather than crashing the app.

## Running locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open the URL Streamlit prints (typically <http://localhost:8501>).

## File layout

```
app.py             # Streamlit app
data/              # Uploaded CSVs (auto-created, timestamp-prefixed)
requirements.txt   # Python dependencies
```

## How aggregation works

- All metrics are bucketed by **ISO week** (Mon–Sun). The week label is shown
  as `YYYY-Www`.
- "Total"-style numeric columns (e.g. `spend`, `impressions`, `clicks`,
  `visits`) are **summed** within each week.
- "Rate"-style columns (anything containing `cpm`, `cpe`, `cpa`, `cpc`,
  `ctr`, `cvr`, `rate`, `ratio`, `pct`, `%`, `avg`, `mean`, etc.) are
  **weighted-averaged by impressions** when an impressions column exists,
  and otherwise fall back to a simple mean.
- The "most recent complete week" is the latest ISO week strictly before
  the current calendar week.

## Notes

- The app uses Streamlit's native components (`st.dataframe`, `st.metric`,
  `st.altair_chart`) and pandas only — no database is required.
- The `data/` folder is created on first run; uploaded files persist between
  sessions.
