/*
 * SkyRC MC3000 dashboard card for Home Assistant.
 *
 *   type: custom:mc3000-card
 *   view: overzicht | cellen | programmas
 *   reports_path: rapporten        (optional) view path of the reports table
 *   reports_url: https://github.com/<you>/<repo>/releases   (optional link)
 *
 * overzicht   the charger's four slots side by side (phone width), tap a slot
 *             to pick a favourite cell, the program button under it to start or
 *             follow a program, and the result block for the full report.
 * cellen      cell database: types with datasheet defaults, and the physical
 *             cell variants (print, note, colour, favourite).
 * programmas  every program with the settings it sends for a chosen cell.
 *
 * State lives in Home Assistant: sensor.mc3000_cellen (database, written via
 * script.mc3000_db_opslaan), input_text.mc3000_chN_* (cell, program, test
 * steps) and /local/mc3000/reports.json (exported by mc3000_report.py).
 * Program values: params() below = custom_templates/mc3000.jinja; keep in step.
 */

const VERSION = "1.0.4";
const CH = [1, 2, 3, 4];
const DB = "sensor.mc3000_cellen";
const PALETTE = ["#e5484d", "#f76b15", "#ffc53d", "#46a758", "#12a594", "#0090ff",
                 "#3e63dd", "#8e4ec6", "#d6409f", "#a18072", "#8b8d98", "#f0f0f3"];
const CHEMS = ["LiIon", "LiFe", "LiIon 4.35V", "LTO", "NiMH", "NiCd", "Eneloop", "NiZn", "RAM"];
const VMAX = { LiIon: 4200, LiFe: 3600, "LiIon 4.35V": 4350, LTO: 2850, NiMH: 1650, NiCd: 1650, Eneloop: 1650, NiZn: 1900, RAM: 1650 };
const VCUT = { LiIon: 2500, LiFe: 2500, "LiIon 4.35V": 3000, LTO: 1800, NiMH: 1000, NiCd: 1000, Eneloop: 1000, NiZn: 1200, RAM: 900 };
const VSTO = { LiIon: 3700, LiFe: 3300, "LiIon 4.35V": 3800, LTO: 2400 };

const PROGRAMS = [
  { key: "kwaliteit", name: "Kwaliteitstest", icon: "mdi:shield-check", best: true,
    short: "Alles meten en eindigen op opslagspanning",
    desc: "Volledige, gestandaardiseerde test: laden, 30 min rust, capaciteit meten bij 0,2C (IEC 61960), " +
          "30 min rust, opnieuw volladen, 30 min rust, IR-test bij volle cel, naar opslagspanning en daarna " +
          "zelfontlading meten (1 u rust + 24 u). Eén rapport met oordeel aan het eind." },
  { key: "capaciteit", name: "Capaciteitstest", icon: "mdi:battery-sync",
    short: "Laden, ontladen, opslagspanning",
    desc: "Snelle capaciteitsmeting: de lader doet C>D (vol laden, ontladen bij 0,2C), daarna naar de " +
          "opslagspanning van deze cel. Eén rapport." },
  { key: "laden", name: "Laden", icon: "mdi:battery-charging-high", short: "CC/CV tot vol",
    desc: "Laden tot de laadspanning, stopt op de eindstroom (C/20)." },
  { key: "opslag", name: "Opslag", icon: "mdi:battery-50", short: "Naar opslagspanning",
    desc: "Laadt of ontlaadt naar de opslagspanning van het celtype." },
  { key: "ontladen", name: "Ontladen", icon: "mdi:battery-arrow-down-outline", short: "0,2C tot ontlaadgrens",
    desc: "Ontladen bij 0,2C tot de ontlaadspanning. Laat de cel leeg achter: daarna opladen of op opslag zetten." },
  { key: "cyclus", name: "Cyclus C>D>C", icon: "mdi:sync", short: "Laden, ontladen, laden",
    desc: "Eén volledige cyclus van de lader zelf; eindigt vol." },
  { key: "refresh", name: "Refresh", icon: "mdi:refresh", short: "Opfrissen van de lader",
    desc: "Refresh-modus van de MC3000 (ontladen en laden in één keer)." },
  { key: "ir", name: "IR-test", icon: "mdi:omega", short: "15 s bij 1 A",
    desc: "Korte inwendige-weerstandsmeting: 15 s ontladen bij 1 A. Geen rapport." },
];

const STAGES = {
  start: "Starten", laden1: "Laden (1/2)", rust1: "Rust na laden", ontladen: "Capaciteit meten",
  rust2: "Rust na ontladen", laden2: "Laden (2/2)", rust3: "Rust voor IR", ir: "IR-test",
  opslag: "Naar opslagspanning", zo_wacht: "Rust voor zelfontlading", zo_meet: "Zelfontlading 24 u",
  koelen: "Afkoelen na fout", pauze: "Pauze na fout", cd: "Laden + ontladen", rapport: "Rapport",
  klaar: "Klaar", afgebroken: "Afgebroken", gestopt: "Gestopt",
};
const SEQ = {
  kwaliteit: ["laden1", "rust1", "ontladen", "rust2", "laden2", "rust3", "ir", "opslag", "zo_wacht", "zo_meet"],
  capaciteit: ["cd", "opslag"],
};
const ACTIVE_STATUS = ["Charging", "Discharging", "Paused"];

/* ---------------------------------------------------------------- helpers */
const md = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "$1");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = null) => { const n = parseFloat(v); return Number.isFinite(n) ? n : d; };
// Same rounding as Jinja/Python round() (half to even), so card and HA agree.
const rnd = (x) => { const f = Math.floor(x); return Math.abs(x - f - 0.5) < 1e-9 ? (f % 2 === 0 ? f : f + 1) : Math.round(x); };
const r50 = (v) => rnd(v / 50) * 50;
const hm = (s) => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return `${h}:${String(m).padStart(2, "0")}`; };
const fmtDate = (s) => { if (!s) return ""; const d = new Date(String(s).replace(" ", "T")); return isNaN(d) ? s : d.toLocaleDateString("nl-NL", { day: "numeric", month: "short" }) + " " + d.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" }); };
const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 40) || "type";
const newId = () => "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 5);
const contrast = (hex) => { const h = (hex || "#888").replace("#", ""); const r = parseInt(h.substr(0, 2), 16), g = parseInt(h.substr(2, 2), 16), b = parseInt(h.substr(4, 2), 16); return (r * 299 + g * 587 + b * 114) / 1000 > 150 ? "#111" : "#fff"; };
const verdictClass = (v) => { v = (v || "").toLowerCase(); if (v.startsWith("goed")) return "good"; if (v.startsWith("redelijk")) return "fair"; if (v.startsWith("matig")) return "warn"; if (v.startsWith("slecht")) return "bad"; return "unk"; };
const verdictShort = (v) => { if (!v) return "–"; const s = v.split(" (")[0]; return s.startsWith("Slecht") ? "Slecht" : s; };

/** Charger values for one cell type and program (same rules as mc3000.jinja). */
function params(t, mode, factor = 1) {
  if (!t) return null;
  const chem = t.chem || "LiIon";
  const f = Math.min(Math.max(factor, 0.5), 1);
  const cap = parseInt(t.cap) || 2000;
  const std = parseInt(t.chg_std) || 0;
  const chg = Math.max(r50(Math.min(std > 0 ? std : cap * 0.5, cap * 0.5, 2000) * f), 100);
  const dis = Math.min(Math.max(r50(cap * 0.2 * f), 100), 2000);
  const endc = Math.max(rnd(cap / 20 / 10) * 10, 50);
  const vmax = parseInt(t.v_max) || VMAX[chem] || 4200;
  let vcut = parseInt(t.v_cut) || VCUT[chem] || 2500;
  if (chem === "LiIon") vcut = Math.max(vcut, 2500);
  const vsto = parseInt(t.v_store) || VSTO[chem] || 0;
  const tChg = cap / chg * 60 * 1.5 + 60, tDis = cap / dis * 60 * 1.3 + 30;
  const tc = mode === "laden" ? tChg : mode === "ontladen" ? tDis : Math.max(tChg, tDis);
  const tcut = Math.min(Math.max(Math.ceil(tc / 10) * 10, 60), 1440);
  return { chem, cap, caplimit: Math.min(Math.ceil(cap * 1.25 / 100) * 100, 20000), chg, dis, endc, dis_end: dis,
           vmax, vcut, vsto, tcut, temp: 45, ir: parseInt(t.ir) || 0 };
}

/** Rough duration of a program in hours, for the program list. */
function duration(key, p) {
  if (!p) return "";
  const ch = p.cap / p.chg * 1.25 + 0.3, di = p.cap / p.dis;
  const h = { kwaliteit: ch * 0.6 + 0.5 + di + 0.5 + ch + 0.5 + 0.1 + di * 0.5, capaciteit: ch * 0.6 + 0.2 + di + di * 0.5,
              laden: ch, opslag: di * 0.5, ontladen: di, cyclus: ch * 2 + di + 0.4, refresh: ch + di, ir: 0.02 }[key];
  if (h === undefined) return "";
  const txt = h < 0.1 ? "± 1 min" : `± ${h < 10 ? h.toFixed(1).replace(".", ",") : Math.round(h)} u`;
  return key === "kwaliteit" ? txt + " + 25 u zelfontlading" : txt;
}

