"""
Step 6: Build the dashboard.

Queries the SQLite database and writes a single self-contained HTML file to
output/dashboard.html — no external libraries, no internet needed. Open it in
a browser (or commit it / screenshot it for your portfolio).

Run:  python scripts/06_build_dashboard.py

If the DB was populated by 00_demo_seed.py, the dashboard renders a loud
"DEMO DATA" banner. That banner disappears automatically once the data came
from the real pipeline (scripts 01-05) — see IS_DEMO detection below.
"""

import json
import os
from collections import defaultdict

from utils import setup_logging, get_connection, PROJECT_ROOT

logger = setup_logging(__name__)

OUT_DIR = os.path.join(PROJECT_ROOT, "output")
OUT_PATH = os.path.join(OUT_DIR, "dashboard.html")

# Sales below this are nominal transfers, not arm's-length market sales.
MIN_PLAUSIBLE_SALE_PRICE = 10_000
REITS = ("SLG", "VNO", "ESRT")


# ---------------------------------------------------------------- queries
# Dashboard queries mirror the portfolio queries in sql/queries.sql.

def q_kpis(cur):
    tickers = cur.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    days = cur.execute("SELECT COUNT(DISTINCT date) FROM daily_prices").fetchone()[0]
    valid = cur.execute(
        "SELECT COUNT(*) FROM nyc_property_sales WHERE sale_price >= ? AND gross_sqft > 0",
        (MIN_PLAUSIBLE_SALE_PRICE,),
    ).fetchone()[0]
    # The DB holds only surviving rows, so the drop count comes from the
    # lineage stamped at load time — never from counting what is not there.
    def meta(key, default=0):
        try:
            r = cur.execute("SELECT value FROM pipeline_meta WHERE key=?", (key,)).fetchone()
            return int(r[0]) if r else default
        except Exception:
            return default
    junk = meta("dropped_rows")
    source_rows = meta("source_rows", valid + junk)
    lo, hi = cur.execute("SELECT MIN(date), MAX(date) FROM daily_prices").fetchone()
    return {"tickers": tickers, "days": days, "valid": valid, "junk": junk,
            "pct_junk": round(100 * junk / max(1, source_rows), 1),
            "range": f"{lo} to {hi}"}


def q_reit_index(cur):
    """Each REIT's close indexed to 100 at its first trading day (one axis)."""
    rows = cur.execute(
        "SELECT p.ticker, p.date, p.close FROM daily_prices p "
        "JOIN companies c ON c.ticker = p.ticker "
        f"WHERE p.ticker IN ({','.join('?' * len(REITS))}) ORDER BY p.ticker, p.date",
        REITS,
    ).fetchall()
    series = defaultdict(list)
    for t, d, c in rows:
        series[t].append((d, c))
    out = []
    for t in REITS:
        pts = series.get(t, [])
        if not pts:
            continue
        base = pts[0][1]
        out.append({"name": t,
                    "points": [{"x": d, "y": round(100 * c / base, 2)} for d, c in pts]})
    return out


def q_psf_by_borough(cur):
    """Median-ish (avg) price per square foot by borough, junk filtered."""
    rows = cur.execute(
        "SELECT borough, AVG(sale_price / gross_sqft) AS psf, COUNT(*) AS n "
        "FROM nyc_property_sales "
        "WHERE sale_price >= ? AND gross_sqft > 0 "
        "GROUP BY borough ORDER BY psf DESC",
        (MIN_PLAUSIBLE_SALE_PRICE,),
    ).fetchall()
    return [{"label": b, "value": round(p, 2), "n": n} for b, p, n in rows]


def q_monthly_volume(cur):
    rows = cur.execute(
        "SELECT substr(sale_date, 1, 7) AS ym, COUNT(*) "
        "FROM nyc_property_sales "
        "WHERE sale_price >= ? AND gross_sqft > 0 "
        "GROUP BY ym ORDER BY ym",
        (MIN_PLAUSIBLE_SALE_PRICE,),
    ).fetchall()
    return [{"x": ym, "y": n} for ym, n in rows]


