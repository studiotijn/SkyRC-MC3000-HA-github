#!/usr/bin/env python3
"""
SkyRC MC3000 - publish one channel's finished (or failed) test as a GitHub
Release, straight from Home Assistant.

Runs inside the HA container via shell_command (see packages/mc3000.yaml),
triggered by the "MC3000 rapport" automation when a channel goes from an
active state (Charging / Discharging / Paused) to Done or to any FAULT.

Data source: HA's own history API (the recorder already has every value the
ESPHome bridge published), so no separate logger is needed. Output format
(release tag/title, markdown body + trailing JSON block, CSV asset) matches
tools/publish_report.py, so tools/build_reports_page.py and the GitHub
workflow keep working unchanged.

Secrets:
  /config/secrets.yaml          mc3000_ha_token: long-lived HA access token
  /config/secrets.yaml          mc3000_github_token (optional) - otherwise
  /config/esphome/secrets.yaml  github_token is reused (needs "repo" scope)

Usage:
  python3 mc3000_report.py --sync-page          (pull the overview page)
  python3 mc3000_report.py --delete <tag>       (delete one measurement)
  python3 mc3000_report.py --rebuild-all [--force]   (redo analysis + chart of
                                                existing reports from HA history)
  python3 mc3000_report.py --rebuild <tag>      (just one report)
  python3 mc3000_report.py --export             (reports.json + charts for the dashboard card)
  python3 mc3000_report.py --backup-db / --restore-db   (cell database <-> /config/mc3000_cellen.json)
  python3 mc3000_report.py --push <dir>         (commit files from <dir>/manifest.txt to GitHub)
  python3 mc3000_report.py --channel 4 --start 2026-10-09T01:00:00+02:00 \
      --battery "Molicel P28A #3" --rated 2800 --program "Cycle C>D>C" \
      --outcome "Done"
"""

import argparse
import csv
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import yaml

SETTINGS = "/config/mc3000_settings.yaml"   # github_repo: owner/name, ha_url: https://...


def _settings():
    try:
        import yaml as _y
        with open(SETTINGS, encoding="utf-8") as f:
            return _y.safe_load(f) or {}
    except (FileNotFoundError, ImportError):
        return {}


def _default_ha_url():
    """ha_url from mc3000_settings.yaml, else HA's own external/internal URL."""
    url = _settings().get("ha_url")
    if url:
        return url.rstrip("/")
    try:
        with open("/config/.storage/core.config", encoding="utf-8") as f:
            data = json.load(f).get("data", {})
        url = data.get("external_url") or data.get("internal_url")
        if url:
            return url.rstrip("/")
    except (FileNotFoundError, ValueError):
        pass
    return "http://homeassistant.local:8123"


# GitHub repository that holds the measurement releases (owner/name).
REPO = _settings().get("github_repo") or "your-github-user/your-reports-repo"
SECRETS = "/config/secrets.yaml"
ESPHOME_SECRETS = "/config/esphome/secrets.yaml"
LOG_PREFIX = "[mc3000_report]"

TEMP_RISE_WARN_C = 15
TEMP_ABS_WARN_C = 45
RESISTANCE_WARN_MOHM = 150

# State of Health. Capacity: discharged mAh vs rated mAh. Resistance: 100% at
# (or below) the cell's as-new internal resistance, 0% once it has doubled
# (the usual end-of-life criterion). Combined score weights capacity heavier,
# since it is the more reliable reading on the MC3000.
SOH_W_CAPACITY = 0.7
SOH_W_RESISTANCE = 0.3
SOH_IR_EOL_FACTOR = 2.0

ACTIVE = {"Charging", "Discharging", "Paused"}

# column name -> entity suffix
FIELDS = {
    "status": "status",
    "voltage_v": "voltage",
    "current_a": "current",
    "capacity_mah": "capacity",
    "temp_c": "temperature",
    "resistance_mohm": "resistance",
    "cycles": "cycle",
    "elapsed_s": "elapsed_time",
}


def log(msg):
    print(f"{LOG_PREFIX} {msg}", flush=True)