/* ---------------------------------------------------------------- styles */
const CSS = `
:host { display:block; --mc-gap:8px; --mc-radius:14px; --mc-panel:#15181d; --mc-panel2:#1d2128; --mc-lcd:#9fd6ff;
  --mc-good:#30a46c; --mc-fair:#7cb342; --mc-warn:#f5a524; --mc-bad:#e5484d; --mc-unk:#8b8d98; }
* { box-sizing:border-box; }
.wrap { max-width:640px; margin:0 auto; padding:4px 0 24px; color:var(--primary-text-color); font-family:var(--paper-font-body1_-_font-family, inherit); }
.panel { background:linear-gradient(180deg,#22262e,#14171c); border-radius:20px; padding:12px 10px 14px; box-shadow:0 6px 18px rgba(0,0,0,.25), inset 0 1px 0 rgba(255,255,255,.06); color:#e8eaee; }
.phead { display:flex; align-items:center; justify-content:space-between; padding:0 4px 10px; font-size:12px; color:#9aa3b2; }
.brand { font-weight:700; letter-spacing:.08em; color:#e8eaee; font-size:13px; }
.brand span { color:#ff4d4f; }
.chips { display:flex; gap:8px; align-items:center; }
.dotc { width:7px; height:7px; border-radius:50%; display:inline-block; margin-right:4px; vertical-align:middle; }
.grid4 { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:var(--mc-gap); }
.slot { background:var(--mc-panel2); border-radius:12px; padding:8px 4px 8px; text-align:center; cursor:pointer; position:relative; border:1px solid rgba(255,255,255,.05); -webkit-tap-highlight-color:transparent; }
.slot:active, .pbtn:active, .res:active { transform:scale(.97); }
.slot .no { position:absolute; top:6px; left:7px; font-size:11px; font-weight:700; color:#9aa3b2; }
.slot .led { position:absolute; top:7px; right:7px; width:8px; height:8px; border-radius:50%; background:#3a3f48; }
.led.red { background:#ff3b3b; box-shadow:0 0 8px #ff3b3b; } .led.green { background:#2fd16b; box-shadow:0 0 8px #2fd16b; }
.led.orange { background:#ff9a1f; box-shadow:0 0 8px #ff9a1f; } .led.blink { animation:blink 1s infinite; }
@keyframes blink { 50% { opacity:.2; } }
.cell { width:34px; height:66px; margin:14px auto 6px; position:relative; }
.cell .cap { position:absolute; top:0; left:11px; width:12px; height:4px; border-radius:2px 2px 0 0; background:#cfd3da; }
.cell .body { position:absolute; top:4px; left:0; right:0; bottom:0; border-radius:7px; border:2px solid var(--c,#555); background:#0d0f12; overflow:hidden; }
.cell .fill { position:absolute; left:0; right:0; bottom:0; background:var(--c,#555); opacity:.85; transition:height .6s; }
.cell .fill.chg { background:repeating-linear-gradient(-45deg, var(--c,#555) 0 6px, color-mix(in srgb, var(--c,#555) 70%, #fff) 6px 12px); background-size:200% 200%; animation:flow 1.2s linear infinite; }
@keyframes flow { to { background-position:34px 0; } }
.cell.empty .body { border-style:dashed; border-color:#4a505a; }
.cell .plus { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:#6b7280; font-size:22px; }
.lcd { font-variant-numeric:tabular-nums; color:var(--mc-lcd); font-weight:600; font-size:15px; line-height:1.1; }
.lcd.small { font-size:11px; font-weight:500; color:#b9c3d0; }
.cname { font-size:10.5px; color:#cfd5de; margin-top:5px; line-height:1.2; height:2.4em; overflow:hidden; word-break:break-word; }
.cname b { color:#fff; font-weight:600; }
.row-label { font-size:11px; color:var(--secondary-text-color); margin:14px 2px 6px; text-transform:uppercase; letter-spacing:.06em; }
.pbtn { border-radius:12px; padding:9px 4px; text-align:center; cursor:pointer; background:var(--card-background-color,#fff); border:1px solid var(--divider-color,#e0e0e0); min-height:58px; display:flex; flex-direction:column; justify-content:center; gap:2px; -webkit-tap-highlight-color:transparent; }
.pbtn ha-icon { --mdc-icon-size:20px; color:var(--primary-color); margin:0 auto; }
.pbtn .pn { font-size:11.5px; font-weight:600; line-height:1.15; }
.pbtn .ps { font-size:10px; color:var(--secondary-text-color); line-height:1.15; }
.pbtn.run { background:color-mix(in srgb, var(--primary-color) 14%, var(--card-background-color,#fff)); border-color:var(--primary-color); }
.pbtn.err { border-color:var(--mc-bad); }
.res { border-radius:12px; padding:8px 6px; background:var(--card-background-color,#fff); border:1px solid var(--divider-color,#e0e0e0); cursor:pointer; font-size:11px; -webkit-tap-highlight-color:transparent; }
.res .k { color:var(--secondary-text-color); font-size:9.5px; text-transform:uppercase; letter-spacing:.04em; margin-top:5px; }
.res .k:first-child { margin-top:0; }
.res .v { font-weight:600; font-size:13px; font-variant-numeric:tabular-nums; }
.res .when { font-size:9.5px; color:var(--secondary-text-color); margin-bottom:4px; }
.chip { display:inline-block; padding:2px 7px; border-radius:999px; font-size:10.5px; font-weight:600; color:#fff; background:var(--mc-unk); white-space:nowrap; max-width:100%; overflow:hidden; text-overflow:ellipsis; }
.chip.good { background:var(--mc-good); } .chip.fair { background:var(--mc-fair); } .chip.warn { background:var(--mc-warn); color:#111; } .chip.bad { background:var(--mc-bad); } .chip.live { background:var(--primary-color); }
.res .chip { margin-top:6px; }
.foot { margin-top:16px; display:flex; gap:8px; flex-wrap:wrap; }
.btn { border:none; border-radius:10px; padding:10px 14px; font:inherit; font-size:14px; font-weight:600; cursor:pointer; background:var(--primary-color); color:var(--text-primary-color,#fff); }
.btn.sec { background:var(--secondary-background-color,#eee); color:var(--primary-text-color); }
.btn.danger { background:var(--mc-bad); color:#fff; }
.btn.small { padding:6px 10px; font-size:12px; }
.btn[disabled] { opacity:.5; }
a.lnk { color:var(--primary-color); text-decoration:none; font-size:13px; }
/* modal */
.ov { position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:9999; display:flex; align-items:flex-end; justify-content:center; animation:fade .15s; }
@keyframes fade { from { opacity:0; } }
.sheet { background:var(--card-background-color,#fff); color:var(--primary-text-color); width:100%; max-width:640px; max-height:92vh; overflow:auto; border-radius:18px 18px 0 0; padding:14px 16px calc(20px + env(safe-area-inset-bottom)); animation:up .2s ease-out; -webkit-overflow-scrolling:touch; }
@media (min-width:700px) { .ov { align-items:center; } .sheet { border-radius:18px; } }
@keyframes up { from { transform:translateY(40px); opacity:.6; } }
.sh { display:flex; align-items:center; gap:10px; margin-bottom:10px; position:sticky; top:-14px; background:var(--card-background-color,#fff); padding:6px 0; z-index:1; }
.sh h2 { font-size:17px; margin:0; flex:1; line-height:1.25; }
.x { border:none; background:var(--secondary-background-color,#eee); width:32px; height:32px; border-radius:50%; font-size:18px; cursor:pointer; color:var(--primary-text-color); flex:none; }
.list { display:flex; flex-direction:column; gap:6px; }
.item { display:flex; align-items:center; gap:10px; padding:10px; border-radius:12px; border:1px solid var(--divider-color,#e0e0e0); cursor:pointer; }
.item.sel { border-color:var(--primary-color); background:color-mix(in srgb, var(--primary-color) 8%, transparent); }
.item .sw { width:22px; height:22px; border-radius:50%; flex:none; border:1px solid rgba(0,0,0,.15); }
.item .tx { flex:1; min-width:0; }
.item .t1 { font-weight:600; font-size:14px; }
.item .t2 { font-size:12px; color:var(--secondary-text-color); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.item ha-icon { color:var(--primary-color); flex:none; }
.star { cursor:pointer; color:#c9ccd3; font-size:20px; line-height:1; flex:none; padding:2px; }
.star.on { color:#f5b301; }
.badge { font-size:10.5px; padding:2px 6px; border-radius:6px; background:var(--secondary-background-color,#eee); color:var(--secondary-text-color); white-space:nowrap; }
.kv { display:grid; grid-template-columns:1fr auto; gap:4px 12px; font-size:13px; }
.kv div:nth-child(odd) { color:var(--secondary-text-color); }
.kv div:nth-child(even) { font-weight:600; text-align:right; font-variant-numeric:tabular-nums; }
.sec-t { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--secondary-text-color); margin:16px 0 8px; font-weight:600; }
.box { border:1px solid var(--divider-color,#e0e0e0); border-radius:12px; padding:12px; }
ul.rs { margin:0; padding-left:18px; font-size:13px; line-height:1.45; }
img.chart { width:100%; border-radius:10px; background:#fff; display:block; }
.steps { display:flex; flex-direction:column; gap:0; font-size:13px; }
.step { display:flex; gap:10px; align-items:flex-start; padding:5px 0; }
.step .bul { width:18px; height:18px; border-radius:50%; border:2px solid var(--divider-color,#ccc); flex:none; margin-top:1px; display:flex; align-items:center; justify-content:center; font-size:11px; }
.step.done .bul { background:var(--mc-good); border-color:var(--mc-good); color:#fff; }
.step.now .bul { border-color:var(--primary-color); background:var(--primary-color); color:#fff; animation:blink 1.4s infinite; }
.step.now .sn { font-weight:700; }
.step .sd { font-size:12px; color:var(--secondary-text-color); }
label.f { display:block; font-size:12px; color:var(--secondary-text-color); margin:10px 0 4px; }
input.in, select.in, textarea.in { width:100%; font:inherit; font-size:15px; padding:9px 10px; border-radius:10px; border:1px solid var(--divider-color,#ccc); background:var(--secondary-background-color,#fafafa); color:var(--primary-text-color); }
textarea.in { min-height:64px; resize:vertical; }
.two { display:grid; grid-template-columns:1fr 1fr; gap:0 10px; }
.pal { display:flex; flex-wrap:wrap; gap:8px; margin-top:4px; align-items:center; }
.pal span { width:30px; height:30px; border-radius:50%; cursor:pointer; border:2px solid transparent; box-shadow:inset 0 0 0 1px rgba(0,0,0,.15); }
.pal span.on { border-color:var(--primary-text-color); }
.pal input[type=color] { width:36px; height:32px; border:none; background:none; padding:0; }
.toolbar { display:flex; gap:8px; align-items:center; margin:4px 0 10px; flex-wrap:wrap; }
.seg { display:inline-flex; border:1px solid var(--divider-color,#ccc); border-radius:10px; overflow:hidden; }
.seg button { border:none; background:none; padding:7px 12px; font:inherit; font-size:13px; cursor:pointer; color:var(--primary-text-color); }
.seg button.on { background:var(--primary-color); color:var(--text-primary-color,#fff); }
.grp { margin-top:12px; }
.grp-h { display:flex; align-items:center; gap:8px; font-size:13px; font-weight:700; margin:0 2px 6px; }
.typecard { border:1px solid var(--divider-color,#e0e0e0); border-radius:12px; padding:10px 12px; cursor:pointer; }
.typecard .tn { font-weight:700; font-size:14px; display:flex; justify-content:space-between; gap:8px; }
.specs { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:6px; margin-top:8px; }
.specs div { background:var(--secondary-background-color,#f4f4f4); border-radius:8px; padding:5px 6px; }
.specs .sk { font-size:9.5px; color:var(--secondary-text-color); text-transform:uppercase; }
.specs .sv { font-size:13px; font-weight:600; font-variant-numeric:tabular-nums; }
.prog { border:1px solid var(--divider-color,#e0e0e0); border-radius:14px; padding:12px; margin-bottom:10px; }
.prog .ph { display:flex; align-items:center; gap:10px; }
.prog .ph ha-icon { color:var(--primary-color); }
.prog .pt { font-weight:700; font-size:15px; flex:1; }
.prog p { font-size:13px; margin:8px 0; color:var(--secondary-text-color); line-height:1.45; }
.best { font-size:10.5px; background:var(--mc-good); color:#fff; padding:2px 7px; border-radius:6px; font-weight:700; }
.hint { font-size:12px; color:var(--secondary-text-color); line-height:1.45; }
.toast { position:fixed; left:50%; bottom:28px; transform:translateX(-50%); background:#222; color:#fff; padding:10px 16px; border-radius:10px; z-index:10000; font-size:14px; box-shadow:0 4px 14px rgba(0,0,0,.3); }
.empty { padding:18px; text-align:center; color:var(--secondary-text-color); font-size:14px; }
.live { font-size:13px; }
.lchart { margin-bottom:12px; }
@media (max-width:380px) { .lcd { font-size:13.5px; } .pbtn .pn { font-size:10.5px; } }
`;

