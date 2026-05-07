"""Tatari TV Campaign Performance Dashboard.

A Streamlit dashboard for analyzing TV campaign performance data exported
from Tatari as CSV files. Upload CSVs through the UI; they're stored in the
local ``data/`` folder and merged into a single deduplicated dataframe for
analysis.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Heuristics: substrings that mark a column as a "rate" rather than a sum-able
# total. Rates are averaged (or weighted-averaged) instead of summed when we
# aggregate to weekly buckets.
RATE_HINTS = (
    "cpe", "cpm", "cpa", "cpc", "cpv", "cpi", "cpl",
    "ctr", "cvr", "rate", "ratio", "percent", "pct", "%",
    "avg", "average", "mean",
)

# Columns we'd like to surface as KPIs, in priority order.
SPEND_HINTS = ("spend", "cost", "media_cost", "total_spend")
IMPRESSION_HINTS = ("impressions", "imps", "impression")
CPE_HINTS = ("cpe", "cost_per_engagement", "cost_per_visit", "cpv")
CPM_HINTS = ("cpm", "cost_per_mille", "cost_per_thousand")


# ---------------------------------------------------------------------------
# Column / dtype helpers
# ---------------------------------------------------------------------------

# Common synonyms across slightly-different Tatari exports. After base
# normalization we also try to map these to a canonical form so dedupe and
# joins work even when one export uses ``campaign_name`` and another uses
# ``campaign``.
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


def normalize_col(name: str) -> str:
    """Normalize a column name so slightly-different exports line up.

    Lowercases, strips, collapses whitespace, removes surrounding punctuation,
    converts non-alphanumerics to underscores, and applies a small synonym
    map for the most common Tatari export field name variations.
    """
    s = str(name).strip().lower()
    s = re.sub(r"[^\w%]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return COLUMN_SYNONYMS.get(s, s)


def find_col(df: pd.DataFrame, hints: Iterable[str]) -> str | None:
    """Return the first column whose normalized name contains any hint."""
    for hint in hints:
        for col in df.columns:
            if hint in col:
                return col
    return None


def is_rate_col(col: str) -> bool:
    return any(h in col for h in RATE_HINTS)


def coerce_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Auto-detect and parse date columns in-place.

    A column is treated as a date column if its name contains "date"/"day"/
    "week"/"month" OR if a sample of its values parses successfully as dates.
    """
    df = df.copy()
    name_hints = ("date", "day", "week", "month", "timestamp", "time")
    for col in df.columns:
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
    """Pick the most likely "date of record" column."""
    datetime_cols = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if not datetime_cols:
        return None
    for hint in ("date", "day", "week_start", "week", "air_date", "report_date"):
        for c in datetime_cols:
            if hint in c:
                return c
    return datetime_cols[0]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def list_stored_files() -> list[Path]:
    return sorted(
        (p for p in DATA_DIR.glob("*.csv") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def save_uploaded_file(uploaded) -> Path:
    """Persist an uploaded CSV with a timestamp prefix and return its path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^\w.\-]+", "_", uploaded.name)
    out = DATA_DIR / f"{ts}__{safe_name}"
    out.write_bytes(uploaded.getbuffer())
    return out


@st.cache_data(show_spinner=False)
def read_csv(path_str: str, mtime: float) -> pd.DataFrame:
    """Read one CSV file, normalizing columns and parsing dates.

    ``mtime`` is part of the cache key so edits to a file invalidate the cache.
    """
    del mtime  # only used for cache busting
    raw = pd.read_csv(path_str)
    raw.columns = [normalize_col(c) for c in raw.columns]
    raw["_source_file"] = Path(path_str).name
    return coerce_dates(raw)


def load_all(paths: list[Path]) -> pd.DataFrame:
    """Load and combine multiple CSV files into one deduplicated frame."""
    if not paths:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for p in paths:
        try:
            frames.append(read_csv(str(p), p.stat().st_mtime))
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Could not read {p.name}: {exc}")
    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True, sort=False)

    # Re-coerce dates after concat (some files may have had a column as
    # strings while others had it as datetimes).
    combined = coerce_dates(combined)

    # Dedupe ignoring the bookkeeping ``_source_file`` column so that the same
    # row appearing in two exports collapses to one record.
    dedupe_cols = [c for c in combined.columns if c != "_source_file"]
    combined = combined.drop_duplicates(subset=dedupe_cols, keep="first")
    return combined.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_weekly(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    """Aggregate numeric columns by ISO week.

    Sum-style columns (spend, impressions, etc.) are summed; rate columns are
    weighted-averaged by impressions when an impressions column is available,
    otherwise they fall back to a simple mean.
    """
    if df.empty or date_col is None or date_col not in df.columns:
        return pd.DataFrame()

    work = df.dropna(subset=[date_col]).copy()
    if work.empty:
        return pd.DataFrame()

    iso = work[date_col].dt.isocalendar()
    work["_iso_year"] = iso["year"].astype(int)
    work["_iso_week"] = iso["week"].astype(int)
    # Monday of the ISO week (handy for time-axis charts).
    work["_week_start"] = work[date_col] - pd.to_timedelta(work[date_col].dt.weekday, unit="D")
    work["_week_start"] = work["_week_start"].dt.normalize()

    numeric_cols = [
        c for c in work.columns
        if c not in {"_iso_year", "_iso_week", "_week_start"}
        and pd.api.types.is_numeric_dtype(work[c])
    ]
    if not numeric_cols:
        return pd.DataFrame()

    impressions_col = find_col(work[numeric_cols], IMPRESSION_HINTS)

    rows: list[dict] = []
    for (yr, wk, start), grp in work.groupby(["_iso_year", "_iso_week", "_week_start"], sort=True):
        row: dict[str, float | int | pd.Timestamp | str] = {
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

    weekly = pd.DataFrame(rows).sort_values("week_start").reset_index(drop=True)
    return weekly


def wow_table(weekly: pd.DataFrame) -> tuple[pd.DataFrame, pd.Timestamp | None, pd.Timestamp | None]:
    """Build a current-vs-prior-week comparison table."""
    if weekly is None or weekly.empty or len(weekly) < 1:
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
        if prior_val in (0, None) or pd.isna(prior_val):
            pct = np.nan
        else:
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
        current["week_start"] if "week_start" in current else None,
        prior["week_start"] if prior is not None and "week_start" in prior else None,
    )


def style_wow(df: pd.DataFrame):
    """Conditional formatting: green for positive Δ/% change, red for negative."""
    if df.empty:
        return df

    def color_change(val):
        if pd.isna(val):
            return ""
        if val > 0:
            return "color: #0a7c2a; font-weight: 600;"
        if val < 0:
            return "color: #c0392b; font-weight: 600;"
        return ""

    # ``Styler.map`` replaces the deprecated ``applymap`` in pandas >= 2.1.
    styler = df.style
    if hasattr(styler, "map"):
        styler = styler.map(color_change, subset=["Δ", "% Change"])
    else:  # pragma: no cover - older pandas fallback
        styler = styler.applymap(color_change, subset=["Δ", "% Change"])
    styler = styler.format({
        "Current Week": "{:,.2f}",
        "Prior Week": "{:,.2f}",
        "Δ": "{:+,.2f}",
        "% Change": lambda v: "—" if pd.isna(v) else f"{v:+.1f}%",
    })
    return styler


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def render_kpis(weekly: pd.DataFrame) -> None:
    """KPI cards for the most recent complete week."""
    if weekly.empty:
        return

    # "Most recent complete week" = latest ISO week we have, excluding the
    # current calendar week if it's still in progress.
    today = pd.Timestamp.today().normalize()
    this_week_start = today - pd.Timedelta(days=today.weekday())
    complete = weekly[weekly["week_start"] < this_week_start]
    target = complete.iloc[-1] if not complete.empty else weekly.iloc[-1]

    spend_col = find_col(weekly, SPEND_HINTS)
    imp_col = find_col(weekly, IMPRESSION_HINTS)
    cpe_col = find_col(weekly, CPE_HINTS)
    cpm_col = find_col(weekly, CPM_HINTS)

    cards = []
    if spend_col:
        cards.append(("Total Spend", f"${target[spend_col]:,.2f}"))
    if imp_col:
        cards.append(("Total Impressions", f"{target[imp_col]:,.0f}"))
    if cpe_col:
        cards.append(("Avg CPE", f"${target[cpe_col]:,.2f}"))
    if cpm_col:
        cards.append(("Avg CPM", f"${target[cpm_col]:,.2f}"))

    if not cards:
        st.info("No recognizable KPI columns (spend / impressions / CPE / CPM) in the loaded data.")
        return

    st.caption(f"Most recent complete week: **{target['week_label']}** (starting {target['week_start'].date()})")
    cols = st.columns(len(cards))
    for col, (label, value) in zip(cols, cards):
        col.metric(label, value)


def sidebar_filters(df: pd.DataFrame, date_col: str | None) -> pd.DataFrame:
    """Render sidebar filters and return the filtered dataframe."""
    if df.empty:
        return df

    st.sidebar.header("Filters")

    filtered = df.copy()

    if date_col and date_col in filtered.columns:
        valid_dates = filtered[date_col].dropna()
        if not valid_dates.empty:
            min_d, max_d = valid_dates.min().date(), valid_dates.max().date()
            picked = st.sidebar.date_input(
                "Date range",
                value=(min_d, max_d),
                min_value=min_d,
                max_value=max_d,
                key="date_range",
            )
            if isinstance(picked, tuple) and len(picked) == 2:
                start, end = picked
                filtered = filtered[
                    (filtered[date_col].dt.date >= start)
                    & (filtered[date_col].dt.date <= end)
                ]

    cat_cols = [
        c for c in filtered.columns
        if c not in {"_source_file"}
        and not pd.api.types.is_numeric_dtype(filtered[c])
        and not pd.api.types.is_datetime64_any_dtype(filtered[c])
    ]
    # Only show filters for columns with a sensible number of unique values.
    cat_cols = [c for c in cat_cols if 1 < filtered[c].nunique(dropna=True) <= 200]

    for col in cat_cols:
        options = sorted(filtered[col].dropna().astype(str).unique().tolist())
        chosen = st.sidebar.multiselect(col.replace("_", " ").title(), options, key=f"flt_{col}")
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
    ][:3] or metric_cols[: min(3, len(metric_cols))]

    chosen = st.multiselect(
        "Columns to plot",
        options=metric_cols,
        default=defaults,
    )
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


def render_uploader_and_picker() -> list[Path]:
    """Render upload UI + previously-uploaded file picker; return selected paths."""
    st.sidebar.header("Data")

    uploaded = st.sidebar.file_uploader(
        "Upload Tatari CSV export(s)",
        type=["csv"],
        accept_multiple_files=True,
        help="Files are saved to the local data/ folder with a timestamp prefix.",
    )
    if uploaded:
        for f in uploaded:
            try:
                path = save_uploaded_file(f)
                st.sidebar.success(f"Saved {path.name}")
            except Exception as exc:  # noqa: BLE001
                st.sidebar.error(f"Failed to save {f.name}: {exc}")
        # Clear cached reads since new files exist.
        read_csv.clear()

    stored = list_stored_files()
    if not stored:
        st.sidebar.info("No CSVs uploaded yet.")
        return []

    labels = {p: f"{p.name}  ({p.stat().st_size/1024:,.1f} KB)" for p in stored}
    selected = st.sidebar.multiselect(
        "Files to include",
        options=stored,
        default=stored,
        format_func=lambda p: labels[p],
    )

    with st.sidebar.expander("Manage files"):
        to_delete = st.multiselect(
            "Delete files",
            options=stored,
            format_func=lambda p: p.name,
            key="delete_picker",
        )
        if to_delete and st.button("Delete selected", type="secondary"):
            for p in to_delete:
                try:
                    p.unlink()
                except OSError as exc:
                    st.warning(f"Could not delete {p.name}: {exc}")
            read_csv.clear()
            st.rerun()

    return selected


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
    st.caption("Upload Tatari CSV exports to track week-over-week TV performance.")

    selected_paths = render_uploader_and_picker()
    df = load_all(selected_paths)

    if df.empty:
        st.info("Upload a Tatari CSV from the sidebar to get started.")
        return

    date_col = primary_date_col(df)
    if date_col is None:
        st.warning("Could not detect a date column in the uploaded data. "
                   "Most analyses require a parseable date.")

    filtered = sidebar_filters(df, date_col)

    st.subheader("Snapshot")
    cols = st.columns(4)
    cols[0].metric("Rows", f"{len(filtered):,}")
    cols[1].metric("Columns", f"{filtered.shape[1] - 1:,}")  # exclude _source_file
    cols[2].metric("Files combined", f"{filtered['_source_file'].nunique() if '_source_file' in filtered else 0:,}")
    if date_col and date_col in filtered.columns and filtered[date_col].notna().any():
        span = f"{filtered[date_col].min().date()} → {filtered[date_col].max().date()}"
    else:
        span = "—"
    cols[3].metric("Date range", span)

    weekly = aggregate_weekly(filtered, date_col) if date_col else pd.DataFrame()

    st.subheader("Most Recent Complete Week")
    render_kpis(weekly)

    st.subheader("Week-over-Week Comparison")
    if weekly.empty:
        st.info("Not enough data to compute weekly metrics.")
    else:
        wow_df, cur_start, prior_start = wow_table(weekly)
        if cur_start is not None:
            cur_label = f"{cur_start.date()}"
            prior_label = f"{prior_start.date()}" if prior_start is not None else "—"
            st.caption(f"Current week starting **{cur_label}** vs prior week starting **{prior_label}**")
        if wow_df.empty:
            st.info("No comparable metrics between current and prior week.")
        else:
            st.dataframe(style_wow(wow_df), use_container_width=True, hide_index=True)

    st.subheader("Weekly Trends")
    render_trend_charts(weekly)

    with st.expander("Raw combined data"):
        st.dataframe(filtered, use_container_width=True, height=400)
        csv_bytes = filtered.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download filtered data as CSV",
            data=csv_bytes,
            file_name=f"tatari_filtered_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
