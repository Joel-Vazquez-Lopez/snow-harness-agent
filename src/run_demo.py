"""
IMPORTS
"""
import os
# For results 
import json

from pathlib import Path

# For the API for our data
import requests

# For data
import pandas as pd
# Our cheap-data is from: Open-Meteo API

# Anomalies
import numpy as np

# For keys and agentic work 
from dotenv import load_dotenv
from openai import OpenAI

"""
DATA LOADING
"""
load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "outputs"

DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url=os.environ["OPENAI_BASE_URL"],
)

MODEL = os.environ["OPENAI_MODEL"]

"""
TEST EXPERIMENT 
"""
def fetch_open_meteo_data(
    latitude=59.8588,
    longitude=17.6389,
    start_date="2024-01-01",
    end_date="2024-01-31",
    timezone="UTC",
):
    """
    We get the real hourly historical weather data from Open-Meteo.
    Default location: Uppsala, Sweden.
    """

    url = "https://archive-api.open-meteo.com/v1/archive"

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": "temperature_2m,snowfall,precipitation",
        "timezone": timezone,
    }

    print("Requesting Open-Meteo data for Uppsala, Sweden...")
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()

    payload = response.json()
    hourly = payload["hourly"]

    df = pd.DataFrame(
        {
            "time": pd.to_datetime(hourly["time"], utc=True),
            "temperature_2m": hourly["temperature_2m"],
            "snowfall": hourly["snowfall"],
            "precipitation": hourly["precipitation"],
        }
    )

    df["source"] = "open_meteo_archive_api"
    df["location"] = "Uppsala, Sweden"
    df["latitude"] = latitude
    df["longitude"] = longitude

    return df, payload

"""
INJECTION 
"""
def inject_corruption(df, value_col="snowfall"):
    """
    Create a controlled data-quality issue.

    We take the largest real snowfall value and multiply it by 10.
    This simulates a possible unit/decimal/parsing error.

    The corruption record is hidden from our agent and used only for evaluation.
    """

    corrupted_df = df.copy()

    candidates = corrupted_df[corrupted_df[value_col].fillna(0) > 0]

    if candidates.empty:
        raise ValueError("No positive snowfall values found. Try another date range or location.")

    idx = candidates[value_col].idxmax()

    original_value = float(corrupted_df.loc[idx, value_col])
    corrupted_value = original_value * 10

    corrupted_df.loc[idx, value_col] = corrupted_value

    corruption_record = {
        "row_index": int(idx),
        "time": str(corrupted_df.loc[idx, "time"]),
        "column": value_col,
        "original_value": original_value,
        "corrupted_value": corrupted_value,
        "corruption_type": "multiplied_existing_snowfall_by_10",
        "hidden_from_agent": True,
    }

    return corrupted_df, corruption_record

"""
SIMPLE HARNESS CHECKING
"""
def check_known_suspicious_point(df, corruption_record):
    
    # corrupted
    idx = corruption_record["row_index"]
    
    # we just check closest neightbours 
    previous_value = float(df.loc[idx - 1, "snowfall"])
    current_value = float(df.loc[idx, "snowfall"])
    next_value = float(df.loc[idx + 1, "snowfall"])

    # we check if there is weird values 
    neighbour_average = (previous_value + next_value) / 2

    ratio = current_value / neighbour_average if neighbour_average > 0 else None
 
    value_divided_by_10 = current_value / 10

    # we check if the difference is "significant"
    unit_shift_plausible = (
        neighbour_average > 0
        and abs(value_divided_by_10 - neighbour_average) / neighbour_average < 0.5
    )

    # we do a simple report 
    result = {
        "check_name": "known_suspicious_point_check",
        "time": str(df.loc[idx, "time"]),
        "previous_snowfall": previous_value,
        "current_snowfall": current_value,
        "next_snowfall": next_value,
        "neighbour_average": neighbour_average,
        "ratio_to_neighbours": ratio,
        "is_suspicious": ratio is not None and ratio > 5,
        "value_divided_by_10": value_divided_by_10,
        "unit_or_decimal_shift_plausible": unit_shift_plausible,
    }

    return result

"""
AGENTIC WORK 
"""

def parse_agent_json(text):
    """
    Parse agent JSON even if the model wraps it in Markdown fences.
    """

    cleaned = text.strip()

    if cleaned.startswith("```json"):
        cleaned = cleaned.removeprefix("```json").strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").strip()

    if cleaned.endswith("```"):
        cleaned = cleaned.removesuffix("```").strip()

    return json.loads(cleaned)

