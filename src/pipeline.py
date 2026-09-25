"""
Macroeconomic Data Pipeline: U.S. Wage Growth vs. Inflation
Author: Gabriel Jayachand Pallekonda
Description:
    End-to-end data engineering and analytics pipeline. Ingests CPI-U 
    and Average Hourly Earnings from the Bureau of Labor Statistics (BLS) 
    API v2, stages raw monthly observations in SQLite, executes calendar-based 
    YoY lookback transformations in SQL, and exports both analytical fact 
    tables and publication-quality trend visualizations.
"""

import os
import json
import sqlite3
from datetime import datetime
import requests
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# ==============================================================================
# 1. DIRECTORY CONFIGURATION & CONSTANTS
# ==============================================================================
# Resolves paths relative to project root: bls-wage-inflation-pipeline/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
FIGURES_DIR = os.path.join(BASE_DIR, "figures")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(FIGURES_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "bls_analytics.db")
CSV_PATH = os.path.join(DATA_DIR, "bls_real_wage_analysis.csv")
FIGURE_PATH = os.path.join(FIGURES_DIR, "real_wage_growth_trends.png")

BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
SERIES_CPI = "CUSR0000SA0"          # Headline CPI-U (All Urban Consumers)
SERIES_EARNINGS = "CES0500000003"    # Total Private Average Hourly Earnings
START_YEAR = "2019"
CURRENT_YEAR = str(datetime.now().year)


# ==============================================================================
# 2. EXTRACTION & NORMALIZATION (BLS API v2)
# ==============================================================================
def extract_bls_data() -> pd.DataFrame:
    """
    Queries BLS API v2 for CPI-U and Average Hourly Earnings.
    Parses nested JSON payloads into a normalized pandas DataFrame.
    """
    print(f"Connecting to BLS API v2 ({START_YEAR} - {CURRENT_YEAR})...")
    payload = {
        "seriesid": [SERIES_CPI, SERIES_EARNINGS],
        "startyear": START_YEAR,
        "endyear": CURRENT_YEAR
        # "registrationkey": "YOUR_KEY_HERE"  # Optional BLS API v2 key
    }
    headers = {"Content-type": "application/json"}
    
    response = requests.post(BLS_API_URL, data=json.dumps(payload), headers=headers)
    response.raise_for_status()
    raw_json = response.json()
    
    if raw_json.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS API returned an error: {raw_json.get('message')}")
    
    records = []
    for series in raw_json["Results"]["series"]:
        series_id = series["seriesID"]
        for item in series["data"]:
            # Exclude annual average summary periods (M13)
            if item["period"] != "M13":
                val_str = item["value"].strip()
                val_float = float(val_str) if (val_str != "-" and val_str != "") else None
                records.append({
                    "series_id": series_id,
                    "year": int(item["year"]),
                    "period": item["period"],
                    "period_name": item["periodName"],
                    "value": val_float
                })
                
    df_raw = pd.DataFrame(records)
    
    # Calendar date normalization to ISO standard YYYY-MM-DD
    df_raw["month_num"] = df_raw["period"].str.replace("M", "").astype(int)
    df_raw["date"] = pd.to_datetime(dict(year=df_raw["year"], month=df_raw["month_num"], day=1))
    df_raw["date_str"] = df_raw["date"].dt.strftime("%Y-%m-%d")
    df_raw = df_raw.sort_values(by=["series_id", "date"]).reset_index(drop=True)
    
    print(f"Extraction successful: Retrieved {len(df_raw)} records across {df_raw['series_id'].nunique()} series.")
    return df_raw