def q_reit_vs_property(cur):
    """
    THE CENTERPIECE QUERY.

    Both series indexed to 100 at the first month so they share ONE axis —
    never a dual-axis chart. Answers: does public REIT stock performance
    actually track physical NYC property prices?
    """
    reit = cur.execute(
        "SELECT substr(p.date, 1, 7) AS ym, AVG(p.close) "
        f"FROM daily_prices p WHERE p.ticker IN ({','.join('?' * len(REITS))}) "
        "GROUP BY ym ORDER BY ym",
        REITS,
    ).fetchall()
    prop = cur.execute(
        "SELECT substr(sale_date, 1, 7) AS ym, AVG(sale_price / gross_sqft) "
        "FROM nyc_property_sales WHERE sale_price >= ? AND gross_sqft > 0 "
        "GROUP BY ym ORDER BY ym",
        (MIN_PLAUSIBLE_SALE_PRICE,),
    ).fetchall()

    def index(rows):
        if not rows:
            return []
        base = rows[0][1]
        return [{"x": ym, "y": round(100 * v / base, 2)} for ym, v in rows]

    months = sorted({ym for ym, _ in reit} & {ym for ym, _ in prop})
    keep = lambda s: [p for p in s if p["x"] in months]
    return [
        {"name": "NYC REIT stocks", "points": keep(index(reit))},
        {"name": "NYC property $/sqft", "points": keep(index(prop))},
    ]


def q_sector_returns(cur):
    rows = cur.execute(
        "SELECT c.sector, AVG(r.daily_return) * 100 AS avg_ret, COUNT(DISTINCT c.ticker) "
        "FROM daily_returns r JOIN companies c ON c.ticker = r.ticker "
        "WHERE r.daily_return IS NOT NULL AND c.is_nyc_hq = 1 "
        "GROUP BY c.sector ORDER BY avg_ret DESC"
    ).fetchall()
    return [{"label": s, "value": round(v, 4), "n": n} for s, v, n in rows]


def detect_demo(cur) -> bool:
    """
    Read provenance from the pipeline_meta table rather than guessing.

    The previous version inferred "demo" from the row count and a ticker
    flag — values the REAL pipeline also produces, so real dashboards were
    stamped DEMO. Provenance is a fact to record at write time, never a
    property to infer at read time.
    """
    try:
        row = cur.execute(
            "SELECT value FROM pipeline_meta WHERE key = 'data_source'"
        ).fetchone()
    except Exception:
        return False          # table absent => built by the real pipeline
    return bool(row) and row[0] == "demo"


# ---------------------------------------------------------------- render

def build_html(data):
    return HTML_TEMPLATE.replace("__DATA__", json.dumps(data))


def main():
    conn = get_connection()
    cur = conn.cursor()

    data = {
        "isDemo": detect_demo(cur),
        "kpis": q_kpis(cur),
        "reitIndex": q_reit_index(cur),
        "psfByBorough": q_psf_by_borough(cur),
        "monthlyVolume": q_monthly_volume(cur),
        "reitVsProperty": q_reit_vs_property(cur),
        "sectorReturns": q_sector_returns(cur),
    }
    conn.close()

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(build_html(data))

    logger.info("Dashboard written to %s", OUT_PATH)
    if data["isDemo"]:
        logger.warning("Dashboard built from DEMO data — numbers are not real findings")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NYC Financial &amp; Real Estate Dashboard</title>
