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
def inspect_snowfall_context(df):
    """
    Safe diagnostic tool for the agent version.

    The agent does not know the hidden corruption record.
    This tool simply inspects the snowfall time series and finds the largest
    snowfall value, then compares it with the previous and next timestamps.
    """

    idx = int(df["snowfall"].astype(float).idxmax())

    previous_value = float(df.loc[idx - 1, "snowfall"])
    current_value = float(df.loc[idx, "snowfall"])
    next_value = float(df.loc[idx + 1, "snowfall"])

    neighbour_average = (previous_value + next_value) / 2
    ratio = current_value / neighbour_average if neighbour_average > 0 else None

    result = {
        "tool_name": "inspect_snowfall_context",
        "time": str(df.loc[idx, "time"]),
        "row_index": idx,
        "previous_snowfall": previous_value,
        "current_snowfall": current_value,
        "next_snowfall": next_value,
        "neighbour_average": neighbour_average,
        "ratio_to_neighbouring_timestamps": ratio,
        "is_suspicious": ratio is not None and ratio > 5,
        "note": "Previous and next values are neighbouring timestamps in the same time series, not neighbouring weather stations.",
    }

    return result

def check_unit_shift(context_result):
    """
    Safe diagnostic tool.

    Checks whether dividing the suspicious snowfall value by 10 makes it
    closer to the neighbouring timestamps.
    """

    current_value = float(context_result["current_snowfall"])
    neighbour_average = float(context_result["neighbour_average"])

    value_divided_by_10 = current_value / 10

    relative_difference = (
        abs(value_divided_by_10 - neighbour_average) / neighbour_average
        if neighbour_average > 0
        else None
    )

    unit_shift_plausible = (
        relative_difference is not None and relative_difference < 0.5
    )

    result = {
        "tool_name": "check_unit_shift",
        "suspicious_value": current_value,
        "value_divided_by_10": value_divided_by_10,
        "neighbour_average": neighbour_average,
        "relative_difference_after_dividing_by_10": relative_difference,
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

def run_agent_loop(issue, df, max_steps=4):
    """
    Agent investigation loop.

    The agent decides which diagnostic tool to request.
    The harness controls which tools are allowed and executes them safely.
    """

    tool_results = {}
    trace = []

    system_prompt = """
You are a read-only diagnostic agent for mission-critical weather and energy time-series pipelines.

You are investigating possible data-quality issues in hourly snowfall data.

You cannot modify data, overwrite values, deploy code, or automatically correct anything.

You may request ONLY these tools:
1. inspect_snowfall_context
2. check_unit_shift

The previous and next snowfall values refer to neighbouring timestamps in the same time series, not neighbouring weather stations.

At each step, return ONLY raw valid JSON.

To request a tool, return:
{
  "action": "call_tool",
  "tool_name": "inspect_snowfall_context",
  "tool_args": {}
}

or:
{
  "action": "call_tool",
  "tool_name": "check_unit_shift",
  "tool_args": {}
}

When you have enough evidence, return:
{
  "action": "final_report",
  "report": {
    "summary": string,
    "issue_type": string,
    "risk_level": "low" | "medium" | "medium_high" | "high",
    "evidence": [string],
    "likely_cause": string,
    "suggested_action": string,
    "auto_fix_allowed": boolean,
    "requires_human_approval": boolean
  }
}

Do not wrap JSON in Markdown.
Do not include explanation outside the JSON.
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "issue": issue,
                    "available_tools": [
                        "inspect_snowfall_context",
                        "check_unit_shift",
                    ],
                    "instruction": "Investigate the issue using safe tools, then produce a final diagnostic report.",
                },
                indent=2,
            ),
        },
    ]

    for step in range(1, max_steps + 1):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0,
        )

        agent_text = response.choices[0].message.content
        agent_action = parse_agent_json(agent_text)

        trace.append(
            {
                "step": step,
                "agent_action": agent_action,
            }
        )

        if agent_action["action"] == "final_report":
            return agent_action["report"], trace

        if agent_action["action"] != "call_tool":
            return {
                "summary": "Agent returned an unsupported action.",
                "issue_type": "agent_protocol_error",
                "risk_level": "medium",
                "evidence": [json.dumps(agent_action)],
                "likely_cause": "The agent did not follow the allowed action protocol.",
                "suggested_action": "Review the agent prompt and retry.",
                "auto_fix_allowed": False,
                "requires_human_approval": True,
            }, trace

        tool_name = agent_action["tool_name"]

        if tool_name == "inspect_snowfall_context":
            tool_result = inspect_snowfall_context(df)
            tool_results["inspect_snowfall_context"] = tool_result

        elif tool_name == "check_unit_shift":
            if "inspect_snowfall_context" not in tool_results:
                tool_result = {
                    "tool_name": "check_unit_shift",
                    "error": "inspect_snowfall_context must be run first.",
                }
            else:
                tool_result = check_unit_shift(
                    tool_results["inspect_snowfall_context"]
                )
                tool_results["check_unit_shift"] = tool_result

        else:
            tool_result = {
                "error": f"Tool '{tool_name}' is not allowed by the harness."
            }

        trace.append(
            {
                "step": step,
                "harness_executed_tool": tool_name,
                "tool_result": tool_result,
            }
        )

        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(agent_action),
            }
        )

        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "tool_result": tool_result,
                        "instruction": "Continue the investigation or provide the final report.",
                    },
                    indent=2,
                ),
            }
        )

    return {
        "summary": "Agent did not produce a final report within the step limit.",
        "issue_type": "agent_timeout",
        "risk_level": "medium",
        "evidence": ["Maximum number of investigation steps reached."],
        "likely_cause": "The agent did not stop after using the available tools.",
        "suggested_action": "Review the trace and retry with a stricter prompt.",
        "auto_fix_allowed": False,
        "requires_human_approval": True,
    }, trace

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
    
    # AGENT INVESTIGATION
    issue = {
        "issue_id": "uppsala_snowfall_agent_led_demo_001",
        "pipeline": "weather_forecast_ingestion",
        "location": "Uppsala, Sweden",
        "problem": "Investigate whether the hourly snowfall time series contains a suspicious data-quality issue.",
        "mode": "read_only",
    }

    agent_report, trace = run_agent_loop(
        issue=issue,
        df=corrupted_df,
    )

    agent_report_path = OUTPUT_DIR / "agent_led_diagnostic_report.json"
    trace_path = OUTPUT_DIR / "agent_led_trace.json"

    agent_report_path.write_text(json.dumps(agent_report, indent=2))
    trace_path.write_text(json.dumps(trace, indent=2))

    print("\nAgent-led diagnostic report:")
    print(json.dumps(agent_report, indent=2))

    print("\nAgent-led trace:")
    print(json.dumps(trace, indent=2))

    print(f"\nSaved agent-led diagnostic report to: {agent_report_path}")
    print(f"Saved agent-led trace to: {trace_path}")


if __name__ == "__main__":
    main()