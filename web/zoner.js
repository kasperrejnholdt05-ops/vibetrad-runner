/* Zonemotoren i JavaScript – samme regler som runner/zoner.py (Vibetrad indlejrer denne fil uændret).
 * Fælles testvektorer: tests/fixtures/zoner_cases.json (pytest kører tests/zoner_js_check.mjs).
 * Begreber og begrundelser: se docstringen i runner/zoner.py.
 */
(function (root) {
  'use strict';
  const TOP_DAYS = 756, CONFIRM_DAYS = 10, FOUNDATION_DAYS = 20, NORDNET_LIMIT_PCT = 13.0, BREAK_RATIO = 2.0;
  // Planen gælder fra 27/10/2026: brug PLAN_START som armFrom i live-status (se PLAN_START i runner/zoner.py).
  const PLAN_START = '2026-10-27';
  const ZONES_INDEX = [['korrektion', 'Korrektion', 10, 20], ['bjorn', 'Bjørnemarked', 20, 30], ['kollaps', 'Kollaps', 30, null]];
  const ZONES_STOCK = [['korrektion', 'Korrektion', 20, 35], ['bjorn', 'Bjørnemarked', 35, 50], ['kollaps', 'Kollaps', 50, null]];
  const ZONE_WEIGHTS = { korrektion: 0.2, bjorn: 0.3, kollaps: 0.5 };
  const TRANCHE_WEIGHTS = [1 / 3, 1 / 3, 1 / 3];
  const KINDS = ['liggende', 'bekraeftelse', 'fundament'];
  const KIND_DA = { liggende: 'Liggende ordre', bekraeftelse: 'Bekræftelse', fundament: 'Fundament' };
  const MARKETS = [
    { id: 'sp500', symbol: 'GSPC.INDX', name: 'S&P 500', region: 'USA' },
    { id: 'ndx', symbol: 'NDX.INDX', name: 'Nasdaq 100', region: 'USA' },
    { id: 'omxs30', symbol: 'OMXS30.INDX', name: 'OMXS30', region: 'Sverige' },
    { id: 'omxc25', symbol: 'OMXC25.INDX', name: 'OMXC25', region: 'Danmark' },
    { id: 'obx', symbol: 'OBX.OL', name: 'OBX', region: 'Norge' },
  ];

  const r = (v, d = 4) => (v == null || !isFinite(v) ? v : Math.round(v * 10 ** d) / 10 ** d);
  function dmy(d) { const s = String(d || '').slice(0, 10); return /^\d{4}-\d{2}-\d{2}$/.test(s) ? `${s.slice(8, 10)}/${s.slice(5, 7)}/${s.slice(0, 4)}` : '–'; }
  function nf(v, d = 2) {
    if (v == null || !isFinite(v)) return '–';
    const s = Math.abs(v).toFixed(d); const [a, b] = s.split('.');
    const g = a.replace(/\B(?=(\d{3})+(?!\d))/g, '.');
    return (v < 0 ? '-' : '') + g + (b ? ',' + b : '');
  }
  const num = v => { const x = Number(v); return isFinite(x) && x > 0 ? x : null; };
  const days = (a, b) => (Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 864e5;

  function guard(rows, name = '') {
    const raw = new Map(); const issues = []; let bad = 0;
    for (const row of rows || []) {
      let d, v;
      if (Array.isArray(row)) { d = String(row[0]).slice(0, 10); v = num(row[1]); }
      else { d = String(row.date || '').slice(0, 10); v = num(row.adjusted_close != null ? row.adjusted_close : row.close); }
      if (d.length !== 10 || v == null) { bad++; continue; }
      raw.set(d, v);
    }
    if (bad) issues.push({ kind: 'ugyldig', count: bad, note: `${bad} rækker uden gyldig dato/kurs er sprunget over` });
    const data = [...raw.entries()].sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
    for (let k = data.length - 1; k > 0; k--) {
      const [d0, a] = data[k - 1], [d1, b] = data[k]; const ratio = b / a;
      if (ratio > BREAK_RATIO || ratio < 1 / BREAK_RATIO) {
        for (let i = 0; i < k; i++) data[i] = [data[i][0], data[i][1] * ratio];
        issues.push({ kind: 'databrud', from: d0, to: d1, ratio: r(ratio, 4),
          note: `${name || 'Serien'} springer fra ${a} til ${b} (${dmy(d0)} → ${dmy(d1)}); ældre kurser er skaleret, så bruddet ikke ligner et kursfald` });
      }
    }
    const gaps = [];
    for (let k = 1; k < data.length; k++) if (days(data[k - 1][0], data[k][0]) > 14) gaps.push([data[k - 1][0], data[k][0]]);
    if (gaps.length) issues.push({ kind: 'huller', count: gaps.length, note: `${gaps.length} huller på over 14 dage (største ${dmy(gaps[0][0])} → ${dmy(gaps[0][1])})` });
    return { series: data, issues };
  }

  function zoneTable(top, zones = ZONES_INDEX, levels = null) {
    if (levels && levels.length) {
      return levels.map(z => {
        const hi = Number(z.hi), lo = z.lo != null ? Number(z.lo) : null;
        return { id: z.id, label: z.label || z.id, price_hi: hi, price_lo: lo, pct_hi: r((1 - hi / top) * 100, 2), pct_lo: lo == null ? null : r((1 - lo / top) * 100, 2) };
      });
    }
    return zones.map(([id, label, a, b]) => ({ id, label, pct_hi: a, pct_lo: b, price_hi: top * (1 - a / 100), price_lo: b == null ? null : top * (1 - b / 100) }));
  }
  function zoneAt(price, table) { let cur = 'normal'; for (const z of table) if (price <= z.price_hi) cur = z.id; return cur; }

  function walk(series, o = {}) {
    const zones = o.zones || ZONES_INDEX, levels = o.levels || null, topDays = o.topDays || TOP_DAYS;
    const confirmDays = o.confirmDays || CONFIRM_DAYS, foundationDays = o.foundationDays || FOUNDATION_DAYS, start = o.start || 0, armFrom = o.armFrom || null;
    const n = series.length, closes = series.map(x => x[1]); const fills = [], episodes = []; let ep = null, armed = !armFrom;
    const argmax = (lo, hi) => { let b = lo; for (let j = lo; j <= hi; j++) if (closes[j] >= closes[b]) b = j; return b; };
    const argmin = (lo, hi) => { let b = lo; for (let j = lo; j <= hi; j++) if (closes[j] <= closes[b]) b = j; return b; };
    const closeEp = (e, endI) => { if (!e) return; const b = e.bottom_i; episodes.push({ top_date: series[e.top_i][0], top: e.top, bottom_date: series[b][0], bottom: closes[b], max_dd_pct: r((1 - closes[b] / e.top) * 100, 2), end_date: series[endI][0] }); };
    const newEp = (ti, upto) => ({ top_i: ti, top: closes[ti], table: zoneTable(closes[ti], zones, levels), bottom_i: argmin(ti, upto), done: new Set(), reached: new Set() });
    const fill = (i, z, kind, price) => fills.push({ date: series[i][0], i, zone: z.id, kind, price, top: ep.top, top_date: series[ep.top_i][0], dd_pct: r((1 - price / ep.top) * 100, 2) });
    for (let i = 0; i < n; i++) {
      const c = closes[i];
      if (i < start) continue;
      if (!ep) { ep = newEp(argmax(Math.max(0, i - topDays + 1), i), i); if (ep.top_i === i) continue; }
      if (c > ep.top) { closeEp(ep, i - 1); ep = newEp(i, i); continue; }
      if (i - ep.top_i > topDays) { closeEp(ep, i - 1); ep = newEp(argmax(Math.max(0, i - 251), i), i); if (ep.top_i === i) continue; }
      if (!armed) {
        if (series[i][0] < armFrom && i < n - 1) { if (c < closes[ep.bottom_i]) ep.bottom_i = i; continue; }
        armed = true; ep.done = new Set(); ep.reached = new Set(); ep.bottom_i = i;
      }
      if (c < closes[ep.bottom_i]) ep.bottom_i = i;
      const table = ep.table, bottom = closes[ep.bottom_i];
      for (const z of table) if (bottom <= z.price_hi) ep.reached.add(z.id);
      for (const z of table) { const k = z.id + '|liggende'; if (!ep.done.has(k) && c <= z.price_hi) { ep.done.add(k); fill(i, z, 'liggende', Math.min(c, z.price_hi)); } }
      const bi = ep.bottom_i;
      if (ep.reached.size && i > bi && i - confirmDays >= 0) {
        let prevHi = -Infinity; for (let j = i - confirmDays; j < i; j++) prevHi = Math.max(prevHi, closes[j]);
        if (c > prevHi) for (const z of table) { const k = z.id + '|bekraeftelse'; if (ep.reached.has(z.id) && !ep.done.has(k)) { ep.done.add(k); fill(i, z, 'bekraeftelse', c); } }
      }
      if (ep.reached.size && i - bi >= foundationDays) {
        for (const z of table) { const k = z.id + '|fundament'; if (ep.reached.has(z.id) && ep.done.has(z.id + '|bekraeftelse') && !ep.done.has(k)) { ep.done.add(k); fill(i, z, 'fundament', c); } }
      }
    }
    closeEp(ep, n - 1);
    return { fills, episode: ep, episodes, last_i: n - 1 };
  }

  function analyse(series, o = {}) {
    const kind = o.kind || 'index', levels = o.levels || null, limit = o.nordnetLimitPct != null ? o.nordnetLimitPct : NORDNET_LIMIT_PCT;
    const recentDays = o.recentDays != null ? o.recentDays : 5;
    const zones = kind === 'stock' ? ZONES_STOCK : ZONES_INDEX;
    if (series.length < CONFIRM_DAYS + 2) return { symbol: o.symbol || '', name: o.name || '', kind, ok: false, note: 'for få kurser' };
    const w = walk(series, { zones, levels, armFrom: o.armFrom || null }); const ep = w.episode;
    const closes = series.map(x => x[1]); const i = series.length - 1, c = closes[i];
    const top = ep.top, table = ep.table, bi = ep.bottom_i, bottom = closes[bi];
    const done = new Map(); for (const f of w.fills) if (f.i >= ep.top_i) done.set(f.zone + '|' + f.kind, f);
    let prevHi = null; if (i >= CONFIRM_DAYS) { prevHi = -Infinity; for (let j = i - CONFIRM_DAYS + 1; j <= i; j++) prevHi = Math.max(prevHi, closes[j]); }
    const tr = [];
    for (const z of table) {
      const reached = ep.reached.has(z.id);
      KINDS.forEach((k, idx) => {
        const f = done.get(z.id + '|' + k);
        const t = { zone: z.id, zone_label: z.label, n: idx + 1, kind: k, kind_label: KIND_DA[k] };
        if (f) { const age = i - f.i; Object.assign(t, { status: age < recentDays ? 'ny' : 'udløst', date: f.date, price: r(f.price, 4) }); }
        else if (k === 'liggende') {
          const p = z.price_hi, dist = (c / p - 1) * 100;
          Object.assign(t, { status: 'venter', price: r(p, 4), distance_pct: r(dist, 2), order: dist <= limit ? 'limit' : 'alarm', alarm_at: r(p / (1 - limit / 100), 4) });
        } else if (!reached) t.status = 'ikke nået';
        else if (k === 'bekraeftelse') Object.assign(t, { status: 'venter', level: prevHi ? r(prevHi, 4) : null, note: 'køb, når en lukkekurs er over de seneste 10 dages højeste' });
        else {
          const need = done.get(z.id + '|bekraeftelse'); const daysLeft = Math.max(FOUNDATION_DAYS - (i - bi), 0);
          Object.assign(t, { status: 'venter', days_left: daysLeft, note: (need ? '' : 'efter bekræftelsen og ') + `${FOUNDATION_DAYS} handelsdage uden ny bund` + (daysLeft ? ` (${daysLeft} tilbage)` : '') });
        }
        tr.push(t);
      });
    }
    const zone = zoneAt(c, table);
    const zl = table.find(z => z.id === zone);
    const deepest = [...table].reverse().find(z => ep.reached.has(z.id));
    const out = {
      symbol: o.symbol || '', name: o.name || '', kind, ok: true, date: series[i][0], close: r(c, 4), top: r(top, 4), top_date: series[ep.top_i][0],
      dd_pct: r((1 - c / top) * 100, 2), bottom: r(bottom, 4), bottom_date: series[bi][0], max_dd_pct: r((1 - bottom / top) * 100, 2),
      zone, zone_label: zl ? zl.label : 'Normal', deepest: deepest ? deepest.id : 'normal', confirm_level: prevHi ? r(prevHi, 4) : null,
      zones: table.map(z => { const o2 = {}; for (const [k, v] of Object.entries(z)) o2[k] = typeof v === 'number' && !Number.isInteger(v) ? r(v, 4) : v; o2.reached = ep.reached.has(z.id); return o2; }),
      tranches: tr, fixed_levels: !!(levels && levels.length), arm_from: o.armFrom || null,
    };
    out.next = nextStep(out, limit);
    return out;
  }

  function nextStep(a, limit = NORDNET_LIMIT_PCT) {
    const nw = a.tranches.filter(t => t.status === 'ny');
    if (nw.length) { const t = nw[nw.length - 1]; return { type: 'koeb', text: `Køb nu: ${t.kind_label.toLowerCase()} i ${t.zone_label.toLowerCase()} (udløst ${dmy(t.date)} ved ${nf(t.price)})` }; }
    const pend = a.tranches.filter(t => t.status === 'venter' && t.kind === 'liggende');
    if (pend.length) {
      const t = pend[0];
      if (t.order === 'limit') return { type: 'ordre', text: `Læg limitordre: ${t.zone_label.toLowerCase()} ved ${nf(t.price)} (${nf(t.distance_pct, 1)} % under kursen)` };
      return { type: 'alarm', text: `Ingen ordre endnu: ${t.zone_label.toLowerCase()} ved ${nf(t.price)} ligger ${nf(t.distance_pct, 1)} % under kursen. Sæt kursalarm ved ${nf(t.alarm_at)} (Nordnet tager kun ca. ${limit} % under kursen)` };
    }
    const conf = a.tranches.filter(t => t.status === 'venter' && t.kind === 'bekraeftelse');
    if (conf.length) return { type: 'vent', text: `Vent på bekræftelse: luk over ${nf(conf[0].level)}` };
    return { type: 'ro', text: 'Alle trancher i denne nedtur er brugt. Ingen handling før ny top.' };
  }

  function median(v) { const s = [...v].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }

  function backtest(series, o = {}) {
    const kind = o.kind || 'index', zones = kind === 'stock' ? ZONES_STOCK : ZONES_INDEX;
    const warm = o.warmupDays != null ? o.warmupDays : 252;
    const w = walk(series, { zones, levels: o.levels || null, start: Math.min(warm, Math.max(series.length - 1, 0)) });
    const closes = series.map(x => x[1]);
    const fwd = (i, d) => (i + d >= closes.length ? null : closes[i + d] / closes[i] - 1);
    const rows = w.fills.map(f => { let lo = f.price; for (let j = f.i + 1; j < closes.length; j++) { if (closes[j] >= f.price) break; lo = Math.min(lo, closes[j]); } return Object.assign({}, f, { fwd_1y: fwd(f.i, 252), fwd_3y: fwd(f.i, 756), further_dd: lo / f.price - 1 }); });
    const st = (sel, key) => { const v = sel.map(x => x[key]).filter(x => x != null); if (!v.length) return { n: 0 }; return { n: v.length, median: r(median(v) * 100, 1), worst: r(Math.min(...v) * 100, 1), best: r(Math.max(...v) * 100, 1), positive: v.filter(x => x > 0).length }; };
    const agg = sel => ({ fills: sel.length, fwd_1y: st(sel, 'fwd_1y'), fwd_3y: st(sel, 'fwd_3y'), further_dd: st(sel, 'further_dd') });
    const byZone = {}; for (const z of zones) byZone[z[0]] = agg(rows.filter(x => x.zone === z[0]));
    const byKind = {}; for (const k of KINDS) byKind[k] = agg(rows.filter(x => x.kind === k));
    const byTop = {}; for (const x of rows) (byTop[x.top_date] = byTop[x.top_date] || []).push(x);
    const eps = [];
    for (const e of w.episodes) {
      const sel = byTop[e.top_date] || [];
      if (!sel.length && e.max_dd_pct < 10) continue;
      const wt = x => ZONE_WEIGHTS[x.zone] * TRANCHE_WEIGHTS[KINDS.indexOf(x.kind)];
      const used = sel.reduce((a, x) => a + wt(x), 0);
      const r3 = sel.filter(x => x.fwd_3y != null); const sw = r3.reduce((a, x) => a + wt(x), 0);
      const wavg = r3.length ? r3.reduce((a, x) => a + wt(x) * x.fwd_3y, 0) / sw : null;
      const ee = {}; for (const [k, v] of Object.entries(e)) ee[k] = typeof v === 'number' ? r(v, 2) : v;
      eps.push(Object.assign(ee, { fills: sel.length, first_fill: sel.length ? sel[0].date : null, reserve_used_pct: Math.round(used * 100), fwd_3y_weighted: wavg == null ? null : r(wavg * 100, 1), zones: zones.map(z => z[0]).filter(id => sel.some(x => x.zone === id)) }));
    }
    const years = series.length ? days(series[0][0], series[series.length - 1][0]) / 365.25 : 0;
    return { from: series.length ? series[0][0] : null, to: series.length ? series[series.length - 1][0] : null, years: r(years, 1), fills: rows.length, by_zone: byZone, by_kind: byKind, episodes: eps, note: 'Kursindeks uden udbytte; lukkekurser; ingen kurtage. Fortiden er ingen garanti.' };
  }

  const api = { PLAN_START, TOP_DAYS, CONFIRM_DAYS, FOUNDATION_DAYS, NORDNET_LIMIT_PCT, ZONES_INDEX, ZONES_STOCK, ZONE_WEIGHTS, TRANCHE_WEIGHTS, KINDS, KIND_DA, MARKETS, guard, zoneTable, zoneAt, walk, analyse, nextStep, backtest, nf, dmy };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.VTZ = api;
})(typeof window !== 'undefined' ? window : globalThis);
