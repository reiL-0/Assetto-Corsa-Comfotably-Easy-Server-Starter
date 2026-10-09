// Runs the inline script of app/admin/servers.html against a fake DOM and a scripted API (plan T5.1).
// Races: overlapping reads piling up / duplicating rows, a failed status read shown as «stopped», a PATCH redirected to another server.
const assert = require("assert"), fs = require("fs"), path = require("path"), vm = require("vm");
const html = fs.readFileSync(process.env.PAGE || path.join(__dirname, "..", "..", "app", "admin", "servers.html"), "utf8");
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];

function env(routes) {
  const els = {}, calls = [], timers = [];
  const el = (id) => {
    const o = { id, textContent: "", innerHTML: "", value: "", className: "", disabled: false, rows: [], _q: {}, classList: { toggle() {}, add() {}, remove() {} }, style: {},
      querySelector(sel) { return (this._q[sel] = this._q[sel] || { onclick: null, sel }); },
      appendChild(c) { this.rows.push(c); return c; }, replaceChildren(f) { this.rows = f.rows ? f.rows.slice() : []; },
      addEventListener(ev, fn) { this["on_" + ev] = fn; }, scrollIntoView() {}, reset() {}, showModal() {}, close() {}, open: false };
    return o;
  };
  const document = { getElementById: (id) => (els[id] = els[id] || el(id)), createElement: () => el("row"), createDocumentFragment: () => el("frag") };
  const form = document.getElementById("server-form");
  ["name", "track", "track_config", "max_clients", "password"].forEach((k) => { form[k] = { value: "" }; });
  const fetch = (url, opts) => {
    opts = opts || {}; const m = opts.method || "GET"; calls.push(m + " " + url);
    const r = routes.find(([f]) => f(m, url));
    return r ? r[1](m, url, opts) : Promise.resolve(reply({}));
  };
  const reply = (o, status = 200) => ({ ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => o, text: async () => JSON.stringify(o) });
  const ctx = { document, fetch, location: { hostname: "h" }, FormData: function (f) { this.get = (k) => f[k].value; }, confirm: () => true, console,
    setTimeout, clearTimeout, setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, Object, Number, String, Promise, JSON, Array };
  vm.createContext(ctx);
  vm.runInContext(script, ctx);
  return { ctx, document, form, els, calls, timers, reply };
}
const settle = async (n = 30) => { for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r)); };
const gate = () => { let go; const p = new Promise((r) => { go = r; }); return { p, go }; };
const S = (id, name) => ({ id, name, ports: { tcp: 9600 + id, http: 9601 + id }, config: { SERVER: { TRACK: "monza", MAX_CLIENTS: 8 } }, entry_list: [] });
const rep = (o, st) => ({ ok: (st || 200) < 400, status: st || 200, headers: { get: () => "application/json" }, json: async () => o, text: async () => JSON.stringify(o) });

(async () => {
  // 1) a slow read is not joined by the timer's ticks, and the table never gets duplicated rows
  {
    const slow = gate(); let lists = 0;
    const e = env([
      [(m, u) => u.endsWith("/servers"), () => { lists++; return Promise.resolve(rep([S(1, "A"), S(2, "B")])); }],
      [(m, u) => u.endsWith("/servers/1/status"), () => slow.p.then(() => rep({ running: false }))],
      [(m, u) => u.endsWith("/servers/2/status"), () => Promise.resolve(rep({ running: true }))],
      [(m, u) => u.endsWith("/servers/2/cars"), () => Promise.resolve(rep({ 0: {}, 1: {} }))],
    ]);
    await settle();
    const tick = e.timers.find((t) => t.ms === 4000).fn;
    for (let i = 0; i < 6; i++) { tick(); await settle(3); }
    assert.strictEqual(lists, 1, "six ticks during a slow read made " + lists + " list reads");
    slow.go(); await settle(60);
    assert.strictEqual(lists, 2, "the ticks collapse into ONE rerun");
    assert.strictEqual(e.els["servers-tbody"].rows.length, 2, "two servers, two rows (no duplicates)");
  }

  // 2) a failed status read is «DESCONOCIDO», its toggle is disabled, and it is not counted as stopped
  {
    const e = env([
      [(m, u) => u.endsWith("/servers"), () => Promise.resolve(rep([S(1, "A"), S(2, "B")]))],
      [(m, u) => u.endsWith("/servers/1/status"), () => Promise.resolve(rep({ detail: "boom" }, 502))],
      [(m, u) => u.endsWith("/servers/2/status"), () => Promise.resolve(rep({ running: false }))],
    ]);
    await settle(60);
    const rows = e.els["servers-tbody"].rows;
    assert.strictEqual(rows.length, 2);
    assert.ok(/DESCONOCIDO/.test(rows[0].innerHTML) && !/DETENIDO/.test(rows[0].innerHTML), "unknown, not stopped");
    assert.ok(/disabled/.test(rows[0].innerHTML) && !(rows[0]._q['[data-action="toggle"]'] && rows[0]._q['[data-action="toggle"]'].onclick), "no start/stop on a server we could not ask");
    assert.ok(/DETENIDO/.test(rows[1].innerHTML), "a server that answered «not running» IS stopped");
    assert.ok(/SIN DATO/.test(e.els["online-count"].innerHTML) && /0\/2/.test(e.els["online-count"].innerHTML));
  }

  // 3) saving the form of A is not redirected to B when «edit» is clicked on B meanwhile
  {
    const slow = gate();
    const e = env([
      [(m, u) => u.endsWith("/servers"), () => Promise.resolve(rep([S(1, "A"), S(2, "B")]))],
      [(m, u) => /status$/.test(u), () => Promise.resolve(rep({ running: false }))],
      [(m, u) => m === "GET" && u.endsWith("/servers/1"), () => slow.p.then(() => rep(S(1, "A")))],
      [(m, u) => m === "GET" && u.endsWith("/servers/2"), () => Promise.resolve(rep(S(2, "B")))],
    ]);
    await settle(60);
    const rows = e.els["servers-tbody"].rows;
    rows[0]._q['[data-action="edit"]'].onclick();                  // «edit» A
    e.form.name.value = "A renamed";
    const submit = e.els["server-form"].on_submit({ preventDefault() {} });   // save: reads A's current config first (slow)
    await settle(3);
    rows[1]._q['[data-action="edit"]'].onclick();                  // the user clicks «edit» on B before A's save finished
    slow.go(); await submit; await settle();
    const patches = e.calls.filter((c) => c.startsWith("PATCH"));
    assert.deepStrictEqual(patches, ["PATCH /api/v1/servers/1"], "A's form went to A, not to B: " + patches);
  }

  console.log("servers page ok");
})().catch((x) => { console.error(x); process.exit(1); });
