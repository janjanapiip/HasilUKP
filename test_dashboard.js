// Headless smoke test for the dashboard script: runs render() against real API
// data with a stub Chart + DOM, so a broken chart config fails here instead of
// silently drawing nothing in the user's browser.
const fs = require('fs');
const http = require('http');

const built = [];
class FakeChart {
  constructor(el, cfg) {
    if (!cfg || !cfg.type) throw new Error('chart config missing type');
    if (!cfg.data || !Array.isArray(cfg.data.datasets)) throw new Error('chart missing datasets');
    for (const ds of cfg.data.datasets) {
      if (!Array.isArray(ds.data)) throw new Error(`${cfg.type}: dataset.data not an array`);
      if (ds.data.some(v => v === undefined || Number.isNaN(v)))
        throw new Error(`${cfg.type}: dataset contains undefined/NaN`);
    }
    const n = cfg.data.labels ? cfg.data.labels.length : 0;
    for (const ds of cfg.data.datasets)
      if (n && ds.data.length !== n)
        throw new Error(`${cfg.type}: ${ds.data.length} points vs ${n} labels`);
    built.push({ el, type: cfg.type, labels: n, series: cfg.data.datasets.length });
    this.destroy = () => {};
  }
}
FakeChart.defaults = { font: {}, color: '' };

const controls = { fYear: 'all', fBasis: 'ujian', fMode: 'bar', fPeriod: 'monthly' };
const sink = {};
global.Chart = FakeChart;
global.document = {
  getElementById: id => ({
    get value() { return controls[id]; },
    set innerHTML(v) { sink[id] = v; },
    get innerHTML() { return sink[id] || ''; },
    set textContent(v) { sink[id] = v; },
    get textContent() { return sink[id] || ''; },
    addEventListener() {},
    id,
  }),
};
global.fetch = url => new Promise((res, rej) => {
  http.get('http://127.0.0.1:5057' + url, r => {
    let b = ''; r.on('data', c => b += c);
    r.on('end', () => res({ ok: r.statusCode === 200, status: r.statusCode, json: () => JSON.parse(b) }));
  }).on('error', rej);
});

let src = fs.readFileSync('_dash.js', 'utf8');
src = src.replace(/^load\(\);\s*$/m, '');           // don't auto-run; we drive it
// `eval` keeps top-level const/function in its own scope, so re-export the
// pieces the assertions below need.
src += '\n;globalThis.render = render; globalThis.FAMILY = FAMILY;'
     + ' globalThis.ijzhColor = ijzhColor; globalThis.famOf = famOf;';
eval(src);

(async () => {
  const combos = [];
  for (const mode of ['bar', 'pie', 'doughnut'])
    for (const period of ['monthly', 'annual'])
      for (const [year, basis] of [['all', 'ujian'], ['2026', 'sidang']])
        combos.push({ mode, period, year, basis });

  for (const c of combos) {
    controls.fMode = c.mode; controls.fPeriod = c.period;
    controls.fYear = c.year; controls.fBasis = c.basis;
    built.length = 0;
    const d = await (await fetch(`/api/stats?year=${c.year}&basis=${c.basis}`)).json();
    render(d);
    const ids = built.map(b => b.el.id).sort().join(',');
    if (ids !== 'cDiklat,cIjzh,cPeriod,cRate')
      throw new Error(`${JSON.stringify(c)} drew [${ids}]`);
    const period = built.find(b => b.el.id === 'cPeriod');
    const want = c.mode === 'bar' ? 'bar' : c.mode;
    if (period.type !== want) throw new Error(`period chart ${period.type} != ${want}`);
    const rate = built.find(b => b.el.id === 'cRate');
    if (rate.type !== 'bar') throw new Error('success-rate chart must stay a bar');
    if (!sink.strip.includes('Tingkat Kelulusan') && !sink.strip.includes('tingkat kelulusan'))
      throw new Error('stat strip missing pass-rate line');
    // abbreviation legends must be populated and colour-coded
    for (const id of ['lRate', 'lIjzh']) {
      const html = sink[id] || '';
      if (!html.includes('<span class="lg">')) throw new Error(`${id} legend empty`);
      if (!/UGN|GMDSS|PAS|PRA/.test(html)) throw new Error(`${id} legend has no known family`);
    }
  }

  // every ijazah code charted must get a distinct colour, or the legend lies
  const codes = (await (await fetch('/api/stats?year=all&basis=ujian')).json())
                  .per_ijzh.map(r => r.ijzh);
  const seen = new Map();
  for (const code of codes) {
    const col = ijzhColor(code);
    if (seen.has(col) && seen.get(col) !== code)
      throw new Error(`colour clash: ${code} and ${seen.get(col)} both ${col}`);
    seen.set(col, code);
  }
  for (const [pre, , hue] of FAMILY)
    if (!/^#[0-9a-f]{6}$/i.test(hue)) throw new Error(`bad hue for ${pre}`);
  console.log(`colours OK: ${codes.length} ijazah codes, ${seen.size} distinct`);
  console.log(`dashboard OK: ${combos.length} view combinations rendered, 4 charts each`);
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
