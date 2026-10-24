"""
One-shot helper (not part of the firmware) to kick off a C>D>C cycle test on
all 4 MC3000 channels and monitor it to completion.

Usage: python cycle_test_monitor.py <COM port>

Opens the serial port exactly once (each open resets the ESP32-C6 and it
takes a few seconds to reconnect over BLE) and keeps it open for the whole
run - the firmware's own background poll (every 3s) is what we actually read;
we don't need to send our own "poll" commands.

Writes:
  cycle_test_log.csv     - one row per channel per snapshot (every ~60s)
  cycle_test_report.txt  - final summary once all 4 channels are Done/faulted

On completion, also publishes each channel as its own GitHub release via
publish_report.py - see that script for the "historical reports" mechanism
(one release per battery/channel, not one for the whole 4-channel session).
Pass --battery1..--battery4 to label which physical battery is in which
channel; without them, channels are just labeled "Unknown".
"""

import re
import sys
import time
import csv
import argparse
import subprocess
from pathlib import Path
import serial

CHANNELS = [1, 2, 3, 4]
# type=0 (LiIon), mode=4 (Cycle), capacity=0 (unknown/not needed for a
# cutoff-based test), chgI=2500mA, dischgI=1000mA, chgCutoff=4200mV,
# dischgCutoff=2800mV, chgEndI=0 (no current-based charge termination,
# cutoff voltage decides), cycleCount=1, cycleMode=1 (C>D>C)
PROGRAM_ARGS = "0 4 0 2500 1000 4200 2800 0 1 1"

SNAPSHOT_INTERVAL_S = 60
MAX_RUNTIME_S = 48 * 3600  # safety cap on the *monitor*, not the charger itself

LINE_RE = re.compile(
    r"ch(\d): (\S+)\s+mode=(\S+)\s+status=(.*?)\s+([\d.]+)V\s+([\d.]+)A"
    r" cap=\s*(\d+)mAh T=\s*(-?\d+)C R=\s*(\d+)mOhm cycles=(\d+)"
)

DONE_STATUSES = {"Done"}
FAULT_PREFIX = "FAULT:"


def read_for(ser, seconds):
    end = time.time() + seconds
    buf = b""
    while time.time() < end:
        chunk = ser.read(512)
        if chunk:
            buf += chunk
    return buf.decode(errors="replace")


def parse_lines(text, latest):
    for line in text.splitlines():
        m = LINE_RE.search(line)
        if not m:
            continue
        ch = int(m.group(1))
        latest[ch] = {
            "type": m.group(2),
            "mode": m.group(3),
            "status": m.group(4).strip(),
            "voltage": float(m.group(5)),
            "current": float(m.group(6)),
            "capacity_mah": int(m.group(7)),
            "temp_c": int(m.group(8)),
            "resistance_mohm": int(m.group(9)),
            "cycles": int(m.group(10)),
        }


def is_finished(state):
    if state is None:
        return False
    status = state["status"]
    return status in DONE_STATUSES or status.startswith(FAULT_PREFIX)


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default="COM3")
    ap.add_argument("--battery1", default="Unknown")
    ap.add_argument("--battery2", default="Unknown")
    ap.add_argument("--battery3", default="Unknown")
    ap.add_argument("--battery4", default="Unknown")
    ap.add_argument("--capacity1", type=int, default=None, help="rated mAh, for SOC%% in the report")
    ap.add_argument("--capacity2", type=int, default=None)
    ap.add_argument("--capacity3", type=int, default=None)
    ap.add_argument("--capacity4", type=int, default=None)
    ap.add_argument("--program", default="CycleCDC")
    return ap.parse_args()


def main():
    args = parse_args()
    port = args.port
    battery_labels = {1: args.battery1, 2: args.battery2, 3: args.battery3, 4: args.battery4}
    rated_capacity = {1: args.capacity1, 2: args.capacity2, 3: args.capacity3, 4: args.capacity4}
    print(f"Opening {port} ...")
    ser = serial.Serial(port, 115200, timeout=1)

    print("Waiting for boot + BLE reconnect...")
    time.sleep(8)
    ser.reset_input_buffer()

    latest = {ch: None for ch in CHANNELS}

    print("Setting C>D>C program on all 4 channels...")
    for ch in CHANNELS:
        cmd = f"program {ch} {PROGRAM_ARGS}\n"
        ser.write(cmd.encode())
        reply = read_for(ser, 3)
        parse_lines(reply, latest)
        print(f"  ch{ch}: {reply.strip().splitlines()[-1] if reply.strip() else '(no reply)'}")
        time.sleep(1)

    print("Starting all 4 channels...")
    for ch in CHANNELS:
        ser.write(f"start {ch}\n".encode())
        reply = read_for(ser, 2)
        parse_lines(reply, latest)
        time.sleep(1)

    with open("cycle_test_log.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "channel", "status", "voltage_v", "current_a",
                          "capacity_mah", "temp_c", "resistance_mohm", "cycles"])

    print("Monitoring until all channels are Done or faulted (snapshot every "
          f"{SNAPSHOT_INTERVAL_S}s, max {MAX_RUNTIME_S/3600:.0f}h)...")
    start_time = time.time()
    last_snapshot = 0

    while time.time() - start_time < MAX_RUNTIME_S:
        text = read_for(ser, 5)
        parse_lines(text, latest)

        if time.time() - last_snapshot >= SNAPSHOT_INTERVAL_S:
            last_snapshot = time.time()
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open("cycle_test_log.csv", "a", newline="") as f:
                writer = csv.writer(f)
                for ch in CHANNELS:
                    s = latest[ch]
                    if s:
                        writer.writerow([ts, ch, s["status"], s["voltage"], s["current"],
                                          s["capacity_mah"], s["temp_c"], s["resistance_mohm"], s["cycles"]])
            statuses = {ch: (latest[ch]["status"] if latest[ch] else "?") for ch in CHANNELS}
            print(f"[{ts}] " + " ".join(f"ch{ch}={statuses[ch]}" for ch in CHANNELS))

        if all(is_finished(latest[ch]) for ch in CHANNELS):
            break

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open("cycle_test_report.txt", "w") as f:
        f.write(f"MC3000 cycle test report - {ts}\n")
        f.write(f"Program: {args.program} (C>D>C, 4.2V/2.8V cutoffs, 2.5A charge / 1.0A discharge, 1 cycle)\n\n")
        for ch in CHANNELS:
            s = latest[ch]
            if s is None:
                f.write(f"Channel {ch}: no data received\n")
                continue
            f.write(
                f"Channel {ch} ({battery_labels[ch]}): status={s['status']}  capacity={s['capacity_mah']} mAh  "
                f"internal_resistance={s['resistance_mohm']} mOhm  temp={s['temp_c']}C  "
                f"cycles={s['cycles']}\n"
            )
    print("Done. Report written to cycle_test_report.txt")
    ser.close()

    print("Publishing each channel as its own GitHub release...")
    for ch in CHANNELS:
        cmd = [sys.executable, str(Path(__file__).with_name("publish_report.py")),
               "cycle_test_log.csv", str(ch), battery_labels[ch], args.program]
        if rated_capacity[ch]:
            cmd += ["--capacity-mah", str(rated_capacity[ch])]
        subprocess.run(cmd, check=False)  # don't crash the run if `gh` isn't set up - the local files still exist


if __name__ == "__main__":
    main()
