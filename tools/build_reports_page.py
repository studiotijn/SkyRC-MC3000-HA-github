"""
Build the static GitHub Pages dashboard for historical MC3000 test reports,
from this repo's GitHub Releases (see publish_report.py - one release per
battery/channel measurement).

Run by .github/workflows/pages.yml whenever a release is published, edited,
or deleted - deleting a release and letting this re-run is how a measurement
gets removed from the dashboard (no separate "delete" tooling needed; the
dashboard always just reflects whatever releases currently exist).

Each release's body is expected to end with a fenced ```json block with the
structured fields `publish_report.py` writes - releases without one (e.g. a
future actual software-version release, unrelated to measurements) are
skipped.

Inside Home Assistant (the page is served from /config/www and shown in an
iframe on the MC3000 dashboard) every row gets a delete button: it calls
shell_command.mc3000_delete_report through the logged-in HA session, which
deletes the release and rebuilds this page. On GitHub the button is hidden.

Usage: python build_reports_page.py [output_dir]   (default: public/)
"""

import html
import json
import re
import subprocess
import sys
from pathlib import Path

JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def fetch_releases() -> list[dict]:
    out = subprocess.run(
        ["gh", "api", "--paginate", "repos/{owner}/{repo}/releases"],
        capture_output=True, text=True, check=True,
    ).stdout
    # --paginate with multiple pages concatenates JSON arrays back-to-back;
    # handle both the single-page and multi-page case.
    text = out.strip()
    if not text:
        return []
    decoder = json.JSONDecoder()
    results = []
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \n\r\t":
            idx += 1
        if idx >= len(text):
            break
        obj, end = decoder.raw_decode(text, idx)
        results.extend(obj)
        idx = end
    return results