<style>
  :root {
    color-scheme: light;
    --page:#f9f9f7; --surface:#fcfcfb;
    --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
    --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
    --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --neg:#d03b3b;
    --warnbg:#fff4e0; --warnfg:#7a4a00; --warnbd:#fab219;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --page:#0d0d0d; --surface:#1a1a19;
      --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
      --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
      --s1:#3987e5; --s2:#d95926; --s3:#199e70; --neg:#e66767;
      --warnbg:#2e2410; --warnfg:#f5c869; --warnbd:#fab219;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --neg:#e66767;
    --warnbg:#2e2410; --warnfg:#f5c869; --warnbd:#fab219;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--page); color:var(--text-primary);
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
    line-height:1.5; padding:24px 16px 64px;
  }
  .wrap { max-width:1120px; margin:0 auto; }
  header h1 { font-size:1.5rem; margin:0 0 4px; letter-spacing:-0.01em; }
  header p { margin:0; color:var(--text-secondary); font-size:0.875rem; }
  .banner {
    background:var(--warnbg); color:var(--warnfg);
    border:1px solid var(--warnbd); border-radius:8px;
    padding:12px 16px; margin:20px 0; font-size:0.875rem;
  }
  .banner strong { display:block; font-size:0.9375rem; margin-bottom:2px; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:20px 0; }
  .kpi { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:16px; }
  .kpi .label { font-size:0.75rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em; }
  .kpi .value { font-size:1.75rem; font-weight:600; margin-top:4px; letter-spacing:-0.02em; }
  .kpi .sub { font-size:0.75rem; color:var(--text-secondary); margin-top:2px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(460px,1fr)); gap:16px; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:18px 18px 12px; overflow:hidden; }
  .card h2 { font-size:0.9375rem; margin:0 0 2px; font-weight:600; }
  .card .sub { font-size:0.8125rem; color:var(--text-secondary); margin:0 0 14px; }
  .chart-scroll { overflow-x:auto; }
  .legend { display:flex; flex-wrap:wrap; gap:14px; margin:10px 0 2px; font-size:0.8125rem; color:var(--text-secondary); }
  .legend span { display:inline-flex; align-items:center; gap:6px; }
  .swatch { width:10px; height:10px; border-radius:2px; flex:none; }
  .toggle { background:none; border:1px solid var(--border); color:var(--text-secondary);
    border-radius:6px; padding:4px 10px; font-size:0.75rem; cursor:pointer; font-family:inherit; margin-top:8px; }
  .toggle:hover { color:var(--text-primary); }
  table { border-collapse:collapse; width:100%; font-size:0.8125rem; margin-top:10px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--grid); font-variant-numeric:tabular-nums; }
  th { color:var(--muted); font-weight:600; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.03em; }
  td.num, th.num { text-align:right; }
  .hidden { display:none; }
  .tip { position:fixed; pointer-events:none; background:var(--surface); color:var(--text-primary);
    border:1px solid var(--border); border-radius:6px; padding:7px 10px; font-size:0.75rem;
    box-shadow:0 4px 14px rgba(0,0,0,0.14); opacity:0; transition:opacity .1s; z-index:50; white-space:nowrap; }
  text { font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }
  footer { margin-top:28px; font-size:0.75rem; color:var(--muted); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>NYC Financial &amp; Real Estate Dashboard</h1>
    <p>Public company performance vs. physical property transactions across New York City</p>
  </header>
  <div id="banner"></div>
  <div class="kpis" id="kpis"></div>
  <div class="grid" id="charts"></div>
  <footer id="foot"></footer>
</div>
<div class="tip" id="tip"></div>
<script>
const DATA = __DATA__;
const tip = document.getElementById('tip');
const fmt = (n,d=0)=>n.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const SERIES = ['--s1','--s2','--s3'];

function showTip(e, html){ tip.innerHTML=html; tip.style.opacity='1';
  tip.style.left=Math.min(e.clientX+14, innerWidth-tip.offsetWidth-8)+'px';
  tip.style.top=(e.clientY-tip.offsetHeight-10)+'px'; }
function hideTip(){ tip.style.opacity='0'; }

/* ---------- banner ---------- */
document.getElementById('banner').innerHTML = DATA.isDemo ? `
  <div class="banner"><strong>⚠ Demo data — these numbers are synthetic</strong>
  Generated by <code>00_demo_seed.py</code> to prove the pipeline works end to end.
  Nothing here is a real finding. Load the real Kaggle datasets (scripts 01–05),
  re-run <code>06_build_dashboard.py</code>, and this banner disappears.</div>` : '';

/* ---------- KPIs ---------- */
const k = DATA.kpis;
document.getElementById('kpis').innerHTML = [
  ['Tickers tracked', fmt(k.tickers), 'NYC-weighted sample'],
  ['Trading days', fmt(k.days), k.range],
  ['Valid property sales', fmt(k.valid), 'after filtering'],
  ['Rows filtered out', fmt(k.junk), k.pct_junk + '% of source records'],
].map(([l,v,s])=>`<div class="kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${s}</div></div>`).join('');

/* ---------- generic line chart (multi-series, indexed, one axis) ---------- */
function lineChart(el, series, opts){
  const W=560, H=260,
        M={t:14,r:64,b:34,l:48}, pw=W-M.l-M.r, ph=H-M.t-M.b;
  const xs = series[0].points.map(p=>p.x);
  const allY = series.flatMap(s=>s.points.map(p=>p.y));
  let lo=Math.min(...allY), hi=Math.max(...allY);
  const pad=(hi-lo)*0.12||1; lo-=pad; hi+=pad;
  const X=i=>M.l+(xs.length<2?pw/2:i*pw/(xs.length-1));
  const Y=v=>M.t+ph-((v-lo)/(hi-lo))*ph;
  const ticks=4, tv=[...Array(ticks+1)].map((_,i)=>lo+(hi-lo)*i/ticks);
  let svg=`<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;height:auto" preserveAspectRatio="xMidYMid meet" role="img">`;
  tv.forEach(v=>{ svg+=`<line x1="${M.l}" y1="${Y(v).toFixed(1)}" x2="${M.l+pw}" y2="${Y(v).toFixed(1)}" stroke="${css('--grid')}" stroke-width="1"/>`
    +`<text x="${M.l-8}" y="${(Y(v)+4).toFixed(1)}" text-anchor="end" font-size="11" fill="${css('--muted')}">${fmt(v,0)}</text>`; });
  const step=Math.max(1,Math.ceil(xs.length/6));
  const shown=[]; xs.forEach((x,i)=>{ if(i%step===0) shown.push(i); });
  const last=xs.length-1;
  if(shown[shown.length-1]!==last){
    if(last-shown[shown.length-1] < step*0.6) shown.pop();
    shown.push(last);
  }
  shown.forEach(i=>{
    svg+=`<text x="${X(i).toFixed(1)}" y="${H-12}" text-anchor="middle" font-size="11" fill="${css('--muted')}">${(opts.xfmt||(v=>v))(xs[i])}</text>`; });
  svg+=`<line x1="${M.l}" y1="${M.t+ph}" x2="${M.l+pw}" y2="${M.t+ph}" stroke="${css('--axis')}" stroke-width="1"/>`;
  series.forEach((s,si)=>{ const c=css(SERIES[si%3]);
    const d=s.points.map((p,i)=>`${i?'L':'M'}${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join('');
    svg+=`<path d="${d}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    const last=s.points[s.points.length-1];
    svg+=`<circle cx="${X(s.points.length-1).toFixed(1)}" cy="${Y(last.y).toFixed(1)}" r="3.5" fill="${c}" stroke="${css('--surface')}" stroke-width="2"/>`
      +`<text x="${(X(s.points.length-1)+8).toFixed(1)}" y="${(Y(last.y)+4).toFixed(1)}" font-size="11" font-weight="600" fill="${css('--text-secondary')}">${fmt(last.y,0)}</text>`;
  });
  svg+=`<line id="cross" x1="0" y1="${M.t}" x2="0" y2="${M.t+ph}" stroke="${css('--axis')}" stroke-width="1" opacity="0"/>`;
  svg+=`<rect x="${M.l}" y="${M.t}" width="${pw}" height="${ph}" fill="transparent" id="hit"/></svg>`;
  el.innerHTML=svg;
  const node=el.querySelector('svg'), cross=node.querySelector('#cross');
  node.querySelector('#hit').addEventListener('mousemove',e=>{
    const r=node.getBoundingClientRect(), sx=(e.clientX-r.left)*(W/r.width);
    let i=Math.round((sx-M.l)/(pw/Math.max(1,xs.length-1))); i=Math.max(0,Math.min(xs.length-1,i));
    cross.setAttribute('x1',X(i)); cross.setAttribute('x2',X(i)); cross.setAttribute('opacity','1');
    showTip(e, `<strong>${(opts.xfmt||(v=>v))(xs[i])}</strong><br>`+
      series.map((s,si)=>`<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${css(SERIES[si%3])};margin-right:5px"></span>${s.name} ${fmt(s.points[i].y,1)}`).join('<br>'));
  });
  node.addEventListener('mouseleave',()=>{cross.setAttribute('opacity','0');hideTip();});
}

/* ---------- generic bar chart (single series) ---------- */
function barChart(el, rows, opts){
  const W=560, rowH=34, H=rows.length*rowH+30,
        M={t:6,r:70,b:24,l:142}, pw=W-M.l-M.r;
  const signed = opts.signed && rows.some(r=>r.value<0);
  const maxAbs = Math.max(...rows.map(r=>Math.abs(r.value)))*1.05 || 1;
  // signed: zero sits mid-plot and bars grow both ways. otherwise zero is at left.
  const zeroX = signed ? M.l+pw/2 : M.l;
  // leave room for the value label that sits outside a negative bar's left end
  const scale = signed ? Math.max(1,(pw/2-52))/maxAbs : pw/maxAbs;
  const cPos=css('--s1'), cNeg=css('--neg');
  let svg=`<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;height:auto" preserveAspectRatio="xMidYMid meet" role="img">`;
  rows.forEach((r,i)=>{ const y=M.t+i*rowH, neg=r.value<0;
    const w=Math.max(2, Math.abs(r.value)*scale);
    const x=neg ? zeroX-w : zeroX;
    const lx=neg ? x-8 : x+w+8;
    // Truncate long category names rather than letting them clip. Full text
    // stays available in the hover tooltip and the data table.
    const lbl = r.label.length > 19 ? r.label.slice(0,18)+'\u2026' : r.label;
    svg+=`<text x="${M.l-10}" y="${y+19}" text-anchor="end" font-size="12" fill="${css('--text-secondary')}">${lbl}</text>`
      +`<rect class="bar" data-i="${i}" x="${x.toFixed(1)}" y="${y+6}" width="${w.toFixed(1)}" height="18" rx="4" fill="${neg?cNeg:cPos}"/>`
      +`<text x="${lx.toFixed(1)}" y="${y+19}" text-anchor="${neg?'end':'start'}" font-size="11" font-weight="600" fill="${css('--text-secondary')}">${opts.vfmt(r.value)}</text>`;
  });
  svg+=`<line x1="${zeroX}" y1="${M.t}" x2="${zeroX}" y2="${M.t+rows.length*rowH}" stroke="${css('--axis')}" stroke-width="1"/></svg>`;
  el.innerHTML=svg;
  el.querySelectorAll('.bar').forEach(b=>{
    b.style.cursor='crosshair';
    b.addEventListener('mousemove',e=>{ const r=rows[+b.dataset.i];
      showTip(e,`<strong>${r.label}</strong><br>${opts.vfmt(r.value)}<br><span style="color:${css('--muted')}">n = ${fmt(r.n)}</span>`); });
    b.addEventListener('mouseleave',hideTip);
  });
}

/* ---------- table view ---------- */
function tableFor(cols, rows){
  return `<table><thead><tr>${cols.map(c=>`<th class="${c.num?'num':''}">${c.h}</th>`).join('')}</tr></thead><tbody>`
    + rows.map(r=>`<tr>${cols.map(c=>`<td class="${c.num?'num':''}">${c.get(r)}</td>`).join('')}</tr>`).join('')
    + `</tbody></table>`;
}

function card(title, sub, legend, renderFn, tableHTML){
  const el=document.createElement('div'); el.className='card';
  el.innerHTML=`<h2>${title}</h2><p class="sub">${sub}</p>`
    + (legend?`<div class="legend">${legend}</div>`:'')
    + `<div class="chart-scroll"></div>`
    + `<button class="toggle">Show data table</button><div class="tbl hidden">${tableHTML}</div>`;
  document.getElementById('charts').appendChild(el);
  renderFn(el.querySelector('.chart-scroll'));
  const btn=el.querySelector('.toggle'), tbl=el.querySelector('.tbl');
  btn.addEventListener('click',()=>{ const open=!tbl.classList.contains('hidden');
    tbl.classList.toggle('hidden'); btn.textContent = open?'Show data table':'Hide data table'; });
  return el;
}
const lg = names => names.map((n,i)=>`<span><i class="swatch" style="background:${css(SERIES[i%3])}"></i>${n}</span>`).join('');
const ym = s => { const [y,m]=s.split('-'); return ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+m-1]+" '"+y.slice(2); };

/* ---------- cards ---------- */
card('NYC REIT stocks vs. NYC property prices',
     'Both indexed to 100 at the start of the window, so they share one axis. The centerpiece question: does public REIT performance track physical property sales?',
     lg(DATA.reitVsProperty.map(s=>s.name)),
     el=>lineChart(el, DATA.reitVsProperty, {xfmt:ym}),
     tableFor([{h:'Month',get:r=>ym(r.x)},
               {h:DATA.reitVsProperty[0].name,num:1,get:r=>fmt(r.y,1)},
               {h:DATA.reitVsProperty[1].name,num:1,get:r=>{
                 const m=DATA.reitVsProperty[1].points.find(p=>p.x===r.x); return m?fmt(m.y,1):'—';}}],
              DATA.reitVsProperty[0].points));

card('NYC REIT performance',
     'SL Green, Vornado, and Empire State Realty, each indexed to 100 at its first trading day.',
     lg(DATA.reitIndex.map(s=>s.name)),
     el=>lineChart(el, DATA.reitIndex, {xfmt:d=>ym(d.slice(0,7))}),
     tableFor([{h:'Date',get:r=>r.x},
               ...DATA.reitIndex.map((s,i)=>({h:s.name,num:1,get:r=>{
                 const m=s.points.find(p=>p.x===r.x); return m?fmt(m.y,1):'—';}}))],
              DATA.reitIndex[0].points.filter((_,i)=>i%21===0)));

card('Average price per square foot by borough',
     `Arm's-length sales only — records under $${fmt(10000)} or with zero square footage are excluded as nominal transfers.`,
     '',
     el=>barChart(el, DATA.psfByBorough, {vfmt:v=>'$'+fmt(v,0)}),
     tableFor([{h:'Borough',get:r=>r.label},{h:'$/sqft',num:1,get:r=>'$'+fmt(r.value,0)},{h:'Sales',num:1,get:r=>fmt(r.n)}],
              DATA.psfByBorough));

card('Monthly sale volume',
     'Number of valid arm\'s-length property sales recorded per month across all five boroughs.',
     '',
     el=>lineChart(el, [{name:'Sales',points:DATA.monthlyVolume}], {xfmt:ym}),
     tableFor([{h:'Month',get:r=>ym(r.x)},{h:'Sales',num:1,get:r=>fmt(r.y)}], DATA.monthlyVolume));

if (DATA.sectorReturns.length)
  card('Average daily return by sector',
       'NYC-headquartered companies only. Mean daily percentage return; bars read left/right from a zero line.',
       '',
       el=>barChart(el, DATA.sectorReturns, {vfmt:v=>fmt(v,3)+'%', signed:true}),
       tableFor([{h:'Sector',get:r=>r.label},{h:'Avg daily return',num:1,get:r=>fmt(r.value,4)+'%'},{h:'Tickers',num:1,get:r=>fmt(r.n)}],
                DATA.sectorReturns));

document.getElementById('foot').textContent =
  'Built by scripts/06_build_dashboard.py from the project SQLite database. '
  + (DATA.isDemo ? 'Source: synthetic demo seed.' : 'Source: Kaggle datasets (see README).');

addEventListener('resize', ()=>{ /* charts are viewBox-scaled; no re-render needed */ });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
