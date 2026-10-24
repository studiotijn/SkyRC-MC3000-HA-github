# Home Assistant: dashboard "Batterijlader"

Everything Home Assistant needs for the MC3000 besides the ESPHome device.

| File | Goes to | What |
|---|---|---|
| `packages/mc3000.yaml` | `/config/packages/mc3000.yaml` | shell commands, cell database sensor, per-channel helpers, scripts, the program-step automation |
| `custom_templates/mc3000.jinja` | `/config/custom_templates/mc3000.jinja` | charger values for a cell + program (macro `mc3000_params`) |
| `www/mc3000-card.js` | `/config/www/mc3000/mc3000-card.js` | dashboard card, resource `/local/mc3000/mc3000-card.js?v=<VERSION>` (module) |
| `www/rapporten-live.html` | `/config/www/mc3000/rapporten-live.html` | loader for the reports table (iframe on the Rapporten tab) |
| `dashboard.yaml` | dashboard raw config | the dashboard tabs |
| `../tools/ha_report.py` | `/config/py_scripts/mc3000_report.py` | GitHub reports, `reports.json` export, cell DB backup, push |

## Dashboard

Views (tabs), each a `panel` view with one `custom:mc3000-card`:

- `view: overzicht` - the four slots side by side (sized for a phone). Tap a slot: pick a
  favourite cell. Tap the program button: start a program, or follow / stop the running one.
  Tap the result block: the last report of that channel (chart, measured and set values,
  verdict, explanation, earlier reports).
- `view: cellen` - cell database. Types hold the datasheet defaults (chemistry, capacity,
  standard charge current, max discharge, charge/cutoff/storage voltage, IR). Cells are the
  physical variants: type + print + note + colour + favourite. "Afsplitsen" copies a cell.
- `view: programmas` - every program with the values it sends for a chosen cell.

A separate Rapporten tab shows the table of all reports (iframe). Card options: `reports_path` (view path of that tab, default `rapporten`) and `reports_url` (link to your releases).

## Programs

Single charger runs (laden, ontladen, opslag, cyclus, refresh, IR-test) go to the charger in
one go via `script.mc3000_toepassen`. The ESP has one shared set of program entities, so the
script sets them for the cell, targets only that channel and presses apply (queued, one
channel at a time).

Multi-step programs are run by the automation `MC3000 programmaverloop`:

- **Kwaliteitstest**: charge, 30 min rest, discharge 0.2C (capacity), 30 min rest, charge,
  30 min rest, IR test at full charge, storage voltage, 1 h rest + 24 h self-discharge
  (optional, `input_boolean.mc3000_zelfontlading`), one report.
- **Capaciteitstest**: C>D (charger cycle), storage voltage of that cell, one report.

On a fault: temperature -> cool down to 32 °C and resume with 25 % less current (2x);
time protection -> 15 min pause and resume once, then carry on; capacity protection -> no
more charging, rest/self-discharge test, storage; supply fault -> 15 min pause (2x);
connection break / short / reverse polarity / cell removed -> abort with a report.

State per channel: `input_text.mc3000_chN_cel` (cell id), `_programma`, `_kt`
(`step|retries|start|resume|current factor`), `_kt_data` (measurements), `_kt_log`,
`input_datetime.mc3000_chN_kt_moment` (end of the current rest step).

## Cell database

`sensor.mc3000_cellen`, attributes `types` and `cells`, written only through
`script.mc3000_db_opslaan` (fires `mc3000_db_set`), backed up to `/config/mc3000_cellen.json`
after every save. `shell_command.mc3000_db_restore` puts the backup back.

Reports carry the cell (`cell`: id, type, print, note, colour) and the quality-test
measurements (`metingen`). `mc3000_report.py --sync-page` (hourly) also writes
`/config/www/mc3000/reports.json` and the charts for the card.