/* ================================================================== card */
class Mc3000Card extends HTMLElement {
  setConfig(config) {
    this._cfg = config || {};
    this._view = this._cfg.view || "overzicht";
  }
  static getStubConfig() { return { view: "overzicht" }; }
  getCardSize() { return 10; }
  getGridOptions() { return { columns: "full", rows: "auto" }; }

  set hass(h) {
    this._hass = h;
    if (!this._root) this._init();
    const sig = this._sig();
    if (sig !== this._lastSig) { this._lastSig = sig; this._render(); if (this._modal?.live) this._modal.live(); }
  }

  /* --------------------------------------------------------- plumbing */
  _init() {
    this._root = this.attachShadow({ mode: "open" });
    this._root.innerHTML = `<style>${CSS}</style><div class="wrap" id="main"></div><div id="modal"></div>`;
    this._main = this._root.getElementById("main");
    this._modalEl = this._root.getElementById("modal");
    this._reports = [];
    this._repGen = "";
    this._cellFilter = "fav";
    this._progCell = null;
    this._loadReports();
    this._timer = setInterval(() => this._loadReports(), 120000);
    this._tick = setInterval(() => { if (this._view === "overzicht") { this._lastSig = ""; this._render(); if (this._modal?.live) this._modal.live(); } }, 30000);
  }
  disconnectedCallback() { clearInterval(this._timer); clearInterval(this._tick); this._timer = this._tick = null; }
  connectedCallback() {
    if (this._root && !this._timer) {
      this._timer = setInterval(() => this._loadReports(), 120000);
      this._tick = setInterval(() => { if (this._view === "overzicht") { this._lastSig = ""; this._render(); } }, 30000);
      this._loadReports();
    }
  }
  async _loadReports() {
    try {
      const r = await fetch(`/local/mc3000/reports.json?t=${Date.now()}`, { cache: "no-store" });
      if (!r.ok) return;
      const j = await r.json();
      if (j.generated !== this._repGen) { this._repGen = j.generated; this._reports = j.reports || []; this._lastSig = ""; this._render(); }
    } catch (e) { /* not exported yet */ }
  }
  _st(id) { return this._hass?.states?.[id]; }
  _s(id) { const s = this._st(id); return s ? s.state : undefined; }
  _e(ch, suf) { return `sensor.skyrc_mc3000_ch${ch}_${suf}`; }
  _sig() {
    const ids = [DB, "sensor.skyrc_mc3000_input_voltage", "input_boolean.mc3000_zelfontlading"];
    if (this._view !== "overzicht") {
      for (const c of CH) ids.push(`input_text.mc3000_ch${c}_cel`);
      return this._view + "|" + this._repGen + "|" + ids.map((i) => { const s = this._st(i); return s ? s.state + (i === DB ? s.last_updated : "") : "-"; }).join("|");
    }
    for (const c of CH) {
      for (const s of ["status", "voltage", "current", "capacity", "temperature", "resistance", "led", "ir_test", "charged_capacity", "discharged_capacity"]) ids.push(this._e(c, s));
      for (const s of ["cel", "programma", "kt", "kt_data", "batterij"]) ids.push(`input_text.mc3000_ch${c}_${s}`);
      ids.push(`input_datetime.mc3000_ch${c}_kt_moment`, `sensor.mc3000_ch${c}_verstreken`);
    }
    return this._view + "|" + ids.map((i) => { const s = this._st(i); return s ? s.state + (i === DB ? s.last_updated : "") : "-"; }).join("|");
  }
  _call(domain, service, data) {
    return this._hass.callService(domain, service, data).catch((e) => this._toast("Mislukt: " + (e.message || e)));
  }
  _toast(msg) {
    const t = document.createElement("div");
    t.className = "toast"; t.textContent = msg;
    this._root.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }
  _db() {
    const a = this._st(DB)?.attributes || {};
    return { types: a.types || {}, cells: a.cells || {} };
  }
  async _saveDb(types, cells) {
    await this._call("script", "mc3000_db_opslaan", { types, cells });
  }

  /* --------------------------------------------------------- channel model */
  _chan(ch) {
    const db = this._db();
    const cellId = this._s(`input_text.mc3000_ch${ch}_cel`) || "";
    const cell = db.cells[cellId] ? { id: cellId, ...db.cells[cellId] } : null;
    const type = cell ? db.types[cell.type] : null;
    const status = this._s(this._e(ch, "status")) || "unavailable";
    const kt = (this._s(`input_text.mc3000_ch${ch}_kt`) || "").split("|");
    const prog = this._s(`input_text.mc3000_ch${ch}_programma`) || "";
    const multi = ["kwaliteit", "capaciteit"].includes(prog);
    const stage = kt[0] || "";
    const testActive = multi && !!STAGES[stage] && !["", "klaar", "afgebroken", "gestopt", "rapport"].includes(stage);
    const running = ACTIVE_STATUS.includes(status) || testActive;
    const ir = num(this._s(this._e(ch, "resistance")));
    return {
      ch, cellId, cell, type, status, prog, stage, kt, testActive, running,
      fault: status.startsWith("FAULT"),
      v: num(this._s(this._e(ch, "voltage"))), a: num(this._s(this._e(ch, "current"))),
      mah: num(this._s(this._e(ch, "capacity"))), temp: num(this._s(this._e(ch, "temperature"))),
      ir: ir !== null && ir > 0 && ir < 65535 ? ir : null,
      led: this._s(this._e(ch, "led")) || "",
      elapsed: this._s(`sensor.mc3000_ch${ch}_verstreken`),
      moment: this._s(`input_datetime.mc3000_ch${ch}_kt_moment`),
      label: this._s(`input_text.mc3000_ch${ch}_batterij`) || "",
    };
  }
  _lastReport(ch) { return this._reports.find((r) => String(r.channel) === String(ch)); }

  /* --------------------------------------------------------- render */
  _render() {
    if (!this._hass || !this._main) return;
    try {
      if (this._view === "cellen") this._renderCells();
      else if (this._view === "programmas") this._renderPrograms();
      else this._renderOverview();
    } catch (e) {
      this._main.innerHTML = `<div class="empty">Fout in kaart: ${esc(e.message)}</div>`;
      console.error(e);
    }
  }