def extract_data(release: dict) -> dict | None:
    m = JSON_BLOCK_RE.search(release.get("body") or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    data["html_url"] = release["html_url"]
    return data


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SkyRC MC3000 - Measurement History</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --border: #2a2f3a; --text: #e6e8eb;
    --muted: #9aa3b2; --accent: #5aa9ff; --good: #4caf7d; --bad: #e0616b;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f7f8fa; --panel:#ffffff; --border:#e2e5ea; --text:#1b1f27; --muted:#5b6472; --accent:#1b6fd0; --good:#1f9d63; --bad:#c23b4b; }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
  }
  header { padding: 2rem 1.5rem 1rem; max-width: 1200px; margin: 0 auto; }
  h1 { margin: 0 0 0.25rem; font-size: 1.5rem; }
  .sub { color: var(--muted); font-size: 0.9rem; }
  main { max-width: 1200px; margin: 0 auto; padding: 0 1.5rem 3rem; }
  .tabs { display: flex; gap: 0.5rem; margin: 1rem 0; }
  .tab {
    padding: 0.5rem 1rem; border: 1px solid var(--border); border-radius: 8px;
    background: var(--panel); color: var(--text); cursor: pointer; font-size: 0.9rem;
  }
  .tab.active { border-color: var(--accent); color: var(--accent); }
  table { width: 100%%; border-collapse: collapse; background: var(--panel); border-radius: 10px; overflow: hidden; }
  td:nth-child(1), td:nth-child(2), td:nth-child(3) { white-space: nowrap; }
  th, td { padding: 0.5rem 0.55rem; text-align: left; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
  th { color: var(--muted); font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  a.row-link { color: var(--accent); text-decoration: none; }
  a.row-link:hover { text-decoration: underline; }
  .status-done { color: var(--good); }
  .status-fault { color: var(--bad); }
  .flag { margin-left: 0.4em; cursor: help; }
  .muted { color: var(--muted); font-size: 0.8rem; white-space: nowrap; }
  /* Rows cycle through 4 clearly different colours (one per row, repeating
     every 4 rows - one MC3000 batch); the hue is set per row as --h. */
  tbody tr { background: hsla(var(--h), 75%%, 55%%, 0.18); box-shadow: inset 5px 0 0 hsl(var(--h), 75%%, 55%%); }
  tbody tr:hover { background: hsla(var(--h), 75%%, 55%%, 0.30); }
  tr.deleting { opacity: 0.35; pointer-events: none; }
  .del { background: none; border: 0; padding: 0.2rem 0.35rem; border-radius: 6px; cursor: pointer;
         color: var(--muted); line-height: 0; }
  .del:hover { color: var(--bad); background: rgba(224, 97, 107, 0.12); }
  .del svg { width: 18px; height: 18px; }
  .ha-only { display: none; }
  body.in-ha .ha-only { display: table-cell; }
  .toast { position: fixed; bottom: 1rem; left: 50%%; transform: translateX(-50%%); background: var(--panel);
           border: 1px solid var(--border); border-radius: 8px; padding: 0.6rem 1rem; font-size: 0.9rem; display: none; }
  .battery-group { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 1rem; overflow: hidden; }
  .battery-group h2 { margin: 0; padding: 0.8rem 1rem; font-size: 1rem; border-bottom: 1px solid var(--border); }
  .empty { color: var(--muted); padding: 2rem; text-align: center; }
  tr.detail { display: none; box-shadow: none; }
  tr.detail.open { display: table-row; }
  tr.detail td { background: var(--panel); padding: 0.8rem 1rem 1.2rem; white-space: normal; }
  tr.detail svg { max-width: 100%%; height: auto; border-radius: 8px; display: block; margin: 0.5rem 0 0.8rem; }
  tr.detail h3 { margin: 0.6rem 0 0.3rem; font-size: 0.95rem; }
  tr.detail ul { margin: 0.2rem 0 0.4rem 1.1rem; padding: 0; }
  tr.detail li { margin: 0.15rem 0; font-size: 0.88rem; }
  tbody tr[data-tag]:not(.detail) { cursor: pointer; }
  .verdict { font-weight: 600; }
  .v-goed { color: var(--good); } .v-slecht { color: var(--bad); }
  .view { display: none; }
  .view.active { display: block; overflow-x: auto; }
</style>
</head>
<body>
<header>
  <h1>SkyRC MC3000 - Measurement History</h1>
  <div class="sub">%(count)d measurement(s) - generated %(generated)s - delete a measurement by deleting its release on GitHub (this page regenerates automatically)</div>
</header>
<main>
  <div class="tabs">
    <button class="tab active" data-view="table">Table</button>
    <button class="tab" data-view="battery">Per battery</button>
  </div>

  <div class="view active" id="view-table">
    %(table_html)s
  </div>

  <div class="view" id="view-battery">
    %(battery_html)s
  </div>
</main>
<div class="toast" id="toast"></div>
<script>
  // Delete support only when served by Home Assistant (same origin as the HA
  // frontend): use the parent dashboard's live connection, else the stored
  // HA login token for a plain REST call.
  function haParent() {
    try { var ha = window.parent && window.parent.document.querySelector('home-assistant'); return ha && ha.hass ? ha.hass : null; }
    catch (e) { return null; }
  }
  function haToken() {
    try { var t = JSON.parse(localStorage.getItem('hassTokens') || 'null'); return t && t.access_token; }
    catch (e) { return null; }
  }
  var canDelete = location.pathname.indexOf('/local/') === 0 && (haParent() || haToken());
  if (canDelete) document.body.classList.add('in-ha');

  function toast(msg) {
    var t = document.getElementById('toast'); t.textContent = msg; t.style.display = 'block';
  }
  function callDelete(tag) {
    var hass = haParent();
    if (hass) return hass.callService('shell_command', 'mc3000_delete_report', { tag: tag });
    return fetch('/api/services/shell_command/mc3000_delete_report', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + haToken(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ tag: tag })
    }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); });
  }
  document.querySelectorAll('.del').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var tag = btn.dataset.tag;
      if (!confirm('Meting verwijderen?\\n\\n' + tag + '\\n\\nDe GitHub-release wordt definitief verwijderd.')) return;
      document.querySelectorAll('tr[data-tag="' + tag + '"]').forEach(function (r) { r.classList.add('deleting'); });
      toast('Verwijderen en pagina opnieuw opbouwen... (tot ~45 s)');
      callDelete(tag).then(function () {
        location.replace(location.pathname + '?t=' + Date.now());
      }, function (err) {
        document.querySelectorAll('tr[data-tag="' + tag + '"]').forEach(function (r) { r.classList.remove('deleting'); });
        toast('Verwijderen mislukt: ' + err.message);
      });
    });
  });

  document.querySelectorAll('tbody tr[data-tag]:not(.detail)').forEach(function (row) {
    row.addEventListener('click', function (ev) {
      if (ev.target.closest('a, button')) return;
      var d = row.nextElementSibling;
      if (d && d.classList.contains('detail')) d.classList.toggle('open');
    });
  });

  document.querySelectorAll('.tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      document.querySelectorAll('.tab').forEach(function (t) { t.classList.remove('active'); });
      document.querySelectorAll('.view').forEach(function (v) { v.classList.remove('active'); });
      tab.classList.add('active');
      document.getElementById('view-' + tab.dataset.view).classList.add('active');
    });
  });
