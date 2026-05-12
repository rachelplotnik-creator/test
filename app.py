"""Tatari TV Campaign Performance Dashboard.

Upload Tatari CSV exports through the sidebar. Each upload is appended to an
Airtable base (two tables: ``raw_data`` and ``uploads_log``) so the data
persists across sessions and deployments.

Required Streamlit secrets (see .streamlit/secrets.toml.example):
    AIRTABLE_BASE_ID  - your Airtable base ID (starts with "app")
    AIRTABLE_PAT      - personal access token (starts with "pat")

Each row of the original CSV is stored as one Airtable record in ``raw_data``.
The dynamic per-row column data is JSON-encoded into the ``row_data`` long-text
field, so the Airtable schema is fixed regardless of which columns Tatari
exports include.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from datetime import datetime
from typing import Iterable

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Constants / heuristics
# ---------------------------------------------------------------------------

RATE_HINTS = (
    "cpe", "cpm", "cpa", "cpc", "cpv", "cpi", "cpl",
    "ctr", "cvr", "rate", "ratio", "percent", "pct", "%",
    "avg", "average", "mean",
)

SPEND_HINTS = ("spend", "cost", "media_cost", "total_spend")
IMPRESSION_HINTS = ("impressions", "imps", "impression")
CPE_HINTS = ("cpe", "cost_per_engagement", "cost_per_visit", "cpv")
CPM_HINTS = ("cpm", "cost_per_mille", "cost_per_thousand")

# Bookkeeping columns the app adds — excluded from dedup / filters / charts.
META_COLS = {"_week_label", "_upload_ts", "_source_file"}

COLUMN_SYNONYMS: dict[str, str] = {
    "campaign_name": "campaign",
    "campaign_title": "campaign",
    "creative_name": "creative",
    "creative_title": "creative",
    "ad_name": "creative",
    "network_name": "network",
    "channel": "network",
    "channel_name": "network",
    "air_date": "date",
    "report_date": "date",
    "day": "date",
    "spend_usd": "spend",
    "media_cost": "spend",
    "cost": "spend",
    "imps": "impressions",
    "impression": "impressions",
}

RAW_DATA_TABLE = "raw_data"
UPLOADS_LOG_TABLE = "uploads_log"


# ---------------------------------------------------------------------------
# Column / dtype helpers
# ---------------------------------------------------------------------------

def normalize_col(name: str) -> str:
    s = str(name).strip().lower()
    s = re.sub(r"[^\w%]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return COLUMN_SYNONYMS.get(s, s)


def find_col(df: pd.DataFrame, hints: Iterable[str]) -> str | None:
    for hint in hints:
        for col in df.columns:
            if hint in col:
                return col
    return None


def is_rate_col(col: str) -> bool:
    return any(h in col for h in RATE_HINTS)


def coerce_dates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    name_hints = ("date", "day", "week", "month", "timestamp", "time")
    for col in df.columns:
        if col in META_COLS:
            continue
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            continue
        looks_like_date = any(h in col for h in name_hints)
        if not looks_like_date and df[col].dtype != object:
            continue
        sample = df[col].dropna().astype(str).head(25)
        if sample.empty:
            continue
        parsed = pd.to_datetime(sample, errors="coerce", utc=False)
        if parsed.notna().mean() >= (0.6 if looks_like_date else 0.9):
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=False)
    return df


def primary_date_col(df: pd.DataFrame) -> str | None:
    datetime_cols = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if not datetime_cols:
        return None
    for hint in ("date", "day", "week_start", "week", "air_date", "report_date"):
        for c in datetime_cols:
            if hint in c:
                return c
    return datetime_cols[0]


# ---------------------------------------------------------------------------
# Week helpers
# ---------------------------------------------------------------------------

def nearest_monday(d: _dt.date) -> _dt.date:
    return d - _dt.timedelta(days=d.weekday())


def week_label_from_date(d: _dt.date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ---------------------------------------------------------------------------
# Airtable client
# ---------------------------------------------------------------------------

def _secrets_configured() -> bool:
    try:
        _ = st.secrets["AIRTABLE_BASE_ID"]
        _ = st.secrets["AIRTABLE_PAT"]
        return True
    except (KeyError, FileNotFoundError):
        return False


@st.cache_resource(show_spinner=False)
def _airtable_api():
    """Return an authenticated pyairtable Api (cached for process lifetime)."""
    from pyairtable import Api
    return Api(st.secrets["AIRTABLE_PAT"])


def _table(name: str):
    return _airtable_api().table(st.secrets["AIRTABLE_BASE_ID"], name)


def _table_with_schema(name: str):
    """Like _table, but goes through the Base object so .schema() works."""
    api = _airtable_api()
    base = api.base(st.secrets["AIRTABLE_BASE_ID"])
    return base.table(name)


# ---------------------------------------------------------------------------
# Storage operations
# ---------------------------------------------------------------------------

# Reserved field names in the raw_data table — never touched by schema sync.
RESERVED_FIELDS = {"upload_ts", "week_label", "source_file", "row_data"}


def _row_to_airtable(row: dict) -> dict:
    """Convert a pandas row dict to types Airtable accepts natively."""
    out: dict = {}
    for k, v in row.items():
        if pd.isna(v):
            continue
        if isinstance(v, pd.Timestamp):
            out[k] = v.strftime("%Y-%m-%d") if v == v.normalize() else v.isoformat()
        elif isinstance(v, _dt.datetime):
            out[k] = v.isoformat()
        elif isinstance(v, _dt.date):
            out[k] = v.isoformat()
        elif isinstance(v, np.integer):
            out[k] = int(v)
        elif isinstance(v, np.floating):
            out[k] = float(v)
        elif isinstance(v, np.bool_):
            out[k] = bool(v)
        else:
            out[k] = v
    return out


# Cache schema lookups for a single Streamlit run.
_SCHEMA_CACHE: dict[str, set[str]] = {}


def _existing_field_names(table_name: str, refresh: bool = False) -> set[str]:
    """Return the set of field names currently defined in an Airtable table."""
    if not refresh and table_name in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[table_name]
    table = _table_with_schema(table_name)
    schema = table.schema()
    names = {f.name for f in schema.fields}
    _SCHEMA_CACHE[table_name] = names
    return names


def _infer_airtable_type(series: pd.Series) -> tuple[str, dict | None]:
    """Pick an Airtable field type + options for a pandas column."""
    s = series.dropna()
    if s.empty:
        return "singleLineText", None

    if pd.api.types.is_datetime64_any_dtype(series):
        return "date", {"dateFormat": {"name": "iso"}}

    if pd.api.types.is_numeric_dtype(series):
        # Integer-only column gets precision 0; otherwise 2 decimal places.
        try:
            is_int = (s == s.astype("int64")).all()
        except (TypeError, ValueError):
            is_int = False
        return "number", {"precision": 0 if is_int else 2}

    # Try parsing string values as dates
    parsed = pd.to_datetime(s, errors="coerce")
    if len(parsed) and parsed.notna().mean() > 0.9:
        return "date", {"dateFormat": {"name": "iso"}}

    # Try parsing string values as numbers
    coerced = pd.to_numeric(s, errors="coerce")
    if len(coerced) and coerced.notna().mean() > 0.9:
        try:
            is_int = (coerced.dropna() == coerced.dropna().astype("int64")).all()
        except (TypeError, ValueError):
            is_int = False
        return "number", {"precision": 0 if is_int else 2}

    # Fall back to text — use multilineText if values are long
    if s.astype(str).str.len().max() > 200:
        return "multilineText", None
    return "singleLineText", None


def _is_permission_error(exc: Exception) -> bool:
    """True if the Airtable error indicates a missing scope / permission."""
    msg = str(exc).upper()
    return (
        "401" in msg
        or "403" in msg
        or "INVALID_PERMISSIONS" in msg
        or "NOT_AUTHORIZED" in msg
        or "AUTHENTICATION_REQUIRED" in msg
    )


def ensure_data_fields(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    """Create raw_data fields for any CSV columns that don't exist yet.

    Returns (created_field_names, skipped_field_names_due_to_error).
    Raises SchemaPermissionError if the PAT lacks the schema scopes.
    """
    existing = _existing_field_names(RAW_DATA_TABLE)
    table = _table_with_schema(RAW_DATA_TABLE)

    created: list[str] = []
    skipped: list[str] = []
    for col in df.columns:
        if col in RESERVED_FIELDS or col in existing or col in META_COLS:
            continue
        ftype, options = _infer_airtable_type(df[col])
        try:
            if options:
                table.create_field(col, ftype, options=options)
            else:
                table.create_field(col, ftype)
            existing.add(col)
            created.append(col)
        except Exception as exc:  # noqa: BLE001
            if _is_permission_error(exc):
                raise SchemaPermissionError(
                    "Your Airtable token can't modify the table schema. "
                    "Go to airtable.com/create/tokens, open your token, and "
                    "add the 'schema.bases:read' and 'schema.bases:write' "
                    "scopes (you don't need to regenerate the token — adding "
                    "scopes keeps the same value). Then restart the app."
                ) from exc
            # Type was likely rejected (e.g. mixed data) — fall back to text.
            try:
                table.create_field(col, "singleLineText")
                existing.add(col)
                created.append(col)
            except Exception as exc2:  # noqa: BLE001
                if _is_permission_error(exc2):
                    raise SchemaPermissionError(
                        "Your Airtable token can't modify the table schema. "
                        "Add 'schema.bases:read' and 'schema.bases:write' scopes "
                        "to your token at airtable.com/create/tokens."
                    ) from exc2
                skipped.append(col)

    _SCHEMA_CACHE[RAW_DATA_TABLE] = existing
    return created, skipped


@st.cache_data(show_spinner=False, ttl=60)
def _load_raw_data(cache_bust: int) -> pd.DataFrame:  # noqa: ARG001
    """Read all rows from the raw_data table into a wide DataFrame.

    Supports both new-style records (CSV columns stored as real Airtable
    fields) and legacy records (data in a ``row_data`` JSON blob).
    """
    records = _table(RAW_DATA_TABLE).all()
    if not records:
        return pd.DataFrame()

    rows: list[dict] = []
    for rec in records:
        fields = rec.get("fields", {})
        row: dict = {}

        # Legacy: row_data was a JSON blob — expand it first so real fields
        # below take precedence on key collision.
        row_blob = fields.get("row_data", "")
        if row_blob:
            try:
                blob = json.loads(row_blob)
                if isinstance(blob, dict):
                    row.update(blob)
            except (json.JSONDecodeError, TypeError):
                pass

        # Copy every real Airtable field except the reserved meta ones.
        for k, v in fields.items():
            if k in RESERVED_FIELDS:
                continue
            row[k] = v

        row["_upload_ts"]   = fields.get("upload_ts", "")
        row["_week_label"]  = fields.get("week_label", "")
        row["_source_file"] = fields.get("source_file", "")
        row["_record_id"]   = rec.get("id", "")
        rows.append(row)

    return pd.DataFrame(rows)


@st.cache_data(show_spinner=False, ttl=60)
def _load_uploads_log(cache_bust: int) -> pd.DataFrame:  # noqa: ARG001
    records = _table(UPLOADS_LOG_TABLE).all()
    if not records:
        return pd.DataFrame()

    rows = []
    for rec in records:
        f = rec.get("fields", {})
        rows.append({
            "upload_ts":         f.get("upload_ts", ""),
            "week_label":        f.get("week_label", ""),
            "original_filename": f.get("original_filename", ""),
            "row_count":         f.get("row_count", 0),
            "_record_id":        rec.get("id", ""),
        })
    return pd.DataFrame(rows)


def _invalidate_cache() -> None:
    st.session_state["_cache_bust"] = st.session_state.get("_cache_bust", 0) + 1
    _load_raw_data.clear()
    _load_uploads_log.clear()
    _SCHEMA_CACHE.clear()


class SchemaPermissionError(RuntimeError):
    """Raised when the PAT lacks schema.bases:write scope."""


def upload_to_airtable(uploaded_file, week_label: str, upload_ts: str) -> int:
    """Parse a CSV, sync table schema, dedupe vs existing, and write rows.

    Returns the number of new records actually written.
    """
    raw = pd.read_csv(uploaded_file)
    raw.columns = [normalize_col(c) for c in raw.columns]
    raw = coerce_dates(raw)

    # Sync schema: create Airtable fields for any new columns
    _, skipped = ensure_data_fields(raw)

    # Refresh and use the live schema as the source of truth: only write to
    # fields that actually exist in the table.
    available_fields = _existing_field_names(RAW_DATA_TABLE, refresh=True)
    if skipped:
        st.warning(
            "These CSV columns couldn't be added to Airtable and will be "
            "skipped for this upload: " + ", ".join(skipped)
        )

    data_cols = [
        c for c in raw.columns
        if c not in RESERVED_FIELDS and c in available_fields
    ]
    new_rows = [_row_to_airtable({k: r.get(k) for k in data_cols})
                for r in raw.to_dict(orient="records")]
    new_signatures = [
        json.dumps(r, sort_keys=True, default=str) for r in new_rows
    ]

    existing = _load_raw_data(st.session_state.get("_cache_bust", 0))
    existing_sigs: set[str] = set()
    if not existing.empty:
        existing_data_cols = [
            c for c in existing.columns
            if c not in META_COLS and c != "_record_id" and c in data_cols
        ]
        if existing_data_cols:
            for r in existing[existing_data_cols].to_dict(orient="records"):
                existing_sigs.add(json.dumps(_row_to_airtable(r), sort_keys=True, default=str))

    new_records: list[dict] = []
    for row, sig in zip(new_rows, new_signatures):
        if sig in existing_sigs:
            continue
        existing_sigs.add(sig)
        rec = {
            "upload_ts":   upload_ts,
            "week_label":  week_label,
            "source_file": uploaded_file.name,
        }
        rec.update(row)
        new_records.append(rec)

    if new_records:
        _table(RAW_DATA_TABLE).batch_create(new_records, typecast=True)

    _table(UPLOADS_LOG_TABLE).create({
        "upload_ts":         upload_ts,
        "week_label":        week_label,
        "original_filename": uploaded_file.name,
        "row_count":         len(new_records),
    }, typecast=True)

    return len(new_records)


def delete_upload_batch(upload_ts: str) -> None:
    """Delete all raw_data records and the log entry for a given upload batch."""
    raw_table = _table(RAW_DATA_TABLE)
    log_table = _table(UPLOADS_LOG_TABLE)

    formula = f"{{upload_ts}}='{upload_ts}'"
    raw_records = raw_table.all(formula=formula, fields=[])
    if raw_records:
        raw_table.batch_delete([r["id"] for r in raw_records])

    log_records = log_table.all(formula=formula, fields=[])
    if log_records:
        log_table.batch_delete([r["id"] for r in log_records])


def load_selected_batches(selected_ts: list[str]) -> pd.DataFrame:
    bust = st.session_state.get("_cache_bust", 0)
    raw = _load_raw_data(bust)
    if raw.empty:
        return pd.DataFrame()
    if selected_ts and "_upload_ts" in raw.columns:
        raw = raw[raw["_upload_ts"].isin(selected_ts)]
    if raw.empty:
        return pd.DataFrame()

    raw = raw.drop(columns=[c for c in ["_record_id"] if c in raw.columns])
    df = coerce_dates(raw)

    # Dedupe again just in case (cheap insurance). Guard against the case
    # where every column is a meta column (records with empty row_data),
    # which would make ``drop_duplicates(subset=[])`` blow up.
    dedupe_cols = [c for c in df.columns if c not in META_COLS]
    if dedupe_cols:
        df = df.drop_duplicates(subset=dedupe_cols, keep="first")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_weekly(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    if df.empty or date_col is None or date_col not in df.columns:
        return pd.DataFrame()

    work = df.dropna(subset=[date_col]).copy()
    if work.empty:
        return pd.DataFrame()

    iso = work[date_col].dt.isocalendar()
    work["_iso_year"] = iso["year"].astype(int)
    work["_iso_week"] = iso["week"].astype(int)
    work["_week_start"] = work[date_col] - pd.to_timedelta(work[date_col].dt.weekday, unit="D")
    work["_week_start"] = work["_week_start"].dt.normalize()

    # Coerce columns that look numeric but came back as strings (Airtable
    # round-trips everything as JSON, so a column might be all stringly).
    for col in work.columns:
        if col in META_COLS or col in {"_iso_year", "_iso_week", "_week_start"}:
            continue
        if pd.api.types.is_numeric_dtype(work[col]):
            continue
        if pd.api.types.is_datetime64_any_dtype(work[col]):
            continue
        coerced = pd.to_numeric(work[col], errors="coerce")
        if coerced.notna().sum() >= max(1, int(0.6 * work[col].notna().sum())):
            work[col] = coerced

    numeric_cols = [
        c for c in work.columns
        if c not in {"_iso_year", "_iso_week", "_week_start"} | META_COLS
        and pd.api.types.is_numeric_dtype(work[c])
    ]
    if not numeric_cols:
        return pd.DataFrame()

    impressions_col = find_col(work[numeric_cols], IMPRESSION_HINTS)

    rows: list[dict] = []
    for (yr, wk, start), grp in work.groupby(["_iso_year", "_iso_week", "_week_start"], sort=True):
        row: dict = {
            "iso_year": int(yr),
            "iso_week": int(wk),
            "week_start": start,
            "week_label": f"{int(yr)}-W{int(wk):02d}",
        }
        for col in numeric_cols:
            series = grp[col]
            if is_rate_col(col):
                if impressions_col and impressions_col != col:
                    weights = grp[impressions_col]
                    mask = series.notna() & weights.notna() & (weights > 0)
                    if mask.any():
                        row[col] = float(np.average(series[mask], weights=weights[mask]))
                        continue
                row[col] = float(series.mean()) if series.notna().any() else np.nan
            else:
                row[col] = float(series.sum(skipna=True))
        rows.append(row)

    return pd.DataFrame(rows).sort_values("week_start").reset_index(drop=True)


def wow_table(weekly: pd.DataFrame) -> tuple[pd.DataFrame, pd.Timestamp | None, pd.Timestamp | None]:
    if weekly is None or weekly.empty:
        return pd.DataFrame(), None, None

    weekly = weekly.sort_values("week_start").reset_index(drop=True)
    current = weekly.iloc[-1]
    prior = weekly.iloc[-2] if len(weekly) >= 2 else None

    metric_cols = [c for c in weekly.columns if c not in {"iso_year", "iso_week", "week_start", "week_label"}]
    rows = []
    for col in metric_cols:
        cur_val = current.get(col, np.nan)
        prior_val = prior.get(col, np.nan) if prior is not None else np.nan
        if pd.isna(cur_val) and pd.isna(prior_val):
            continue
        pct = np.nan
        if pd.notna(prior_val) and prior_val != 0:
            pct = (cur_val - prior_val) / abs(prior_val) * 100.0
        rows.append({
            "Metric": col,
            "Current Week": cur_val,
            "Prior Week": prior_val,
            "Δ": (cur_val - prior_val) if pd.notna(prior_val) and pd.notna(cur_val) else np.nan,
            "% Change": pct,
        })
    return (
        pd.DataFrame(rows),
        current.get("week_start"),
        prior.get("week_start") if prior is not None else None,
    )


def style_wow(df: pd.DataFrame):
    if df.empty:
        return df

    def color_change(val):
        if pd.isna(val):
            return ""
        return ("color: #0a7c2a; font-weight: 600;" if val > 0
                else "color: #c0392b; font-weight: 600;" if val < 0 else "")

    styler = df.style
    if hasattr(styler, "map"):
        styler = styler.map(color_change, subset=["Δ", "% Change"])
    else:
        styler = styler.applymap(color_change, subset=["Δ", "% Change"])
    styler = styler.format({
        "Current Week": "{:,.2f}",
        "Prior Week": "{:,.2f}",
        "Δ": "{:+,.2f}",
        "% Change": lambda v: "—" if pd.isna(v) else f"{v:+.1f}%",
    })
    return styler


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def render_kpis(weekly: pd.DataFrame) -> None:
    if weekly.empty:
        return

    today = pd.Timestamp.today().normalize()
    this_week_start = today - pd.Timedelta(days=today.weekday())
    complete = weekly[weekly["week_start"] < this_week_start]
    target = complete.iloc[-1] if not complete.empty else weekly.iloc[-1]

    spend_col = find_col(weekly, SPEND_HINTS)
    imp_col   = find_col(weekly, IMPRESSION_HINTS)
    cpe_col   = find_col(weekly, CPE_HINTS)
    cpm_col   = find_col(weekly, CPM_HINTS)

    cards = []
    if spend_col:
        cards.append(("Total Spend",       f"${target[spend_col]:,.2f}"))
    if imp_col:
        cards.append(("Total Impressions", f"{target[imp_col]:,.0f}"))
    if cpe_col:
        cards.append(("Avg CPE",           f"${target[cpe_col]:,.2f}"))
    if cpm_col:
        cards.append(("Avg CPM",           f"${target[cpm_col]:,.2f}"))

    if not cards:
        st.info("No recognizable KPI columns (spend / impressions / CPE / CPM) in the data.")
        return

    st.caption(f"Most recent complete week: **{target['week_label']}** (starting {target['week_start'].date()})")
    for col, (label, value) in zip(st.columns(len(cards)), cards):
        col.metric(label, value)


def sidebar_filters(df: pd.DataFrame, date_col: str | None) -> pd.DataFrame:
    if df.empty:
        return df

    st.sidebar.header("Filters")
    filtered = df.copy()

    if date_col and date_col in filtered.columns:
        valid = filtered[date_col].dropna()
        if not valid.empty:
            min_d, max_d = valid.min().date(), valid.max().date()
            picked = st.sidebar.date_input(
                "Date range", value=(min_d, max_d),
                min_value=min_d, max_value=max_d, key="date_range",
            )
            if isinstance(picked, tuple) and len(picked) == 2:
                start, end = picked
                filtered = filtered[
                    (filtered[date_col].dt.date >= start) &
                    (filtered[date_col].dt.date <= end)
                ]

    cat_cols = [
        c for c in filtered.columns
        if c not in META_COLS
        and not pd.api.types.is_numeric_dtype(filtered[c])
        and not pd.api.types.is_datetime64_any_dtype(filtered[c])
        and 1 < filtered[c].nunique(dropna=True) <= 200
    ]
    for col in cat_cols:
        options = sorted(filtered[col].dropna().astype(str).unique().tolist())
        chosen = st.sidebar.multiselect(
            col.replace("_", " ").title(), options, key=f"flt_{col}"
        )
        if chosen:
            filtered = filtered[filtered[col].astype(str).isin(chosen)]

    return filtered


def render_trend_charts(weekly: pd.DataFrame) -> None:
    if weekly.empty:
        return

    metric_cols = [
        c for c in weekly.columns
        if c not in {"iso_year", "iso_week", "week_start", "week_label"}
        and pd.api.types.is_numeric_dtype(weekly[c])
    ]
    if not metric_cols:
        st.info("No numeric metrics available to chart.")
        return

    defaults = [
        c for c in metric_cols
        if any(h in c for h in SPEND_HINTS + IMPRESSION_HINTS)
    ][:3] or metric_cols[:min(3, len(metric_cols))]

    chosen = st.multiselect("Columns to plot", options=metric_cols, default=defaults)
    if not chosen:
        st.info("Pick one or more columns to display the trend.")
        return

    long = weekly.melt(
        id_vars=["week_start", "week_label"],
        value_vars=chosen,
        var_name="metric",
        value_name="value",
    ).dropna(subset=["value"])

    if long.empty:
        st.info("No non-null values to chart for the selected columns.")
        return

    chart = (
        alt.Chart(long)
        .mark_line(point=True)
        .encode(
            x=alt.X("week_start:T", title="Week"),
            y=alt.Y("value:Q", title="Value"),
            color=alt.Color("metric:N", title="Metric"),
            tooltip=[
                alt.Tooltip("week_label:N", title="Week"),
                alt.Tooltip("metric:N"),
                alt.Tooltip("value:Q", format=",.2f"),
            ],
        )
        .properties(height=380)
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)


def render_uploader_and_picker() -> list[str]:
    st.sidebar.header("Data")

    today = _dt.date.today()
    default_monday = today - _dt.timedelta(days=today.weekday())
    week_input = st.sidebar.date_input(
        "Week (Mon – Sun) this file covers",
        value=default_monday,
        help="Pick any day in the week; the app snaps it to Monday.",
        key="upload_week",
    )
    if isinstance(week_input, tuple):
        week_input = week_input[0] if week_input else default_monday
    monday = nearest_monday(week_input)
    wlabel = week_label_from_date(monday)
    st.sidebar.caption(
        f"Will tag upload as **{wlabel}** "
        f"({monday} – {monday + _dt.timedelta(days=6)})"
    )

    uploaded_files = st.sidebar.file_uploader(
        "Upload Tatari CSV export(s)",
        type=["csv"],
        accept_multiple_files=True,
        help="Rows are appended to your Airtable base with a week label.",
    )
    if uploaded_files:
        upload_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        for f in uploaded_files:
            with st.sidebar.status(f"Uploading {f.name}…", expanded=False):
                try:
                    added = upload_to_airtable(f, wlabel, upload_ts)
                    st.sidebar.success(f"{wlabel} · {f.name} (+{added:,} rows)")
                except SchemaPermissionError as exc:
                    st.sidebar.error(str(exc))
                except Exception as exc:  # noqa: BLE001
                    st.sidebar.error(f"Failed: {exc}")
        _invalidate_cache()
        st.rerun()

    bust = st.session_state.get("_cache_bust", 0)
    log_df = _load_uploads_log(bust)

    if log_df.empty or "upload_ts" not in log_df.columns:
        st.sidebar.info("No uploads yet.")
        return []

    log_df = log_df.sort_values("upload_ts", ascending=False).reset_index(drop=True)

    def batch_label(row) -> str:
        wk = row.get("week_label", "?")
        fn = row.get("original_filename", "unknown")
        rc = row.get("row_count", "?")
        try:
            rc = f"{int(float(rc)):,}"
        except (TypeError, ValueError):
            pass
        return f"{wk}  ·  {fn}  ({rc} rows)"

    all_ts = log_df["upload_ts"].tolist()
    labels = {row["upload_ts"]: batch_label(row) for _, row in log_df.iterrows()}

    selected_ts = st.sidebar.multiselect(
        "Upload batches to include",
        options=all_ts,
        default=all_ts,
        format_func=lambda ts: labels.get(ts, ts),
    )

    with st.sidebar.expander("Manage uploads"):
        to_delete = st.multiselect(
            "Delete batches",
            options=all_ts,
            format_func=lambda ts: labels.get(ts, ts),
            key="delete_picker",
        )
        if to_delete and st.button("Delete selected", type="secondary"):
            for ts in to_delete:
                try:
                    delete_upload_batch(ts)
                except Exception as exc:  # noqa: BLE001
                    st.warning(f"Could not delete {ts}: {exc}")
            _invalidate_cache()
            st.rerun()

    return selected_ts


def render_setup_guide() -> None:
    st.error("Airtable credentials not configured.", icon="🔑")
    st.markdown("""
