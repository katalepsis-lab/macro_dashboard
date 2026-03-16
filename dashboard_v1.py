"""
dashboard_v1.py

Streamlit macroeconomic dashboard.
  - Sidebar "Refresh Global Data" calls data_downloader.run_all_updates with a
    live st.status / st.progress callback.
  - 9 indicators displayed in a 3x3 Plotly grid.
  - All API / download logic lives exclusively in data_downloader.py.

Run:  streamlit run dashboard_v1.py

Data collected from APIs using Katalepsis-Lab code,
Dashboard generated using Claude Code Sonnet 4.6

@Katalepsis-Lab 2026
"""

from __future__ import annotations

import os
from datetime import datetime

import data_downloader
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# App config
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Macro Dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
)

CACHE_DIR = "./source_data"

# Clip all series to this start date so every chart shows a uniform window.
# 2000-01-01 is the practical floor given the earliest complete series
# (CAN_CPI, US_rGDP).  Series that begin later (e.g. CAN_1Y_INF in 2014,
# yield data from 2001, US CB BS from 2003) will show NaN gaps before their
# actual start — the chart builder drops those via dropna().
START_DATE = "2000-01-01"

# Total number of _cb("...") calls inside run_all_updates — used to compute
# the progress-bar fraction.  (28 download blocks + 1 final status message)
_TOTAL_STEPS = 29


# ──────────────────────────────────────────────────────────────────────────────
# Refresh orchestrator
# ──────────────────────────────────────────────────────────────────────────────

def run_refresh() -> None:
    """Call data_downloader.run_all_updates and surface progress in the UI."""
    counter = {"n": 0}

    with st.status("Refreshing global data...", expanded=True) as status_box:
        prog = st.progress(0.0, text="Starting...")

        def _callback(message: str) -> None:
            counter["n"] += 1
            fraction = min(counter["n"] / _TOTAL_STEPS, 1.0)
            prog.progress(fraction, text=message)
            st.write(message)

        error_occurred = data_downloader.run_all_updates(progress_callback=_callback)

        if error_occurred:
            status_box.update(
                label="Completed with errors. Check the log above.",
                state="error",
                expanded=True,
            )
        else:
            status_box.update(
                label="All data refreshed successfully.",
                state="complete",
                expanded=False,
            )

    st.cache_data.clear()
    st.rerun()


# ──────────────────────────────────────────────────────────────────────────────
# Data-loading helpers  (frequency alignment via pd.merge_asof)
# ──────────────────────────────────────────────────────────────────────────────

def _read(filename: str) -> pd.DataFrame:
    """Read a CSV from CACHE_DIR and parse DATE.  Returns empty DF if missing."""
    path = os.path.join(CACHE_DIR, filename)
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "Unnamed: 0" in df.columns:
        df = df.drop(columns=["Unnamed: 0"])
    df["DATE"] = pd.to_datetime(df["DATE"])
    return df.sort_values("DATE").reset_index(drop=True)


def _to_monthly(df: pd.DataFrame, cols: list[str], method: str = "last") -> pd.DataFrame:
    """
    Resample to month-end frequency.
      method='last'  — daily / weekly series  (take final obs of the month)
      method='ffill' — quarterly series       (carry last known value forward)
    """
    if df.empty:
        return df
    cols = [c for c in cols if c in df.columns]
    if not cols:
        return pd.DataFrame()
    tmp = df.set_index("DATE")[cols]
    resampled = tmp.resample("ME").last()
    if method == "ffill":
        resampled = resampled.ffill()
    return resampled.reset_index()


def _to_weekly(df: pd.DataFrame, cols: list[str], method: str = "last") -> pd.DataFrame:
    """
    Resample to week-end (Sunday) frequency.
      method='last'  — daily series  (take final obs of the week)
      method='ffill' — quarterly series  (carry last known value forward)
    """
    if df.empty:
        return df
    cols = [c for c in cols if c in df.columns]
    if not cols:
        return pd.DataFrame()
    tmp = df.set_index("DATE")[cols]
    resampled = tmp.resample("W").last()
    if method == "ffill":
        resampled = resampled.ffill()
    return resampled.reset_index()


def _merge_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """
    Merge a list of month-end DataFrames into one master frame using
    pd.merge_asof(direction='backward') so every month carries the most
    recent available observation for each series.
    """
    valid = [f for f in frames if f is not None and not f.empty]
    if not valid:
        return pd.DataFrame()
    all_dates = (
        pd.Series(pd.concat([f["DATE"] for f in valid]).unique())
        .sort_values()
        .reset_index(drop=True)
    )
    master = pd.DataFrame({"DATE": pd.to_datetime(all_dates)})
    for frame in valid:
        frame = frame.copy().sort_values("DATE")
        master = pd.merge_asof(
            master, frame,
            on="DATE",
            direction="backward",
            tolerance=pd.Timedelta("62 days"),
        )
    return master.set_index("DATE")