def run_agent(issue, check_result):
    """
    Real LLM diagnostic agent.

    The agent receives the issue and the harness check result.
    It does not receive the hidden original corruption record.
    """
    
    # we need a prompt for the agent to know what it needs to do 
    system_prompt = """
You are a read-only diagnostic agent for mission-critical weather and energy time-series pipelines.

You are investigating a possible data-quality issue in hourly snowfall data.

You are not allowed to modify data, overwrite values, deploy code, or automatically correct anything.

Base every conclusion on the provided issue and harness check result.
If evidence is insufficient, say so clearly.

Return ONLY raw valid JSON with this exact schema.
Do not wrap it in Markdown.
Do not use ```json fences.
Do not include any explanation outside the JSON.
{
  "summary": string,
  "issue_type": string,
  "risk_level": "low" | "medium" | "medium_high" | "high",
  "evidence": [string],
  "likely_cause": string,
  "suggested_action": string,
  "auto_fix_allowed": boolean,
  "requires_human_approval": boolean
}
"""

    payload = {
        "issue": issue,
        "harness_check_result": check_result,
    }

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, indent=2)},
        ],
        temperature=0,
    )

    text = response.choices[0].message.content

    try:
        return parse_agent_json(text)
    except json.JSONDecodeError:
        return {
            "summary": "Agent did not return valid JSON.",
            "issue_type": "agent_output_format_error",
            "risk_level": "medium",
            "evidence": [text],
            "likely_cause": "The model did not follow the required JSON schema.",
            "suggested_action": "Retry with stricter structured output settings.",
            "auto_fix_allowed": False,
            "requires_human_approval": True,
        }

"""
OUTPUT RESULTS 
"""

def main():
    print("Snow Harness Agent demo started.")
    print(f"Using model: {MODEL}")
    print(f"Data directory: {DATA_DIR}")
    print(f"Output directory: {OUTPUT_DIR}")
    clean_df, raw_payload = fetch_open_meteo_data()

    clean_path = DATA_DIR / "open_meteo_uppsala_clean_original.csv"
    raw_path = DATA_DIR / "open_meteo_uppsala_raw_response.json"

    clean_df.to_csv(clean_path, index=False)
    raw_path.write_text(json.dumps(raw_payload, indent=2))

    print("\nFetched real Uppsala weather data successfully.")
    print(f"Rows: {len(clean_df)}")
    print(f"Saved clean data to: {clean_path}")
    print(f"Saved raw API response to: {raw_path}")

    print("\nPreview:")
    print(clean_df.head(10))

    print("\nSnow summary:")
    print(f"Max snowfall: {clean_df['snowfall'].max()}")
    print(f"Rows with snow: {(clean_df['snowfall'] > 0).sum()}")
    # injection: 
    corrupted_df, corruption_record = inject_corruption(clean_df)

    corrupted_path = DATA_DIR / "open_meteo_uppsala_corrupted.csv"
    corruption_record_path = OUTPUT_DIR / "corruption_record_hidden_from_agent.json"

    corrupted_df.to_csv(corrupted_path, index=False)
    corruption_record_path.write_text(json.dumps(corruption_record, indent=2))

    print("\nInjected controlled corruption.")
    print(json.dumps(corruption_record, indent=2))
    print(f"Saved corrupted data to: {corrupted_path}")
    print(f"Saved hidden corruption record to: {corruption_record_path}")
    check_result = check_known_suspicious_point(
        corrupted_df,
        corruption_record
    )

    check_result_path = OUTPUT_DIR / "harness_check_result.json"
    check_result_path.write_text(json.dumps(check_result, indent=2))

    print("\nHarness check result:")
    print(json.dumps(check_result, indent=2))
    print(f"Saved harness check result to: {check_result_path}")

    # AGENT REPORT 
    issue = {
        "issue_id": "uppsala_snowfall_demo_001",
        "pipeline": "weather_forecast_ingestion",
        "location": "Uppsala, Sweden",
        "problem": "One hourly snowfall value appears suspicious after ingestion.",
        "mode": "read_only",
    }

    agent_report = run_agent(
        issue=issue,
        check_result=check_result,
    )

    agent_report_path = OUTPUT_DIR / "agent_diagnostic_report.json"
    agent_report_path.write_text(json.dumps(agent_report, indent=2))

    print("\nAgent diagnostic report:")
    print(json.dumps(agent_report, indent=2))
    print(f"Saved agent diagnostic report to: {agent_report_path}")


if __name__ == "__main__":
    main()