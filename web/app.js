"use strict";
const S = { group: "managers", win: "1y", q: "", famous: false, chamber: "all", board: null, version: null,
            open: new Set(), prev: new Map(), seq: 0, details: new Map() };
const $ = (s) => document.querySelector(s);

function el(tag, props, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined) n.append(c instanceof Node ? c : String(c));
  return n;
}
function svg(tag, attrs) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
}
const sign = (x) => (x >= 0 ? "+" : "−");
const pct = (x) => (x === null || x === undefined ? "—" : sign(x) + Math.abs(x * 100).toFixed(1) + "%");
const pp = (x) => (x === null || x === undefined ? "—" : sign(x) + Math.abs(x).toFixed(1));
const tone = (x) => (x === null || x === undefined ? "" : x >= 0 ? "up" : "dn");
const share = (x) => (x === null || x === undefined ? "—" : (x * 100).toFixed(1) + "%");
function usd(x) {
  if (x === null || x === undefined) return "—";
  if (x >= 1e9) return "$" + (x / 1e9).toFixed(1) + "B";
  if (x >= 1e6) return "$" + (x / 1e6).toFixed(1) + "M";
  return "$" + Math.round(x).toLocaleString("en-US");
}
function ago(sec) {
  if (sec === null || sec === undefined) return "never";
  if (sec < 90) return Math.round(sec) + "s";
  if (sec < 5400) return Math.round(sec / 60) + "m";
  if (sec < 172800) return Math.round(sec / 3600) + "h";
  return Math.round(sec / 86400) + "d";
}
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(path + " → " + r.status);
  return r.json();
}

const RET = { h: "RET", cls: "r ret", cell: (r) => {
  const main = el("span", { class: "main " + tone(r.total), text: pct(r.total) });
  if (S.group === "congress" && r.low !== undefined) return [main, el("span", { class: "sub", text: pct(r.low) + "…" + pct(r.high) })];
  return [main];
} };
const YR = { h: "/YR", cls: "r yr", cell: (r) => [el("span", { class: tone(r.annualized), text: pct(r.annualized) })] };
const VS = { h: "vs SPX", cls: "r", cell: (r) => [el("span", { class: tone(r.vs_spx), text: pp(r.vs_spx) })] };
const BADGE = { h: "", cls: "", cell: (r) => [el("span", { class: "badge " + r.badge, text: r.badge })] };
const RANK = { h: "#", cls: "", cell: (r) => [String(r.rank)] };
const COLS = {
  managers: [RANK, { h: "NAME", cls: "", cell: (r) => [r.name] },
    { h: "FUNDS", cls: "mut lp", cell: (r) => [(r.funds || []).join(" ")] }, RET, YR, VS, BADGE],
  investors: [RANK, { h: "NAME", cls: "", cell: (r) => [r.name, r.person ? el("span", { class: "sub", text: r.person }) : null] },
    { h: "ETF $", cls: "r lp", cell: (r) => [usd(r.etf_value_usd)] },
    { h: "% BOOK", cls: "r lp", cell: (r) => [el("span", { text: share(r.etf_share),
      title: r.etf_share_avg === null || r.etf_share_avg === undefined ? null : "average over the window: " + share(r.etf_share_avg) })] },
    { h: "ETFS", cls: "r lp", cell: (r) => [String(r.n_etfs ?? "—")] },
    { h: "TOP", cls: "lp", cell: (r) => [r.top_etf || "—"] }, RET, YR, VS, BADGE],
  congress: [RANK, { h: "NAME", cls: "", cell: (r) => [r.name] }, { h: "P-ST", cls: "", cell: (r) => [r.party_state] },
    { h: "CH", cls: "lp", cell: (r) => [r.chamber ? r.chamber[0] : ""] }, { h: "TRADES", cls: "r lp", cell: (r) => [String(r.n_trades ?? "—")] },
    { h: "TOP", cls: "lp", cell: (r) => [r.top_etf || "—"] }, RET, YR, VS, BADGE],
};
const cols = () => COLS[S.group].filter((c) => S.win !== "1y" || !c.cls.includes("yr"));

