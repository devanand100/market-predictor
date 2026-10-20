/*
 * Minimal stand-in for the Claude Artifact "Dashboard" runtime (`dash`), so dashboard/index.html runs as a normal page.
 * Supports what index.html uses: dash.data, dash.onData, dash.calc, dash.params, dash.setParams, dash.resetParams,
 * dash.colors, dash.setLink. Data comes from window.DASH_SOURCES ({id: rows}) or, when DASH_API is set, from
 * `${DASH_API}/data/<id>` (the planned Lambda endpoint, see docs/DASHBOARD_PLAN.md).
 */
(function () {
  const sources = {};          // id -> {status, data, message}
  const calcs = {};            // id -> {inputs, fn}
  const listeners = [];
  let params = Object.assign({}, window.DASH_DEFAULT_PARAMS || {});
  const defaults = Object.assign({}, params);

  function fire() { for (const fn of listeners) { try { fn(); } catch (e) { console.error(e); } } }

  function load(id) {
    if (sources[id]) return;
    if (window.DASH_SOURCES && id in window.DASH_SOURCES) {
      sources[id] = { status: 'ok', data: window.DASH_SOURCES[id] };
      return;
    }
    if (window.DASH_API) {
      sources[id] = { status: 'loading', data: [] };
      fetch(`${window.DASH_API}/data/${encodeURIComponent(id)}${window.DASH_KEY ? '?key=' + encodeURIComponent(window.DASH_KEY) : ''}`)
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status))))
        .then((rows) => { sources[id] = { status: 'ok', data: rows }; fire(); })
        .catch((e) => { sources[id] = { status: 'error', data: [], message: String(e) }; fire(); });
      return;
    }
    sources[id] = { status: 'missing', data: [] };
  }

  function data(id) {
    if (calcs[id]) {
      const c = calcs[id];
      const ins = c.inputs.map((i) => data(i));
      const bad = ins.find((x) => x.status !== 'ok');
      if (bad) return { status: bad.status, data: [], message: bad.message };
      if (!c.cache) {
        try { c.cache = { status: 'ok', data: c.fn(...ins.map((x) => x.data)) }; }
        catch (e) { console.error(e); c.cache = { status: 'error', data: [], message: String(e) }; }
      }
      return c.cache;
    }
    load(id);
    const s = sources[id];
    return { status: s.status, data: s.data, message: s.message, refreshing: false, meta: [],
             columns: Array.isArray(s.data) && s.data.length ? Object.keys(s.data[0]) : [] };
  }

  window.dash = {
    data,
    onData(fn) { listeners.push(fn); try { fn(); } catch (e) { console.error(e); } },
    calc(id, spec) { calcs[id] = { inputs: spec.inputs, fn: spec.fn }; },
    loader() {},
    params() { return Object.assign({}, params); },
    setParams(p) { params = Object.assign({}, params, p); fire(); },
    resetParams() { params = Object.assign({}, defaults); fire(); },
    setLink(name) { history.replaceState(null, '', '#' + name); },
    refresh() {},
    colors: [],
  };
  // SVG attributes can't take var(): resolve the series tokens to concrete colours
  const cs = getComputedStyle(document.documentElement);
  const fallback = ['#2f6fdf', '#c2410c', '#0f766e', '#7c3aed', '#b45309', '#be185d', '#4d7c0f', '#475569'];
  window.dash.colors = fallback.map((f, i) => cs.getPropertyValue(`--series-${i + 1}`).trim() || f);
  // calcs cache by inputs: invalidate when a source finishes loading
  const origFire = fire;
  fire = function () { for (const k in calcs) calcs[k].cache = null; origFire(); };
  let t;
  window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(fire, 150); });
})();
