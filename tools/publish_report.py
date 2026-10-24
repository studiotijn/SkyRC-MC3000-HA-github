"""
Publish one channel's finished test as a GitHub Release.

This is the project's "historical measurement reports" mechanism (see
README's "Historical test reports" section). One release per channel/
battery, not one release per 4-channel test session - a measurement is
fundamentally about a single battery, not a session.

Tag/title format (fixed - both for human sorting and so the dashboard
generator, tools/build_reports_page.py, can parse it):

    yyyy-mm-dd_hh.mm.ss_<BatteryNameType>_<Program>_Ch<N>

The datetime is the SESSION START time (first CSV row for this channel),
not publish time, so releases sort chronologically by when the test ran.

The release body is human-readable markdown (status/capacity/resistance
table, flagged if anything looks off) with a trailing fenced ```json block
carrying the same data structured - that's what build_reports_page.py
reads, instead of an extra asset (one less round-trip, and it's
visible/readable on the release page too). The channel's own CSV rows are
attached as a download-able asset for anyone who wants the full time
series.

Anomaly thresholds below are deliberately simple fixed defaults, not
per-chemistry/per-cell-size tuned values - they're a "does this deserve a
second look" flag, not a safety cutoff (the charger's own protections
handle actual safety). Adjust the constants if they're too noisy/quiet for
your cells.

Usage:
  python publish_report.py <csv_path> <channel 1-4> <battery_label> <program_label> [--capacity-mah N]
"""

import argparse
import csv
import json
import re
import subprocess
import tempfile
from pathlib import Path

TEMP_RISE_WARN_C = 15  # flag if peak temp rose more than this above the start temp
TEMP_ABS_WARN_C = 45  # flag if peak temp exceeds this outright
RESISTANCE_WARN_MOHM = 150  # flag if internal resistance ever exceeds this


def sanitize(s: str) -> str:
    """Make a string safe to use inside a git tag (no spaces/colons/etc)."""
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"[^A-Za-z0-9._-]", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s or "unknown"


def read_channel_rows(csv_path: Path, channel: str) -> list[dict]:
    with open(csv_path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["channel"] == channel]
    if not rows:
        raise ValueError(f"No rows for channel {channel} in {csv_path}")
    return rows


def compute_stats(rows: list[dict], rated_capacity_mah: int | None) -> dict:
    temps = [int(r["temp_c"]) for r in rows]
    # 0 mOhm readings happen while standby/idle (no measurement yet) - ignore those for "peak".
    resistances = [int(r["resistance_mohm"]) for r in rows if int(r["resistance_mohm"]) > 0]

    last = rows[-1]
    temp_start = temps[0]
    temp_max = max(temps)
    temp_rise = temp_max - temp_start
    resistance_max = max(resistances) if resistances else 0
    capacity_mah = int(last["capacity_mah"])

    soc_percent = None
    if rated_capacity_mah:
        soc_percent = round(capacity_mah / rated_capacity_mah * 100)

    flags = []
    if temp_rise > TEMP_RISE_WARN_C:
        flags.append(f"temperature rose {temp_rise}C ({temp_start}C -> {temp_max}C) during the session")
    if temp_max > TEMP_ABS_WARN_C:
        flags.append(f"peak temperature {temp_max}C exceeds {TEMP_ABS_WARN_C}C")
    if resistance_max > RESISTANCE_WARN_MOHM:
        flags.append(f"internal resistance reached {resistance_max}mΩ (above {RESISTANCE_WARN_MOHM}mΩ)")

    return {
        "status": last["status"],
        "capacity_mah": capacity_mah,
        "rated_capacity_mah": rated_capacity_mah,
        "soc_percent": soc_percent,
        "resistance_mohm": int(last["resistance_mohm"]),
        "resistance_max_mohm": resistance_max,
        "temp_c": int(last["temp_c"]),
        "temp_start_c": temp_start,
        "temp_max_c": temp_max,
        "temp_rise_c": temp_rise,
        "cycles": int(last["cycles"]),
        "flags": flags,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", type=Path)
    ap.add_argument("channel", help="1-4")
    ap.add_argument("battery_label", help='e.g. "18650-Blue-A"')
    ap.add_argument("program_label", help='e.g. "CycleCDC"')
    ap.add_argument("--capacity-mah", type=int, default=None,
                     help="battery's rated capacity, to compute SOC%% - omit if unknown")
    args = ap.parse_args()

    rows = read_channel_rows(args.csv_path, args.channel)
    start = rows[0]["timestamp"]
    end = rows[-1]["timestamp"]
    stats = compute_stats(rows, args.capacity_mah)

    start_tag = start.replace(" ", "_").replace(":", ".")
    tag = f"{start_tag}_{sanitize(args.battery_label)}_{sanitize(args.program_label)}_Ch{args.channel}"

    data = {
        "tag": tag,
        "battery": args.battery_label,
        "program": args.program_label,
        "channel": int(args.channel),
        "start": start,
        "end": end,
        **stats,
    }

    soc_str = f"{stats['soc_percent']}%" if stats["soc_percent"] is not None else "N/A (no rated capacity given)"
    flags_md = (
        "\n".join(f"- :warning: {f}" for f in stats["flags"])
        if stats["flags"] else "- none"
    )

    body = (
        f"**Battery:** {args.battery_label}  **Program:** {args.program_label}  "
        f"**Channel:** {args.channel}\n\n"
        f"**Start:** {start}  **End:** {end}\n\n"
        f"| Status | SOC | Capacity (mAh) | Resistance (mΩ, peak) | Temp (°C, peak) | Cycles |\n"
        f"|---|---|---|---|---|---|\n"
        f"| {stats['status']} | {soc_str} | {stats['capacity_mah']} | {stats['resistance_max_mohm']} | "
        f"{stats['temp_max_c']} | {stats['cycles']} |\n\n"
        f"**Flags:**\n{flags_md}\n\n"
        f"<details><summary>Raw data (JSON, read by the dashboard)</summary>\n\n"
        f"```json\n{json.dumps(data, indent=2)}\n```\n</details>\n"
    )

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        notes_path = f.name

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        asset_path = f.name
    asset_upload_name = f"{tag}.csv"

    title = tag.replace("_", " ")
    if stats["flags"]:
        title += " [!]"

    cmd = [
        "gh", "release", "create", tag,
        f"{asset_path}#{asset_upload_name}",
        "--title", title,
        "--notes-file", notes_path,
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"Published release '{tag}'.")


if __name__ == "__main__":
    main()