  /* ========================== OVERZICHT ========================== */
  _renderOverview() {
    const chans = CH.map((c) => this._chan(c));
    const vin = this._s("sensor.skyrc_mc3000_input_voltage");
    const online = this._s(this._e(1, "status")) && this._s(this._e(1, "status")) !== "unavailable";
    const slot = (c) => {
      const color = c.cell?.color || "#4a505a";
      const soc = c.v && c.v > 0.5 ? Math.min(Math.max((c.v - 3.0) / 1.2, 0.03), 1) : 0;
      const ledCls = c.fault ? "red blink" : c.led === "Red" ? "red" : c.led === "Green" ? "green" : c.led === "Orange" ? "orange" : "";
      const charging = c.status === "Charging" || c.status === "Discharging";
      const cellHtml = c.cell
        ? `<div class="cell" style="--c:${esc(color)}"><div class="cap"></div><div class="body"><div class="fill ${charging ? "chg" : ""}" style="height:${Math.round(soc * 100)}%"></div></div></div>`
        : `<div class="cell empty"><div class="cap"></div><div class="body"></div><div class="plus">+</div></div>`;
      const amps = c.a !== null && charging ? `${c.a > 0 ? "+" : ""}${c.a.toFixed(2)} A` : (c.temp !== null ? `${c.temp.toFixed(0)} °C` : "");
      const tn = (c.type?.name || "?").split(" ");
      const name = c.cell ? `<b>${esc(c.cell.print || tn[0])}</b><br>${esc(tn.slice(-1)[0])}` : `<span style="color:#7c8594">Kies cel</span>`;
      return `<div class="slot" data-act="cell" data-ch="${c.ch}">
        <div class="no">${c.ch}</div><div class="led ${ledCls}"></div>
        ${cellHtml}
        <div class="lcd">${c.v && c.v > 0.5 ? c.v.toFixed(3) : "–.–––"}</div>
        <div class="lcd small">${esc(amps)}</div>
        <div class="cname">${name}</div>
      </div>`;
    };
    const pbtn = (c) => {
      const P = PROGRAMS.find((p) => p.key === c.prog);
      let icon = "mdi:play-circle-outline", n = "Programma", s = "kiezen", cls = "";
      if (c.testActive) {
        icon = P?.icon || "mdi:progress-clock"; n = P?.name || c.prog; cls = "run";
        s = STAGES[c.stage] || c.stage;
        const left = this._left(c);
        if (left) s += ` · ${left}`;
      } else if (ACTIVE_STATUS.includes(c.status)) {
        icon = P?.icon || "mdi:progress-clock"; n = P?.name || "Bezig"; cls = "run";
        s = `${c.status === "Charging" ? "Laden" : c.status === "Discharging" ? "Ontladen" : "Pauze"} · ${c.elapsed || ""}`;
      } else if (c.fault) {
        icon = "mdi:alert-circle"; n = "Fout"; s = c.status.replace("FAULT: ", ""); cls = "err";
      } else if (c.status === "Done") {
        icon = "mdi:check-circle-outline"; n = P?.name || "Klaar"; s = "klaar · opnieuw";
      }
      return `<div class="pbtn ${cls}" data-act="prog" data-ch="${c.ch}"><ha-icon icon="${icon}"></ha-icon><div class="pn">${esc(n)}</div><div class="ps">${esc(s)}</div></div>`;
    };
    const res = (c) => {
      const r = this._lastReport(c.ch);
      if (!r) return `<div class="res" data-act="rep" data-ch="${c.ch}"><div class="when">Nog geen rapport</div><div class="k">mAh</div><div class="v">–</div><div class="k">IR mΩ</div><div class="v">–</div><div class="k">SOH</div><div class="v">–</div></div>`;
      const ir = r.metingen?.ir ?? r.resistance_median_mohm;
      return `<div class="res" data-act="rep" data-ch="${c.ch}">
        <div class="when">${esc(fmtDate(r.end))}</div>
        <div class="k">Ontladen</div><div class="v">${r.discharged_mah ?? "–"}${r.discharged_mah ? "" : ""}</div>
        <div class="k">IR mΩ</div><div class="v">${ir ?? "–"}</div>
        <div class="k">SOH</div><div class="v">${r.soh_percent != null ? r.soh_percent + "%" : "–"}</div>
        <span class="chip ${verdictClass(r.verdict)}">${esc(verdictShort(r.verdict))}</span>
      </div>`;
    };
    this._main.innerHTML = `
      <div class="panel">
        <div class="phead"><div class="brand">SKYRC <span>MC3000</span></div>
          <div class="chips"><span><span class="dotc" style="background:${online ? "#2fd16b" : "#e5484d"}"></span>${online ? "verbonden" : "offline"}</span>
          ${vin ? `<span>${num(vin, 0).toFixed(1)} V</span>` : ""}</div></div>
        <div class="grid4">${chans.map(slot).join("")}</div>
      </div>
      <div class="row-label">Programma</div>
      <div class="grid4">${chans.map(pbtn).join("")}</div>
      <div class="row-label">Laatste resultaat</div>
      <div class="grid4">${chans.map(res).join("")}</div>
      <div class="foot" style="align-items:center"><button class="btn" data-nav="${esc(this._cfg.reports_path || "rapporten")}">Rapporten-tabel</button>
        ${this._cfg.reports_url ? `<a class="lnk" href="${esc(this._cfg.reports_url)}" target="_blank" rel="noopener">GitHub ›</a>` : ""}</div>`;
    this._main.querySelector("[data-nav]").addEventListener("click", (e) => this._navigate(e.target.dataset.nav));
    this._main.querySelectorAll("[data-act]").forEach((el) => el.addEventListener("click", () => {
      const ch = +el.dataset.ch;
      if (el.dataset.act === "cell") this._openCellPicker(ch);
      else if (el.dataset.act === "prog") this._openProgram(ch);
      else this._openReport(ch);
    }));
  }
  /** Go to another view of this dashboard (same as a navigate tap_action). */
  _navigate(path) {
    const base = location.pathname.split("/").slice(0, 2).join("/");
    history.pushState(null, "", `${base}/${path}`);
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }
  _left(c) {
    if (!c.moment || !["rust1", "rust2", "rust3", "ir", "zo_wacht", "zo_meet", "koelen", "pauze"].includes(c.stage)) return c.elapsed && ACTIVE_STATUS.includes(c.status) ? c.elapsed : "";
    const t = new Date(c.moment.replace(" ", "T")).getTime() - Date.now();
    return t > 0 ? `nog ${hm(t / 1000)}` : "";
  }

  /* --------------------------------------------------------- modal */
  _openModal(title, html, bind, opts = {}) {
    this._modalEl.innerHTML = `<div class="ov"><div class="sheet"><div class="sh">${opts.dot ? `<span class="dotc" style="width:14px;height:14px;background:${esc(opts.dot)}"></span>` : ""}<h2>${title}</h2><button class="x" aria-label="Sluiten">×</button></div><div class="mb">${html}</div></div></div>`;
    const ov = this._modalEl.querySelector(".ov");
    ov.addEventListener("click", (e) => { if (e.target === ov) this._closeModal(); });
    ov.querySelector(".x").addEventListener("click", () => this._closeModal());
    this._modal = { live: opts.live || null, body: ov.querySelector(".mb") };
    if (bind) bind(this._modal.body);
  }
  _closeModal() { clearInterval(this._chartTimer); this._chartTimer = null; this._modalEl.innerHTML = ""; this._modal = null; }

  /* ---------------- cell picker (tap on slot) ---------------- */
  _openCellPicker(ch) {
    const db = this._db();
    const c = this._chan(ch);
    let showAll = false;
    const draw = (body) => {
      const ids = Object.keys(db.cells).filter((id) => showAll || db.cells[id].fav || id === c.cellId);
      ids.sort((a, b) => (db.types[db.cells[a].type]?.name || "").localeCompare(db.types[db.cells[b].type]?.name || "") || (db.cells[a].print || "").localeCompare(db.cells[b].print || ""));
      const items = ids.map((id) => {
        const x = db.cells[id], t = db.types[x.type] || {};
        const last = this._reports.find((r) => r.cell?.id === id);
        return `<div class="item ${id === c.cellId ? "sel" : ""}" data-id="${esc(id)}">
          <span class="sw" style="background:${esc(x.color || "#888")}"></span>
          <div class="tx"><div class="t1">${esc(t.name || "?")}${x.print ? ` · ${esc(x.print)}` : ""}</div>
          <div class="t2">${esc(x.note || "")}${last ? ` · laatst ${esc(verdictShort(last.verdict))}${last.soh_percent != null ? " " + last.soh_percent + "%" : ""}` : ""}</div></div>
          ${x.fav ? `<span class="star on">★</span>` : ""}</div>`;
      }).join("");
      body.innerHTML = `
        ${c.running ? `<div class="hint" style="margin-bottom:10px">Dit kanaal is bezig. Een andere cel kiezen verandert alleen naam/kleur in het rapport, niet de lopende instellingen.</div>` : ""}
        <div class="list">${items || `<div class="empty">Nog geen favorieten. Zet een ster bij cellen op het tabblad Cellen, of toon alle cellen.</div>`}</div>
        <div class="foot"><button class="btn sec" data-a="all">${showAll ? "Alleen favorieten" : "Alle cellen"}</button>
        <button class="btn sec" data-a="none">Geen cel</button></div>`;
      body.querySelectorAll(".item").forEach((el) => el.addEventListener("click", async () => {
        await this._call("script", "mc3000_cel_kiezen", { channel: ch, cell: el.dataset.id });
        this._closeModal(); this._toast(`Kanaal ${ch}: cel gekozen`);
      }));
      body.querySelector('[data-a="all"]').addEventListener("click", () => { showAll = !showAll; draw(body); });
      body.querySelector('[data-a="none"]').addEventListener("click", async () => {
        await this._call("script", "mc3000_cel_kiezen", { channel: ch, cell: "" }); this._closeModal();
      });
    };
    this._openModal(`Kanaal ${ch} · cel kiezen`, "", draw);
  }

