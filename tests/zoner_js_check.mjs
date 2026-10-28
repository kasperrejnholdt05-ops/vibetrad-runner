// Kører web/zoner.js mod de fælles testvektorer og sammenligner med Python-facit (tolerance 1e-4: begge sider runder til 4 decimaler).
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
const Z = require('../web/zoner.js');
const fx = JSON.parse(readFileSync(new URL('./fixtures/zoner_cases.json', import.meta.url), 'utf8'));
const errs = [];
function cmp(a, b, p) {
  if (typeof a === 'number' && typeof b === 'number') { if (Math.abs(a - b) > 1.01e-4) errs.push(`${p}: ${a} ≠ ${b}`); return; }
  if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object') { if (a !== b) errs.push(`${p}: ${JSON.stringify(a)} ≠ ${JSON.stringify(b)}`); return; }
  if (Array.isArray(a) !== Array.isArray(b)) { errs.push(`${p}: type`); return; }
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const k of keys) cmp(a[k], b[k], `${p}.${k}`);
}
for (const c of fx.cases) {
  const rows = c.series.map(([date, close]) => ({ date, close }));
  const { series, issues } = Z.guard(rows, c.id);
  const zones = c.kind === 'stock' ? Z.ZONES_STOCK : Z.ZONES_INDEX;
  const w = Z.walk(series, { zones, levels: c.levels || null, armFrom: c.arm_from || null });
  const got = {
    guard: { n: series.length, first: series[0], issues: issues.map(({ note, ...x }) => x) },
    fills: w.fills.map(f => { const o = {}; for (const [k, v] of Object.entries(f)) o[k] = typeof v === 'number' && !Number.isInteger(v) ? Math.round(v * 1e6) / 1e6 : v; return o; }),
    episodes: w.episodes,
    analyse: Z.analyse(series, { kind: c.kind, levels: c.levels || null, name: c.id, armFrom: c.arm_from || null }),
    backtest: Z.backtest(series, { kind: c.kind, levels: c.levels || null, warmupDays: 20 }),
  };
  cmp(got, c.expected, c.id);
}
if (errs.length) { console.error(errs.slice(0, 40).join('\n')); console.error(`${errs.length} afvigelser`); process.exit(1); }
console.log(`OK: ${fx.cases.length} tilfælde ens i Python og JavaScript`);
