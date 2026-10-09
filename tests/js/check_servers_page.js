// Runs the inline script of app/admin/servers.html against a fake DOM and a scripted API (plan T5.1).
// Races: overlapping reads piling up / duplicating rows, a failed status read shown as «stopped», a PATCH redirected to another server.
const assert = require("assert"), fs = require("fs"), path = require("path"), vm = require("vm");
const html = fs.readFileSync(process.env.PAGE || path.join(__dirname, "..", "..", "app", "admin", "servers.html"), "utf8");
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];

function env(routes, opts2) {
  const els = {}, calls = [], timers = [], deadlines = [];
  const fakeSignal = opts2 && opts2.noTimeoutApi ? {} : { timeout: (ms) => { const c = new AbortController(); deadlines.push({ ms, fire: () => c.abort(Object.assign(new Error('t'), { name: 'TimeoutError' })) }); return c.signal; } };
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
    const r = routes.find(([f]) => f(m, url, opts));
    const p = r ? r[1](m, url, opts) : Promise.resolve(reply({}));
    return new Promise((res, rej) => {   // an aborted request rejects, like the browser's
      if (opts.signal) opts.signal.addEventListener("abort", () => rej(Object.assign(new Error("aborted"), { name: "TimeoutError" })));
      p.then(res, rej);
    });
  };
  const reply = (o, status = 200) => ({ ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => o, text: async () => JSON.stringify(o) });
  const ctx = { document, fetch, AbortSignal: fakeSignal, AbortController, location: { hostname: "h" }, FormData: function (f) { this.get = (k) => f[k].value; }, confirm: () => true, console,
    setTimeout: (f, ms) => { const t = setTimeout(f, ms); t.unref && t.unref(); return t; }, clearTimeout, setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, Object, Number, String, Promise, JSON, Array };
  vm.createContext(ctx);
  vm.runInContext(script, ctx);
  return { ctx, document, form, els, calls, timers, reply, deadlines };
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
    e.els["btn-reload"].onclick(); e.els["btn-reload"].onclick(); await settle(3);   // the user presses «reload» twice meanwhile: ONE more read is queued
    slow.go(); await settle(60);
    assert.strictEqual(lists, 2, "the ticks are skipped; the manual reloads collapse into ONE rerun (reads: " + lists + ")");
    assert.strictEqual(e.els["servers-tbody"].rows.length, 2, "two servers, two rows (no duplicates)");
    // a slow page does not chain reads back to back: after it finished, the timer's next tick starts the next read, not an immediate one
    assert.strictEqual(lists, 2);
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

  // 4) a status read that NEVER answers: the page frees itself at the 8 s deadline, shows the server as unknown, and the next load runs
  {
    let lists = 0;
    const e = env([
      [(m, u) => u.endsWith("/servers"), () => { lists++; return Promise.resolve(rep([S(1, "A")])); }],
      [(m, u) => /status$/.test(u), () => new Promise(() => {})],
    ]);
    await settle(20);
    const read = e.deadlines.filter((d) => d.ms === 8000);
    assert.ok(read.length >= 2, "every read has an 8 s deadline");
    read[read.length - 1].fire(); await settle(40);                 // the status read times out
    assert.ok(/DESCONOCIDO/.test(e.els["servers-tbody"].rows[0].innerHTML), "a read that timed out is unknown, not stopped");
    e.els["btn-reload"].onclick(); await settle(30);
    assert.strictEqual(lists, 2, "the page was not left stuck «loading»");
  }

  // 5) a write has a longer deadline and, when it expires, says the change may have been applied (no blind retry)
  {
    const e = env([
      [(m, u) => u.endsWith("/servers"), () => Promise.resolve(rep([S(1, "A")]))],
      [(m, u) => /status$/.test(u), () => Promise.resolve(rep({ running: false }))],
      [(m, u) => m === "POST" && /\/start$/.test(u), () => new Promise(() => {})],
    ]);
    await settle(30);
    e.els["servers-tbody"].rows[0]._q['[data-action="toggle"]'].onclick();   // «start»
    await settle(5);
    const w = e.deadlines.filter((d) => d.ms === 60000);
    assert.strictEqual(w.length, 1, "a write has the 60 s deadline");
    w[0].fire(); await settle(30);
    assert.ok(/PUEDE haberse aplicado/.test(e.els["toast"].textContent), "the toast says the result is uncertain: " + e.els["toast"].textContent);
  }

  // 6) a browser without AbortSignal.timeout still gets a deadline (a timer) and the page works
  {
    const e = env([
      [(m, u) => u.endsWith("/servers"), () => Promise.resolve(rep([S(1, "A")]))],
      [(m, u) => /status$/.test(u), () => Promise.resolve(rep({ running: true }))],
      [(m, u) => /cars$/.test(u), () => Promise.resolve(rep({}))],
    ], { noTimeoutApi: true });
    await settle(30);
    assert.strictEqual(e.els["servers-tbody"].rows.length, 1);
    assert.ok(/EN LÍNEA/.test(e.els["servers-tbody"].rows[0].innerHTML));
  }

  console.log("servers page ok");
})().catch((x) => { console.error(x); process.exit(1); });