  /* ---------------- program sheet ---------------- */
  _openProgram(ch) {
    let open = null;
    const draw = (body) => {
      const c = this._chan(ch);
      const p = params(c.type, "laden");
      if (c.running || c.fault) {
        if (!body.querySelector("#live")) {
          body.innerHTML = `<div id="live" class="lchart"><div class="hint">Grafiek laden…</div></div><div id="info"></div>`;
          this._liveChart(ch, body.querySelector("#live"));
          clearInterval(this._chartTimer);
          this._chartTimer = setInterval(() => { const el = this._modal?.body?.querySelector("#live"); if (el) this._liveChart(ch, el); }, 60000);
        }
        const info = body.querySelector("#info");
        info.innerHTML = this._runHtml(c) + `<div class="foot"><button class="btn danger" data-a="stop">Stoppen</button>${c.fault ? `<button class="btn sec" data-a="new">Nieuw programma</button>` : ""}</div>`;
        body = info;
        body.querySelector('[data-a="stop"]').addEventListener("click", async () => {
          if (!confirm(`Kanaal ${ch} stoppen?${c.testActive ? " De test wordt afgebroken." : ""}`)) return;
          await this._call("script", "mc3000_programma_stop", { channel: ch }); this._toast("Gestopt"); this._closeModal();
        });
        body.querySelector('[data-a="new"]')?.addEventListener("click", () => { clearInterval(this._chartTimer); this._chartTimer = null; c.running = false; drawList(this._modal.body, c, p); });
        return;
      }
      drawList(body, c, p);
    };
    const drawList = (body, c, p) => {
      if (!c.cell) {
        body.innerHTML = `<div class="empty">Kies eerst een cel voor kanaal ${ch}.</div><div class="foot"><button class="btn" data-a="pick">Cel kiezen</button></div>`;
        body.querySelector('[data-a="pick"]').addEventListener("click", () => this._openCellPicker(ch));
        return;
      }
      const rows = PROGRAMS.map((P) => {
        const q = params(c.type, P.key === "ontladen" ? "ontladen" : P.key === "laden" ? "laden" : "x");
        const det = open === P.key ? `<div style="padding:4px 2px 2px"><p class="hint" style="margin:6px 0 8px">${esc(P.desc)}</p>${this._paramKv(P.key, q)}
          ${P.key === "kwaliteit" ? `<label class="f" style="display:flex;gap:8px;align-items:center"><input type="checkbox" data-a="zo" ${this._s("input_boolean.mc3000_zelfontlading") === "on" ? "checked" : ""}> Zelfontlading meten (+25 u)</label>` : ""}
          <div class="foot"><button class="btn" data-a="start" data-k="${P.key}">Start ${esc(P.name.toLowerCase())}</button></div></div>` : "";
        return `<div class="item ${open === P.key ? "sel" : ""}" data-k="${P.key}" style="flex-wrap:wrap">
          <ha-icon icon="${P.icon}"></ha-icon><div class="tx"><div class="t1">${esc(P.name)} ${P.best ? `<span class="best">aanbevolen</span>` : ""}</div>
          <div class="t2">${esc(P.short)} · ${esc(duration(P.key, q))}</div></div>
          <div style="width:100%">${det}</div></div>`;
      }).join("");
      body.innerHTML = `<div class="hint" style="margin-bottom:10px"><span class="dotc" style="background:${esc(c.cell.color)}"></span><b>${esc(c.type?.name || "")}</b>${c.cell.print ? " · " + esc(c.cell.print) : ""} · ${p.cap} mAh</div><div class="list">${rows}</div>`;
      body.querySelectorAll(".item").forEach((el) => el.addEventListener("click", (e) => {
        if (e.target.closest("button,input,label")) return;
        open = open === el.dataset.k ? null : el.dataset.k; drawList(body, c, p);
      }));
      body.querySelector('[data-a="zo"]')?.addEventListener("change", (e) =>
        this._call("input_boolean", e.target.checked ? "turn_on" : "turn_off", { entity_id: "input_boolean.mc3000_zelfontlading" }));
      body.querySelector('[data-a="start"]')?.addEventListener("click", async (e) => {
        const k = e.target.dataset.k;
        e.target.disabled = true; e.target.textContent = "Bezig met starten…";
        await this._call("script", "mc3000_programma_start", { channel: ch, program: k });
        this._toast(`Kanaal ${ch}: ${PROGRAMS.find((x) => x.key === k).name} gestart`);
        this._closeModal();
      });
    };
    this._openModal(`Kanaal ${ch} · programma`, "", draw, { live: () => { const b = this._modal?.body; if (b && (this._chan(ch).running || this._chan(ch).fault) && !b.querySelector("input")) draw(b); } });
  }
  /** Start of the running program: test start (multi-step) or the channel's start helper. */
  _runStart(ch) {
    const c = this._chan(ch);
    if (c.testActive && parseInt(c.kt[2]) > 0) return new Date(parseInt(c.kt[2]) * 1000);
    const ts = this._st(`input_datetime.mc3000_ch${ch}_start`)?.attributes?.timestamp;
    const t = ts ? new Date(ts * 1000) : null;
    return t && Date.now() - t < 7 * 864e5 ? t : new Date(Date.now() - 6 * 3600e3);
  }
  /** Voltage, current and capacity since the program start from HA's history; redrawn every minute. */
  async _liveChart(ch, el) {
    const start = this._runStart(ch);
    const ids = ["voltage", "current", "capacity"].map((s) => this._e(ch, s));
    let hist;
    try {
      hist = await this._hass.callApi("GET", `history/period/${encodeURIComponent(start.toISOString())}?filter_entity_id=${ids.join(",")}` +
        `&end_time=${encodeURIComponent(new Date().toISOString())}&minimal_response&no_attributes`);
    } catch (e) { el.innerHTML = `<div class="hint">Geschiedenis niet beschikbaar (${esc(e.message || e)})</div>`; return; }
    const series = {};
    for (const arr of hist || []) {
      if (!arr.length) continue;
      const id = arr[0].entity_id;
      series[id] = arr.map((x) => [new Date(x.last_changed || x.last_updated).getTime(), parseFloat(x.state)]).filter((x) => Number.isFinite(x[1]));
    }
    // the current state is already final in the history, but add "now" so the lines reach the right edge
    for (const id of ids) { const st = num(this._s(id)); if (st !== null) (series[id] = series[id] || []).push([Date.now(), st]); }
    el.innerHTML = this._chartSvg(start.getTime(), Date.now(), [
      { label: "Spanning", unit: "V", color: "#1b6fd0", pts: series[ids[0]] || [], dec: 2 },
      { label: "Stroom", unit: "A", color: "#d9731a", pts: series[ids[1]] || [], dec: 2, zero: true },
      { label: "Capaciteit", unit: "mAh", color: "#30a46c", pts: series[ids[2]] || [], dec: 0 },
    ]) + `<div class="hint" style="margin-top:4px">Sinds ${esc(start.toLocaleString("nl-NL", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }))} · ververst elke minuut · ${new Date().toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" })}</div>`;
  }
  _chartSvg(t0, t1, panels) {
    const W = 600, L = 46, R = 10, PH = 78, GAP = 22, T = 6;
    const H = T + panels.length * (PH + GAP) + 6;
    const span = Math.max(t1 - t0, 60000);
    const x = (t) => L + (t - t0) / span * (W - L - R);
    const stepH = [0.25, 0.5, 1, 2, 3, 4, 6, 12, 24].find((h) => span / 3600e3 / h <= 6) || 24;
    let o = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block" font-family="inherit" font-size="11">`;
    panels.forEach((p, i) => {
      const top = T + i * (PH + GAP) + 14, bot = top + PH;
      const vals = p.pts.map((q) => q[1]);
      let lo = vals.length ? Math.min(...vals) : 0, hi = vals.length ? Math.max(...vals) : 1;
      if (p.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      if (hi - lo < 1e-6) { hi += p.dec ? 0.05 : 10; lo -= p.dec ? 0.05 : 10; }
      const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
      const y = (v) => bot - (v - lo) / (hi - lo) * PH;
      const last = vals.length ? vals[vals.length - 1] : null;
      o += `<text x="${L}" y="${top - 4}" fill="${p.color}" font-weight="600">${p.label}${last !== null ? `: ${last.toFixed(p.dec)} ${p.unit}` : ""}</text>`;
      o += `<rect x="${L}" y="${top}" width="${W - L - R}" height="${PH}" fill="none" stroke="var(--divider-color,#ccc)"/>`;
      for (let k = 0; k <= 2; k++) { const v = lo + (hi - lo) * k / 2, yy = y(v);
        o += `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" stroke="var(--divider-color,#ddd)" stroke-width=".5"/><text x="${L - 4}" y="${yy + 4}" text-anchor="end" fill="var(--secondary-text-color,#888)">${v.toFixed(p.dec)}</text>`; }
      if (p.zero && lo < 0 && hi > 0) o += `<line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" stroke="${p.color}" stroke-dasharray="3 3" stroke-width=".8"/>`;
      for (let h = 0; h * 3600e3 <= span; h += stepH) { const xx = x(t0 + h * 3600e3);
        o += `<line x1="${xx}" x2="${xx}" y1="${top}" y2="${bot}" stroke="var(--divider-color,#ddd)" stroke-width=".5"/>`;
        if (i === panels.length - 1) o += `<text x="${xx}" y="${bot + 13}" text-anchor="middle" fill="var(--secondary-text-color,#888)">${hm(h * 3600)}</text>`; }
      if (p.pts.length) {
        // step line: a value holds until the next change
        let d = "", prev = null;
        for (const [t, v] of p.pts) { const xx = x(Math.max(t, t0)).toFixed(1), yy = y(v).toFixed(1);
          d += prev === null ? `M${xx},${yy}` : `H${xx}V${yy}`; prev = yy; }
        o += `<path d="${d}" fill="none" stroke="${p.color}" stroke-width="1.6"/>`;
      } else o += `<text x="${(L + W - R) / 2}" y="${top + PH / 2}" text-anchor="middle" fill="var(--secondary-text-color,#888)">geen data</text>`;
    });
    return o + `</svg>`;
  }
  _paramKv(key, q) {
    if (!q) return "";
    const kv = [];
    const add = (k, v) => kv.push(`<div>${esc(k)}</div><div>${esc(v)}</div>`);
    const chg = ["kwaliteit", "capaciteit", "laden", "cyclus", "refresh", "opslag"].includes(key);
    const dis = ["kwaliteit", "capaciteit", "ontladen", "cyclus", "refresh", "opslag"].includes(key);
    if (key === "ir") { add("Ontlaadstroom", "1,00 A, 15 s stabiel"); add("Resultaat", "IR in mΩ op het kanaal"); return `<div class="kv">${kv.join("")}</div>`; }
    if (chg) { add("Laadstroom", `${(q.chg / 1000).toFixed(2)} A`); add("Laadspanning", `${(q.vmax / 1000).toFixed(2)} V`); add("Eindstroom", `${q.endc} mA`); }
    if (dis) { add("Ontlaadstroom", `${(q.dis / 1000).toFixed(2)} A (0,2C)`); add("Ontlaadgrens", `${(q.vcut / 1000).toFixed(2)} V`); }
    if (["kwaliteit", "capaciteit", "opslag"].includes(key)) add("Opslagspanning", `${(q.vsto / 1000).toFixed(2)} V`);
    add("Capaciteitslimiet", `${q.caplimit} mAh`); add("Tijdslimiet/stap", `${hm(q.tcut * 60)} u`); add("Temperatuurgrens", `${q.temp} °C`);
    return `<div class="kv">${kv.join("")}</div>`;
  }
  _runHtml(c) {
    const P = PROGRAMS.find((p) => p.key === c.prog);
    const kv = `<div class="kv">
      <div>Status</div><div>${esc(c.status)}</div>
      <div>Spanning</div><div>${c.v !== null ? c.v.toFixed(3) + " V" : "–"}</div>
      <div>Stroom</div><div>${c.a !== null ? c.a.toFixed(2) + " A" : "–"}</div>
      <div>Capaciteit (stap)</div><div>${c.mah !== null ? Math.round(c.mah) + " mAh" : "–"}</div>
      <div>Temperatuur</div><div>${c.temp !== null ? c.temp.toFixed(0) + " °C" : "–"}</div>
      <div>IR</div><div>${c.ir !== null ? c.ir + " mΩ" : "–"}</div>
      <div>Verstreken (stap)</div><div>${esc(c.elapsed || "–")}</div></div>`;
    let steps = "";
    if (c.testActive) {
      const seq = SEQ[c.prog].filter((s) => c.prog !== "kwaliteit" || this._s("input_boolean.mc3000_zelfontlading") === "on" || !s.startsWith("zo_"));
      const cur = ["koelen", "pauze"].includes(c.stage) ? c.kt[3] : c.stage;
      const idx = seq.indexOf(cur);
      steps = `<div class="sec-t">Verloop</div><div class="steps">` + seq.map((s, i) => {
        const cls = i < idx ? "done" : i === idx ? "now" : "";
        let sd = "";
        if (i === idx) sd = ["koelen", "pauze"].includes(c.stage) ? `${STAGES[c.stage]} ${this._left(c)}` : this._left(c);
        return `<div class="step ${cls}"><div class="bul">${i < idx ? "✓" : ""}</div><div><div class="sn">${esc(STAGES[s])}</div>${sd ? `<div class="sd">${esc(sd)}</div>` : ""}</div></div>`;
      }).join("") + `<div class="step"><div class="bul"></div><div><div class="sn">Rapport</div></div></div></div>`;
      const data = this._s(`input_text.mc3000_ch${c.ch}_kt_data`) || "";
      const m = Object.fromEntries(data.split(";").filter((x) => x.includes("=") && !x.startsWith("notes")).map((x) => x.split("=")));
      const ml = { v_vol: ["Na laden", "mV"], v_rust: ["Na 30 min rust", "mV"], v_leeg: ["Na ontladen + rust", "mV"], ir: ["IR-test", "mΩ"], sd0: ["Start zelfontlading", "mV"], sd: ["Zelfontlading 24 u", "mV"] };
      const mk = Object.keys(ml).filter((k) => m[k] !== undefined);
      if (mk.length) steps += `<div class="sec-t">Metingen tot nu toe</div><div class="kv">${mk.map((k) => `<div>${ml[k][0]}</div><div>${esc(m[k])} ${ml[k][1]}</div>`).join("")}</div>`;
    }
    return `<div class="hint" style="margin-bottom:10px"><b>${esc(P?.name || "Programma")}</b>${c.cell ? ` · <span class="dotc" style="background:${esc(c.cell.color)}"></span>${esc(c.type?.name || "")}${c.cell.print ? " · " + esc(c.cell.print) : ""}` : ""}</div>${kv}${steps}`;
  }

  /* ---------------- report sheet ---------------- */
  _openReport(ch, tag) {
    const list = this._reports.filter((r) => String(r.channel) === String(ch));
    const r = tag ? this._reports.find((x) => x.tag === tag) : list[0];
    if (!r) { this._toast(this._reports.length ? `Nog geen rapport voor kanaal ${ch}` : "Rapporten nog niet geladen"); return; }
    const pm = r.params || {};
    const m = r.metingen || {};
    const v = (x, u = "") => (x === null || x === undefined || x === "" ? "–" : `${x}${u}`);
    const kv = (rows) => `<div class="kv">${rows.filter((x) => x[1] !== "–").map(([k, val]) => `<div>${esc(k)}</div><div>${esc(val)}</div>`).join("")}</div>`;
    const meas = kv([
      ["SOH", v(r.soh_percent, " %")], ["Capaciteit (ontladen)", v(r.discharged_mah, " mAh")], ["Nominaal", v(r.rated_capacity_mah, " mAh")],
      ["Geladen", v(r.charged_mah, " mAh")], ["Volledig ontladen", r.full_discharge ? "ja" : "nee"],
      ["IR-test volle cel", v(m.ir, " mΩ")], ["IR mediaan (ontladen)", v(r.resistance_median_mohm, " mΩ")], ["IR piek", v(r.resistance_max_mohm, " mΩ")],
      ["Spanning na laden", v(m.v_vol, " mV")], ["Na 30 min rust", v(m.v_rust, " mV")], ["Na ontladen + rust", v(m.v_leeg, " mV")],
      ["Zelfontlading 24 u", v(m.sd, " mV")], ["Laagste ontlaadspanning", v(r.discharge_min_mv, " mV")],
      ["Temperatuur start / max", r.temp_start_c != null ? `${r.temp_start_c} / ${r.temp_max_c} °C` : "–"], ["Cycli", v(r.cycles)],
    ]);
    const set = kv([
      ["Laadspanning", pm.charge_cutoff_mv ? (pm.charge_cutoff_mv / 1000).toFixed(2) + " V" : "–"],
      ["Ontlaadgrens", pm.discharge_cutoff_mv || r.discharge_cutoff_mv ? ((pm.discharge_cutoff_mv || r.discharge_cutoff_mv) / 1000).toFixed(2) + " V" : "–"],
      ["Eindstroom laden", v(pm.charge_end_ma, " mA")], ["Eindstroom ontladen", v(pm.discharge_end_ma, " mA")],
      ["Capaciteitslimiet", v(pm.capacity_limit_mah, " mAh")], ["Tijdslimiet", pm.time_cutoff_min ? hm(pm.time_cutoff_min * 60) + " u" : "–"],
      ["Temperatuurgrens", v(pm.temp_cutoff_c, " °C")], ["Nominale IR", v(r.rated_resistance_mohm, " mΩ")],
    ]);
    const cellTxt = r.cell ? `${r.cell.type_name || ""}${r.cell.print ? " · " + r.cell.print : ""}${r.cell.note ? " · " + r.cell.note : ""}` : r.battery;
    const others = list.filter((x) => x.tag !== r.tag).slice(0, 12).map((x) => `<div class="item" data-tag="${esc(x.tag)}"><span class="sw" style="background:${esc(x.cell?.color || "#bbb")}"></span><div class="tx"><div class="t1">${esc(fmtDate(x.end))} · ${esc(x.program)}</div><div class="t2">${esc(x.battery)} · ${x.soh_percent != null ? x.soh_percent + "%" : "–"}</div></div><span class="chip ${verdictClass(x.verdict)}">${esc(verdictShort(x.verdict))}</span></div>`).join("");
    const html = `
      <div class="hint">${esc(cellTxt)}<br>${esc(r.program)} · ${esc(fmtDate(r.start))} – ${esc(fmtDate(r.end))} · ${esc(r.status)}</div>
      <div style="margin:10px 0"><span class="chip ${verdictClass(r.verdict)}" style="font-size:13px;padding:4px 10px">${esc(r.verdict || "–")}</span></div>
      ${r.svg ? `<img class="chart" src="${esc(r.svg)}" alt="grafiek spanning en stroom">` : `<div class="hint">Grafiek nog niet lokaal beschikbaar.</div>`}
      <div class="sec-t">Conclusie</div><ul class="rs">${(r.verdict_reasons || []).map((x) => `<li>${md(x)}</li>`).join("")}</ul>
      <div class="sec-t">Gemeten</div>${meas}
      <div class="sec-t">Ingesteld</div>${set}
      <div class="sec-t">Verklaring einde</div><ul class="rs">${(r.explanation || []).map((x) => `<li>${md(x)}</li>`).join("")}</ul>
      ${r.flags?.length ? `<div class="sec-t">Waarschuwingen</div><ul class="rs">${r.flags.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      <div class="foot">${r.url ? `<a class="btn sec" href="${esc(r.url)}" target="_blank" rel="noopener" style="text-decoration:none">Op GitHub</a>` : ""}</div>
      ${others ? `<div class="sec-t">Eerdere rapporten kanaal ${ch}</div><div class="list">${others}</div>` : ""}`;
    this._openModal(`Kanaal ${ch} · rapport`, html, (body) => {
      body.querySelectorAll("[data-tag]").forEach((el) => el.addEventListener("click", () => this._openReport(ch, el.dataset.tag)));
    }, { dot: r.cell?.color });
  }

  /* ============================ CELLEN ============================ */
  _renderCells() {
    const db = this._db();
    const ids = Object.keys(db.cells).filter((id) => this._cellFilter === "all" || db.cells[id].fav);
    const inCh = {};
    for (const c of CH) { const id = this._s(`input_text.mc3000_ch${c}_cel`); if (id) (inCh[id] = inCh[id] || []).push(c); }
    const byType = {};
    for (const id of ids) (byType[db.cells[id].type] = byType[db.cells[id].type] || []).push(id);
    const tkeys = Object.keys(byType).sort((a, b) => (db.types[a]?.name || a).localeCompare(db.types[b]?.name || b));
    const cellsHtml = tkeys.map((tk) => {
      const t = db.types[tk] || { name: tk };
      const rows = byType[tk].sort((a, b) => (db.cells[a].print || "").localeCompare(db.cells[b].print || "")).map((id) => {
        const x = db.cells[id];
        const reps = this._reports.filter((r) => r.cell?.id === id);
        const last = reps[0];
        return `<div class="item" data-id="${esc(id)}">
          <span class="star ${x.fav ? "on" : ""}" data-star="${esc(id)}">${x.fav ? "★" : "☆"}</span>
          <span class="sw" style="background:${esc(x.color || "#888")}"></span>
          <div class="tx"><div class="t1">${x.print ? esc(x.print) : `<span style="color:var(--secondary-text-color)">(geen print)</span>`}</div>
          <div class="t2">${esc(x.note || "geen notitie")}</div></div>
          ${inCh[id] ? `<span class="badge">Ch${inCh[id].join(",")}</span>` : ""}
          ${last ? `<span class="chip ${verdictClass(last.verdict)}">${last.soh_percent != null ? last.soh_percent + "%" : esc(verdictShort(last.verdict))}</span>` : ""}
        </div>`;
      }).join("");
      return `<div class="grp"><div class="grp-h">${esc(t.name)} <span class="badge">${esc(t.chem || "")} · ${esc(t.cap || "?")} mAh</span></div><div class="list">${rows}</div></div>`;
    }).join("");
    const usedTypes = new Set(Object.values(db.cells).map((x) => x.type));
    const allTypes = Object.keys(db.types);
    const shownTypes = this._showAllTypes ? allTypes : allTypes.filter((k) => usedTypes.has(k));
    const typesHtml = shownTypes.sort((a, b) => db.types[a].name.localeCompare(db.types[b].name)).map((tk) => {
      const t = db.types[tk]; const p = params(t, "laden");
      const n = Object.values(db.cells).filter((x) => x.type === tk).length;
      const sp = (k, val) => `<div><div class="sk">${k}</div><div class="sv">${val}</div></div>`;
      return `<div class="typecard" data-type="${esc(tk)}"><div class="tn"><span>${esc(t.name)}</span><span class="badge">${n} cel${n === 1 ? "" : "len"}</span></div>
        <div class="specs">${sp("Chemie", esc(t.chem || "LiIon"))}${sp("Capaciteit", `${esc(t.cap)}`)}${sp("Std. laden", t.chg_std ? `${(t.chg_std / 1000).toFixed(2)} A` : "–")}${sp("Max ontl.", t.dis_max ? `${t.dis_max} A` : "–")}
        ${sp("V max", (p.vmax / 1000).toFixed(2))}${sp("V min", (p.vcut / 1000).toFixed(2))}${sp("V opslag", p.vsto ? (p.vsto / 1000).toFixed(2) : "–")}${sp("IR mΩ", t.ir || "–")}</div></div>`;
    }).join("");
    const empty = !Object.keys(db.types).length;
    this._main.innerHTML = `
      ${empty ? `<div class="box" style="margin-bottom:12px"><div class="empty">De celdatabase is leeg.</div><div class="foot"><button class="btn" data-a="restore">Herstel uit backup</button></div></div>` : ""}
      <div class="toolbar"><div class="seg"><button data-f="fav" class="${this._cellFilter === "fav" ? "on" : ""}">★ Favorieten</button><button data-f="all" class="${this._cellFilter === "all" ? "on" : ""}">Alle cellen</button></div>
        <span style="flex:1"></span><button class="btn small" data-a="newcell">+ Cel</button></div>
      ${cellsHtml || `<div class="empty">${this._cellFilter === "fav" ? "Geen favorieten. Kies “Alle cellen” en tik op ☆." : "Nog geen cellen."}</div>`}
      <div class="row-label" style="margin-top:22px;display:flex;align-items:center"><span style="flex:1">Celtypes · datasheet</span><button class="btn small sec" data-a="newtype">+ Type</button></div>
      <div class="list">${typesHtml}</div>
      ${allTypes.length > shownTypes.length || this._showAllTypes ? `<div class="foot"><button class="btn sec small" data-a="alltypes">${this._showAllTypes ? "Alleen types in gebruik" : `Alle ${allTypes.length} types tonen`}</button></div>` : ""}
      <div class="hint" style="margin-top:14px">Wijzigingen worden direct opgeslagen in Home Assistant (sensor.mc3000_cellen) met een backup in /config/mc3000_cellen.json. De programma's rekenen met de waarden van het celtype; print, notitie en kleur zijn per cel.</div>`;
    const M = this._main;
    M.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => { this._cellFilter = b.dataset.f; this._lastSig = ""; this._render(); }));
    M.querySelector('[data-a="newcell"]').addEventListener("click", () => this._editCell(null));
    M.querySelector('[data-a="alltypes"]')?.addEventListener("click", () => { this._showAllTypes = !this._showAllTypes; this._lastSig = ""; this._render(); });
    M.querySelector('[data-a="newtype"]').addEventListener("click", () => this._editType(null));
    M.querySelector('[data-a="restore"]')?.addEventListener("click", async () => { await this._call("shell_command", "mc3000_db_restore", {}); this._toast("Backup teruggezet"); });
    M.querySelectorAll("[data-star]").forEach((s) => s.addEventListener("click", async (e) => {
      e.stopPropagation();
      const d = this._db(); const id = s.dataset.star;
      d.cells[id] = { ...d.cells[id], fav: !d.cells[id].fav };
      s.classList.toggle("on"); s.textContent = d.cells[id].fav ? "★" : "☆";
      await this._saveDb(d.types, d.cells);
    }));
    M.querySelectorAll(".item[data-id]").forEach((el) => el.addEventListener("click", () => this._editCell(el.dataset.id)));
    M.querySelectorAll("[data-type]").forEach((el) => el.addEventListener("click", () => this._editType(el.dataset.type)));
  }