# ==============================================================================
# 3. RELATIONAL STAGING & SQL MODELING (SQLITE)
# ==============================================================================
def transform_and_load(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Loads raw records into SQLite staging, performs calendar-lookback self-joins 
    for YoY metrics and real wage growth, and persists modeled analytical layers.
    """
    print(f"Connecting to SQLite database at: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    
    # 3.1 Idempotent Staging Layer
    df_raw.to_sql("stg_bls_monthly", conn, index=False, if_exists="replace")
    print("Loaded records into staging table: 'stg_bls_monthly'")

    # 3.2 SQL Feature Engineering & Analytical Fact Modeling
    sql_transformation = """
    -- =============================================================================
    -- Defining CTE 1: base_data
    -- Derives exact 1-year calendar lookback for each monthly record
    -- Used (..., '-1 year') instead of LAG(12) to ensure accuracy even when survey
    -- interruptions occur (e.g., Oct. 2025)
    -- =============================================================================
    WITH base_data AS (
        SELECT
            date_str,
            date(date_str, '-1 year') AS prior_year_date,
            series_id,
            "value"
        FROM stg_bls_monthly
    ),
    -- =============================================================================
    -- Defining CTE 2: yoy_metrics
    -- Self-joins on series_id and prior_year_date to align relevant metrics and
    -- computes 12-month percent change for CPI-U and Average Hourly Earnings
    -- Formula: ((Current - Prior) / Prior) * 100
    -- If either value is NULL, the calculation safely returns NULL.
    -- =============================================================================
    yoy_metrics AS (
        SELECT
            curr.date_str,
            curr.series_id,
            curr."value" AS current_val,
            prior."value" AS prior_val,
            ROUND(((curr."value" - prior."value") / prior."value") * 100.0, 2) AS yoy_pct_change
        FROM base_data curr
        LEFT JOIN base_data prior
          ON curr.series_id = prior.series_id
         AND curr.prior_year_date = prior.date_str
        WHERE prior."value" IS NOT NULL -- Excludes baseline year where prior year is outside dataset
    )
    -- =============================================================================
    -- Final SELECT statement: Fact Table
    -- Pivots tall series records into side-by-side columns by dates in order to
    -- calculate:
    -- Real Wage Growth (%) = Nominal Wage Growth (%) - CPI Inflation (%)
    -- =============================================================================
    SELECT
        cpi.date_str AS "date",
        cpi.current_val AS cpi_index,
        cpi.yoy_pct_change AS cpi_inflation_yoy,
        wages.current_val AS avg_hourly_earnings,
        wages.yoy_pct_change AS wage_growth_yoy,
        ROUND(wages.yoy_pct_change - cpi.yoy_pct_change, 2) AS real_wage_growth
    FROM yoy_metrics cpi
    JOIN yoy_metrics wages
      ON cpi.date_str = wages.date_str
    WHERE cpi.series_id = 'CUSR0000SA0'
      AND wages.series_id = 'CES0500000003'
    ORDER BY cpi.date_str ASC;
    """

    df_reporting = pd.read_sql_query(sql_transformation, conn)
    
    # 3.3 Persist Modeled Fact Table & Export Flat CSV
    df_reporting.to_sql("fct_economic_indicators", conn, index=False, if_exists="replace")
    print("Committed modeled table: 'fct_economic_indicators'")
    
    df_reporting.to_csv(CSV_PATH, index=False)
    print(f"Exported clean reporting CSV for Tableau: {CSV_PATH}")
    
    conn.close()
    return df_reporting


# ==============================================================================
# 4. VISUALIZATION ARTIFACT GENERATION
# ==============================================================================
def export_visualizations(df_reporting: pd.DataFrame):
    """
    Plots dual-axis nominal vs. inflation trends and diverging real wage 
    growth fills, saving a 300 DPI figure to figures/.
    """
    print("Generating Matplotlib trend charts...")
    df_reporting["plot_date"] = pd.to_datetime(df_reporting["date"])

    plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), sharex=True)

    # --------------------------------------------------------------------------
    # Chart 1: Nominal Wage Growth vs. CPI-U Inflation Rate (YoY %)
    # --------------------------------------------------------------------------
    ax1.plot(df_reporting["plot_date"], df_reporting["wage_growth_yoy"],
             label="Avg Hourly Earnings Growth (YoY %)", color="#1f77b4", linewidth=2.2)
    ax1.plot(df_reporting["plot_date"], df_reporting["cpi_inflation_yoy"],
             label="CPI-U Inflation Rate (YoY %)", color="#d62728", linewidth=2.2, linestyle="--")

    ax1.set_title("U.S. Nominal Wage Growth vs. CPI Inflation (2020 – 2026)", fontsize=14, fontweight="bold", pad=12)
    ax1.set_ylabel("Year-over-Year Change (%)", fontsize=11)
    ax1.axhline(0, color="black", linewidth=0.8, linestyle=":")
    ax1.legend(loc="upper right", frameon=True)

    # --------------------------------------------------------------------------
    # Chart 2: Real Wage Growth (% Change)
    # --------------------------------------------------------------------------
    ax2.plot(df_reporting["plot_date"], df_reporting["real_wage_growth"],
             color="#2ca02c", linewidth=2.0, label="Real Wage Growth (%)")
    ax2.axhline(0, color="black", linewidth=1.0)

    ax2.fill_between(df_reporting["plot_date"], df_reporting["real_wage_growth"], 0,
                     where=(df_reporting["real_wage_growth"] >= 0),
                     interpolate=True, color="#2ca02c", alpha=0.25, label="Purchasing Power Expansion")
    ax2.fill_between(df_reporting["plot_date"], df_reporting["real_wage_growth"], 0,
                     where=(df_reporting["real_wage_growth"] < 0),
                     interpolate=True, color="#d62728", alpha=0.25, label="Purchasing Power Contraction")

    ax2.set_title("U.S. Real Wage Growth (Nominal Wages minus Inflation)", fontsize=14, fontweight="bold", pad=12)
    ax2.set_ylabel("Real Growth (%)", fontsize=11)
    ax2.set_xlabel("Date", fontsize=11)
    ax2.legend(loc="lower right", frameon=True)

    # Format X-axis dates
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    plt.tight_layout()
    plt.savefig(FIGURE_PATH, dpi=300)
    plt.close()
    print(f"Visualization artifact saved: {FIGURE_PATH}")


# ==============================================================================
# PIPELINE ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("STARTING MACROECONOMIC INDICATOR PIPELINE")
    print("=" * 70)
    
    df_raw = extract_bls_data()
    df_modeled = transform_and_load(df_raw)
    export_visualizations(df_modeled)
    
    print("=" * 70)
    print("PIPELINE EXECUTION COMPLETED SUCCESSFULLY")
    print(f"Total reporting periods modeled: {len(df_modeled)}")
    print("=" * 70)