def _merge_frames_weekly(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Like _merge_frames but with a 14-day tolerance for weekly-frequency data."""
    valid = [f for f in frames if f is not None and not f.empty]
    if not valid:
        return pd.DataFrame()
    all_dates = (
        pd.Series(pd.concat([f["DATE"] for f in valid]).unique())
        .sort_values()
        .reset_index(drop=True)
    )
    master = pd.DataFrame({"DATE": pd.to_datetime(all_dates)})
    for frame in valid:
        frame = frame.copy().sort_values("DATE")
        master = pd.merge_asof(
            master, frame,
            on="DATE",
            direction="backward",
            tolerance=pd.Timedelta("14 days"),
        )
    return master.set_index("DATE")


# ──────────────────────────────────────────────────────────────────────────────
# Indicator calculation
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data
def get_processed_data() -> dict[str, pd.DataFrame]:
    """
    Load all source CSVs, align to a common monthly frequency using
    pd.merge_asof, calculate the 9 indicators, and return
    {"CAN": DataFrame, "US": DataFrame}.

    Frequency alignment
    -------------------
    Quarterly fiscal data  → resample(ME).last().ffill()
    Daily / weekly yields  → resample(ME).last()
    Monthly GDP            → resample(ME).last()
    All frames merged via pd.merge_asof(direction='backward').

    Indicators
    ----------
    1.  Debt / Fed Govt Revenues         = CAN_DEBT / CAN_REV (TTM)
    2.  Deficit / Fed Govt Revenues      = CAN_FISC TTM / CAN_REV TTM
    3.  Interest / Fed Govt Revenues     = CAN_INTEREST TTM / CAN_REV TTM
    4.  Real Yield Spread                = (10Y nominal - 10Y inf exp)
                                           - (1Y nominal - 1Y inf exp)
    5.  Real Yield Ratio                 = Real 10Y / Real 1Y
    6.  Real GDP Growth                  = YoY(%) from CAN_rGDP.csv
    7.  Current Account                 = TTM(B) from CAN_CURRENT_ACCOUNT.csv
    8.  Capital Flows                    = Z-Score(12Q) from CAN_CAP_FLOWS.csv
    9.  CB Balance Sheet                 = 3M_Rolling_Avg_Change_% from CAN_CB_BS.csv
    """

    # ── Canada ────────────────────────────────────────────────────────────────
    # Quarterly fiscal → ffill to monthly
    can_debt_m  = _to_monthly(_read("CAN_DEBT.csv"),            ["CAN Govt Debt(B)"],         "ffill")
    can_rev_m   = _to_monthly(_read("CAN_REV.csv"),             ["TTM(B)"],                   "ffill").rename(columns={"TTM(B)": "CAN_REV_TTM"})
    can_fisc_m  = _to_monthly(_read("CAN_FISC.csv"),            ["TTM(B)"],                   "ffill").rename(columns={"TTM(B)": "CAN_FISC_TTM"})
    can_int_m   = _to_monthly(_read("CAN_INTEREST.csv"),        ["TTM(B)"],                   "ffill").rename(columns={"TTM(B)": "CAN_INT_TTM"})
    can_ca_m    = _to_monthly(_read("CAN_CURRENT_ACCOUNT.csv"), ["TTM(B)", "Z-Score(12Q)"],   "ffill").rename(columns={"TTM(B)": "CAN_CA_TTM", "Z-Score(12Q)": "CAN_CA_ZSCORE"})
    can_flows_m = _to_monthly(_read("CAN_CAP_FLOWS.csv"),       ["Z-Score(12Q)"],             "ffill").rename(columns={"Z-Score(12Q)": "CAN_FLOWS_ZSCORE"})

    # Daily yields / inflation expectations → last obs per month
    # NOTE: CAN_10Y_TBILL is ALL CAPS — the column is 'CAN_10Y_TBILL' not 'CAN_10Y_tbill'
    can_1y_tbill_m  = _to_monthly(_read("CAN_1Y_TBILL.csv"),  ["CAN_1Y_TBILL_yield"], "last")
    can_10y_tbill_m = _to_monthly(_read("CAN_10Y_TBILL.csv"), ["CAN_10Y_TBILL"],      "last")
    can_10y_inf_m   = _to_monthly(_read("CAN_10Y_INF.csv"),   ["10Y_BE_INF"],         "last")
    # CAN_1Y_INF is quarterly (BoC CES survey) → ffill like fiscal data
    can_1y_inf_m    = _to_monthly(_read("CAN_1Y_INF.csv"),    ["INF_EXP_1Y(%)"],      "ffill")

    can_gdp_m   = _to_monthly(_read("CAN_rGDP.csv"),  ["YoY(%)"],                  "last").rename(columns={"YoY(%)": "CAN_rGDP_YoY"})
    can_cb_bs_m = _to_monthly(_read("CAN_CB_BS.csv"), ["3M_Rolling_Avg_Change_%"], "last")

    can_df = _merge_frames([
        can_debt_m, can_rev_m, can_fisc_m, can_int_m,
        can_1y_tbill_m, can_10y_tbill_m, can_1y_inf_m, can_10y_inf_m,
        can_gdp_m, can_ca_m, can_flows_m, can_cb_bs_m,
    ])
    if not can_df.empty:
        can_df = can_df[can_df.index >= START_DATE]

    if not can_df.empty:
        # 1. Debt / Revenues
        can_df["Debt / Fed Govt Revenues"]     = can_df["CAN Govt Debt(B)"] / can_df["CAN_REV_TTM"]
        # 2. Deficit / Revenues  (negative = deficit)
        can_df["Deficit / Fed Govt Revenues"]  = can_df["CAN_FISC_TTM"]     / can_df["CAN_REV_TTM"]
        # 3. Interest / Revenues  (TTM for both)
        can_df["Interest / Fed Govt Revenues"] = can_df["CAN_INT_TTM"]      / can_df["CAN_REV_TTM"]
        # 4 & 5. Real yields, spread, ratio
        can_df["CAN_ST_Real"] = can_df["CAN_1Y_TBILL_yield"] - can_df["INF_EXP_1Y(%)"]
        can_df["CAN_LT_Real"] = can_df["CAN_10Y_TBILL"]      - can_df["10Y_BE_INF"]
        can_df["L-T real yield - S-T real yield"] = can_df["CAN_LT_Real"] - can_df["CAN_ST_Real"]
        # 6–9: CAN_rGDP_YoY, CAN_CA_TTM, CAN_FLOWS_ZSCORE, 3M_Rolling_Avg_Change_% are ready

    # ── United States ─────────────────────────────────────────────────────────
    us_debt_m  = _to_monthly(_read("US_DEBT.csv"),     ["US Govt Debt(B)"],          "ffill")
    us_rev_m   = _to_monthly(_read("US_REV.csv"),      ["TTM", "US_GOVT_REV"],       "ffill").rename(columns={"TTM": "US_REV_TTM", "US_GOVT_REV": "US_REV_QTR"})
    us_fisc_m  = _to_monthly(_read("US_FISC.csv"),     ["TTM(B)"],                   "last").rename(columns={"TTM(B)": "US_FISC_TTM"})  # monthly Treasury data
    us_int_m   = _to_monthly(_read("US_INTEREST.csv"), ["Interest_Payments_B"],      "ffill")
    us_ca_m    = _to_monthly(_read("US_CAB.csv"),      ["TTM(B)", "Z-Score(12Q)"],   "ffill").rename(columns={"TTM(B)": "US_CA_TTM", "Z-Score(12Q)": "US_CA_ZSCORE"})
    us_flows_m = _to_monthly(_read("US_CAP_FLOWS.csv"),["Z-Score(12Q)"],             "ffill").rename(columns={"Z-Score(12Q)": "US_FLOWS_ZSCORE"})

    us_1y_tbill_m  = _to_monthly(_read("US_1Y_TBILL.csv"),  ["US_1Y_TBILL"],  "last")
    us_10y_tbill_m = _to_monthly(_read("US_10Y_TBILL.csv"), ["US_10Y_TBILL"], "last")
    us_1y_inf_m    = _to_monthly(_read("US_1Y_INF.csv"),    ["EXPINF1YR"],    "last")
    us_10y_inf_m   = _to_monthly(_read("US_10Y_INF.csv"),   ["EXPINF10YR"],   "last")

    us_gdp_m   = _to_monthly(_read("US_rGDP.csv"),   ["YoY(%)"],                   "last").rename(columns={"YoY(%)": "US_rGDP_YoY"})
    us_cb_bs_m = _to_monthly(_read("US_CB_BS.csv"),  ["3M_Rolling_Avg_Change(%)"], "last")

    us_df = _merge_frames([
        us_debt_m, us_rev_m, us_fisc_m, us_int_m,
        us_1y_tbill_m, us_10y_tbill_m, us_1y_inf_m, us_10y_inf_m,
        us_gdp_m, us_ca_m, us_flows_m, us_cb_bs_m,
    ])
    if not us_df.empty:
        us_df = us_df[us_df.index >= START_DATE]

    if not us_df.empty:
        us_df["Debt / Fed Govt Revenues"]     = us_df["US Govt Debt(B)"] / us_df["US_REV_TTM"]
        us_df["Deficit / Fed Govt Revenues"]  = us_df["US_FISC_TTM"]     / us_df["US_REV_TTM"]
        # A091RC1Q027SBEA is SAAR (annual rate); divide by 4 to get actual
        # quarterly payments before summing to a 4-quarter TTM.
        us_df["US_INT_TTM"] = (us_df["Interest_Payments_B"] / 4).rolling(window=4, min_periods=4).sum()
        us_df["Interest / Fed Govt Revenues"] = us_df["US_INT_TTM"] / us_df["US_REV_TTM"]
        us_df["US_ST_Real"] = us_df["US_1Y_TBILL"] - us_df["EXPINF1YR"]
        us_df["US_LT_Real"] = us_df["US_10Y_TBILL"] - us_df["EXPINF10YR"]
        us_df["L-T real yield - S-T real yield"] = us_df["US_LT_Real"] - us_df["US_ST_Real"]

    # ── High-frequency raw series ──────────────────────────────────────────────
    # Weekly real yield spread — CAN
    _can_sp_w = _merge_frames_weekly([
        _to_weekly(_read("CAN_1Y_TBILL.csv"),  ["CAN_1Y_TBILL_yield"], "last"),
        _to_weekly(_read("CAN_10Y_TBILL.csv"), ["CAN_10Y_TBILL"],      "last"),
        _to_weekly(_read("CAN_10Y_INF.csv"),   ["10Y_BE_INF"],         "last"),
        _to_weekly(_read("CAN_1Y_INF.csv"),    ["INF_EXP_1Y(%)"],      "ffill"),
    ])
    if not _can_sp_w.empty:
        _can_sp_w["yield_spread"] = (
            (_can_sp_w["CAN_10Y_TBILL"] - _can_sp_w["10Y_BE_INF"]) -
            (_can_sp_w["CAN_1Y_TBILL_yield"] - _can_sp_w["INF_EXP_1Y(%)"])
        )
        _can_sp_w = _can_sp_w[_can_sp_w.index >= START_DATE]

    # Weekly real yield spread — US
    _us_sp_w = _merge_frames_weekly([
        _to_weekly(_read("US_1Y_TBILL.csv"),  ["US_1Y_TBILL"],  "last"),
        _to_weekly(_read("US_10Y_TBILL.csv"), ["US_10Y_TBILL"], "last"),
        _to_weekly(_read("US_10Y_INF.csv"),   ["EXPINF10YR"],   "last"),
        _to_weekly(_read("US_1Y_INF.csv"),    ["EXPINF1YR"],    "last"),
    ])
    if not _us_sp_w.empty:
        _us_sp_w["yield_spread"] = (
            (_us_sp_w["US_10Y_TBILL"] - _us_sp_w["EXPINF10YR"]) -
            (_us_sp_w["US_1Y_TBILL"] - _us_sp_w["EXPINF1YR"])
        )
        _us_sp_w = _us_sp_w[_us_sp_w.index >= START_DATE]

    def _raw_col(filename: str, col: str) -> pd.Series:
        """Load a single column from a CSV at its native frequency, clipped to START_DATE."""
        df = _read(filename)
        if df.empty or col not in df.columns:
            return pd.Series(dtype=float)
        return df.set_index("DATE")[col].loc[START_DATE:].dropna()

    def _sp(df: pd.DataFrame, col: str) -> pd.Series:
        """Extract a column from a weekly merged frame as a Series."""
        if df.empty or col not in df.columns:
            return pd.Series(dtype=float)
        return df[col].dropna()

    return {
        "CAN": can_df,
        "US":  us_df,
        "CAN_raw": {
            "yield_spread": _sp(_can_sp_w, "yield_spread"),
            "CB_BS":        _raw_col("CAN_CB_BS.csv", "3M_Rolling_Avg_Change_%"),
            "rGDP":         _raw_col("CAN_rGDP.csv",     "YoY(%)"),
            "rGDP_EXP_QoQ": _raw_col("CAN_rGDP_EXP.csv", "QoQ(%)_annualized"),
            "CPI_YoY":      _raw_col("CAN_CPI.csv",   "YoY(%)"),
            "CPI_MoM":      _raw_col("CAN_CPI.csv",   "MoM(%)"),
        },
        "US_raw": {
            "yield_spread": _sp(_us_sp_w, "yield_spread"),
            "CB_BS":        _raw_col("US_CB_BS.csv",  "3M_Rolling_Avg_Change(%)"),
            "rGDP":         _raw_col("US_rGDP.csv",   "YoY(%)"),
            "rGDP_QoQ":     _raw_col("US_rGDP.csv",   "MoM(%)_annualized"),
            "CPI_YoY":      _raw_col("US_CPI.csv",    "YoY(%)"),
            "CPI_MoM":      _raw_col("US_CPI.csv",    "MoM(%)"),
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Chart builder
# ──────────────────────────────────────────────────────────────────────────────

_COLORS = {"CAN": "#C8102E", "US": "#1D61F4"}  # Canada red, US blue

_LAYOUT = dict(
    margin=dict(l=10, r=10, t=32, b=10),
    plot_bgcolor="rgba(0,0,0,0)",
    paper_bgcolor="rgba(0,0,0,0)",
    font=dict(size=11),
    xaxis=dict(showgrid=False, zeroline=False),
    yaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.15)", zeroline=False),
    hovermode="x unified",
    showlegend=False,
)


def make_chart(
    series: pd.Series,
    country: str,
    *,
    chart_type: str = "line",
    zero_line: bool = False,
    y_label: str = "",
    zscore_bands: bool = False,
) -> go.Figure:
    """
    Build a Plotly figure for a single indicator series.

    chart_type   : 'line' | 'bar'
    zero_line    : draw a dashed reference at y = 0
    zscore_bands : shade ±1σ region (for Z-score charts)
    """
    series = series.dropna()
    color  = _COLORS.get(country, "#888888")
    fig    = go.Figure()

    if chart_type == "bar":
        bar_colors = ["#15B47C" if v >= 0 else "#C8102E" for v in series.values]
        fig.add_trace(go.Bar(
            x=series.index,
            y=series.values,
            marker_color=bar_colors,
            marker_line_width=0,
            name=y_label,
        ))
    else:
        fig.add_trace(go.Scatter(
            x=series.index,
            y=series.values,
            mode="lines",
            line=dict(color=color, width=2),
            name=y_label,
        ))

    if zscore_bands:
        fig.add_hrect(y0=-1, y1=1, fillcolor="rgba(128,128,128,0.08)", line_width=0)
        fig.add_hline(y=1,  line=dict(color="rgba(128,128,128,0.4)", dash="dot", width=1))
        fig.add_hline(y=-1, line=dict(color="rgba(128,128,128,0.4)", dash="dot", width=1))

    if zero_line:
        fig.add_hline(y=0, line=dict(color="rgba(200,200,200,0.5)", dash="dash", width=1))

    fig.update_layout(**_LAYOUT, yaxis_title=y_label)
    return fig


# ──────────────────────────────────────────────────────────────────────────────
# Metric definitions  (drives the 3×3 grid)
# ──────────────────────────────────────────────────────────────────────────────

METRICS = [
    dict(
        title="Debt / Fed Govt Revenues",
        subtitle="Federal debt as a multiple of trailing-12-month revenues",
        can_col="Debt / Fed Govt Revenues",
        us_col="Debt / Fed Govt Revenues",
        y_label="Ratio (x)",
        chart_type="line",
        zero_line=False,
        zscore_bands=False,
        can_frequency="Quarterly (ffill)",
        us_frequency="Quarterly (ffill)",
    ),
    dict(
        title="Deficit / Fed Govt Revenues",
        subtitle="TTM fiscal balance / TTM revenues — negative = deficit",
        can_col="Deficit / Fed Govt Revenues",
        us_col="Deficit / Fed Govt Revenues",
        y_label="Ratio",
        chart_type="bar",
        zero_line=True,
        zscore_bands=False,
        can_frequency="Quarterly (ffill)",
        us_frequency="Monthly",
    ),
    dict(
        title="Interest / Fed Govt Revenues",
        subtitle="TTM interest payments / TTM revenues",
        can_col="Interest / Fed Govt Revenues",
        us_col="Interest / Fed Govt Revenues",
        y_label="Ratio",
        chart_type="line",
        zero_line=False,
        zscore_bands=False,
        can_frequency="Quarterly (ffill)",
        us_frequency="Quarterly (ffill)",
    ),
    dict(
        title="Real Yield Spread  (L-T minus S-T)",
        subtitle="(10Y nominal − 10Y infl. exp.) − (1Y nominal − 1Y infl. exp.)",
        can_col="L-T real yield - S-T real yield",
        us_col="L-T real yield - S-T real yield",
        y_label="pp",
        chart_type="line",
        zero_line=True,
        zscore_bands=False,
        can_frequency="Weekly",
        us_frequency="Weekly",
        can_raw_key="yield_spread",
        us_raw_key="yield_spread",
        can_note="10Y infl. exp. = market breakeven rate (GoC bonds); 1Y infl. exp. = BoC CES survey",
    ),
    dict(
        title="Real GDP Growth",
        subtitle="YoY: monthly output-based · QoQ ann.: expenditure-based, chained (2017) dollars",
        can_col="",
        us_col="",
        y_label="%",
        chart_type="bar",
        zero_line=True,
        zscore_bands=False,
        display="readings_split",
        can_frequency="Monthly / Quarterly",
        us_frequency="",
        can_raw_key_yoy="rGDP",
        can_raw_key_mom="rGDP_EXP_QoQ",
        us_raw_key_yoy="rGDP",
        us_raw_key_mom="rGDP_QoQ",
        sub_label_yoy="YoY %",
        sub_label_mom="QoQ % ann.",
        can_raw_date_fmt_yoy="%b %Y",
        can_raw_date_fmt_mom="Q",
        us_raw_date_fmt_yoy="Q",
        us_raw_date_fmt_mom="Q",
    ),
    dict(
        title="Current Account  (TTM)",
        subtitle="Trailing-12-month current account balance",
        can_col="CAN_CA_TTM",
        us_col="US_CA_TTM",
        y_label="Billions",
        chart_type="bar",
        zero_line=True,
        zscore_bands=False,
        can_frequency="Quarterly (ffill)",
        us_frequency="Quarterly (ffill)",
    ),
    dict(
        title="Capital Flows  (Z-Score, 12Q rolling)",
        subtitle="Net lending / net borrowing — 12-quarter rolling Z-score",
        can_col="CAN_FLOWS_ZSCORE",
        us_col="US_FLOWS_ZSCORE",
        y_label="Z-Score",
        chart_type="line",
        zero_line=True,
        zscore_bands=True,
        can_frequency="Quarterly (ffill)",
        us_frequency="Quarterly (ffill)",
        default_x_slice=("2018", None),
    ),
    dict(
        title="CB Balance Sheet  (3M Rolling Avg Change %)",
        subtitle="3-month rolling average of weekly % change in central bank total assets",
        can_col="3M_Rolling_Avg_Change_%",
        us_col="3M_Rolling_Avg_Change(%)",
        y_label="%",
        chart_type="bar",
        zero_line=True,
        zscore_bands=False,
        display="readings",
        can_frequency="Weekly",
        us_frequency="Weekly",
        can_raw_key="CB_BS",
        us_raw_key="CB_BS",
        can_raw_date_fmt="%b %d, %Y",
        us_raw_date_fmt="%b %d, %Y",
    ),
    dict(
        title="CPI Inflation",
        subtitle="Consumer Price Index — all-items, year-over-year and month-over-month",
        can_col="",
        us_col="",
        y_label="%",
        chart_type="line",
        zero_line=False,
        zscore_bands=False,
        display="readings_split",
        can_frequency="Monthly",
        us_frequency="Monthly",
        can_raw_key_yoy="CPI_YoY",
        can_raw_key_mom="CPI_MoM",
        us_raw_key_yoy="CPI_YoY",
        us_raw_key_mom="CPI_MoM",
        can_raw_date_fmt="%b %Y",
        us_raw_date_fmt="%b %Y",
    ),
]


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_date(date: pd.Timestamp, fmt: str) -> str:
    """Format a date.  fmt='Q' produces 'Q4 2025'; otherwise delegates to strftime."""
    if fmt == "Q":
        q = (date.month - 1) // 3 + 1
        return f"Q{q} {date.year}"
    return date.strftime(fmt)


def _last_updated() -> str:
    sentinel = os.path.join(CACHE_DIR, "CAN_DEBT.csv")
    if not os.path.exists(sentinel):
        return "No data found — click Refresh to download."
    mtime = os.path.getmtime(sentinel)
    return datetime.fromtimestamp(mtime).strftime("Last updated: %Y-%m-%d %H:%M")


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────────────────────────────────────

def _has_data() -> bool:
    """Return True if at least one CSV exists in CACHE_DIR."""
    if not os.path.isdir(CACHE_DIR):
        return False
    return any(f.endswith(".csv") for f in os.listdir(CACHE_DIR))


def render_dashboard() -> None:
    # ── Auto-download on cold start (e.g. Streamlit Cloud ephemeral filesystem)
    if not _has_data() and not st.session_state.get("_auto_downloaded"):
        st.session_state["_auto_downloaded"] = True
        run_refresh()

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.title("Macro Dashboard")
        st.caption("Canada & United States | 9 Indicators")
        st.divider()
        st.caption(_last_updated())
        st.divider()

        if st.button(
            "Refresh Global Data",
            type="primary",
            use_container_width=True,
            help="Downloads the latest data from StatsCan, Bank of Canada, and FRED.",
        ):
            run_refresh()

        st.divider()
        st.caption("Sources: Statistics Canada · Bank of Canada Valet API · FRED")

    # ── Load processed data ───────────────────────────────────────────────────
    with st.spinner("Loading data..."):
        data = get_processed_data()

    can_df  = data["CAN"]
    us_df   = data["US"]
    can_raw = data.get("CAN_raw", {})
    us_raw  = data.get("US_raw",  {})

    if can_df.empty and us_df.empty:
        st.warning(
            "No source data found. Use **Refresh Global Data** in the sidebar to download."
        )
        return

    # ── Country tabs ──────────────────────────────────────────────────────────
    tab_can, tab_us = st.tabs(["Canada", "United States"])

    def _render_readings(
        monthly_series: pd.Series,
        raw_series: pd.Series | None = None,
        date_fmt: str = "%b %Y",
        use_raw_for_prior: bool = False,
    ) -> None:
        """
        Display latest reading large + 3 prior readings small.

        use_raw_for_prior=True  — big number + prior 3 all from raw_series
                                   (native frequency; e.g. monthly for CAN rGDP,
                                    quarterly for US rGDP).
        use_raw_for_prior=False — big number from raw_series (exact date),
                                   prior 3 from monthly_series (e.g. CB BS).
        date_fmt='Q' renders as 'Q4 2025'; otherwise delegates to strftime.
        """
        r = raw_series.dropna() if raw_series is not None else pd.Series(dtype=float)
        s = monthly_series.dropna()

        if use_raw_for_prior and not r.empty:
            # Use raw series for everything — last 4 native-frequency observations
            tail = r.iloc[-4:]
            latest_val  = tail.iloc[-1]
            latest_date = _fmt_date(tail.index[-1], date_fmt)
            color = "#15B47C" if latest_val >= 0 else "#C8102E"
            st.markdown(
                f"<p style='font-size:2.4rem;font-weight:700;color:{color};margin:4px 0'>"
                f"{latest_val:+.2f}%</p>"
                f"<p style='font-size:0.85rem;color:rgba(150,150,150,0.9);margin:0 0 12px'>"
                f"{latest_date}</p>",
                unsafe_allow_html=True,
            )
            for i in range(len(tail) - 2, -1, -1):
                v = tail.iloc[i]
                d = _fmt_date(tail.index[i], date_fmt)
                c = "#15B47C" if v >= 0 else "#C8102E"
                st.markdown(
                    f"<span style='font-size:1.05rem;font-weight:600;color:{c}'>{v:+.2f}%</span>"
                    f"<span style='font-size:0.8rem;color:rgba(150,150,150,0.8);margin-left:8px'>{d}</span>",
                    unsafe_allow_html=True,
                )
            return

        # Big number from raw (exact date), prior 3 from monthly
        if not r.empty:
            latest_val  = r.iloc[-1]
            latest_date = _fmt_date(r.index[-1], date_fmt)
        elif not s.empty:
            latest_val  = s.iloc[-1]
            latest_date = s.index[-1].strftime("%b %Y")
        else:
            st.warning("Data unavailable")
            return

        color = "#15B47C" if latest_val >= 0 else "#C8102E"
        st.markdown(
            f"<p style='font-size:2.4rem;font-weight:700;color:{color};margin:4px 0'>"
            f"{latest_val:+.2f}%</p>"
            f"<p style='font-size:0.85rem;color:rgba(150,150,150,0.9);margin:0 0 12px'>"
            f"{latest_date}</p>",
            unsafe_allow_html=True,
        )
        if s.empty:
            return
        tail = s.iloc[-4:]
        for i in range(len(tail) - 2, -1, -1):
            v = tail.iloc[i]
            d = tail.index[i].strftime("%b %Y")
            c = "#15B47C" if v >= 0 else "#C8102E"
            st.markdown(
                f"<span style='font-size:1.05rem;font-weight:600;color:{c}'>{v:+.2f}%</span>"
                f"<span style='font-size:0.8rem;color:rgba(150,150,150,0.8);margin-left:8px'>{d}</span>",
                unsafe_allow_html=True,
            )

    def _render_tab(df: pd.DataFrame, col_key: str, country: str, raw: dict) -> None:
        if df.empty:
            st.error(f"No data available for {country}.")
            return

        freq_field         = "can_frequency"    if country == "CAN" else "us_frequency"
        raw_key_field      = "can_raw_key"      if country == "CAN" else "us_raw_key"
        raw_date_fmt_field = "can_raw_date_fmt" if country == "CAN" else "us_raw_date_fmt"

        for row_start in range(0, len(METRICS), 3):
            cols = st.columns(3, gap="medium")
            for j in range(3):
                idx = row_start + j
                if idx >= len(METRICS):
                    break
                m = METRICS[idx]
                col_name   = m[col_key]
                raw_series = raw.get(m.get(raw_key_field))  # pd.Series or None
                frequency  = m.get(freq_field, "")

                with cols[j]:
                    st.markdown(f"**{m['title']}**")
                    caption = m["subtitle"]
                    if frequency:
                        caption = f"{caption}  ·  *{frequency}*"
                    st.caption(caption)
                    note_key = "can_note" if country == "CAN" else "us_note"
                    if note := m.get(note_key):
                        st.caption(f"*{note}*")

                    if m.get("display") == "readings":
                        monthly_s = df[col_name] if col_name in df.columns else pd.Series(dtype=float)
                        _render_readings(
                            monthly_s,
                            raw_series,
                            m.get(raw_date_fmt_field, "%b %Y"),
                            m.get("use_raw_for_prior", False),
                        )
                        continue

                    if m.get("display") == "readings_split":
                        raw_key_yoy_field = "can_raw_key_yoy" if country == "CAN" else "us_raw_key_yoy"
                        raw_key_mom_field = "can_raw_key_mom" if country == "CAN" else "us_raw_key_mom"
                        raw_yoy = raw.get(m.get(raw_key_yoy_field))
                        raw_mom = raw.get(m.get(raw_key_mom_field))
                        default_fmt = m.get(raw_date_fmt_field, "%b %Y")
                        date_fmt_yoy_field = "can_raw_date_fmt_yoy" if country == "CAN" else "us_raw_date_fmt_yoy"
                        date_fmt_mom_field = "can_raw_date_fmt_mom" if country == "CAN" else "us_raw_date_fmt_mom"
                        date_fmt_yoy = m.get(date_fmt_yoy_field, default_fmt)
                        date_fmt_mom = m.get(date_fmt_mom_field, default_fmt)
                        sub_label_yoy = m.get("sub_label_yoy", "YoY %")
                        sub_label_mom = m.get("sub_label_mom", "MoM %")
                        sub_yoy, sub_mom = st.columns(2)
                        with sub_yoy:
                            st.markdown(f"**{sub_label_yoy}**")
                            _render_readings(
                                pd.Series(dtype=float), raw_yoy, date_fmt_yoy, use_raw_for_prior=True,
                            )
                        with sub_mom:
                            st.markdown(f"**{sub_label_mom}**")
                            _render_readings(
                                pd.Series(dtype=float), raw_mom, date_fmt_mom, use_raw_for_prior=True,
                            )
                        continue

                    # Chart: prefer higher-frequency raw series when available
                    if raw_series is not None and not raw_series.dropna().empty:
                        chart_series = raw_series
                    elif col_name in df.columns and not df[col_name].dropna().empty:
                        chart_series = df[col_name]
                    else:
                        st.warning("Data unavailable")
                        continue

                    x_slice = m.get("default_x_slice")
                    if x_slice:
                        show_all = st.checkbox(
                            "Show full history",
                            value=False,
                            key=f"x_slice_{idx}_{country}",
                        )
                        if not show_all:
                            chart_series = chart_series[x_slice[0] : x_slice[1]]

                    fig = make_chart(
                        chart_series,
                        country=country,
                        chart_type=m["chart_type"],
                        zero_line=m["zero_line"],
                        y_label=m["y_label"],
                        zscore_bands=m["zscore_bands"],
                    )
                    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    with tab_can:
        _render_tab(can_df, "can_col", "CAN", can_raw)

    with tab_us:
        _render_tab(us_df, "us_col", "US", us_raw)


if __name__ == "__main__":
    render_dashboard()