  _editCell(id, preset) {
    const db = this._db();
    const isNew = !id;
    const x = preset || (id ? { ...db.cells[id] } : { type: Object.keys(db.types)[0] || "", print: "", note: "", color: PALETTE[5], fav: true });
    const tOpts = Object.keys(db.types).sort((a, b) => db.types[a].name.localeCompare(db.types[b].name)).map((k) => `<option value="${esc(k)}" ${k === x.type ? "selected" : ""}>${esc(db.types[k].name)}</option>`).join("");
    const reps = id ? this._reports.filter((r) => r.cell?.id === id) : [];
    const html = `
      <label class="f">Celtype</label><select class="in" data-k="type">${tOpts}</select>
      <label class="f">Print (exacte tekst op de cel)</label><input class="in" data-k="print" value="${esc(x.print || "")}" placeholder="bv. INR18650HE4 LGDBHE41865 H17">
      <label class="f">Notitie (herkomst)</label><textarea class="in" data-k="note" placeholder="bv. uit accupack Bosch 18V, 2019">${esc(x.note || "")}</textarea>
      <label class="f">Kleur (dashboard en rapport)</label>
      <div class="pal">${PALETTE.map((c) => `<span data-c="${c}" class="${c.toLowerCase() === (x.color || "").toLowerCase() ? "on" : ""}" style="background:${c}"></span>`).join("")}<input type="color" data-k="color" value="${esc(x.color || "#0090ff")}"></div>
      <label class="f" style="display:flex;gap:8px;align-items:center;font-size:14px;color:var(--primary-text-color)"><input type="checkbox" data-k="fav" ${x.fav ? "checked" : ""}> ★ Favoriet (snel te kiezen op het overzicht)</label>
      <div class="foot"><button class="btn" data-a="save">Opslaan</button>
      ${isNew ? "" : `<button class="btn sec" data-a="split">Afsplitsen</button><button class="btn danger" data-a="del">Verwijderen</button>`}</div>
      ${!isNew ? `<div class="hint" style="margin-top:8px">Afsplitsen maakt een nieuwe cel van hetzelfde type, bv. voor cellen uit een andere bron of met een iets andere print.</div>` : ""}
      ${reps.length ? `<div class="sec-t">Rapporten van deze cel</div><div class="list">${reps.slice(0, 10).map((r) => `<div class="item" data-tag="${esc(r.tag)}" data-ch="${r.channel}"><div class="tx"><div class="t1">${esc(fmtDate(r.end))} · Ch${r.channel} · ${esc(r.program)}</div><div class="t2">${r.discharged_mah ?? "–"} mAh · ${r.soh_percent != null ? r.soh_percent + "%" : "–"}</div></div><span class="chip ${verdictClass(r.verdict)}">${esc(verdictShort(r.verdict))}</span></div>`).join("")}</div>` : ""}`;
    this._openModal(isNew ? "Nieuwe cel" : "Cel bewerken", html, (b) => {
      let color = x.color || "#0090ff";
      const picker = b.querySelector('[data-k="color"]');
      b.querySelectorAll("[data-c]").forEach((s) => s.addEventListener("click", () => { color = s.dataset.c; picker.value = color; b.querySelectorAll("[data-c]").forEach((o) => o.classList.toggle("on", o === s)); }));
      picker.addEventListener("input", () => { color = picker.value; b.querySelectorAll("[data-c]").forEach((o) => o.classList.remove("on")); });
      const read = () => ({ type: b.querySelector('[data-k="type"]').value, print: b.querySelector('[data-k="print"]').value.trim(),
        note: b.querySelector('[data-k="note"]').value.trim(), color, fav: b.querySelector('[data-k="fav"]').checked });
      b.querySelector('[data-a="save"]').addEventListener("click", async () => {
        const d = this._db(); const v = read();
        if (!v.type) { this._toast("Kies een celtype (maak er eerst een aan)"); return; }
        d.cells[id || newId()] = v; await this._saveDb(d.types, d.cells); this._closeModal(); this._toast("Opgeslagen");
      });
      b.querySelector('[data-a="split"]')?.addEventListener("click", () => { const v = read(); this._closeModal(); this._editCell(null, { ...v, print: v.print, note: v.note ? v.note + " (afgesplitst)" : "afgesplitst" }); });
      b.querySelector('[data-a="del"]')?.addEventListener("click", async () => {
        const used = CH.filter((c) => this._s(`input_text.mc3000_ch${c}_cel`) === id);
        if (!confirm(`Cel verwijderen?${used.length ? ` Staat nu op kanaal ${used.join(", ")}.` : ""} Rapporten blijven bestaan.`)) return;
        const d = this._db(); delete d.cells[id]; await this._saveDb(d.types, d.cells); this._closeModal(); this._toast("Verwijderd");
      });
      b.querySelectorAll("[data-tag]").forEach((el) => el.addEventListener("click", () => this._openReport(+el.dataset.ch, el.dataset.tag)));
    }, { dot: isNew ? null : x.color });
  }