function visible(rows) {
  const q = S.q.trim().toLowerCase();
  return rows.filter((r) => (!q || (r.name + " " + (r.person || "")).toLowerCase().includes(q))
    && (S.group !== "investors" || !S.famous || r.person)
    && (S.group !== "congress" || S.chamber === "all" || r.chamber === S.chamber));
}

function render() {
  const b = S.board;
  if (!b) return;                      // nothing computed yet (first run): the caption says so
  const focusId = document.activeElement && document.activeElement.matches && document.activeElement.matches("tr.row") ? document.activeElement.dataset.id : null;
  const table = $("#board");
  table.dataset.window = S.win;
  const head = el("tr", {}, cols().map((c) => el("th", { class: c.cls, scope: "col", text: c.h })));
  table.tHead.replaceChildren(head);
  const body = [];
  for (const r of visible(b.ranked)) {
    const tr = el("tr", { class: "row" + (S.open.has(r.id) ? " open" : ""), tabindex: 0, "data-id": r.id,
      "aria-expanded": S.open.has(r.id) ? "true" : "false",
      onclick: () => toggle(r.id), onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); toggle(r.id); } } },
      cols().map((c) => el("td", { class: c.cls }, c.cell(r))));
    body.push(tr);
    if (S.open.has(r.id)) body.push(detailRow(r.id));
  }
  table.tBodies[0].replaceChildren(...body);
  if (focusId !== null) { const f = document.querySelector(`tr.row[data-id="${CSS.escape(focusId)}"]`); if (f) f.focus(); }
  const years = b.years;
  $("#caption").textContent = `${years}-year total return${years > 1 ? " (and per year)" : ""}, dividends reinvested · ${b.start} → ${b.end} · S&P 500 (SPY): ${pct(b.benchmark.total)}`;
  const nd = visible(b.unranked);
  $("#nodata summary").textContent = `NOT ENOUGH DATA (${nd.length})`;
  $("#nodata ul").replaceChildren(...nd.map((r) => el("li", {}, el("span", { text: r.name }), el("span", { class: "sub", text: "— " + r.reason }))));
}

function chart(series, bench) {
  const all = series.concat(bench || []);
  const t = (d) => Date.parse(d);
  const xs = all.map((p) => t(p[0])), ys = all.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const W = 300, H = 90, pad = 4;
  const px = (p) => `${(pad + ((t(p[0]) - x0) / (x1 - x0 || 1)) * (W - 2 * pad)).toFixed(1)},${(H - pad - ((p[1] - y0) / (y1 - y0 || 1)) * (H - 2 * pad)).toFixed(1)}`;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img", "aria-label": "Growth of $1 vs S&P 500" });
  if (bench && bench.length) s.append(svg("polyline", { points: bench.map(px).join(" "), fill: "none", stroke: "#7d8b99", "stroke-width": 1, "stroke-dasharray": "3 2" }));
  s.append(svg("polyline", { points: series.map(px).join(" "), fill: "none", stroke: "#3ddc84", "stroke-width": 1.5 }));
  return s;
}

function link(url, text) {
  if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return el("span", { class: "mut", text });
  return el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text });
}

function detailContent(r, group, board) {
  const d = r.detail || {};
  const left = el("div", {}, d.series && d.series.length ? chart(d.series, board.benchmark.series) : el("p", { class: "mut", text: "No chart for this window." }),
    el("div", { class: "mut", text: "green = this person · dashed = S&P 500 (SPY)" }));
  const items = [];
  if (group === "managers") for (const f of d.funds || []) items.push(el("div", { class: "item" },
    el("span", { text: f.ticker }), el("span", { class: "mut", text: f.fund_name }), el("span", { text: "since " + f.since }),
    f.counts ? el("span", { class: tone(f.total), text: pct(f.total) }) : el("span", { class: "mut", text: f.reason }),
    link(f.source_url, "↗ source"), el("span", { class: "mut", text: "verified " + f.verified })));
  if (group === "investors") for (const p of (d.periods || []).slice().reverse()) items.push(el("div", { class: "item" },
    el("span", { text: p.period }), el("span", { text: usd(p.etf_value_usd) + " in ETFs" }),
    p.etf_share === null || p.etf_share === undefined ? null : el("span", { class: "mut", text: share(p.etf_share) + " of book" }),
    el("span", { class: "mut", text: (p.positions || []).slice().sort((a, b) => (b.value_usd || 0) - (a.value_usd || 0)).slice(0, 6)
      .map((x) => (x.ticker || x.cusip) + (x.priced ? "" : " (unpriced)")).join(" ") }), link(p.url, "↗ filing")));
  if (group === "congress") {
    for (const t of (d.trades || []).slice().reverse()) items.push(el("div", { class: "item" },
      el("span", { text: t.tx_date }), el("span", { text: t.ticker }), el("span", { text: (t.kind || "").replace("_", " ") }),
      el("span", { class: "mut", text: t.amount + (t.open_ended ? " (open-ended)" : "") }),
      t.priced ? null : el("span", { class: "warn", text: "unpriced" }), link(t.url, "↗ report")));
    for (const n of d.notes || []) items.push(el("div", { class: "item warn", text: n.ref + ": " + n.message }));
    if (r.low !== undefined) items.unshift(el("div", { class: "item mut", text: `Estimate ${pct(r.total)}; if every trade were at the low end of its range ${pct(r.low)}, at the high end ${pct(r.high)}.` }));
  }
  if (r.reason) items.unshift(el("div", { class: "item warn", text: r.reason }));
  return el("div", { class: "dgrid" }, left, el("div", { class: "dlist" }, items));
}