def sanitize(s):
    s = re.sub(r"\s+", "-", str(s).strip())
    s = re.sub(r"[^A-Za-z0-9._-]", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s or "unknown"


def _read_yaml(path):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def load_secrets():
    sec = _read_yaml(SECRETS)
    ha_token = sec.get("mc3000_ha_token")
    # GitHub token: dedicated key in HA's secrets wins, otherwise reuse the
    # (classic, "repo"-scoped) token the ESPHome Device Builder already uses.
    gh_token = sec.get("mc3000_github_token") or _read_yaml(ESPHOME_SECRETS).get("github_token")
    if not ha_token:
        raise SystemExit(f"{LOG_PREFIX} mc3000_ha_token missing in {SECRETS}")
    if not gh_token:
        raise SystemExit(f"{LOG_PREFIX} no GitHub token: set mc3000_github_token in {SECRETS} "
                         f"or github_token in {ESPHOME_SECRETS}")
    return ha_token, gh_token


def http(method, url, token, data=None, content_type="application/json", auth_scheme="Bearer"):
    headers = {"Authorization": f"{auth_scheme} {token}", "User-Agent": "mc3000-report"}
    if data is not None:
        headers["Content-Type"] = content_type
        if content_type == "application/json":
            data = json.dumps(data).encode()
    if "api.github.com" in url or "uploads.github.com" in url:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        # Connection-level failures (RemoteDisconnected, URLError, TLS) carry
        # no URL of their own: add it, otherwise the log can't say which call died.
        raise RuntimeError(f"{method} {url.split('?')[0]} -> {e!r}") from e


def ha_number(ha_url, ha_token, entity_id):
    """Numeric state of a helper, or None if missing / 0 / not a number."""
    try:
        status, data = http("GET", f"{ha_url}/api/states/{entity_id}", ha_token)
    except RuntimeError as e:
        log(f"could not read {entity_id}: {e}")
        return None
    if status != 200:
        return None
    v = num(data.get("state"), float, 0)
    return v or None


def fetch_history(ha_url, ha_token, channel, start, end):
    prefix = f"skyrc_mc3000_ch{channel}_"
    entities = {col: f"sensor.{prefix}{suf}" for col, suf in FIELDS.items()}
    q = urllib.parse.urlencode({
        "filter_entity_id": ",".join(entities.values()),
        "end_time": end.isoformat(),
        "minimal_response": "",
        "no_attributes": "",
    })
    url = f"{ha_url}/api/history/period/{urllib.parse.quote(start.isoformat())}?{q}"
    status, data = http("GET", url, ha_token)
    if status != 200:
        raise SystemExit(f"{LOG_PREFIX} HA history failed ({status}): {data}")
    by_entity = {}
    for series in data:
        if not series:
            continue
        eid = series[0]["entity_id"]
        by_entity[eid] = [(s.get("last_changed") or s.get("last_updated"), s["state"]) for s in series]
    return entities, by_entity


def build_rows(channel, entities, by_entity):
    """Merge per-entity change lists into one time series (carry forward),
    one row per second in which anything changed."""
    events = []
    for col, eid in entities.items():
        for ts, val in by_entity.get(eid, []):
            if val in ("unknown", "unavailable", ""):
                continue
            t = datetime.fromisoformat(ts).astimezone()
            events.append((t, col, val))
    events.sort(key=lambda e: e[0])
    current = {col: None for col in FIELDS}
    rows = []
    for t, col, val in events:
        current[col] = val
        if None in (current["status"], current["voltage_v"], current["capacity_mah"]):
            continue
        stamp = t.strftime("%Y-%m-%d %H:%M:%S")
        row = {"timestamp": stamp, "channel": str(channel), **{k: current[k] for k in FIELDS}}
        # Discharge current negative (out of the cell). Firmware since
        # 2026-10-11 already publishes it signed; older history is unsigned.
        if row["current_a"] is not None:
            amps = abs(num(row["current_a"]))
            row["current_a"] = f"{-amps if row['status'] == 'Discharging' else amps:g}"
        if rows and rows[-1]["timestamp"] == stamp:
            rows[-1] = row
        else:
            rows.append(row)
    return rows


def num(v, cast=float, default=0):
    try:
        return cast(float(v))
    except (TypeError, ValueError):
        return default


def health(capacity, rated, ir, rated_ir):
    """SOH from capacity and/or internal resistance; None where the reference
    value (rated mAh / as-new mOhm) or the measurement is missing."""
    cap_pct = round(min(capacity / rated, 1.0) * 100) if (capacity and rated) else None
    ir_pct = None
    if ir and rated_ir:
        span = rated_ir * (SOH_IR_EOL_FACTOR - 1)
        ir_pct = round(max(0.0, min(1.0, 1 - (ir - rated_ir) / span)) * 100)
    if cap_pct is not None and ir_pct is not None:
        total = round(SOH_W_CAPACITY * cap_pct + SOH_W_RESISTANCE * ir_pct)
    else:
        total = cap_pct if cap_pct is not None else ir_pct
    return {"soh_percent": total, "soh_capacity_percent": cap_pct, "soh_resistance_percent": ir_pct}


FULL_DISCHARGE_MARGIN_MV = 50

# The charger reports 0xFFFF (65535) when it has no valid internal-resistance
# measurement. TEMPORARY: treat it as "no reading" here so it no longer drags
# SOH and the peak down. The permanent fix belongs at the source: find out why
# the charger stopped measuring IR (since mid-October 2026) and handle the
# invalid value in the ESPHome firmware.
IR_INVALID = 65535



def compute_stats(rows, rated, outcome, rated_ir=None, cutoff_mv=None):
    temps = [num(r["temp_c"], int) for r in rows]
    res = [num(r["resistance_mohm"], int) for r in rows if valid_ir(num(r["resistance_mohm"], int))]
    ir_missing = any(num(r["resistance_mohm"], int) >= IR_INVALID for r in rows) and not res

    # The charger's capacity counter restarts every phase: collect the peak
    # capacity (and for discharges the lowest voltage) of every phase.
    chg_phases, dis_phases = [], []   # [capacity], [[capacity, min_mv]]
    prev = None
    for r in rows:
        st, cap = r["status"], num(r["capacity_mah"], int)
        mv = int(num(r["voltage_v"]) * 1000)
        if st == "Charging":
            if prev != "Charging":
                chg_phases.append(0)
            chg_phases[-1] = max(chg_phases[-1], cap)
        elif st == "Discharging":
            if prev != "Discharging":
                dis_phases.append([0, None])
            dis_phases[-1][0] = max(dis_phases[-1][0], cap)
            if mv > 0:
                dis_phases[-1][1] = mv if dis_phases[-1][1] is None else min(dis_phases[-1][1], mv)
        prev = st

    # Only a discharge that actually reached the cutoff measures the capacity;
    # anything shorter (stopped, storage/IR runs) must not produce an SOH. In
    # multi-step programs (quality test: charge, discharge, charge, IR pulse,
    # storage) the capacity discharge is the LAST one that reached the cutoff,
    # not the IR pulse or the storage step after it.
    def reached(ph):
        return ph[1] is not None and cutoff_mv is not None and ph[1] <= cutoff_mv + FULL_DISCHARGE_MARGIN_MV
    full = [ph for ph in dis_phases if reached(ph)]
    pick = full[-1] if full else (dis_phases[-1] if dis_phases else None)
    discharged = pick[0] if pick else None
    dis_min_mv = pick[1] if pick else None
    full_discharge = bool(full)
    # Charged: the largest charge phase (in a quality test the full recharge
    # after the capacity discharge; small storage top-ups don't count).
    charged = max(chg_phases) if chg_phases else None

    capacity = discharged if discharged is not None else (charged or num(rows[-1]["capacity_mah"], int))

    # Resistance for SOH: median during discharge (the charger measures it
    # under load there; the peak is skewed by contact/start-up spikes).
    ir_dis = sorted(num(r["resistance_mohm"], int) for r in rows
                    if r["status"] == "Discharging" and valid_ir(num(r["resistance_mohm"], int)))
    ir_pool = ir_dis or sorted(res)
    ir_med = ir_pool[len(ir_pool) // 2] if ir_pool else None

    if full_discharge:
        soh = health(capacity, rated, ir_med, rated_ir)
    else:
        soh = {"soh_percent": None, "soh_capacity_percent": None, "soh_resistance_percent": None}

    t0, tmax = temps[0], max(temps)
    flags = []
    if outcome != "Done":
        flags.append(f"test ended with: {outcome}")
    if tmax - t0 > TEMP_RISE_WARN_C:
        flags.append(f"temperature rose {tmax - t0}C ({t0}C -> {tmax}C)")
    if tmax > TEMP_ABS_WARN_C:
        flags.append(f"peak temperature {tmax}C exceeds {TEMP_ABS_WARN_C}C")
    rmax = max(res) if res else None
    if ir_missing:
        flags.append("charger gave no internal-resistance reading (65535)")
    if rmax is not None and rmax > RESISTANCE_WARN_MOHM:
        flags.append(f"internal resistance reached {rmax} mOhm (above {RESISTANCE_WARN_MOHM})")

    last = rows[-1]
    if soh["soh_percent"] is not None and soh["soh_percent"] < 80:
        flags.append(f"state of health {soh['soh_percent']}% (below 80%)")

    return {
        "status": outcome,
        # soc_percent kept for older page builds; it now carries the SOH
        "soc_percent": soh["soh_percent"],
        **soh,
        "resistance_median_mohm": ir_med,
        "full_discharge": full_discharge,
        "discharge_min_mv": dis_min_mv,
        "discharge_cutoff_mv": cutoff_mv,
        "rated_resistance_mohm": rated_ir,
        "capacity_mah": capacity,
        "charged_mah": charged,
        "discharged_mah": discharged,
        "rated_capacity_mah": rated,
        "resistance_mohm": num(last["resistance_mohm"], int) if valid_ir(num(last["resistance_mohm"], int)) else None,
        "resistance_max_mohm": rmax,
        "temp_c": num(last["temp_c"], int),
        "temp_start_c": t0,
        "temp_max_c": tmax,
        "temp_rise_c": tmax - t0,
        "cycles": num(last["cycles"], int),
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# Analysis: phases, battery verdict, and an explanation of how the test ended
# (which end criterion / protection fired and which conditions were or were
# not met). Text is Dutch: these sections are read by the user, the JSON keys
# stay English for the dashboard builder.
# ---------------------------------------------------------------------------

IR_INVALID_MOHM = IR_INVALID   # 0xFFFF: the MC3000 had no IR reading
CC_CV_MARGIN_MV = 30      # "at charge voltage" = within this of the cutoff


def valid_ir(v):
    v = num(v, int)
    return 0 < v < IR_INVALID_MOHM


def hms(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _t(row):
    return datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")


def split_phases(rows):
    """Consecutive runs of the same status -> list of dicts with the rows."""
    phases = []
    for r in rows:
        if phases and phases[-1]["status"] == r["status"]:
            phases[-1]["rows"].append(r)
        else:
            phases.append({"status": r["status"], "rows": [r]})
    for p in phases:
        p["start"], p["end"] = _t(p["rows"][0]), _t(p["rows"][-1])
        p["duration_s"] = (p["end"] - p["start"]).total_seconds()
    return phases


def charge_phase_info(p, cutoff_mv):
    """CC/CV breakdown of one Charging phase."""
    rows = p["rows"]
    t0 = _t(rows[0])
    amps = [abs(num(r["current_a"])) for r in rows]
    cv_row = None
    if cutoff_mv:
        for r in rows:
            if num(r["voltage_v"]) * 1000 >= cutoff_mv - CC_CV_MARGIN_MV:
                cv_row = r
                break
    info = {
        "duration_s": p["duration_s"],
        "cc_current_a": max(amps) if amps else 0,
        "last_current_a": amps[-1] if amps else 0,
        "min_current_a": min(a for a in amps if a > 0) if any(a > 0 for a in amps) else 0,
        "max_voltage_v": max(num(r["voltage_v"]) for r in rows),
        "capacity_mah": max(num(r["capacity_mah"], int) for r in rows),
        "cv_reached": cv_row is not None,
    }
    if cv_row is not None:
        info["cc_duration_s"] = (_t(cv_row) - t0).total_seconds()
        info["cv_duration_s"] = (p["end"] - _t(cv_row)).total_seconds()
        info["cv_start_capacity_mah"] = num(cv_row["capacity_mah"], int)
    return info


def discharge_phase_info(p, cutoff_mv=None):
    rows = p["rows"]
    tail_s = tail_cap0 = None
    if cutoff_mv:
        for r in rows:
            if 0 < num(r["voltage_v"]) * 1000 <= cutoff_mv + FULL_DISCHARGE_MARGIN_MV:
                tail_s = (p["end"] - _t(r)).total_seconds()
                tail_cap0 = num(r["capacity_mah"], int)
                break
    amps = [abs(num(r["current_a"])) for r in rows if abs(num(r["current_a"])) > 0]
    volts = [num(r["voltage_v"]) for r in rows if num(r["voltage_v"]) > 0]
    return {
        "duration_s": p["duration_s"],
        "avg_current_a": sum(amps) / len(amps) if amps else 0,
        "min_voltage_v": min(volts) if volts else 0,
        "start_voltage_v": volts[0] if volts else 0,
        "capacity_mah": max(num(r["capacity_mah"], int) for r in rows),
        "cv_tail_s": tail_s,   # time spent AT the cutoff voltage (current tapering)
        "cv_tail_mah": (max(num(r["capacity_mah"], int) for r in rows) - tail_cap0) if tail_s else None,
    }


def explain_outcome(outcome, phases, params, stats, rated):
    """Bullet list explaining WHY the test ended like this: the criterion or
    protection that fired, with each of its conditions marked met / not met."""
    ok, no, info = "✅", "❌", "ℹ️"
    out = []
    active = [p for p in phases if p["status"] in ACTIVE]
    last = active[-1] if active else (phases[-1] if phases else None)
    last_row = last["rows"][-1] if last else None
    chg_mv = params.get("charge_cutoff_mv")
    end_ma = params.get("charge_end_ma")
    cap_lim = params.get("capacity_limit_mah")
    t_lim = params.get("time_cutoff_min")
    temp_lim = params.get("temp_cutoff_c")
    dis_mv = params.get("discharge_cutoff_mv")
    phase_nl = {"Charging": "laadfase", "Discharging": "ontlaadfase", "Paused": "rustfase"}
    ph_name = phase_nl.get(last["status"], last["status"]) if last else "-"

    if outcome.startswith("FAULT: time protection"):
        dur = last["duration_s"] if last else 0
        out.append("**Time protection**: de lader breekt een fase af zodra de verstreken tijd de ingestelde "
                   "tijdslimiet bereikt, terwijl het normale eindcriterium van die fase nog niet gehaald is.")
        if t_lim:
            out.append(f"{ok} Tijdslimiet bereikt: {ph_name} duurde {hms(dur)}, limiet in het HA-programma "
                       f"{int(t_lim)} min.")
        else:
            guess = round(dur / 60 / 10) * 10
            out.append(f"{ok} Tijdslimiet bereikt: {ph_name} duurde {hms(dur)}. Het HA-programma stuurt "
                       f"tijdslimiet 0 (uit), dus deze limiet (≈ {guess} min) kwam uit de slot-instelling "
                       f"van de lader zelf (programma op de lader gestart, of daar ingesteld).")
        if last and last["status"] == "Charging":
            ci = charge_phase_info(last, chg_mv)
            if ci["cv_reached"]:
                out.append(f"{ok} Laadspanning bereikt: {fmt_mv(chg_mv)} na {hms(ci['cc_duration_s'])} "
                           f"(CC-fase op {ci['cc_current_a']:.2f} A, {ci['cv_start_capacity_mah']} mAh geladen).")
            else:
                out.append(f"{no} Laadspanning niet bereikt: hoogste {ci['max_voltage_v']:.3f} V "
                           f"(doel {fmt_mv(chg_mv)}).")
            if end_ma:
                out.append(f"{no} Eindstroom niet bereikt: de stroom moet in de CV-fase zakken tot "
                           f"≤ {int(end_ma)} mA, bij de afbreking was die nog "
                           f"{ci['last_current_a'] * 1000:.0f} mA.")
            if ci["cv_reached"]:
                cv_min = ci["cv_duration_s"] / 60
                out.append(f"{info} CV-fase duurde {hms(ci['cv_duration_s'])}. Normaal zakt de stroom binnen "
                           f"~30-60 min naar de eindstroom; {'een staart van ' + str(round(cv_min)) + ' min wijst op een verhoogde inwendige weerstand of veroudering' if cv_min > 75 else 'dat is niet afwijkend lang'}.")
            if rated and ci["cc_current_a"]:
                # CC delivers ~80-85% of the charge, CV the rest in ~1 h
                need = rated * 0.85 / (ci["cc_current_a"] * 1000) * 3600 + 3600
                out.append(f"{info} Verwachte laadtijd bij {ci['cc_current_a']:.2f} A voor {rated} mAh: "
                           f"≈ {hms(need)} (CC + ~1 u CV). "
                           + ("De limiet was dus te krap gekozen." if need > dur else
                              "De limiet was ruim genoeg; de cel laadde trager dan verwacht."))
        elif last and last["status"] == "Discharging":
            di = discharge_phase_info(last, dis_mv)
            out.append(f"{no} Ontlaadgrens niet bereikt: laagste {di['min_voltage_v']:.3f} V, grens "
                       f"{fmt_mv(dis_mv)}.")
        out.append("**Oplossing:** zet de tijdslimiet op de lader uit of ruim (bv. 1,5× de verwachte laadtijd), "
                   "of start het programma via HA (stuurt de limiet mee).")

    elif outcome.startswith("FAULT: capacity protection"):
        cap = num(last_row["capacity_mah"], int) if last_row else 0
        out.append("**Capacity protection**: de lader stopt zodra er in één fase meer mAh in/uit de cel is gegaan "
                   "dan de ingestelde capaciteitslimiet, terwijl het eindcriterium nog niet gehaald is.")
        out.append(f"{ok} Capaciteitslimiet bereikt: {cap} mAh in de {ph_name} "
                   f"(limiet {int(cap_lim) if cap_lim else '?'} mAh).")
        if last and last["status"] == "Charging":
            ci = charge_phase_info(last, chg_mv)
            out.append((f"{ok} Laadspanning {fmt_mv(chg_mv)} bereikt na {hms(ci['cc_duration_s'])}."
                        if ci["cv_reached"] else
                        f"{no} Laadspanning niet bereikt: hoogste {ci['max_voltage_v']:.3f} V (doel {fmt_mv(chg_mv)})."))
            if end_ma:
                out.append(f"{no} Eindstroom niet bereikt: nog {ci['last_current_a'] * 1000:.0f} mA "
                           f"(moet ≤ {int(end_ma)} mA).")
            if rated:
                out.append(f"{info} Er ging {cap / rated * 100:.0f}% van de nominale {rated} mAh in de cel zonder dat "
                           f"die vol raakte. Een gezonde cel neemt hooguit ~105-110% op. Dit wijst op hoge "
                           f"zelfontlading of een interne (zachte) kortsluiting: de cel verbruikt de laadstroom zelf.")
        out.append("**Oplossing:** niet de limiet verhogen; deze cel apart houden en na een paar dagen rust de "
                   "spanning meten. Zakt die duidelijk, dan is de cel defect.")

    elif outcome.startswith("FAULT: battery temperature too high") or outcome.startswith("FAULT: temperature too high"):
        out.append("**Temperatuurbeveiliging**: de lader stopt zodra de gemeten temperatuur de ingestelde grens bereikt.")
        out.append(f"{ok} Temperatuur bereikt: piek {stats['temp_max_c']} °C (grens {int(temp_lim) if temp_lim else '?'} °C), "
                   f"start {stats['temp_start_c']} °C.")
        out.append(f"{info} Een stijging van {stats['temp_rise_c']} °C bij deze stroom is "
                   + ("normaal; mogelijk was de omgeving warm of de ventilator uit." if stats["temp_rise_c"] <= 12 else
                      "veel; dat wijst op een hoge inwendige weerstand. Gebruik een lagere stroom of keur de cel af."))

    elif outcome.startswith("FAULT: connection break"):
        out.append("**Connection break**: de lader zag de cel wegvallen (spanning ineens ~0 V).")
        out.append(f"{info} Laatste meting: {num(last_row['voltage_v']):.3f} V. Oorzaak is meestal contact: "
                   "cel verschoven, vuile contacten of een te korte cel. Geen oordeel over de cel zelf.")

    elif outcome.startswith("FAULT"):
        out.append(f"**{outcome[7:]}**: beveiliging van de lader. Controleer voeding, contacten en polariteit; "
                   "uit de meetwaarden is hier geen specifieke oorzaak af te leiden.")

    elif outcome == "Done":
        out.append("**Done**: het programma eindigde normaal.")
        chg = [p for p in phases if p["status"] == "Charging"]
        dis = [p for p in phases if p["status"] == "Discharging"]
        if chg:
            ci = charge_phase_info(chg[-1], chg_mv)
            out.append(f"{ok} Laden: {fmt_mv(chg_mv)} bereikt"
                       + (f" na {hms(ci['cc_duration_s'])}, daarna CV tot de stroom ≤ eindstroom "
                          f"({int(end_ma) if end_ma else '?'} mA) zakte (laatst {ci['last_current_a'] * 1000:.0f} mA)."
                          if ci["cv_reached"] else "."))
        if dis:
            di = discharge_phase_info(dis[-1], dis_mv)
            reached = dis_mv and di["min_voltage_v"] * 1000 <= dis_mv + FULL_DISCHARGE_MARGIN_MV
            out.append(f"{ok if reached else no} Ontladen: tot {di['min_voltage_v']:.3f} V (grens {fmt_mv(dis_mv)}) "
                       f"in {hms(di['duration_s'])} bij gem. {di['avg_current_a']:.2f} A → {di['capacity_mah']} mAh.")
            if di["cv_tail_s"] and di["cv_tail_s"] > 30 * 60:
                tail = di["cv_tail_mah"] or 0
                out.append(f"{info} De lader hield de cel daarna nog {hms(di['cv_tail_s'])} op de ontlaadgrens met "
                           f"een steeds kleinere stroom; dat leverde nog {tail} mAh "
                           f"({tail / max(di['capacity_mah'], 1) * 100:.0f}% van het totaal). Oorzaak: ontlaad-eindstroom "
                           f"{int(params.get('discharge_end_ma') or 0)} mA - de fase stopt pas als de stroom daaronder komt. "
                           f"Zet die gelijk aan de ontlaadstroom (zoals de SkyRC-app standaard doet), dan stopt de "
                           f"fase direct op de grens en is de capaciteit vergelijkbaar met de datasheet (die meet ook zonder staart).")

    else:
        out.append(f"**{outcome}**: niet door de lader zelf beëindigd (handmatig gestopt, noodstop van de ESP "
                   f"of batterij verwijderd). Er is geen eindcriterium beoordeeld.")
    return out


def fmt_mv(mv):
    return f"{mv / 1000:.2f} V" if mv else "?"


def battery_verdict(stats, phases, params, rated, rated_ir, outcome, extra=None):
    """Overall state of the cell: (label, list of reasons)."""
    reasons, score = [], 0   # score: 0 good .. higher = worse
    extra = extra or {}
    cap_pct = stats.get("soh_capacity_percent")
    if stats.get("full_discharge") and cap_pct is not None:
        if cap_pct >= 90:
            reasons.append(f"Capaciteit {stats['discharged_mah']} mAh = {cap_pct}% van nominaal: goed.")
        elif cap_pct >= 80:
            reasons.append(f"Capaciteit {cap_pct}% van nominaal: licht verouderd, nog prima bruikbaar.")
            score = max(score, 1)
        elif cap_pct >= 65:
            reasons.append(f"Capaciteit {cap_pct}% van nominaal: duidelijk verouderd (onder de 80%-grens).")
            score = max(score, 2)
        else:
            reasons.append(f"Capaciteit {cap_pct}% van nominaal: sterk verouderd.")
            score = max(score, 3)
    elif not stats.get("full_discharge"):
        reasons.append("Geen volledige ontlading gemeten, dus geen capaciteitsoordeel.")

    ir = stats.get("resistance_median_mohm")
    if ir:
        if rated_ir:
            ratio = ir / rated_ir
            txt = f"Inwendige weerstand {ir} mΩ (nieuw {int(rated_ir)} mΩ, {ratio:.1f}×)"
            if ratio <= 1.3:
                reasons.append(txt + ": goed.")
            elif ratio <= 1.7:
                reasons.append(txt + ": verhoogd.")
                score = max(score, 1)
            else:
                reasons.append(txt + ": sterk verhoogd, de cel wordt warm onder belasting.")
                score = max(score, 2)
        else:
            reasons.append(f"Inwendige weerstand {ir} mΩ (geen nieuwwaarde ingesteld om mee te vergelijken).")
    else:
        reasons.append("Geen geldige weerstandsmeting (lader gaf 65535 = geen meting).")

    rise = stats.get("temp_rise_c", 0)
    if rise > TEMP_RISE_WARN_C:
        reasons.append(f"Temperatuurstijging {rise} °C: hoog, past bij verhoogde weerstand.")
        score = max(score, 2)

    ch, dis = stats.get("charged_mah"), stats.get("discharged_mah")
    if ch and dis and stats.get("full_discharge"):
        eff = ch / dis
        if eff > 1.15:
            reasons.append(f"Laad/ontlaadverhouding {eff:.2f} ({ch}/{dis} mAh): de cel neemt meer op dan ze afgeeft, "
                           f"wijst op zelfontlading.")
            score = max(score, 2)

    if outcome.startswith("FAULT: capacity protection"):
        reasons.append("Capacity protection tijdens laden: cel raakt niet vol, sterk vermoeden van een defect.")
        score = max(score, 3)
    for p in phases:
        if p["status"] == "Charging":
            ci = charge_phase_info(p, params.get("charge_cutoff_mv"))
            if ci.get("cv_duration_s", 0) > 75 * 60:
                reasons.append(f"Lange CV-staart ({hms(ci['cv_duration_s'])}): stroom zakt traag, past bij veroudering.")
                score = max(score, 1)
                break

    # Extra measurements of the quality test (HA-orchestrated steps).
    ir_std = extra.get("ir")
    if ir_std:
        base = f"IR-test bij volle cel (1 A, 15 s, na 30 min rust): {ir_std} mΩ"
        if rated_ir:
            ratio = ir_std / rated_ir
            if ratio <= 1.5:
                reasons.append(base + f" ({ratio:.1f}× datasheet): goed.")
            elif ratio <= 2.2:
                reasons.append(base + f" ({ratio:.1f}× datasheet): verhoogd.")
                score = max(score, 1)
            else:
                reasons.append(base + f" ({ratio:.1f}× datasheet): hoog.")
                score = max(score, 2)
        elif ir_std <= 60:
            reasons.append(base + ": normaal voor een 18650/21700 (DC-meting).")
        elif ir_std <= 100:
            reasons.append(base + ": verhoogd.")
            score = max(score, 1)
        else:
            reasons.append(base + ": hoog, cel zakt onder belasting sterk in.")
            score = max(score, 2)
    if extra.get("v_vol") and extra.get("v_rust"):
        drop = extra["v_vol"] - extra["v_rust"]
        txt = (f"Spanning na laden {extra['v_vol'] / 1000:.3f} V, na 30 min rust {extra['v_rust'] / 1000:.3f} V "
               f"(−{drop} mV)")
        if drop > 120:
            reasons.append(txt + ": zakt snel terug, past bij hoge weerstand of lekstroom.")
            score = max(score, 1)
        else:
            reasons.append(txt + ": normaal.")
    if extra.get("sd") is not None:
        sd = extra["sd"]
        txt = f"Zelfontlading op opslagspanning: {sd} mV in 24 uur"
        if sd <= 10:
            reasons.append(txt + ": goed.")
        elif sd <= 20:
            reasons.append(txt + ": aan de hoge kant, na een maand opnieuw meten.")
            score = max(score, 1)
        else:
            reasons.append(txt + ": te hoog, wijst op lekstroom in de cel (niet meer gebruiken).")
            score = max(score, 3)
    for n in extra.get("notes", []):
        reasons.append(n)

    label = ["Goed", "Redelijk", "Matig", "Slecht / afkeuren"][min(score, 3)]
    if not stats.get("full_discharge"):
        # Without a full discharge the capacity is unknown: never call it good,
        # but do pass on a warning sign if there was one.
        label = "Onbepaald" + ("" if score == 0 else f" (aanwijzing: {label.lower()})")
    return label, reasons


# ---------------------------------------------------------------------------
# Chart: voltage + current vs. time since program start, as a self-contained
# SVG (no matplotlib in the HA container). Phase bands, protection marker.
# ---------------------------------------------------------------------------

def render_svg(rows, phases, outcome, title, params=None, color=None):
    W, H = 960, 380
    L, R, T, B = 64, 70, 46, 46
    pw, ph = W - L - R, H - T - B
    t0 = _t(rows[0])
    pts = [((_t(r) - t0).total_seconds(), num(r["voltage_v"]), num(r["current_a"])) for r in rows]
    if len(pts) > 1500:  # keep the file small
        step = len(pts) / 1500
        pts = [pts[int(i * step)] for i in range(1500)] + [pts[-1]]
    tmax = max(p[0] for p in pts) or 1
    vs = [p[1] for p in pts if p[1] > 0.5]
    vmin = min(vs) if vs else 2.5
    vmax = max(vs) if vs else 4.3
    vmin, vmax = min(vmin, 3.0) - 0.05, max(vmax, 4.2) + 0.05
    imin = min(min(p[2] for p in pts), 0)
    imax = max(max(p[2] for p in pts), 0.5)
    pad = (imax - imin) * 0.08
    imin, imax = imin - pad, imax + pad

    x = lambda s: L + s / tmax * pw
    yv = lambda v: T + (vmax - v) / (vmax - vmin) * ph
    yi = lambda a: T + (imax - a) / (imax - imin) * ph

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
         f'font-family="Segoe UI, Roboto, sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         (f'<circle cx="{L + 7}" cy="19" r="7" fill="{escape_xml(color)}" stroke="#1b1f27" stroke-width="0.6"/>'
          f'<text x="{L + 20}" y="24" font-size="15" font-weight="600" fill="#1b1f27">{escape_xml(title)}</text>'
          if color else
          f'<text x="{L}" y="24" font-size="15" font-weight="600" fill="#1b1f27">{escape_xml(title)}</text>')]
    # Done/Standby inside a run = rest steps of a multi-step program.
    band = {"Charging": "#d8f0e2", "Discharging": "#fbe0e2", "Paused": "#eceff3", "Done": "#eceff3",
            "Standby": "#eceff3"}
    for p in phases:
        c = band.get(p["status"])
        if not c:
            continue
        a, b = (p["start"] - t0).total_seconds(), (p["end"] - t0).total_seconds()
        o.append(f'<rect x="{x(a):.1f}" y="{T}" width="{max(1, x(b) - x(a)):.1f}" height="{ph}" fill="{c}"/>')
    # grid + axes
    step = next((m * 60 for m in (5, 10, 15, 20, 30, 60, 120, 180, 240, 360) if tmax / (m * 60) <= 10), 720 * 60)
    for k in range(int(tmax // step) + 1):
        s = k * step
        xx = x(s)
        o.append(f'<line x1="{xx:.1f}" y1="{T}" x2="{xx:.1f}" y2="{T + ph}" stroke="#d6dae1" stroke-width="0.6"/>')
        o.append(f'<text x="{xx:.1f}" y="{T + ph + 16}" text-anchor="middle" fill="#5b6472">{hms(s)[:-3]}</text>')
    for k in range(6):
        v = vmin + (vmax - vmin) * k / 5
        a = imin + (imax - imin) * k / 5
        yy = yv(v)
        o.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{L + pw}" y2="{yy:.1f}" stroke="#d6dae1" stroke-width="0.6"/>')
        o.append(f'<text x="{L - 8}" y="{yy + 4:.1f}" text-anchor="end" fill="#1b6fd0">{v:.2f}</text>')
        o.append(f'<text x="{L + pw + 8}" y="{yi(a) + 4:.1f}" fill="#d9731a">{a:.2f}</text>')
    if imin < 0 < imax:
        o.append(f'<line x1="{L}" y1="{yi(0):.1f}" x2="{L + pw}" y2="{yi(0):.1f}" stroke="#d9731a" '
                 f'stroke-width="0.8" stroke-dasharray="3 3"/>')
    params = params or {}
    if params.get("charge_cutoff_mv") and vmin < params["charge_cutoff_mv"] / 1000 < vmax:
        yy = yv(params["charge_cutoff_mv"] / 1000)
        o.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{L + pw}" y2="{yy:.1f}" stroke="#1b6fd0" stroke-width="0.8" '
                 f'stroke-dasharray="6 4"/><text x="{L + 4}" y="{yy - 4:.1f}" fill="#1b6fd0">laadspanning</text>')
    if params.get("charge_end_ma") and imin < params["charge_end_ma"] / 1000 < imax:
        yy = yi(params["charge_end_ma"] / 1000)
        o.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{L + pw}" y2="{yy:.1f}" stroke="#d9731a" stroke-width="0.8" '
                 f'stroke-dasharray="6 4"/><text x="{L + pw - 4}" y="{yy - 4:.1f}" text-anchor="end" '
                 f'fill="#d9731a">eindstroom {int(params["charge_end_ma"])} mA</text>')
    o.append(f'<text x="14" y="{T + ph / 2}" fill="#1b6fd0" transform="rotate(-90 14 {T + ph / 2})" '
             f'text-anchor="middle">Spanning (V)</text>')
    o.append(f'<text x="{W - 12}" y="{T + ph / 2}" fill="#d9731a" transform="rotate(90 {W - 12} {T + ph / 2})" '
             f'text-anchor="middle">Stroom (A, − = ontladen)</text>')
    o.append(f'<text x="{L + pw / 2}" y="{H - 8}" text-anchor="middle" fill="#5b6472">Tijd sinds start (u:mm)</text>')
    vline = " ".join(f"{x(s):.1f},{yv(v):.1f}" for s, v, _ in pts if v > 0.5)
    iline = " ".join(f"{x(s):.1f},{yi(a):.1f}" for s, _, a in pts)
    o.append(f'<polyline points="{iline}" fill="none" stroke="#d9731a" stroke-width="1.6"/>')
    o.append(f'<polyline points="{vline}" fill="none" stroke="#1b6fd0" stroke-width="2"/>')
    o.append(f'<rect x="{L}" y="{T}" width="{pw}" height="{ph}" fill="none" stroke="#9aa3b2"/>')
    if outcome != "Done" and not outcome.startswith("Kwaliteitstest") and not outcome.startswith("Capaciteitstest"):
        xe = x(tmax)
        o.append(f'<line x1="{xe:.1f}" y1="{T}" x2="{xe:.1f}" y2="{T + ph}" stroke="#c23b4b" stroke-width="2"/>')
        o.append(f'<text x="{xe - 6:.1f}" y="{T + ph - 10}" text-anchor="end" fill="#c23b4b" font-weight="600">'
                 f'{escape_xml(outcome)}</text>')
    # legend
    lx = L + pw - 360
    o.append(f'<rect x="{lx}" y="8" width="12" height="12" fill="#d8f0e2"/><text x="{lx + 16}" y="18" fill="#1b1f27">laden</text>'
             f'<rect x="{lx + 60}" y="8" width="12" height="12" fill="#fbe0e2"/><text x="{lx + 76}" y="18" fill="#1b1f27">ontladen</text>'
             f'<rect x="{lx + 135}" y="8" width="12" height="12" fill="#eceff3"/><text x="{lx + 151}" y="18" fill="#1b1f27">rust</text>'
             f'<line x1="{lx + 195}" y1="14" x2="{lx + 215}" y2="14" stroke="#1b6fd0" stroke-width="2"/><text x="{lx + 219}" y="18" fill="#1b1f27">spanning</text>'
             f'<line x1="{lx + 280}" y1="14" x2="{lx + 300}" y2="14" stroke="#d9731a" stroke-width="2"/><text x="{lx + 304}" y="18" fill="#1b1f27">stroom</text>')
    o.append("</svg>")
    return "\n".join(o)


def escape_xml(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def commit_repo_file(gh_token, path, content_bytes, message):
    """Create/overwrite one file on the default branch via the contents API.
    Returns the branch name (for building a link) or None on failure."""
    import base64
    st, repo = http("GET", f"https://api.github.com/repos/{REPO}", gh_token)
    if st != 200:
        log(f"repo lookup failed ({st}), chart not stored")
        return None
    branch = repo["default_branch"]
    url = f"https://api.github.com/repos/{REPO}/contents/{urllib.parse.quote(path)}"
    st, cur = http("GET", url + f"?ref={branch}", gh_token)
    payload = {"message": message, "branch": branch,
               "content": base64.b64encode(content_bytes).decode()}
    if st == 200 and isinstance(cur, dict) and cur.get("sha"):
        payload["sha"] = cur["sha"]
    st, res = http("PUT", url, gh_token, payload)
    if st not in (200, 201):
        log(f"storing {path} failed ({st}): {res}")
        return None
    return branch

def free_tag(gh_token, tag):
    """First of tag, tag-2, tag-3 ... that has no release yet."""
    for attempt in range(5):
        t = tag if attempt == 0 else f"{tag}-{attempt + 1}"
        st, _ = http("GET", f"https://api.github.com/repos/{REPO}/releases/tags/{urllib.parse.quote(t)}", gh_token)
        if st == 404:
            return t
    raise SystemExit(f"{LOG_PREFIX} could not find a free tag for {tag}")


def publish(gh_token, tag, title, body, csv_bytes, svg_bytes=None):
    api = f"https://api.github.com/repos/{REPO}/releases"
    for attempt in range(5):
        t = tag if attempt == 0 else f"{tag}-{attempt + 1}"
        status, rel = http("POST", api, gh_token, {"tag_name": t, "name": title, "body": body})
        if status == 201:
            break
        if status == 422 and "already_exists" in str(rel):
            continue
        raise SystemExit(f"{LOG_PREFIX} GitHub release failed ({status}): {rel}")
    else:
        raise SystemExit(f"{LOG_PREFIX} could not find a free tag for {tag}")
    base = rel["upload_url"].split("{")[0]
    upload = base + "?" + urllib.parse.urlencode({"name": f"{t}.csv"})
    status, res = http("POST", upload, gh_token, csv_bytes, content_type="text/csv")
    if status != 201:
        log(f"release created, but CSV upload failed ({status}): {res}")
    if svg_bytes:
        upload = base + "?" + urllib.parse.urlencode({"name": f"{t}.svg"})
        status, res = http("POST", upload, gh_token, svg_bytes, content_type="image/svg+xml")
        if status != 201:
            log(f"release created, but chart upload failed ({status}): {res}")
    dispatch_page_build(gh_token)
    return rel.get("html_url", t)


def dispatch_page_build(gh_token):
    # A release-triggered build used to check out the tag (detached HEAD) and
    # couldn't push; a workflow_dispatch run always builds on the default branch.
    try:
        st, repo = http("GET", f"https://api.github.com/repos/{REPO}", gh_token)
        st, res = http("POST", f"https://api.github.com/repos/{REPO}/actions/workflows/reports-page.yml/dispatches",
                       gh_token, {"ref": repo["default_branch"]})
        if st != 204:
            log(f"page rebuild not started ({st}): {res}")
    except Exception as e:
        log(f"page rebuild not started: {e!r}")


def load_gh_token():
    sec = _read_yaml(SECRETS)
    gh = sec.get("mc3000_github_token") or _read_yaml(ESPHOME_SECRETS).get("github_token")
    if not gh:
        raise SystemExit(f"{LOG_PREFIX} no GitHub token found")
    return gh


PAGE_SRC = "docs/index.html"
PAGE_DST = "/config/www/mc3000/rapporten.html"


def sync_page(gh_token):
    """Download the reports overview page (built by the GitHub workflow from
    all releases) into HA's www folder, served at /local/mc3000/rapporten.html."""
    import os
    url = f"https://api.github.com/repos/{REPO}/contents/{PAGE_SRC}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github.raw",
        "User-Agent": "mc3000-report",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        html = r.read()
    os.makedirs(os.path.dirname(PAGE_DST), exist_ok=True)
    tmp = PAGE_DST + ".tmp"
    with open(tmp, "wb") as f:
        f.write(html)
    os.replace(tmp, PAGE_DST)
    log(f"synced {PAGE_SRC} -> {PAGE_DST} ({len(html)} bytes)")


TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def wait_for_page_build(gh_token, since_iso, timeout_s=40):
    """Wait (bounded, shell_command has a 60 s limit) for the reports-page
    run started after since_iso to finish. Returns its conclusion or None."""
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(4)
        st, runs = http("GET", f"https://api.github.com/repos/{REPO}/actions/workflows/reports-page.yml/runs"
                               f"?event=workflow_dispatch&per_page=1", gh_token)
        run = (runs or {}).get("workflow_runs", [None])[0] if st == 200 else None
        if run and run["created_at"] >= since_iso and run["status"] == "completed":
            return run["conclusion"]
    return None


def delete_report(gh_token, tag):
    """Delete one measurement: its GitHub release and tag, then rebuild and
    re-sync the overview page. Only touches releases that carry a measurement
    JSON block, so a real software release can't be removed this way."""
    if not TAG_RE.match(tag):
        raise SystemExit(f"{LOG_PREFIX} refusing odd tag {tag!r}")
    st, rel = http("GET", f"https://api.github.com/repos/{REPO}/releases/tags/{urllib.parse.quote(tag)}", gh_token)
    if st != 200:
        raise SystemExit(f"{LOG_PREFIX} release {tag} not found ({st})")
    if "```json" not in (rel.get("body") or ""):
        raise SystemExit(f"{LOG_PREFIX} {tag} is not a measurement release, not deleting")
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    st, res = http("DELETE", f"https://api.github.com/repos/{REPO}/releases/{rel['id']}", gh_token)
    if st != 204:
        raise SystemExit(f"{LOG_PREFIX} deleting release {tag} failed ({st}): {res}")
    st, res = http("DELETE", f"https://api.github.com/repos/{REPO}/git/refs/tags/{urllib.parse.quote(tag)}", gh_token)
    if st not in (204, 422):
        log(f"release deleted, tag {tag} not ({st}): {res}")
    log(f"deleted {tag}")
    dispatch_page_build(gh_token)
    result = wait_for_page_build(gh_token, since)
    if result == "success":
        sync_page(gh_token)
    else:
        log(f"page rebuild: {result or 'still running'} - it will show up with the next sync")


ANALYSIS_VERSION = 3


def infer_discharge_cutoff(rows, outcome=None):
    """Cutoff a past test actually used: the lowest voltage of its last
    discharge phase, if that phase was ended by the charger itself (followed
    by rest/charge/Done), rounded to 50 mV. None if it can't be told."""
    phases = split_phases(rows)
    for k in range(len(phases) - 1, -1, -1):
        if phases[k]["status"] == "Discharging":
            # The stored end of a report can lie just before the charger's own
            # "Done": a last phase still Discharging in a Done run also counts.
            nxt = phases[k + 1]["status"] if k + 1 < len(phases) else ("Done" if outcome == "Done" else None)
            if nxt not in ("Paused", "Charging", "Done"):
                return None
            low = min((num(r["voltage_v"]) for r in phases[k]["rows"] if num(r["voltage_v"]) > 0), default=0)
            return int(round(low * 1000 / 50) * 50) if low else None
    return None


def build_report(ha_url, ha_token, channel, start, end, battery, rated, rated_ir, program, outcome,
                 tag=None, note="", overrides=None, cell=None, extra=None):
    """Everything for one report: rows, analysis, chart, markdown body (with a
    {CHART} placeholder), CSV. Used for new reports and for --rebuild."""
    entities, hist = fetch_history(ha_url, ha_token, channel, start, end)
    rows = build_rows(channel, entities, hist)
    if not rows:
        return None

    num_ent = lambda n: ha_number(ha_url, ha_token, f"number.skyrc_mc3000_program_{n}")
    params = {
        "charge_cutoff_mv": num_ent("charge_cutoff_voltage"),
        "discharge_cutoff_mv": num_ent("discharge_cutoff_voltage"),
        "charge_end_ma": num_ent("charge_end_current"),
        "discharge_end_ma": num_ent("discharge_end_current"),
        "capacity_limit_mah": num_ent("capacity"),
        "time_cutoff_min": num_ent("time_cutoff"),
        "temp_cutoff_c": num_ent("temperature_cutoff"),
    }
    for k, v in (overrides or {}).items():
        if v:
            params[k] = v
    cutoff = params["discharge_cutoff_mv"]
    stats = compute_stats(rows, rated, outcome, rated_ir, int(cutoff) if cutoff else None)
    phases = split_phases(rows)
    explanation = explain_outcome(outcome, phases, params, stats, rated)
    extra = extra or {}
    verdict, reasons = battery_verdict(stats, phases, params, rated, rated_ir, outcome, extra)
    if extra.get("log"):
        explanation = explanation + [f"Verloop: {extra['log']}"]
    t_start, t_end = rows[0]["timestamp"], rows[-1]["timestamp"]
    if tag is None:
        tag = f"{t_start.replace(' ', '_').replace(':', '.')}_{sanitize(battery)}_{sanitize(program)}_Ch{channel}"
    chart_path = f"docs/reports/{tag}.svg"
    data = {"tag": tag, "battery": battery, "program": program, "channel": channel,
            "start": t_start, "end": t_end, **stats,
            "verdict": verdict, "verdict_reasons": reasons, "explanation": explanation,
            "chart": chart_path, "analysis_version": ANALYSIS_VERSION,
            "params": params, "cell": cell or None,
            "metingen": {k: v for k, v in extra.items() if k not in ("notes",)} or None}
    svg = render_svg(rows, phases, outcome,
                     f"{battery} - {program} - Ch{channel} - {t_start}", params,
                     color=(cell or {}).get("color")).encode()

    fmt = lambda v: "-" if v is None else v
    pct = lambda v: "N/A" if v is None else f"{v}%"
    cap_txt = f"{stats['capacity_mah']}" + ("" if stats["full_discharge"] else " (incomplete discharge)")
    soh = pct(stats["soh_percent"])
    if not stats["full_discharge"]:
        low = stats["discharge_min_mv"]
        soh_detail = (f"**State of Health:** N/A - no full discharge (lowest "
                      f"{fmt(low and round(low / 1000, 3))} V, cutoff {fmt(stats['discharge_cutoff_mv'])} mV)\n\n")
    else:
        soh_detail = (
            f"**State of Health:** {soh} = {int(SOH_W_CAPACITY*100)}% capacity "
            f"({pct(stats['soh_capacity_percent'])}: {fmt(stats['discharged_mah'])} of {fmt(rated)} mAh) + "
            f"{int(SOH_W_RESISTANCE*100)}% resistance "
            f"({pct(stats['soh_resistance_percent'])}: {fmt(stats['resistance_median_mohm'])} mΩ median vs "
            f"{fmt(rated_ir and int(rated_ir))} mΩ new, 0% at {SOH_IR_EOL_FACTOR:g}x)\n\n"
        )
    flags_md = "\n".join(f"- :warning: {f}" for f in stats["flags"]) or "- none"
    cell_md = ""
    if cell:
        bits = [b for b in (cell.get("type_name"), cell.get("print") and f"print \u201c{cell['print']}\u201d",
                            cell.get("note")) if b]
        cell_md = f"**Cel:** {' · '.join(bits)}  (kleur {cell.get('color', '-')}, id {cell.get('id')})\n\n"
    meas_md = ""
    labels = {"ir": ("IR-test bij volle cel", "mΩ"), "v_vol": ("Spanning direct na laden", "mV"),
              "v_rust": ("Spanning na 30 min rust", "mV"), "v_leeg": ("Spanning 30 min na ontladen", "mV"),
              "sd": ("Zelfontlading in 24 u", "mV"), "sd0": ("Opslagspanning start zelfontladingstest", "mV")}
    rows_md = [f"| {labels[k][0]} | {extra[k]} {labels[k][1]} |" for k in labels if extra.get(k) is not None]
    if rows_md:
        meas_md = "### Extra metingen\n\n| Meting | Waarde |\n|---|---|\n" + "\n".join(rows_md) + "\n\n"
    body = (
        f"**Battery:** {battery}  **Program:** {program}  **Channel:** {channel}\n\n" + cell_md +
        f"**Start:** {t_start}  **End:** {t_end}  **Result:** {outcome}\n\n"
        f"| Status | SOH | Capacity (mAh) | Charged (mAh) | Discharged (mAh) | Resistance (mΩ, peak) | Temp (°C, peak) | Cycles |\n"
        f"|---|---|---|---|---|---|---|---|\n"
        f"| {outcome} | {soh} | {cap_txt} | {fmt(stats['charged_mah'])} | {fmt(stats['discharged_mah'])} | "
        f"{fmt(stats['resistance_max_mohm'])} | {stats['temp_max_c']} | {stats['cycles']} |\n\n"
        + soh_detail +
        "{CHART}"
        f"### Conclusie: {verdict}\n\n" + "\n".join(f"- {r}" for r in reasons) + "\n\n"
        f"### Verklaring resultaat ({outcome})\n\n" + "\n".join(f"- {r}" for r in explanation) + "\n\n"
        + meas_md
        + (f"_{note}_\n\n" if note else "") +
        f"**Flags:**\n{flags_md}\n\n"
        f"<details><summary>Raw data (JSON, read by the dashboard)</summary>\n\n"
        f"```json\n{json.dumps(data, indent=2)}\n```\n</details>\n"
    )
    title = tag.replace("_", " ") + (" [!]" if stats["flags"] else "")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return {"rows": rows, "tag": tag, "title": title, "body": body, "svg": svg,
            "csv": buf.getvalue().encode(), "chart_path": chart_path}


def chart_markdown(gh_token, chart_path, svg, tag):
    """Store the chart in the repo (docs/reports/) so the release body can embed
    it and the overview page can inline it. Returns the markdown section."""
    branch = commit_repo_file(gh_token, chart_path, svg, f"Report chart {tag} [skip ci]")
    if not branch:
        return ""
    return (f"### Spanning en stroom\n\n![Spanning en stroom](https://github.com/{REPO}/blob/{branch}/"
            f"{urllib.parse.quote(chart_path)}?raw=true)\n\n")


def list_measurement_releases(gh_token):
    out, page = [], 1
    while True:
        st, rels = http("GET", f"https://api.github.com/repos/{REPO}/releases?per_page=100&page={page}", gh_token)
        if st != 200 or not rels:
            break
        for rel in rels:
            m = re.search(r"```json\s*(\{.*?\})\s*```", rel.get("body") or "", re.DOTALL)
            if not m:
                continue
            try:
                out.append((rel, json.loads(m.group(1))))
            except json.JSONDecodeError:
                pass
        if len(rels) < 100:
            break
        page += 1
    return out


def rebuild(ha_url, ha_token, gh_token, only_tag=None, force=False):
    """Recompute analysis + chart for existing measurement releases from HA's
    history and update them in place (same tag; body, title, chart asset)."""
    done = skipped = failed = 0
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for rel, d in list_measurement_releases(gh_token):
        tag = rel["tag_name"]
        if only_tag and tag != only_tag:
            continue
        if not force and d.get("analysis_version", 0) >= ANALYSIS_VERSION:
            skipped += 1
            continue
        try:
            fmt_in = "%Y-%m-%d %H:%M:%S"
            start = datetime.strptime(d["start"], fmt_in).astimezone()
            end = datetime.strptime(d["end"], fmt_in).astimezone()
            ch = int(d["channel"])
            rated = d.get("rated_capacity_mah") or None
            rated_ir = d.get("rated_resistance_mohm") or None
            # The discharge cutoff decides "full discharge" (and so SOH): use the
            # one the test itself ran with, not today's setting.
            # Measured first (the stored value of an earlier rebuild can be
            # today's setting), the stored one only as fallback.
            ent, hist = fetch_history(ha_url, ha_token, ch, start, end + timedelta(minutes=5))
            cut = infer_discharge_cutoff(build_rows(ch, ent, hist), d.get("status")) or d.get("discharge_cutoff_mv")
            rep = build_report(ha_url, ha_token, ch, start, end + timedelta(seconds=1), d.get("battery", "Unknown"),
                               rated, rated_ir, d.get("program", "Unknown"), d.get("status", "Done"), tag=tag,
                               overrides={"discharge_cutoff_mv": cut},
                               cell=d.get("cell"), extra=d.get("metingen"),
                               note=f"Analyse en grafiek opnieuw opgebouwd op {stamp} uit de HA-geschiedenis; "
                                    f"ontlaadgrens is die van de test zelf, de overige programma-instellingen in de "
                                    f"verklaring zijn die van dat moment.")
            if not rep:
                log(f"{tag}: no history left, skipped")
                skipped += 1
                continue
            body = rep["body"].replace("{CHART}", chart_markdown(gh_token, rep["chart_path"], rep["svg"], tag))
            st, res = http("PATCH", f"https://api.github.com/repos/{REPO}/releases/{rel['id']}", gh_token,
                           {"body": body, "name": rep["title"]})
            if st != 200:
                raise RuntimeError(f"PATCH {st}: {res}")
            names = {a["name"]: a for a in rel.get("assets", [])}
            if f"{tag}.svg" in names:
                http("DELETE", f"https://api.github.com/repos/{REPO}/releases/assets/{names[tag + '.svg']['id']}",
                     gh_token)
            up = rel["upload_url"].split("{")[0] + "?" + urllib.parse.urlencode({"name": f"{tag}.svg"})
            http("POST", up, gh_token, rep["svg"], content_type="image/svg+xml")
            done += 1
            log(f"{tag}: rebuilt ({len(rep['rows'])} rows)")
        except Exception as e:
            failed += 1
            log(f"{tag}: FAILED {e!r}")
    log(f"rebuild finished: {done} rebuilt, {skipped} skipped, {failed} failed")
    if done:
        since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        dispatch_page_build(gh_token)
        if wait_for_page_build(gh_token, since, timeout_s=300) == "success":
            sync_page(gh_token)


DEFAULT_HA_URL = _default_ha_url()

# ---------------------------------------------------------------------------
# Cell database (sensor.mc3000_cellen, kept by the dashboard card through
# script.mc3000_db_opslaan), local report index for the dashboard, backup.
# ---------------------------------------------------------------------------

DB_ENTITY = "sensor.mc3000_cellen"
DB_BACKUP = "/config/mc3000_cellen.json"
LOCAL_DIR = "/config/www/mc3000"


def ha_state(ha_url, ha_token, entity_id):
    try:
        st, data = http("GET", f"{ha_url}/api/states/{entity_id}", ha_token)
    except RuntimeError as e:
        log(f"could not read {entity_id}: {e}")
        return None
    return data if st == 200 and isinstance(data, dict) else None


def lookup_cell(ha_url, ha_token, channel=None, cell_id=None):
    """Cell variant + its type from the dashboard's cell database, as stored
    in the report: {id, type, type_name, print, note, color}. None if unknown."""
    if not cell_id and channel:
        st = ha_state(ha_url, ha_token, f"input_text.mc3000_ch{channel}_cel")
        cell_id = st and st.get("state")
    if not cell_id or cell_id in ("unknown", "unavailable"):
        return None
    db = (ha_state(ha_url, ha_token, DB_ENTITY) or {}).get("attributes") or {}
    c = (db.get("cells") or {}).get(cell_id)
    if not c:
        return None
    t = (db.get("types") or {}).get(c.get("type")) or {}
    return {"id": cell_id, "type": c.get("type"), "type_name": t.get("name"), "print": c.get("print") or "",
            "note": c.get("note") or "", "color": c.get("color") or "#888888"}


def parse_extra(txt):
    """'ir=45;v_vol=4195;notes=a|b' -> dict with ints where possible."""
    out = {}
    for part in (txt or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or v == "":
            continue
        if k == "notes":
            out["notes"] = [n for n in v.split("|") if n]
        elif k == "log":
            out["log"] = v.replace("|", " → ")
        else:
            try:
                out[k] = int(float(v))
            except ValueError:
                out[k] = v
    return out


def backup_db(ha_url, ha_token):
    import os
    st = ha_state(ha_url, ha_token, DB_ENTITY)
    attrs = (st or {}).get("attributes") or {}
    if not attrs.get("types"):
        log("cell database empty, backup not overwritten")
        return
    data = {"types": attrs.get("types"), "cells": attrs.get("cells") or {}, "saved": datetime.now().isoformat()}
    tmp = DB_BACKUP + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DB_BACKUP)
    log(f"cell database saved to {DB_BACKUP} ({len(data['cells'])} cells, {len(data['types'])} types)")


def restore_db(ha_url, ha_token):
    with open(DB_BACKUP, encoding="utf-8") as f:
        data = json.load(f)
    st, res = http("POST", f"{ha_url}/api/events/mc3000_db_set", ha_token,
                   {"types": data["types"], "cells": data.get("cells") or {}})
    if st != 200:
        raise SystemExit(f"{LOG_PREFIX} restore failed ({st}): {res}")
    log(f"cell database restored from {DB_BACKUP}")


def gh_raw(gh_token, path):
    url = f"https://api.github.com/repos/{REPO}/contents/{urllib.parse.quote(path)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {gh_token}",
                                               "Accept": "application/vnd.github.raw",
                                               "User-Agent": "mc3000-report"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def export_local(gh_token):
    """reports.json + charts in /config/www/mc3000 for the dashboard card
    (report pop-ups), built from the measurement releases on GitHub."""
    import os
    os.makedirs(f"{LOCAL_DIR}/reports", exist_ok=True)
    out = []
    for rel, d in list_measurement_releases(gh_token):
        tag = rel["tag_name"]
        e = dict(d)
        e["tag"] = tag
        e["url"] = rel.get("html_url")
        svg_local = f"{LOCAL_DIR}/reports/{sanitize(tag)}.svg"
        if not os.path.exists(svg_local) or os.path.getmtime(svg_local) < datetime.fromisoformat(
                (rel.get("updated_at") or rel.get("published_at") or "1970-01-01T00:00:00Z").replace("Z", "+00:00")
        ).timestamp():
            try:
                with open(svg_local, "wb") as f:
                    f.write(gh_raw(gh_token, d.get("chart") or f"docs/reports/{tag}.svg"))
            except Exception as ex:
                log(f"{tag}: chart not fetched ({ex!r})")
        e["svg"] = f"/local/mc3000/reports/{sanitize(tag)}.svg" if os.path.exists(svg_local) else None
        out.append(e)
    out.sort(key=lambda e: e.get("end") or e.get("start") or "", reverse=True)
    tmp = f"{LOCAL_DIR}/reports.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.now().isoformat(timespec="seconds"), "reports": out}, f,
                  ensure_ascii=False)
    os.replace(tmp, f"{LOCAL_DIR}/reports.json")
    log(f"exported {len(out)} reports -> {LOCAL_DIR}/reports.json")


def push_dir(gh_token, src):
    """Commit the files listed in <src>/manifest.txt ('<local file> <repo path>'
    per line) as one commit on the default branch; message from msg.txt."""
    import base64
    import os
    import time
    # Optional <src>/repo.txt: target "owner/name" (line 1) and "public" (line 2);
    # a missing target repo is created (with an initial commit).
    target, public = REPO, False
    if os.path.exists(os.path.join(src, "repo.txt")):
        lines = [x.strip() for x in open(os.path.join(src, "repo.txt"), encoding="utf-8") if x.strip()]
        target, public = lines[0], len(lines) > 1 and lines[1].lower() == "public"
    st, repo = http("GET", f"https://api.github.com/repos/{target}", gh_token)
    if st == 404:
        st, repo = http("POST", "https://api.github.com/user/repos", gh_token,
                        {"name": target.split("/")[1], "private": not public, "auto_init": True,
                         "description": "SkyRC MC3000 battery charger in Home Assistant: ESP32 BLE bridge, "
                                        "dashboard, cell database, quality test and reports"})
        print(f"create {target} {st} {repo.get('html_url') if isinstance(repo, dict) else repo}")
        time.sleep(3)
        st, repo = http("GET", f"https://api.github.com/repos/{target}", gh_token)
    branch = repo["default_branch"]
    api = f"https://api.github.com/repos/{target}/git"
    st, ref = http("GET", f"{api}/ref/heads/{branch}", gh_token)
    parent = ref["object"]["sha"]
    st, pc = http("GET", f"{api}/commits/{parent}", gh_token)
    tree = []
    with open(os.path.join(src, "manifest.txt"), encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            local, path = line.split(None, 1)
            with open(os.path.join(src, local), "rb") as fh:
                content = fh.read()
            st, blob = http("POST", f"{api}/blobs", gh_token,
                            {"content": base64.b64encode(content).decode(), "encoding": "base64"})
            print(f"blob {path.strip()} {st} {blob.get('sha') if isinstance(blob, dict) else blob}")
            tree.append({"path": path.strip(), "mode": "100644", "type": "blob", "sha": blob["sha"]})
    st, t = http("POST", f"{api}/trees", gh_token, {"base_tree": pc["tree"]["sha"], "tree": tree})
    print(f"tree {st} {t.get('sha')}")
    with open(os.path.join(src, "msg.txt"), encoding="utf-8") as f:
        msg = f.read()
    st, c = http("POST", f"{api}/commits", gh_token, {"message": msg, "tree": t["sha"], "parents": [parent]})
    print(f"commit {st} {c.get('sha')}")
    st, r = http("PATCH", f"{api}/refs/heads/{branch}", gh_token, {"sha": c["sha"]})
    print(f"ref {st} {r.get('object', {}).get('sha') if isinstance(r, dict) else r}")
    print(f"parent {parent}")


def main():
    if "--delete" in sys.argv:
        gh = load_gh_token()
        delete_report(gh, sys.argv[sys.argv.index("--delete") + 1])
        return
    if "--sync-page" in sys.argv:
        gh = load_gh_token()
        sync_page(gh)
        export_local(gh)
        return
    if "--export" in sys.argv:
        export_local(load_gh_token())
        return
    if "--push" in sys.argv:
        push_dir(load_gh_token(), sys.argv[sys.argv.index("--push") + 1])
        return
    if "--backup-db" in sys.argv:
        backup_db(DEFAULT_HA_URL, load_secrets()[0])
        return
    if "--restore-db" in sys.argv:
        restore_db(DEFAULT_HA_URL, load_secrets()[0])
        return
    if "--rebuild-all" in sys.argv or "--rebuild" in sys.argv:
        ha_token, gh_token = load_secrets()
        only = sys.argv[sys.argv.index("--rebuild") + 1] if "--rebuild" in sys.argv else None
        rebuild(DEFAULT_HA_URL, ha_token, gh_token, only_tag=only, force="--force" in sys.argv)
        return

    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, required=True, choices=[1, 2, 3, 4])
    ap.add_argument("--start", required=True, help="ISO datetime the test started")
    ap.add_argument("--end", default=None, help="ISO datetime, default now")
    ap.add_argument("--battery", default="Unknown")
    ap.add_argument("--rated", type=float, default=0, help="rated mAh, 0 = unknown")
    ap.add_argument("--rated-ir", type=float, default=0,
                    help="as-new internal resistance in mOhm; default: the channel's HA helper")
    ap.add_argument("--program", default="Unknown")
    ap.add_argument("--outcome", default="Done")
    ap.add_argument("--cell", default="", help="cell id in the dashboard database; default: the channel's cell")
    ap.add_argument("--extra", default="", help="extra measurements 'ir=45;v_vol=4190;sd=6;log=a|b'")
    ap.add_argument("--note", default="")
    # HA serves HTTPS on 8123 (DuckDNS cert); plain http://127.0.0.1:8123 gets
    # the connection dropped (RemoteDisconnected). Use the name the cert is for.
    ap.add_argument("--ha-url", default=DEFAULT_HA_URL)
    ap.add_argument("--dry-run", action="store_true", help="print the report, don't publish")
    a = ap.parse_args()

    ha_token, gh_token = load_secrets()
    start = datetime.fromisoformat(a.start).astimezone()
    end = datetime.fromisoformat(a.end).astimezone() if a.end else datetime.now(timezone.utc).astimezone()
    rated = int(a.rated) or None
    rated_ir = a.rated_ir or ha_number(a.ha_url, ha_token, f"input_number.mc3000_ch{a.channel}_nominale_weerstand")

    # Free tag first: the body and chart path carry the final tag.
    cell = lookup_cell(a.ha_url, ha_token, a.channel, a.cell or None)
    extra = parse_extra(a.extra)
    kw = {"cell": cell, "extra": extra, "note": a.note}
    probe = build_report(a.ha_url, ha_token, a.channel, start, end, a.battery, rated, rated_ir,
                         a.program, a.outcome, **kw)
    if not probe:
        raise SystemExit(f"{LOG_PREFIX} no data for channel {a.channel} between {start} and {end}")
    tag = probe["tag"] if a.dry_run else free_tag(gh_token, probe["tag"])
    rep = probe if tag == probe["tag"] else build_report(
        a.ha_url, ha_token, a.channel, start, end, a.battery, rated, rated_ir, a.program, a.outcome, tag=tag, **kw)

    if a.dry_run:
        print(rep["body"].replace("{CHART}", f"[chart: {len(rep['svg'])} bytes SVG]\n\n"))
        import os
        os.makedirs("/config/www/mc3000/charts", exist_ok=True)
        with open(f"/config/www/mc3000/charts/{tag}.svg", "wb") as f:
            f.write(rep["svg"])
        log(f"{len(rep['rows'])} rows, tag {tag} (dry run, chart -> /local/mc3000/charts/{tag}.svg, nothing published)")
        return
    body = rep["body"].replace("{CHART}", chart_markdown(gh_token, rep["chart_path"], rep["svg"], tag))
    url = publish(gh_token, tag, rep["title"], body, rep["csv"], rep["svg"])
    log(f"published {url} ({len(rep['rows'])} rows)")
    try:  # the dashboard's report pop-up reads the local index
        export_local(gh_token)
    except Exception as e:
        log(f"local export failed: {e!r}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # make sure HA's shell_command log shows *something*
        import traceback
        log(f"failed: {e!r}")
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)