  _editType(tk) {
    const db = this._db();
    const isNew = !tk;
    const t = tk ? { ...db.types[tk] } : { name: "", chem: "LiIon", cap: 2500, chg_std: 1250, dis_max: "", v_max: 4200, v_cut: 2500, v_store: 3700, ir: "" };
    const f = (k, label, type = "number", extra = "") => `<div><label class="f">${label}</label><input class="in" data-k="${k}" type="${type}" inputmode="${type === "number" ? "decimal" : "text"}" value="${esc(t[k] ?? "")}" ${extra}></div>`;
    const used = tk ? Object.values(db.cells).filter((x) => x.type === tk).length : 0;
    const html = `
      ${f("name", "Naam (merk + type)", "text", 'placeholder="bv. Samsung INR18650-30Q"')}
      <label class="f">Chemie (batterijtype lader)</label><select class="in" data-k="chem">${CHEMS.map((c) => `<option ${c === (t.chem || "LiIon") ? "selected" : ""}>${c}</option>`).join("")}</select>
      <div class="two">${f("cap", "Nominale capaciteit (mAh)")}${f("chg_std", "Standaard laadstroom (mA)")}
      ${f("dis_max", "Max. continue ontlaadstroom (A)")}${f("ir", "IR datasheet (mΩ, AC 1 kHz)")}
      ${f("v_max", "Laadspanning (mV)")}${f("v_cut", "Ontlaadgrens (mV)")}${f("v_store", "Opslagspanning (mV)")}</div>
      <div class="hint" style="margin-top:8px">Laden = min(standaard laadstroom, 0,5C, 2 A) · ontladen 0,2C · eindstroom C/20 · capaciteitslimiet 1,25× nominaal. Leeg laten = standaard voor de chemie.</div>
      <div class="foot"><button class="btn" data-a="save">Opslaan</button>${isNew ? "" : `<button class="btn danger" data-a="del" ${used ? "disabled" : ""}>Verwijderen</button>`}</div>
      ${used ? `<div class="hint">${used} cel(len) gebruiken dit type; verwijderen kan pas als die weg zijn.</div>` : ""}`;
    this._openModal(isNew ? "Nieuw celtype" : esc(t.name), html, (b) => {
      b.querySelector('[data-a="save"]').addEventListener("click", async () => {
        const v = {};
        b.querySelectorAll("[data-k]").forEach((i) => { const k = i.dataset.k; v[k] = ["name", "chem"].includes(k) ? i.value.trim() : (i.value === "" ? null : Number(String(i.value).replace(",", "."))); });
        if (!v.name || !v.cap) { this._toast("Naam en capaciteit zijn verplicht"); return; }
        const d = this._db();
        let key = tk;
        if (!key) { key = slug(v.name); while (d.types[key]) key += "_2"; }
        d.types[key] = v; await this._saveDb(d.types, d.cells); this._closeModal(); this._toast("Opgeslagen");
      });
      b.querySelector('[data-a="del"]')?.addEventListener("click", async () => {
        if (!confirm("Celtype verwijderen?")) return;
        const d = this._db(); delete d.types[tk]; await this._saveDb(d.types, d.cells); this._closeModal();
      });
    });
  }