function detailRow(id) {
  const group = S.group, win = S.win, board = S.board, key = `${group}|${win}|${id}`;
  const td = el("td", { colspan: cols().length, text: "loading…" });
  let p = S.details.get(key);
  if (!p) {
    p = api(`/api/person?group=${group}&window=${win}&id=${encodeURIComponent(id)}`);
    S.details.set(key, p);
    p.catch(() => { if (S.details.get(key) === p) S.details.delete(key); });
  }
  p.then((r) => td.replaceChildren(detailContent(r, group, board)))
    .catch((e) => { td.textContent = "Could not load details: " + e.message; });
  return el("tr", { class: "detail" }, td);
}

function toggle(id) {
  if (S.open.has(id)) S.open.delete(id); else S.open.add(id);
  render();
  const row = document.querySelector(`tr.row[data-id="${CSS.escape(String(id))}"]`);
  if (row) row.focus();
}

function showComputing() {
  if (!S.board) $("#caption").textContent = "COMPUTING FIRST LEADERBOARDS…";
}

async function loadBoard(flash) {
  const my = ++S.seq;
  const b = await api(`/api/leaderboard?group=${S.group}&window=${S.win}`);
  if (my !== S.seq) return;
  const before = S.prev;
  S.board = b;
  S.prev = new Map(b.ranked.map((r) => [r.id, r.total]));
  render();
  if (flash) for (const tr of document.querySelectorAll("tbody tr.row")) {
    const old = before.get(tr.dataset.id);
    if (old !== undefined && old !== S.prev.get(tr.dataset.id)) tr.querySelector("td.ret").classList.add("flash");
  }
}

async function poll() {
  try {
    const st = await api("/api/status");
    const m = $("#market");
    m.textContent = st.market.open ? `● MKT OPEN ${new Date(st.market.as_of).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false })} ET` : "● MKT CLOSED";
    m.className = st.market.open ? "open" : "mut";
    const names = { prices: "PX", sec13f: "13F", house: "HOUSE", senate: "SENATE" };
    $("#sources").replaceChildren(...Object.entries(names).filter(([k]) => st.sources[k] && (k !== "senate" || st.senate_enabled)).map(([k, label]) => {
      const s = st.sources[k];
      return el("span", { class: s.stale ? "warn" : "mut", text: s.stale ? ` ${label} stale ${ago(s.age_seconds)} ` : ` ${label} ${ago(s.age_seconds)} ` });
    }));
    $("#issues-btn").textContent = `${st.issues} issues`;
    const bf = $("#backfill");
    bf.hidden = !st.backfill;
    if (st.backfill) bf.textContent = `FIRST LOAD ${st.backfill.step}/${st.backfill.of}: ${st.backfill.phase}`;
    const changed = S.version !== null && st.data_version !== S.version;
    if (changed || !S.board) {             // new data, or nothing shown yet (first run)
      S.details.clear();
      try { await loadBoard(changed && S.board !== null); }
      catch (e) { if (S.board) throw e; showComputing(); }   // a failed refresh keeps S.version: retried next poll
    }
    S.version = st.data_version;
  } catch (e) {
    $("#market").textContent = "● APP NOT RESPONDING";
  }
}