### One-time setup (~5 minutes)

**1. Create an Airtable base**
- Sign up at [airtable.com](https://airtable.com) (free)
- Create a base called `Tatari`

**2. Create the `raw_data` table** with these fields (CSV column fields
   will be added automatically on first upload):
- `upload_ts` (Single line text)
- `week_label` (Single line text)
- `source_file` (Single line text)

**3. Create the `uploads_log` table** with these fields:
- `upload_ts` (Single line text)
- `week_label` (Single line text)
- `original_filename` (Single line text)
- `row_count` (Number)

**4. Create a Personal Access Token**
- Go to [airtable.com/create/tokens](https://airtable.com/create/tokens)
- **Scopes**:
  - `data.records:read`
  - `data.records:write`
  - `schema.bases:read`
  - `schema.bases:write`
- **Access**: add your `Tatari` base

**5. Get your Base ID**
- From [airtable.com/developers/web/api/introduction](https://airtable.com/developers/web/api/introduction)
- Click your base — the URL will contain `appXXXXXXXX`

**6. Configure secrets** in `.streamlit/secrets.toml`:

```toml
AIRTABLE_BASE_ID = "appXXXXXXXXXXXXXX"
AIRTABLE_PAT = "patXXXXXXXXXXXXX.YYYYYYYYYYYYYYYYY"
```

On Streamlit Community Cloud: paste the same values under
*App settings → Secrets*.

Then restart the app.
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="Tatari TV Performance",
        page_icon="📺",
        layout="wide",
    )
    st.title("📺 Tatari TV Campaign Performance")
    st.caption("Upload Tatari CSV exports · data stored persistently in Airtable")

    if not _secrets_configured():
        render_setup_guide()
        return

    if "_cache_bust" not in st.session_state:
        st.session_state["_cache_bust"] = 0

    selected_ts = render_uploader_and_picker()
    df = load_selected_batches(selected_ts)

    if df.empty:
        st.info("Upload a Tatari CSV from the sidebar to get started.")
        return

    date_col = primary_date_col(df)
    if date_col is None:
        st.warning("Could not detect a date column. Most analyses require a parseable date.")

    filtered = sidebar_filters(df, date_col)

    st.subheader("Snapshot")
    c = st.columns(4)
    c[0].metric("Rows", f"{len(filtered):,}")
    c[1].metric("Columns", f"{filtered.shape[1] - len(META_COLS):,}")
    c[2].metric("Upload batches", f"{filtered['_upload_ts'].nunique() if '_upload_ts' in filtered else 0:,}")
    if date_col and date_col in filtered.columns and filtered[date_col].notna().any():
        span = f"{filtered[date_col].min().date()} → {filtered[date_col].max().date()}"
    else:
        span = "—"
    c[3].metric("Date range", span)

    weekly = aggregate_weekly(filtered, date_col) if date_col else pd.DataFrame()

    st.subheader("Most Recent Complete Week")
    render_kpis(weekly)

    st.subheader("Week-over-Week Comparison")
    if weekly.empty:
        st.info("Not enough data to compute weekly metrics.")
    else:
        wow_df, cur_start, prior_start = wow_table(weekly)
        if cur_start is not None:
            prior_label = f"{prior_start.date()}" if prior_start is not None else "—"
            st.caption(
                f"Current week starting **{cur_start.date()}** "
                f"vs prior week starting **{prior_label}**"
            )
        if wow_df.empty:
            st.info("No comparable metrics between current and prior week.")
        else:
            st.dataframe(style_wow(wow_df), use_container_width=True, hide_index=True)

    st.subheader("Weekly Trends")
    render_trend_charts(weekly)

    with st.expander("Raw data"):
        display_df = filtered.drop(columns=[c for c in META_COLS if c in filtered.columns])
        st.dataframe(display_df, use_container_width=True, height=400)
        st.download_button(
            "Download filtered data as CSV",
            data=display_df.to_csv(index=False).encode("utf-8"),
            file_name=f"tatari_filtered_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