  /* ========================== PROGRAMMA'S ========================== */
  _renderPrograms() {
    const db = this._db();
    const ids = Object.keys(db.cells).sort((a, b) => ((db.cells[b].fav ? 1 : 0) - (db.cells[a].fav ? 1 : 0)) || (db.types[db.cells[a].type]?.name || "").localeCompare(db.types[db.cells[b].type]?.name || ""));
    if (!this._progCell || !db.cells[this._progCell]) this._progCell = this._s("input_text.mc3000_ch1_cel") || ids[0];
    const cell = db.cells[this._progCell];
    const t = cell ? db.types[cell.type] : null;
    const opts = ids.map((id) => { const x = db.cells[id]; return `<option value="${esc(id)}" ${id === this._progCell ? "selected" : ""}>${x.fav ? "★ " : ""}${esc(db.types[x.type]?.name || "?")}${x.print ? " · " + esc(x.print) : ""}</option>`; }).join("");
    const zo = this._s("input_boolean.mc3000_zelfontlading") === "on";
    const cards = PROGRAMS.map((P) => {
      const q = params(t, P.key === "ontladen" ? "ontladen" : P.key === "laden" ? "laden" : "x");
      let extra = "";
      if (P.key === "kwaliteit" && q) {
        const st = [
          ["Laden (1/2)", `CC/CV ${(q.chg / 1000).toFixed(2)} A tot ${(q.vmax / 1000).toFixed(2)} V, stop bij ${q.endc} mA`],
          ["Rust 30 min", "spanning direct na laden en na rust (terugval)"],
          ["Capaciteit meten", `0,2C = ${(q.dis / 1000).toFixed(2)} A tot ${(q.vcut / 1000).toFixed(2)} V`],
          ["Rust 30 min", "herstelspanning van de lege cel"],
          ["Laden (2/2)", "van leeg tot vol: laad/ontlaadverhouding"],
          ["Rust 30 min", "cel op rusttemperatuur, volle OCV"],
          ["IR-test", "1 A, 15 s stabiel, bij volle cel (vergelijkbaar tussen cellen)"],
          ["Opslagspanning", `${(q.vsto / 1000).toFixed(2)} V`],
          ...(zo ? [["Rust 1 u", "beginspanning vastleggen"], ["Zelfontlading 24 u", "daling in mV: ≤10 goed, >20 lekstroom"]] : []),
          ["Rapport", "grafiek, oordeel en verklaring op GitHub en hier"],
        ];
        extra = `<div class="sec-t">Stappen</div><div class="steps">${st.map((s, i) => `<div class="step"><div class="bul">${i + 1}</div><div><div class="sn">${esc(s[0])}</div><div class="sd">${esc(s[1])}</div></div></div>`).join("")}</div>
          <label class="f" style="display:flex;gap:8px;align-items:center;font-size:14px;color:var(--primary-text-color)"><input type="checkbox" data-a="zo" ${zo ? "checked" : ""}> Zelfontlading meten (1 u + 24 u)</label>
          <div class="sec-t">Bij een fout</div><ul class="rs">
            <li><b>Temperatuur</b> (lader of ESP-noodstop): afkoelen tot 32 °C, stap hervatten met 25% minder stroom (max. 2×).</li>
            <li><b>Tijdslimiet</b>: 15 min pauze en de stap hervatten; tweede keer: stap als klaar beschouwen en door.</li>
            <li><b>Capacity protection</b>: niet verder laden (verdacht), wel rust- en zelfontladingstest en daarna opslagspanning.</li>
            <li><b>Voeding</b> (ingangsspanning): 15 min pauze en hervatten (max. 2×).</li>
            <li><b>Onderbreking, kortsluiting, ompoling, cel verwijderd</b>: test afbreken, rapport met verklaring.</li></ul>`;
      }
      return `<div class="prog"><div class="ph"><ha-icon icon="${P.icon}"></ha-icon><div class="pt">${esc(P.name)}</div>${P.best ? `<span class="best">aanbevolen</span>` : ""}</div>
        <p>${esc(P.desc)}</p>${q ? `<div class="hint" style="margin-bottom:6px">Duur ${esc(duration(P.key, q))}</div>${this._paramKv(P.key, q)}` : `<div class="hint">Kies een cel om de waarden te zien.</div>`}${extra}</div>`;
    }).join("");
    this._main.innerHTML = `
      <div class="box" style="margin-bottom:12px"><label class="f" style="margin-top:0">Waarden voor cel</label><select class="in" data-a="cell">${opts}</select>
      <div class="hint" style="margin-top:8px">Alle programma's gebruiken temperatuurgrens 45 °C op de lader en de ESP-noodstop (${esc(this._s("number.skyrc_mc3000_safety_max_channel_temperature") || "47")} °C). Starten doe je op het Overzicht via de programmaknop onder een kanaal.</div></div>
      ${cards}`;
    this._main.querySelector('[data-a="cell"]')?.addEventListener("change", (e) => { this._progCell = e.target.value; this._lastSig = ""; this._render(); });
    this._main.querySelector('[data-a="zo"]')?.addEventListener("change", (e) =>
      this._call("input_boolean", e.target.checked ? "turn_on" : "turn_off", { entity_id: "input_boolean.mc3000_zelfontlading" }));
  }
}

if (!customElements.get("mc3000-card")) {
  customElements.define("mc3000-card", Mc3000Card);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: "mc3000-card", name: "SkyRC MC3000", description: "Overzicht, celdatabase en programma's van de MC3000" });
  console.info(`%c MC3000-CARD %c ${VERSION} `, "background:#e5484d;color:#fff;border-radius:3px 0 0 3px", "background:#333;color:#fff;border-radius:0 3px 3px 0");
}