const ISSUES_PAGE = 200;

// One collapsed group per source and kind (hundreds of thousands of issues can be open); its items load in pages.
function issueGroup(g) {
  const list = el("div", {});
  const more = el("button", { class: "link", type: "button" });
  const box = el("details", {}, el("summary", {}, el("span", { class: "warn", text: `${g.source} · ${g.kind} ` }),
    el("span", { class: "mut", text: g.count.toLocaleString("en-US") })), list);
  let loaded = 0, busy = false;
  async function loadMore() {
    if (busy) return;
    busy = true;
    more.remove();
    try {
      const q = new URLSearchParams({ source: g.source, kind: g.kind, offset: loaded, limit: ISSUES_PAGE });
      const page = await api("/api/issues?" + q);
      list.append(...page.map((i) => el("div", { class: "item" }, el("span", { text: i.message + " " }),
        i.url ? link(i.url, "↗ original") : null)));
      loaded += page.length;
      if (page.length === ISSUES_PAGE && loaded < g.count) {
        more.textContent = `show ${Math.min(ISSUES_PAGE, g.count - loaded).toLocaleString("en-US")} more of ${(g.count - loaded).toLocaleString("en-US")}`;
        box.append(more);
      }
    } catch (e) {
      list.append(el("div", { class: "item", text: "Could not load issues: " + e.message }));
    }
    busy = false;
  }
  more.addEventListener("click", loadMore);
  box.addEventListener("toggle", () => { if (box.open && loaded === 0) loadMore(); });
  return box;
}

async function showIssues() {
  const panel = $("#issues"), btn = $("#issues-btn");
  if (!panel.hidden) { panel.hidden = true; btn.setAttribute("aria-expanded", "false"); return; }
  try {
    const groups = await api("/api/issues/summary");
    panel.replaceChildren(...groups.map(issueGroup));
    if (!groups.length) panel.textContent = "No open issues.";
  } catch (e) {
    panel.textContent = "Could not load issues: " + e.message;
  }
  panel.hidden = false;
  btn.setAttribute("aria-expanded", "true");
}

function setGroup(g) {
  S.group = g;
  S.open.clear();
  for (const b of document.querySelectorAll("button[data-group]")) { const on = b.dataset.group === g; b.classList.toggle("on", on); b.setAttribute("aria-selected", on); }
  $("#famous-wrap").hidden = g !== "investors";
  $("#chamber").hidden = g !== "congress";
  loadBoard(false).catch(showComputing);
}
function setWindow(w) {
  S.win = w;
  S.open.clear();
  for (const b of document.querySelectorAll("button[data-window]")) { const on = b.dataset.window === w; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); }
  loadBoard(false).catch(showComputing);
}

document.addEventListener("DOMContentLoaded", () => {
  for (const b of document.querySelectorAll("button[data-group]")) b.addEventListener("click", () => setGroup(b.dataset.group));
  for (const b of document.querySelectorAll("button[data-window]")) b.addEventListener("click", () => setWindow(b.dataset.window));
  $("#search").addEventListener("input", (e) => { S.q = e.target.value; render(); });
  $("#famous").addEventListener("change", (e) => { S.famous = e.target.checked; render(); });
  $("#chamber").addEventListener("change", (e) => { S.chamber = e.target.value; render(); });
  $("#issues-btn").addEventListener("click", showIssues);
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const typing = e.target.matches("input, select, textarea");
    if (typing) { if (e.key === "Escape") e.target.blur(); return; }
    if (e.key === "/") { e.preventDefault(); $("#search").focus(); }
    else if (["1", "3", "5"].includes(e.key)) setWindow(e.key + "y");
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const rows = [...document.querySelectorAll("tbody tr.row")];
      if (!rows.length) return;
      e.preventDefault();
      const i = rows.indexOf(document.activeElement);
      const next = e.key === "ArrowDown" ? Math.min(rows.length - 1, i + 1) : Math.max(0, i - 1);
      rows[i === -1 ? 0 : next].focus();
    }
  });
  loadBoard(false).catch(() => showComputing()).finally(poll);
  setInterval(poll, 60000);
});