</script>
</body>
</html>
"""


def status_class(status: str) -> str:
    if status == "Done":
        return "status-done"
    if status.startswith("FAULT"):
        return "status-fault"
    return ""


TRASH_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M9 3v1H4v2h1v13a2 2 0 0 0 2 2h10a2 '
    '2 0 0 0 2-2V6h1V4h-5V3H9zm-2 3h10v13H7V6zm2 2v9h2V8H9zm4 0v9h2V8h-2z"/></svg>'
)
ROW_HUES = (210, 140, 35, 280)  # blue, green, amber, purple - repeats every 4 rows


def row_open(e: dict, i: int) -> str:
    tag = e.get("tag", "")
    return f'<tr data-tag="{tag}" style="--h: {ROW_HUES[i % len(ROW_HUES)]}">'


def delete_cell(e: dict) -> str:
    tag = e.get("tag", "")
    if not tag:
        return '<td class="ha-only"></td>'
    return (f'<td class="ha-only"><button class="del" data-tag="{tag}" title="Meting verwijderen">'
            f"{TRASH_SVG}</button></td>")


def full_discharge(e: dict) -> bool:
    """Capacity/SOH only count when the discharge reached the cutoff. Newer
    reports record that; for older ones, require a 'Done' test that had a
    discharge phase."""
    if "full_discharge" in e:
        return bool(e["full_discharge"])
    return e.get("status") == "Done" and e.get("discharged_mah") is not None


def capacity_cell(e: dict) -> str:
    cap = e.get("capacity_mah")
    if full_discharge(e):
        return f"{cap}"
    return f'{cap} <span class="muted">(incomplete discharge)</span>'


# The charger sends 65535 when it has no IR reading. TEMPORARY display-side
# correction for reports stored before tools/ha_report.py filtered it: hide the
# value and recompute SOH from capacity alone. Remove once the firmware handles
# it at the source.
IR_INVALID = 65535


def ir_invalid(e: dict) -> bool:
    return (e.get("resistance_median_mohm") or 0) >= IR_INVALID or \
        (e.get("resistance_max_mohm") or 0) >= IR_INVALID


def soc_cell(e: dict) -> str:
    if not full_discharge(e):
        return "-"
    soc = e.get("soh_percent", e.get("soc_percent"))
    if ir_invalid(e) and e.get("soh_capacity_percent") is not None:
        soc = e["soh_capacity_percent"]
    return f"{soc}%" if soc is not None else "-"


def flag_cell(e: dict) -> str:
    flags = e.get("flags") or []
    if not flags:
        return ""
    return f'<span class="flag" title="{"; ".join(flags)}">&#9888;</span>'


def shared_cells(e: dict) -> str:
    resistance = e.get("resistance_max_mohm", e.get("resistance_mohm", 0))
    if resistance is None or resistance >= IR_INVALID:
        resistance = '<span class="muted">geen meting</span>'
    temp = e.get("temp_max_c", e.get("temp_c", 0))
    return (
        f"<td class=\"{status_class(e['status'])}\">{e['status']}{flag_cell(e)}</td>"
        f"<td>{soc_cell(e)}</td>"
        f"<td>{capacity_cell(e)}</td><td>{resistance}</td>"
        f"<td>{temp}</td><td>{e['cycles']}</td>"
    )


REPORTS_DIR = Path(__file__).resolve().parents[1] / "docs" / "reports"


def md_inline(text: str) -> str:
    """The analysis bullets use **bold** only; escape everything else."""
    text = html.escape(str(text))
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def verdict_cell(e: dict) -> str:
    v = e.get("verdict")
    if not v:
        return "<td>-</td>"
    cls = "v-goed" if v.startswith("Goed") else ("v-slecht" if v.startswith(("Slecht", "Matig")) else "")
    return f'<td class="verdict {cls}">{html.escape(v)}</td>'


def detail_row(e: dict, colspan: int) -> str:
    """Hidden row under each measurement: chart + conclusion + explanation.
    Opened by clicking the measurement row."""
    parts = []
    svg_path = REPORTS_DIR / f"{e.get('tag', '')}.svg"
    if e.get("tag") and svg_path.is_file():
        parts.append(svg_path.read_text(encoding="utf-8"))
    if e.get("verdict"):
        parts.append(f"<h3>Conclusie: {html.escape(e['verdict'])}</h3><ul>"
                     + "".join(f"<li>{md_inline(r)}</li>" for r in e.get("verdict_reasons") or []) + "</ul>")
    if e.get("explanation"):
        parts.append(f"<h3>Verklaring resultaat ({html.escape(e['status'])})</h3><ul>"
                     + "".join(f"<li>{md_inline(r)}</li>" for r in e["explanation"]) + "</ul>")
    if not parts:
        parts.append('<span class="muted">Geen grafiek of analyse voor deze (oudere) meting.</span>')
    return f'<tr class="detail" data-tag="{e.get("tag", "")}"><td colspan="{colspan}">{"".join(parts)}</td></tr>'


def render_table(entries: list[dict]) -> str:
    if not entries:
        return '<div class="empty">No measurements yet.</div>'
    rows = []
    for i, e in enumerate(entries):
        rows.append(
            row_open(e, i) +
            f"<td><a class=\"row-link\" href=\"{e['html_url']}\">{e['start']}</a></td>"
            f"<td>{e['end']}</td><td>{e['battery']}</td><td>{e['program']}</td>"
            f"<td>Ch{e['channel']}</td>"
            + shared_cells(e) + verdict_cell(e) + delete_cell(e) +
            "</tr>" + detail_row(e, 13)
        )
    return (
        "<table><thead><tr>"
        "<th>Start</th><th>End</th><th>Battery</th><th>Program</th><th>Ch</th>"
        "<th>Status</th><th>SOH</th><th>Capacity (mAh)</th><th>Resistance (mΩ, peak)</th>"
        "<th>Temp (°C, peak)</th><th>Cycles</th><th>Conclusie</th><th class=\"ha-only\"></th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_by_battery(entries: list[dict]) -> str:
    if not entries:
        return '<div class="empty">No measurements yet.</div>'
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(e["battery"], []).append(e)

    sections = []
    for battery in sorted(groups, key=str.lower):
        rows = []
        for i, e in enumerate(sorted(groups[battery], key=lambda e: e["start"], reverse=True)):
            rows.append(
                row_open(e, i) +
                f"<td><a class=\"row-link\" href=\"{e['html_url']}\">{e['start']}</a></td>"
                f"<td>{e['end']}</td><td>{e['program']}</td><td>Ch{e['channel']}</td>"
                + shared_cells(e) + verdict_cell(e) + delete_cell(e) +
                "</tr>" + detail_row(e, 12)
            )
        table = (
            "<table><thead><tr>"
            "<th>Start</th><th>End</th><th>Program</th><th>Ch</th>"
            "<th>Status</th><th>SOH</th><th>Capacity (mAh)</th><th>Resistance (mΩ, peak)</th>"
            "<th>Temp (°C, peak)</th><th>Cycles</th><th>Conclusie</th><th class=\"ha-only\"></th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
        sections.append(f'<div class="battery-group"><h2>{battery}</h2>{table}</div>')
    return "".join(sections)


def build_page(entries: list[dict]) -> str:
    """The whole page for a list of report entries (newest first). Also used by
    ha_report.py for local report storage (entries from local JSON files)."""
    import datetime
    for e in entries:
        e.setdefault("html_url", e.get("url") or "#")
    return PAGE_TEMPLATE % {
        "count": len(entries),
        "generated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "table_html": render_table(entries),
        "battery_html": render_by_battery(entries),
    }


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("public")
    out_dir.mkdir(parents=True, exist_ok=True)

    releases = fetch_releases()
    entries = [e for e in (extract_data(r) for r in releases) if e]
    # Newest first; within the same start minute by channel, so one 4-channel
    # batch lines up with one pass of the row colour gradient.
    entries.sort(key=lambda e: (e["start"][:16], -int(e.get("channel", 0))), reverse=True)

    html = build_page(entries)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    print(f"Wrote {out_dir / 'index.html'} with {len(entries)} measurement(s).")


if __name__ == "__main__":
    main()
