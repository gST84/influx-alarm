"use strict";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const view = () => $("view");
// Like replaceChildren, but skips null/false (optional parts of a page).
const setView = (...nodes) => view().replaceChildren(...nodes.flat().filter((n) => n != null && n !== false));

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "style") e.setAttribute("style", v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else if (k === "checked") e.checked = !!v;
    else if (v === true) e.setAttribute(k, "");
    else e.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) if (c != null && c !== false) e.append(c);
  return e;
}

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const res = await fetch("api/" + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (res.status === 401 && path !== "login" && path !== "me") {
    route();
    throw new HttpError(401, "Niet aangemeld");
  }
  if (!res.ok) throw new HttpError(res.status, data?.error || res.statusText);
  return data;
}

let toastTimer;
function toast(msg, error = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (error ? " error" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), error ? 8000 : 4000);
}

async function busy(button, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Bezig…";
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { button.disabled = false; button.textContent = label; }
}

const fmtTime = new Intl.DateTimeFormat("nl-BE", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const fmtNum = new Intl.NumberFormat("nl-BE", { maximumFractionDigits: 3 });
const time = (s) => (s ? fmtTime.format(new Date(s * 1000)) : "–");

function ago(s) {
  if (!s) return "–";
  const d = Date.now() / 1000 - s;
  if (d < 60) return "zojuist";
  if (d < 3600) return `${Math.round(d / 60)} min geleden`;
  if (d < 2 * 86400) return `${Math.round(d / 3600)} u geleden`;
  return `${Math.round(d / 86400)} dagen geleden`;
}

function fmtValue(v, unit) {
  if (v == null) return "–";
  if (typeof v === "boolean") return v ? "aan" : "uit";
  if (typeof v === "number") return fmtNum.format(v) + (unit ? " " + unit : "");
  return String(v);
}

const STATUS = {
  ok: "OK", alarm: "ALARM", high: "Te hoog", low: "Te laag", nodata: "Geen data", pending: "Buiten grens (wacht)",
  error: "Fout", paused: "Gepauzeerd", unknown: "Nog niet gecontroleerd", empty: "Geen waarden", ignored: "Genegeerd",
};
const badge = (status, text) => el("span", { class: `badge st-${status}` }, text || STATUS[status] || status);

function storeGet(k) { try { return localStorage.getItem("influxalarm." + k); } catch (_) { return null; } }
function storeSet(k, v) { try { localStorage.setItem("influxalarm." + k, v); } catch (_) { /* ignore */ } }

// Number inputs accept a decimal comma.
const parseNum = (s) => (s === "" || s == null ? null : Number(String(s).replace(",", ".")));

// ---------------------------------------------------------------------------
// Routing & auth
// ---------------------------------------------------------------------------
let cleanup = [];
let me = null;
let leaveGuard = null;

let currentHash = location.hash;
window.addEventListener("hashchange", () => {
  if (leaveGuard && !leaveGuard()) {
    history.replaceState(null, "", currentHash || "#/"); // stay on the page, without another hashchange
    return;
  }
  currentHash = location.hash;
  route();
});

async function route() {
  cleanup.forEach((fn) => fn());
  cleanup = [];
  leaveGuard = null;
  try {
    me = await api("GET", "me");
  } catch (e) {
    setView(el("div", { class: "empty-state" }, "Server niet bereikbaar: " + e.message));
    return;
  }
  $("topbar").hidden = !me.user;
  if (me.setup) return renderSetup();
  if (!me.user) return renderLogin();
  $("whoami").textContent = me.user;
  const [, page, id] = (location.hash.replace(/^#/, "") || "/").split("/");
  document.querySelectorAll("[data-nav]").forEach((a) =>
    a.classList.toggle("active", a.dataset.nav === (page || "overview") || (page === "rule" && a.dataset.nav === "overview")));
  try {
    if (!page) await renderOverview();
    else if (page === "rule") await renderRule(id);
    else if (page === "contacts") await renderContacts();
    else if (page === "log") await renderLog();
    else if (page === "settings") await renderSettings();
    else if (page === "users") await renderUsers();
    else location.hash = "#/";
  } catch (e) {
    if (e.status !== 401) setView(el("div", { class: "card" }, "Fout: " + e.message));
  }
}

$("logout").addEventListener("click", async () => {
  await api("POST", "logout");
  location.hash = "#/";
  route();
});

function renderLogin() {
  const user = el("input", { autocomplete: "username", required: true });
  const pw = el("input", { type: "password", autocomplete: "current-password", required: true });
  setView(el("div", { class: "card login" },
    el("h1", {}, "Influx Alarm"),
    el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        try {
          await api("POST", "login", { username: user.value, password: pw.value });
          route();
        } catch (err) { toast(err.message, true); }
      },
    },
    el("label", { class: "field" }, "Gebruikersnaam", user),
    el("label", { class: "field" }, "Wachtwoord", pw),
    el("button", { class: "primary", type: "submit" }, "Aanmelden"))));
  user.focus();
}

function renderSetup() {
  const user = el("input", { value: "admin", required: true });
  const pw = el("input", { type: "password", autocomplete: "new-password", minlength: 8, required: true });
  setView(el("div", { class: "card login" },
    el("h1", {}, "Welkom bij Influx Alarm"),
    el("p", { class: "muted" }, "Maak de eerste gebruiker aan."),
    el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        try {
          await api("POST", "setup", { username: user.value, password: pw.value });
          location.hash = "#/settings";
          route();
        } catch (err) { toast(err.message, true); }
      },
    },
    el("label", { class: "field" }, "Gebruikersnaam", user),
    el("label", { class: "field" }, "Wachtwoord (min. 8 tekens)", pw),
    el("button", { class: "primary", type: "submit" }, "Aanmaken en aanmelden"))));
}

async function updateCheckerStatus() {
  try {
    const s = await api("GET", "status");
    $("checker-status").textContent = s.last_run ? `laatste controle ${ago(s.last_run)}` : "controle start…";
    $("checker-status").title = s.last_error || "";
    $("checker-status").style.color = s.last_error ? "var(--alarm)" : "";
  } catch (_) { /* ignore */ }
}
setInterval(() => me?.user && updateCheckerStatus(), 30000);

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------
async function renderOverview() {
  updateCheckerStatus();
  const rules = await api("GET", "rules");
  const count = (s) => rules.filter((r) => r.state === s).length;
  const tile = (n, label, status) => el("div", { class: "tile" }, el("b", { style: n && status === "alarm" ? "color:var(--alarm)" : "" }, n), label);

  const cards = rules.map((r) => {
    const why = r.state === "alarm"
      ? `${r.counts.alarm} waarde${r.counts.alarm === 1 ? "" : "n"} in alarm sinds ${time(r.alarm_since)}` +
        (r.counts.acked ? ` · ${r.counts.acked} bevestigd` : "")
      : r.state === "error" ? `${r.counts.error} waarde(n) konden niet gecontroleerd worden` : "";
    return el("a", { class: `rule-card ${r.state}`, href: `#/rule/${r.id}` },
      el("div", { class: "top" }, el("b", {}, r.name), badge(r.state === "alarm" ? "alarm" : r.state)),
      el("div", { class: "meta" },
        el("span", {}, `${r.counts.checks} waarden${r.counts.paused ? ` (${r.counts.paused} gepauzeerd)` : ""}`),
        el("span", {}, `${r.contacts} contact${r.contacts === 1 ? "" : "en"}`),
        r.counts.ignored ? el("span", {}, `${r.counts.ignored} genegeerd`) : null,
        el("span", {}, `gecontroleerd ${ago(r.checked_at)}`)),
      why ? el("div", { class: "why" }, why) : null);
  });

  setView(
    el("div", { class: "page-head" }, el("h1", {}, "Overzicht"),
      el("div", { class: "actions" }, el("button", { class: "primary", onclick: () => (location.hash = "#/rule/new") }, "+ Nieuwe regel"))),
    el("div", { class: "summary" },
      tile(count("alarm"), "in alarm", "alarm"), tile(count("ok"), "OK"), tile(count("pending") + count("error"), "aandacht"),
      tile(count("paused"), "gepauzeerd")),
    rules.length ? el("div", { class: "rules" }, cards)
      : el("div", { class: "card empty-state" }, "Nog geen regels. Maak er een met ", el("b", {}, "+ Nieuwe regel"), "."));
  const timer = setInterval(() => !document.hidden && location.hash.replace(/^#\/?/, "") === "" && renderOverview(), 30000);
  cleanup.push(() => clearInterval(timer));
}

// ---------------------------------------------------------------------------
// Rule editor
// ---------------------------------------------------------------------------
const RANGES = { "24u": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "1j": 365 * 86400 };

const ed = {};

async function renderRule(id) {
  const isNew = id === "new";
  const [rule, contacts] = await Promise.all([
    isNew ? Promise.resolve(null) : api("GET", `rules/${id}`),
    api("GET", "contacts"),
  ]);
  Object.assign(ed, {
    id: isNew ? null : Number(id),
    rule: rule || { name: "", message: "", paused: 0, quiet_enabled: 0, quiet_start: "22:00", quiet_end: "07:00",
      ignore_after_hours: 0, ring_seconds: 30, cascade_wait_min: 5, checks: [], recipients: [], alarms: [], log: [] },
    contacts,
    dirty: isNew,
  });
  ed.checks = ed.rule.checks.map((c) => ({ ...c }));
  // Only the people linked to this rule; others are added from the full list.
  ed.recipients = ed.rule.recipients.filter((r) => contacts.some((c) => c.id === r.contact_id)).map((r) => ({ ...r }));
  ed.browser ||= { range: storeGet("range") || "7d", bucket: storeGet("bucket") || "", measurements: null, selected: [], schema: {}, sel: {}, tagValues: {} };
  leaveGuard = () => !ed.dirty || confirm("Er zijn niet-opgeslagen wijzigingen. Toch verlaten?");
  drawRule();
  loadBuckets();
  const timer = setInterval(async () => {
    if (document.hidden || ed.dirty || !ed.id) return;
    try {
      ed.rule = await api("GET", `rules/${ed.id}`);
      ed.checks = ed.rule.checks.map((c) => ({ ...c }));
      drawRule();
    } catch (_) { /* ignore */ }
  }, 30000);
  cleanup.push(() => clearInterval(timer));
}

function markDirty() {
  ed.dirty = true;
  const d = $("dirty");
  if (d) d.hidden = false;
}

function drawRule() {
  const r = ed.rule;
  const state = r.state || (ed.id ? "unknown" : "empty");
  const scrollY = window.scrollY;
  const open = r.alarms.filter((a) => !a.resolved);
  const active = open.filter((a) => !a.ignored);
  const checkById = new Map(ed.checks.map((c) => [c.id, c]));

  const banner = active.length ? el("div", { class: "alarm-banner" },
    el("div", { class: "card-head" }, el("h2", {}, `${active.length} waarde${active.length === 1 ? "" : "n"} in alarm`),
      el("span", { class: "spacer" }),
      r.cascade_running ? el("span", { class: "small" }, "📞 belronde loopt…") : null,
      r.pending_calls?.length ? el("span", { class: "small" }, `📞 bellen uitgesteld tot ${time(r.pending_calls[0].due)}`) : null,
      el("button", { onclick: (e) => busy(e.target, async () => { ed.rule = await api("POST", `rules/${ed.id}/ack`); ed.checks = ed.rule.checks.map((c) => ({ ...c })); drawRule(); toast("Bevestigd: de belronde stopt. Na 24 u volgt een herinnering als het niet opgelost is."); }) }, "Bevestigen")),
    el("ul", {}, active.map((a) => {
      const c = checkById.get(a.check_id);
      return el("li", {}, el("b", {}, c ? c.label || c.auto_label : `controle ${a.check_id}`), ": ", a.reason,
        el("span", { class: "muted small" }, ` — sinds ${time(a.started)}`,
          a.acked ? ` · bevestigd door ${a.acked_by}` : ""));
    })),
    open.length > active.length ? el("p", { class: "small muted" }, `${open.length - active.length} alarm(en) genegeerd na de time-out.`) : null)
    : null;

  setView(
    el("div", { class: "page-head" },
      el("a", { href: "#/" }, "← Overzicht"),
      el("h1", {}, ed.id ? r.name : "Nieuwe regel"),
      ed.id ? badge(state) : null,
      el("span", { id: "dirty", class: "dirty-dot", hidden: !ed.dirty }, "● niet opgeslagen"),
      el("div", { class: "actions" },
        ed.id ? el("button", { onclick: (e) => busy(e.target, async () => { ed.rule = await api("POST", `rules/${ed.id}/check`); if (!ed.dirty) ed.checks = ed.rule.checks.map((c) => ({ ...c })); drawRule(); toast("Gecontroleerd"); }) }, "Nu controleren") : null,
        ed.id ? el("button", { title: "Stuur een testbericht naar wie mail of sms aangevinkt heeft", onclick: (e) => busy(e.target, async () => toast((await api("POST", `rules/${ed.id}/test`, {})).message)) }, "Testmelding") : null,
        ed.id ? el("button", { onclick: (e) => busy(e.target, async () => { ed.rule = await api("POST", `rules/${ed.id}/pause`, { paused: !r.paused }); ed.checks = ed.rule.checks.map((c) => ({ ...c })); drawRule(); }) }, r.paused ? "▶ Hervatten" : "⏸ Pauzeren") : null,
        ed.id ? el("button", { class: "danger", onclick: deleteRule }, "Verwijderen") : null,
        el("button", { class: "primary", onclick: (e) => busy(e.target, saveRule) }, "Opslaan"))),
    r.paused ? el("div", { class: "card", style: "background:var(--paused-bg)" }, "⏸ Deze regel is gepauzeerd: er wordt niets gecontroleerd en er gaan geen berichten uit.") : null,
    banner,
    el("div", { class: "editor" },
      el("aside", { class: "card browser", id: "browser" }),
      el("div", {},
        rulePanel(),
        recipientsPanel(),
        checksPanel(),
        ed.id ? logPanel(r.log) : null)));
  drawBrowser();
  window.scrollTo(0, scrollY);
}

function rulePanel() {
  const r = ed.rule;
  const bind = (key, transform = (v) => v) => (e) => { r[key] = transform(e.target.type === "checkbox" ? (e.target.checked ? 1 : 0) : e.target.value); markDirty(); };
  const ign = r.ignore_after_hours || 0;
  const unit = ign && ign % 168 === 0 ? 168 : ign && ign % 24 === 0 ? 24 : 1;
  const ignNum = el("input", { type: "number", min: 0, step: 1, value: ign ? ign / unit : "", placeholder: "nooit", style: "width:90px" });
  const ignUnit = el("select", {}, [[1, "uur"], [24, "dagen"], [168, "weken"]].map(([v, t]) => el("option", { value: v, selected: v === unit }, t)));
  const setIgnore = () => { r.ignore_after_hours = (parseNum(ignNum.value) || 0) * Number(ignUnit.value); markDirty(); };
  ignNum.addEventListener("input", setIgnore);
  ignUnit.addEventListener("change", setIgnore);

  return el("section", { class: "card" },
    el("div", { class: "grid2" },
      el("label", { class: "field" }, "Naam van de regel *", el("input", { value: r.name, placeholder: "bv. Koelcellen", oninput: bind("name") })),
      el("label", { class: "field" }, "Bericht (in mail en sms, en voorgelezen aan de telefoon)",
        el("textarea", { rows: 2, placeholder: "bv. Controleer de koelcel en bel de technieker", oninput: bind("message") }, r.message || ""))),
    el("details", { style: "margin-top:10px" },
      el("summary", {}, "Meldingsinstellingen ",
        el("span", { class: "muted small" }, `bellen ${r.ring_seconds}s · 2 belrondes, ${r.cascade_wait_min} min ertussen` +
          (r.quiet_enabled ? ` · stil ${r.quiet_start}–${r.quiet_end}` : "") + (ign ? ` · negeren na ${ign / unit} ${ignUnit.selectedOptions[0].text}` : ""))),
      el("div", { class: "grid2", style: "margin-top:10px" },
        el("div", { class: "field" },
          el("label", { class: "inline" }, el("input", { type: "checkbox", checked: r.quiet_enabled, onchange: bind("quiet_enabled") }), "Stille uren voor oproepen"),
          el("div", { class: "form-row" },
            el("input", { type: "time", value: r.quiet_start, onchange: bind("quiet_start") }), "tot",
            el("input", { type: "time", value: r.quiet_end, onchange: bind("quiet_end") })),
          el("span", { class: "small muted" }, "Mail en sms gaan meteen; het telefoontje volgt na de stille uren.")),
        el("label", { class: "field" }, "Alarm negeren als het niet opgelost is na",
          el("div", { class: "form-row" }, ignNum, ignUnit),
          el("span", { class: "small muted" }, "Daarna geen herinneringen meer. Leeg = nooit negeren.")),
        el("label", { class: "field" }, "Laten rinkelen (seconden)",
          el("input", { type: "number", min: 10, max: 120, value: r.ring_seconds, oninput: bind("ring_seconds", Number) })),
        el("label", { class: "field" }, "Wachttijd voor de 2de belronde (minuten)",
          el("input", { type: "number", min: 0, step: 1, value: r.cascade_wait_min, oninput: bind("cascade_wait_min", parseNum) })))));
}

function recipientsPanel() {
  const byId = new Map(ed.contacts.map((c) => [c.id, c]));
  const callers = ed.recipients.filter((r) => r.call);
  const move = (i, d) => {
    const j = i + d;
    if (j < 0 || j >= ed.recipients.length) return;
    [ed.recipients[i], ed.recipients[j]] = [ed.recipients[j], ed.recipients[i]];
    markDirty();
    redraw();
  };
  const redraw = () => $("recipients").replaceWith(recipientsPanel());
  const toggle = (r, key) => (e) => { r[key] = e.target.checked ? 1 : 0; markDirty(); redraw(); };

  const remove = (i) => { ed.recipients.splice(i, 1); markDirty(); redraw(); };
  const linked = new Set(ed.recipients.map((r) => r.contact_id));
  const available = ed.contacts.filter((c) => !linked.has(c.id));
  const add = (e) => {
    const c = byId.get(Number(e.target.value));
    if (!c) return;
    // Sensible start: mail when there is an address, otherwise sms; adjust with the checkboxes.
    ed.recipients.push({ contact_id: c.id, email: c.email ? 1 : 0, sms: !c.email && c.phone ? 1 : 0, call: 0 });
    markDirty();
    redraw();
  };

  return el("section", { class: "card flush", id: "recipients" },
    el("div", { class: "card-head", style: "padding:12px 14px 0" }, el("h2", {}, `Wie contacteren? (${ed.recipients.length})`),
      el("span", { class: "spacer" }), el("a", { href: "#/contacts", class: "small" }, "Contacten beheren")),
    ed.recipients.length ? el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Naam"), el("th", {}, "Mail"), el("th", {}, "SMS"), el("th", {}, "Bellen"),
        el("th", {}, "Belvolgorde"), el("th", {}, ""))),
      el("tbody", {}, ed.recipients.map((r, i) => {
        const c = byId.get(r.contact_id);
        const pos = callers.indexOf(r);
        const none = !r.email && !r.sms && !r.call;
        return el("tr", {},
          el("td", {}, el("b", {}, c.name), el("div", { class: "small muted" }, [c.email, c.phone].filter(Boolean).join(" · ")),
            none ? el("div", { class: "small", style: "color:var(--warn)" }, "Kies mail, sms of bellen") : null),
          el("td", {}, el("input", { type: "checkbox", checked: r.email, disabled: !c.email, title: c.email ? "" : "geen mailadres", onchange: toggle(r, "email") })),
          el("td", {}, el("input", { type: "checkbox", checked: r.sms, disabled: !c.phone, title: c.phone ? "" : "geen telefoonnummer", onchange: toggle(r, "sms") })),
          el("td", {}, el("input", { type: "checkbox", checked: r.call, disabled: !c.phone, title: c.phone ? "" : "geen telefoonnummer", onchange: toggle(r, "call") })),
          el("td", {}, el("span", { class: "order" },
            el("button", { class: "icon ghost", title: "Hoger", disabled: i === 0, onclick: () => move(i, -1) }, "↑"),
            el("button", { class: "icon ghost", title: "Lager", disabled: i === ed.recipients.length - 1, onclick: () => move(i, 1) }, "↓"),
            r.call ? el("b", {}, pos + 1) : el("span", { class: "muted small" }, "–"))),
          el("td", {}, el("button", { class: "icon ghost danger", title: "Uit deze regel halen", onclick: () => remove(i) }, "✕")));
      })))) : el("p", { class: "empty-state", style: "padding:16px" }, "Nog niemand gekoppeld aan deze regel."),
    el("div", { class: "form-row", style: "padding:10px 14px 0" },
      ed.contacts.length === 0
        ? el("span", { class: "small" }, "Nog geen contactpersonen. ", el("a", { href: "#/contacts" }, "Voeg er eerst toe."))
        : available.length
          ? el("select", { onchange: add, "aria-label": "Contactpersoon toevoegen" },
            el("option", { value: "" }, "+ Contactpersoon toevoegen…"),
            available.map((c) => el("option", { value: c.id }, `${c.name}${c.email || c.phone ? ` (${[c.email, c.phone].filter(Boolean).join(", ")})` : ""}`)))
          : el("span", { class: "small muted" }, "Alle contactpersonen staan al in deze regel.")),
    el("p", { class: "small muted", style: "padding:0 14px" },
      "Mail en sms gaan meteen naar iedereen met een vinkje. Bellen gebeurt één voor één in volgorde: wie niet opneemt of geen 1 drukt, wordt overgeslagen naar de volgende; bevestigt niemand, dan wordt de lijst na de wachttijd nog één keer afgelopen."));
}

function checksPanel() {
  const bulk = { lower: el("input", { placeholder: "–" }), upper: el("input", { placeholder: "–" }), delay: el("input", { placeholder: "–" }), nodata: el("input", { placeholder: "–" }) };
  const applyBulk = () => {
    for (const c of ed.checks) {
      if (bulk.lower.value !== "") c.lower = parseNum(bulk.lower.value);
      if (bulk.upper.value !== "") c.upper = parseNum(bulk.upper.value);
      if (bulk.delay.value !== "") c.delay_min = parseNum(bulk.delay.value);
      if (bulk.nodata.value !== "") c.nodata_min = parseNum(bulk.nodata.value);
    }
    markDirty();
    $("checks").replaceWith(checksPanel());
  };
  const numInput = (c, key) => el("input", {
    class: "num", value: c[key] ?? "", placeholder: "–", inputmode: "decimal",
    oninput: (e) => { c[key] = parseNum(e.target.value); markDirty(); },
  });

  return el("section", { class: "card flush", id: "checks" },
    el("div", { class: "card-head", style: "padding:12px 14px 0" }, el("h2", {}, `Te controleren waarden (${ed.checks.length})`)),
    ed.checks.length > 1 ? el("div", { class: "bulk" },
      el("span", { class: "small" }, "Voor alle waarden:"),
      el("label", { class: "field" }, "Ondergrens", bulk.lower),
      el("label", { class: "field" }, "Bovengrens", bulk.upper),
      el("label", { class: "field" }, "Vertraging (min)", bulk.delay),
      el("label", { class: "field" }, "Geen data na (min)", bulk.nodata),
      el("button", { class: "small", onclick: applyBulk }, "Toepassen")) : null,
    ed.checks.length === 0
      ? el("p", { class: "empty-state" }, "Kies links een bucket, measurements en values en klik op ", el("b", {}, "Toevoegen aan controle"), ".")
      : el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {},
          el("th", { title: "Pauzeren" }, "Aan"), el("th", {}, "Naam / bron"), el("th", { class: "num" }, "Huidige waarde"),
          el("th", {}, "Ondergrens"), el("th", {}, "Bovengrens"),
          el("th", { title: "Zo lang moet de waarde buiten de grens blijven voor er alarm komt" }, "Vertraging (min)"),
          el("th", { title: "Alarm als het laatste punt ouder is dan dit" }, "Geen data na (min)"),
          el("th", {}, "Status"), el("th", {}, ""))),
        el("tbody", {}, ed.checks.map((c, i) => el("tr", { class: (c.paused ? "is-paused " : "") + (["high", "low", "nodata"].includes(c.status) ? "is-alarm" : "") },
          el("td", {}, el("input", { type: "checkbox", checked: !c.paused, title: "Uitvinken = deze waarde pauzeren", onchange: (e) => { c.paused = e.target.checked ? 0 : 1; markDirty(); } })),
          el("td", {},
            el("input", { class: "label", value: c.label || "", placeholder: c.auto_label, oninput: (e) => { c.label = e.target.value; markDirty(); } }),
            el("div", { class: "small muted" }, `${c.bucket} · ${c.measurement} · ${c.field}`),
            el("div", {}, Object.entries(c.tags).map(([k, v]) => el("span", { class: "tagchip" }, `${k}=${v}`)))),
          el("td", { class: "num" }, el("b", {}, fmtValue(c.last_value, c.unit)), el("div", { class: "small muted" }, ago(c.last_time))),
          el("td", {}, c.kind === "number" ? numInput(c, "lower") : el("span", { class: "small muted" }, "n.v.t.")),
          el("td", {}, c.kind === "number" ? numInput(c, "upper") : el("span", { class: "small muted" }, "n.v.t.")),
          el("td", {}, c.kind === "number" ? numInput(c, "delay_min") : "–"),
          el("td", {}, numInput(c, "nodata_min")),
          el("td", {}, c.id ? badge(c.status || "unknown") : badge("unknown", "Nieuw"), c.reason ? el("div", { class: "reason" }, c.reason) : null),
          el("td", {}, el("button", { class: "icon ghost danger", title: "Verwijderen", onclick: () => { ed.checks.splice(i, 1); markDirty(); $("checks").replaceWith(checksPanel()); } }, "✕"))))))),
    el("p", { class: "small muted", style: "padding:0 14px" },
      "Alarm als de waarde de hele vertraging lang boven de bovengrens of onder de ondergrens ligt, of als het laatste punt ouder is dan ‘geen data na’. Laat een grens leeg om ze niet te controleren; 0 bij ‘geen data na’ schakelt die controle uit."));
}

const LOG_CHANNEL = { mail: "Mail", sms: "SMS", call: "Oproep", ack: "Bevestiging", "-": "" };
const LOG_KIND = { alarm: "alarm", reminder: "herinnering", resolved: "opgelost", test: "test", ignored: "genegeerd", pause: "pauze", ack: "" };
const logWhat = (l) => [LOG_CHANNEL[l.channel] ?? l.channel, LOG_KIND[l.kind] ?? l.kind].filter(Boolean).join(" · ");

function logPanel(rows) {
  return el("section", { class: "card flush" },
    el("div", { class: "card-head", style: "padding:12px 14px 0" }, el("h2", {}, "Historiek")),
    rows.length ? el("div", { class: "table-wrap", style: "max-height:360px" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Tijd"), el("th", {}, "Wat"), el("th", {}, "Contact"), el("th", {}, "Resultaat"), el("th", {}, "Detail"))),
      el("tbody", {}, rows.map((l) => el("tr", {},
        el("td", { class: "small" }, time(l.ts)),
        el("td", {}, logWhat(l)),
        el("td", {}, l.contact || ""),
        el("td", {}, el("span", { class: `log-status log-${l.status}` }, logStatus(l.status))),
        el("td", { class: "small" }, l.detail)))))) : el("p", { class: "empty-state" }, "Nog niets gebeurd."));
}

function logStatus(s) {
  return { ok: "OK", ack: "bevestigd (1)", noack: "geen 1 gedrukt", noanswer: "niet opgenomen", busy: "bezet",
    congestion: "niet bereikbaar", failed: "mislukt", error: "fout", niemand: "niemand bevestigde",
    uitgesteld: "uitgesteld", overgeslagen: "overgeslagen" }[s] || s;
}

async function saveRule() {
  const r = ed.rule;
  if (!r.name.trim()) throw new Error("Geef de regel een naam");
  const idle = ed.recipients.filter((x) => !x.email && !x.sms && !x.call);
  if (idle.length) {
    const names = idle.map((x) => ed.contacts.find((c) => c.id === x.contact_id)?.name).join(", ");
    throw new Error(`Kies mail, sms of bellen voor: ${names} (of haal ze uit de regel met ✕)`);
  }
  const body = {
    name: r.name, message: r.message, paused: r.paused, quiet_enabled: r.quiet_enabled, quiet_start: r.quiet_start,
    quiet_end: r.quiet_end, ignore_after_hours: r.ignore_after_hours, ring_seconds: r.ring_seconds, cascade_wait_min: r.cascade_wait_min,
    recipients: ed.recipients.filter((x) => x.email || x.sms || x.call),
    checks: ed.checks.map((c) => ({ id: c.id, bucket: c.bucket, measurement: c.measurement, field: c.field, tags: c.tags, kind: c.kind,
      label: c.label, lower: c.lower, upper: c.upper, delay_min: c.delay_min, nodata_min: c.nodata_min, paused: c.paused })),
  };
  const saved = await api(ed.id ? "PUT" : "POST", ed.id ? `rules/${ed.id}` : "rules", body);
  ed.dirty = false;
  toast("Opgeslagen en gecontroleerd");
  if (!ed.id) {
    leaveGuard = null;
    location.hash = `#/rule/${saved.id}`;
    return;
  }
  ed.rule = saved;
  ed.checks = saved.checks.map((c) => ({ ...c }));
  drawRule();
}

async function deleteRule() {
  if (!confirm(`Regel “${ed.rule.name}” en al zijn waarden en historiek verwijderen?`)) return;
  await api("DELETE", `rules/${ed.id}`);
  ed.dirty = false;
  location.hash = "#/";
}

// --- browser (left column) -------------------------------------------------------
function rangeParams() {
  const stop = Math.floor(Date.now() / 1000);
  return { start: stop - RANGES[ed.browser.range], stop };
}

async function loadBuckets() {
  try {
    ed.buckets = await api("GET", "influx/buckets");
    if (ed.browser.bucket && !ed.buckets.some((b) => b.name === ed.browser.bucket)) ed.browser.bucket = "";
    drawBrowser();
    if (ed.browser.bucket && !ed.browser.measurements) loadMeasurements();
  } catch (e) {
    ed.buckets = [];
    ed.browserError = e.message;
    drawBrowser();
  }
}

async function loadMeasurements() {
  const b = ed.browser;
  b.measurements = null;
  b.tagValues = {};
  drawBrowser();
  try {
    b.measurements = await api("GET", "influx/measurements?" + new URLSearchParams({ bucket: b.bucket, ...rangeParams() }));
  } catch (e) {
    b.measurements = [];
    toast(e.message, true);
  }
  // Fields/tags follow the period too.
  await Promise.all(b.selected.map(loadSchema));
  drawBrowser();
}

async function loadSchema(m) {
  const b = ed.browser;
  try {
    b.schema[m] = await api("GET", "influx/schema?" + new URLSearchParams({ bucket: b.bucket, measurement: m, ...rangeParams() }));
  } catch (e) {
    b.schema[m] = { error: e.message, fields: [], tags: [] };
  }
  const s = b.sel[m];
  if (!s.fields.size) {
    const f = b.schema[m].fields;
    const pick = f.length === 1 ? f[0] : f.find((x) => x.name === "value");
    if (pick) s.fields.add(pick.name);
  }
}

function drawBrowser() {
  const box = $("browser");
  if (!box) return;
  const b = ed.browser;
  const parts = [el("h2", {}, "Waarden zoeken")];
  if (ed.browserError) parts.push(el("p", { class: "small", style: "color:var(--alarm)" }, ed.browserError, " ", el("a", { href: "#/settings" }, "Instellingen")));

  parts.push(el("div", { class: "step" }, el("span", { class: "small muted" }, "Periode om in te zoeken"),
    el("div", { class: "presets" }, Object.keys(RANGES).map((k) => el("button", {
      class: "small" + (k === b.range ? " active" : ""),
      onclick: () => { b.range = k; storeSet("range", k); if (b.bucket) loadMeasurements(); else drawBrowser(); },
    }, k)))));

  parts.push(el("div", { class: "step" }, el("span", { class: "small muted" }, "Bucket"),
    el("select", {
      onchange: (e) => { b.bucket = e.target.value; storeSet("bucket", b.bucket); b.selected = []; b.schema = {}; b.sel = {}; if (b.bucket) loadMeasurements(); else drawBrowser(); },
    }, el("option", { value: "" }, ed.buckets ? "— kies een bucket —" : "laden…"),
    (ed.buckets || []).map((x) => el("option", { value: x.name, selected: x.name === b.bucket }, x.name + (x.system ? " (systeem)" : ""))))));

  if (b.bucket) {
    const filter = el("input", { type: "search", placeholder: "Zoeken…", value: b.filter || "" });
    const list = el("div", { class: "checklist" });
    const drawList = () => {
      const q = (b.filter || "").toLowerCase();
      const names = [...new Set([...b.selected, ...(b.measurements || [])])].sort((x, y) => x.localeCompare(y)).filter((m) => m.toLowerCase().includes(q));
      list.replaceChildren(...(b.measurements === null ? [el("div", { class: "none" }, "laden…")] : names.map((m) => el("label", {},
        el("input", { type: "checkbox", checked: b.selected.includes(m), onchange: (e) => toggleMeasurement(m, e.target.checked) }), m))));
      if (b.measurements && !names.length) list.append(el("div", { class: "none" }, "Geen measurements"));
    };
    filter.addEventListener("input", () => { b.filter = filter.value; drawList(); });
    drawList();
    parts.push(el("div", { class: "step" }, el("span", { class: "small muted" }, "Measurements"), filter, list));
  }

  if (b.selected.length) {
    parts.push(el("div", { class: "step" }, el("span", { class: "small muted" }, "Values en tags"), b.selected.map(selCard)));
    parts.push(el("button", { class: "primary", onclick: (e) => busy(e.target, addSeries) }, "Toevoegen aan controle →"));
    parts.push(el("p", { class: "small muted" }, "Elke gevonden reeks (bv. elke sensor) wordt een aparte regel in de tabel, met eigen grenzen."));
  }
  box.replaceChildren(...parts);
}

async function toggleMeasurement(m, on) {
  const b = ed.browser;
  if (!on) {
    b.selected = b.selected.filter((x) => x !== m);
    return drawBrowser();
  }
  b.selected.push(m);
  b.sel[m] ||= { fields: new Set(), tags: {} };
  drawBrowser();
  await loadSchema(m);
  drawBrowser();
}

function selCard(m) {
  const b = ed.browser;
  const schema = b.schema[m];
  const sel = b.sel[m];
  const card = el("div", { class: "sel-card" }, el("b", {}, m));
  if (!schema) return card.append(el("div", { class: "small muted" }, "laden…")), card;
  if (schema.error) return card.append(el("div", { class: "small" }, schema.error)), card;
  card.append(el("h4", {}, "Values"), el("div", { class: "checklist" },
    schema.fields.length ? schema.fields.map((f) => el("label", {},
      el("input", { type: "checkbox", checked: sel.fields.has(f.name), onchange: (e) => (e.target.checked ? sel.fields.add(f.name) : sel.fields.delete(f.name)) }),
      f.name, f.type !== "number" ? el("span", { class: "tagchip" }, f.type === "boolean" ? "aan/uit" : "tekst") : null))
      : el("div", { class: "none" }, "Geen values in deze periode")));
  if (schema.tags.length) {
    card.append(el("h4", {}, "Tags (filter)"));
    for (const tag of schema.tags) card.append(tagFilter(m, tag));
  }
  return card;
}

function tagFilter(m, tag) {
  const b = ed.browser;
  const chosen = (b.sel[m].tags[tag] ||= new Set());
  const key = m + "\u0000" + tag;
  const count = el("span", { class: "small", style: "color:var(--accent)" }, chosen.size ? ` ${chosen.size} gekozen` : "");
  const search = el("input", { type: "search", placeholder: "Zoeken…" });
  const list = el("div", { class: "checklist" });
  const details = el("details", { class: "tag", open: chosen.size > 0 }, el("summary", {}, tag, count), search, list);
  const render = () => {
    const fetched = b.tagValues[key];
    if (!fetched) return list.replaceChildren(el("div", { class: "none" }, "laden…"));
    const q = search.value.toLowerCase();
    const values = [...new Set([...chosen, ...fetched])].sort((x, y) => x.localeCompare(y)).filter((v) => v.toLowerCase().includes(q)).slice(0, 500);
    list.replaceChildren(...values.map((v) => el("label", {},
      el("input", { type: "checkbox", checked: chosen.has(v), onchange: (e) => { e.target.checked ? chosen.add(v) : chosen.delete(v); count.textContent = chosen.size ? ` ${chosen.size} gekozen` : ""; } }), v)));
  };
  search.addEventListener("input", render);
  details.addEventListener("toggle", async () => {
    if (!details.open) return;
    render();
    if (!b.tagValues[key]) {
      try {
        b.tagValues[key] = await api("GET", "influx/tagvalues?" + new URLSearchParams({ bucket: b.bucket, measurement: m, tag, ...rangeParams() }));
      } catch (e) { b.tagValues[key] = []; toast(e.message, true); }
      render();
    }
  });
  return details;
}

async function addSeries() {
  const b = ed.browser;
  const selections = b.selected.filter((m) => b.sel[m]?.fields.size).map((m) => ({
    measurement: m, fields: [...b.sel[m].fields],
    tags: Object.fromEntries(Object.entries(b.sel[m].tags).filter(([, s]) => s.size).map(([k, s]) => [k, [...s]])),
  }));
  if (!selections.length) throw new Error("Kies minstens één value");
  const series = await api("POST", "influx/series", { bucket: b.bucket, selections, ...rangeParams() });
  const key = (c) => JSON.stringify([c.bucket, c.measurement, c.field, Object.entries(c.tags).sort()]);
  const have = new Set(ed.checks.map(key));
  const fresh = series.filter((s) => !have.has(key(s)));
  for (const s of fresh) {
    ed.checks.push({ id: null, bucket: s.bucket, measurement: s.measurement, field: s.field, tags: s.tags, kind: s.kind,
      label: "", auto_label: s.label, unit: s.unit, lower: null, upper: null, delay_min: s.kind === "number" ? 5 : 0, nodata_min: 60,
      paused: 0, last_value: s.value, last_time: s.time, status: "unknown", reason: "" });
  }
  if (fresh.length) markDirty();
  toast(fresh.length ? `${fresh.length} waarde${fresh.length === 1 ? "" : "n"} toegevoegd. Stel de grenzen in en klik op Opslaan.`
    : series.length ? "Deze waarden staan al in de lijst" : "Geen reeksen gevonden in deze periode");
  $("checks").replaceWith(checksPanel());
}

// ---------------------------------------------------------------------------
// Contacts
// ---------------------------------------------------------------------------
async function renderContacts() {
  const contacts = await api("GET", "contacts");
  const form = (c = {}) => {
    const f = {
      name: el("input", { value: c.name || "", required: true, placeholder: "Naam" }),
      email: el("input", { type: "email", value: c.email || "", placeholder: "naam@bedrijf.be" }),
      phone: el("input", { value: c.phone || "", placeholder: "+32 470 12 34 56" }),
      notes: el("input", { value: c.notes || "", placeholder: "bv. technieker, enkel weekdagen" }),
    };
    return { f, body: () => ({ name: f.name.value, email: f.email.value, phone: f.phone.value, notes: f.notes.value }) };
  };
  const add = form();
  const editing = new Set();

  const draw = () => setView(
    el("div", { class: "page-head" }, el("h1", {}, "Contactpersonen")),
    el("section", { class: "card" }, el("h2", {}, "Nieuwe contactpersoon"),
      el("form", {
        class: "form-row",
        onsubmit: async (e) => {
          e.preventDefault();
          try { await api("POST", "contacts", add.body()); toast("Toegevoegd"); renderContacts(); } catch (err) { toast(err.message, true); }
        },
      },
      el("label", { class: "field" }, "Naam *", add.f.name), el("label", { class: "field" }, "Mail", add.f.email),
      el("label", { class: "field" }, "Telefoon (voor sms en oproep)", add.f.phone), el("label", { class: "field" }, "Notitie", add.f.notes),
      el("button", { class: "primary", type: "submit" }, "Toevoegen")),
      el("p", { class: "small muted" }, "Tip: met de nummers ", el("code", {}, "test:1001"), " (neemt op en drukt 1), ",
        el("code", {}, "test:1002"), " (neemt op, drukt niets) en ", el("code", {}, "test:1003"),
        " (neemt niet op) test je de bel-cascade zonder echte telefoon.")),
    el("section", { class: "card flush" }, contacts.length ? el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Naam"), el("th", {}, "Mail"), el("th", {}, "Telefoon"), el("th", {}, "Notitie"),
        el("th", {}, "Regels"), el("th", {}, "Testen"), el("th", {}, ""))),
      el("tbody", {}, contacts.map((c) => {
        if (editing.has(c.id)) {
          const e = form(c);
          return el("tr", {}, el("td", {}, e.f.name), el("td", {}, e.f.email), el("td", {}, e.f.phone), el("td", {}, e.f.notes), el("td", {}, c.rules), el("td", {}),
            el("td", {}, el("button", { class: "small primary", onclick: (ev) => busy(ev.target, async () => { await api("PUT", `contacts/${c.id}`, e.body()); toast("Opgeslagen"); renderContacts(); }) }, "Opslaan"),
              el("button", { class: "small ghost", onclick: () => { editing.delete(c.id); draw(); } }, "Annuleren")));
        }
        const test = (channel, label, disabled) => el("button", {
          class: "small", disabled,
          onclick: (ev) => busy(ev.target, async () => toast((await api("POST", `contacts/${c.id}/test`, { channel })).message)),
        }, label);
        return el("tr", {},
          el("td", {}, el("b", {}, c.name)), el("td", {}, c.email || "–"), el("td", {}, c.phone || "–"), el("td", { class: "small muted" }, c.notes),
          el("td", {}, c.rules),
          el("td", {}, test("mail", "Mail", !c.email), " ", test("sms", "SMS", !c.phone), " ", test("call", "Bellen", !c.phone)),
          el("td", {}, el("button", { class: "small ghost", onclick: () => { editing.add(c.id); draw(); } }, "Wijzigen"),
            el("button", {
              class: "small ghost danger",
              onclick: async () => {
                if (!confirm(`${c.name} verwijderen?${c.rules ? ` Deze persoon staat in ${c.rules} regel(s).` : ""}`)) return;
                await api("DELETE", `contacts/${c.id}`);
                renderContacts();
              },
            }, "Verwijderen")));
      })))) : el("p", { class: "empty-state" }, "Nog geen contactpersonen.")));
  draw();
}

// ---------------------------------------------------------------------------
// Log
// ---------------------------------------------------------------------------
async function renderLog() {
  const rows = await api("GET", "log");
  setView(
    el("div", { class: "page-head" }, el("h1", {}, "Historiek"), el("span", { class: "muted small" }, "laatste 500 gebeurtenissen")),
    el("section", { class: "card flush" }, rows.length ? el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Tijd"), el("th", {}, "Regel"), el("th", {}, "Wat"),
        el("th", {}, "Contact"), el("th", {}, "Resultaat"), el("th", {}, "Detail"))),
      el("tbody", {}, rows.map((l) => el("tr", {},
        el("td", { class: "small" }, time(l.ts)),
        el("td", {}, l.rule_id ? el("a", { href: `#/rule/${l.rule_id}` }, l.rule || `#${l.rule_id}`) : "–"),
        el("td", {}, logWhat(l)), el("td", {}, l.contact || ""),
        el("td", {}, el("span", { class: `log-status log-${l.status}` }, logStatus(l.status))),
        el("td", { class: "small" }, l.detail)))))) : el("p", { class: "empty-state" }, "Nog niets gebeurd.")));
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
async function renderSettings() {
  const s = await api("GET", "settings");
  const inputs = {};
  const field = (key, label, attrs = {}, hint) => {
    const input = attrs.options
      ? el("select", {}, attrs.options.map(([v, t]) => el("option", { value: v, selected: String(s[key]) === String(v) }, t)))
      : attrs.textarea ? el("textarea", { rows: attrs.rows || 3 }, s[key] ?? "")
        : el("input", { value: s[key] ?? "", type: attrs.type || "text", placeholder: attrs.placeholder || "" });
    inputs[key] = input;
    return el("label", { class: "field" }, label, input, hint ? el("span", { class: "small muted" }, hint) : null);
  };
  const save = async () => {
    const body = Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value]));
    await api("PUT", "settings", body);
    toast("Instellingen opgeslagen");
  };
  const testBtn = (what, label, needsTo) => {
    const to = needsTo ? el("input", { placeholder: needsTo, style: "width:220px" }) : null;
    return el("div", { class: "form-row", style: "margin-top:10px" }, to,
      el("button", { onclick: (e) => busy(e.target, async () => { await save(); toast((await api("POST", "settings/test", { what, to: to?.value })).message); }) }, label));
  };

  const smsFields = el("div");
  const drawSms = () => {
    const p = inputs.sms_provider.value;
    smsFields.replaceChildren(
      p === "http" ? el("div", { class: "grid2" },
        field("sms_http_method", "Methode", { options: [["POST", "POST"], ["GET", "GET"]] }),
        field("sms_http_url", "URL", { placeholder: "https://api.provider.be/sms?to={to}" }, "Plaatshouders: {to} (+32…), {to_digits} (32…), {message}"),
        field("sms_http_content_type", "Content-Type", { options: [["application/json", "JSON"], ["application/x-www-form-urlencoded", "Formulier"], ["text/plain", "Tekst"]] }),
        field("sms_http_body", "Body", { textarea: true }, "bv. {\"to\": \"{to}\", \"text\": \"{message}\"}"),
        field("sms_http_headers", "Extra headers (geheim)", { textarea: true, rows: 2 }, "Eén per regel, bv. Authorization: Bearer abc123")) :
      p === "twilio" ? el("div", { class: "grid2" },
        field("twilio_sid", "Account SID"), field("twilio_token", "Auth token", { type: "password" }), field("twilio_from", "Afzender (nummer of naam)")) :
      p === "asterisk" ? el("div", { class: "grid2" },
        field("sms_asterisk_mode", "Via", { options: [["dongle", "GSM-dongle (chan_dongle)"], ["sip", "SIP MESSAGE via de provider"]] }),
        field("sms_dongle_device", "Dongle-naam"), field("sms_sip_domain", "SIP-domein provider"), field("sms_sip_from", "Afzender")) :
      el("p", { class: "small muted" }, "Geen sms: alleen mail en oproepen."));
  };

  setView(
    el("div", { class: "page-head" }, el("h1", {}, "Instellingen"),
      el("div", { class: "actions" }, el("button", { class: "primary", onclick: (e) => busy(e.target, save) }, "Opslaan"))),
    el("div", { class: "settings-section" },
      el("section", { class: "card" }, el("h2", {}, "InfluxDB"),
        el("div", { class: "grid2" }, field("influx_url", "URL", { placeholder: "http://influxdb:8086" }), field("influx_org", "Organisatie"),
          field("influx_token", "Token (leesrechten volstaan)", { type: "password" })),
        testBtn("influx", "Verbinding testen")),
      el("section", { class: "card" }, el("h2", {}, "Algemeen"),
        el("div", { class: "grid2" },
          field("check_interval", "Controle elke (seconden)", { type: "number" }),
          field("public_url", "Adres van deze app", {}, "Voor de link in de mails"),
          field("timezone", "Tijdzone", { placeholder: "Europe/Brussels" }),
          field("tts_voice", "Stem aan de telefoon", { options: [["nl_BE-nathalie-medium", "Nathalie (Vlaams, vrouw)"], ["nl_BE-rdh-medium", "RDH (Vlaams, man)"]] }))),
      el("section", { class: "card" }, el("h2", {}, "Mail (SMTP)"),
        el("div", { class: "grid2" }, field("smtp_host", "Server", { placeholder: "smtp.gmail.com" }), field("smtp_port", "Poort", { type: "number" }),
          field("smtp_security", "Beveiliging", { options: [["starttls", "STARTTLS (587)"], ["ssl", "SSL/TLS (465)"], ["none", "Geen"]] }),
          field("smtp_user", "Gebruiker"), field("smtp_password", "Wachtwoord", { type: "password" }), field("smtp_from", "Afzender", { placeholder: "alarm@bedrijf.be" })),
        testBtn("mail", "Testmail sturen", "naar mailadres")),
      el("section", { class: "card" }, el("h2", {}, "SMS"),
        field("sms_provider", "Provider", { options: [["none", "Geen"], ["http", "HTTP-API (eigen provider)"], ["twilio", "Twilio"], ["asterisk", "Via Asterisk"]] }),
        smsFields, testBtn("sms", "Test-sms sturen", "naar nummer")),
      el("section", { class: "card" }, el("h2", {}, "Telefonie (Asterisk)"),
        el("p", { class: "small" }, s.sip_configured
          ? "SIP-provider ingesteld via het .env-bestand van docker compose."
          : "Nog geen SIP-provider ingesteld: vul SIP_HOST, SIP_USERNAME en SIP_PASSWORD in het .env-bestand in en herstart. Tot dan werken enkel de testnummers (test:1001 …)."),
        el("p", { class: "small muted" }, `AMI: ${s.ami_user}@${s.ami_host}:${s.ami_port}`),
        el("div", { class: "grid2" }, field("caller_id", "Getoond nummer (caller ID)", { placeholder: "+3250123456" }, "Moet meestal een nummer van je provider zijn")),
        testBtn("asterisk", "Asterisk testen"), testBtn("call", "Testoproep", "nummer, bv. test:1001"))));
  inputs.sms_provider.addEventListener("change", drawSms);
  drawSms();
}

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------
async function renderUsers() {
  const users = await api("GET", "users");
  const name = el("input", { required: true });
  const pw = el("input", { type: "password", minlength: 8, required: true, autocomplete: "new-password" });
  setView(
    el("div", { class: "page-head" }, el("h1", {}, "Gebruikers")),
    el("section", { class: "card" }, el("h2", {}, "Nieuwe gebruiker"),
      el("form", {
        class: "form-row",
        onsubmit: async (e) => {
          e.preventDefault();
          try { await api("POST", "users", { username: name.value, password: pw.value }); toast("Gebruiker aangemaakt"); renderUsers(); } catch (err) { toast(err.message, true); }
        },
      }, el("label", { class: "field" }, "Gebruikersnaam", name), el("label", { class: "field" }, "Wachtwoord (min. 8)", pw),
      el("button", { class: "primary", type: "submit" }, "Toevoegen"))),
    el("section", { class: "card flush" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Gebruiker"), el("th", {}, "Aangemaakt"), el("th", {}, ""))),
      el("tbody", {}, users.map((u) => el("tr", {},
        el("td", {}, el("b", {}, u.username), u.username === me.user ? el("span", { class: "muted small" }, " (jij)") : null),
        el("td", { class: "small" }, time(u.created)),
        el("td", {},
          el("button", {
            class: "small ghost",
            onclick: async () => {
              const p = prompt(`Nieuw wachtwoord voor ${u.username} (min. 8 tekens):`);
              if (!p) return;
              try { await api("PUT", `users/${u.id}/password`, { password: p }); toast("Wachtwoord gewijzigd"); } catch (err) { toast(err.message, true); }
            },
          }, "Wachtwoord"),
          u.username !== me.user ? el("button", {
            class: "small ghost danger",
            onclick: async () => { if (confirm(`${u.username} verwijderen?`)) { await api("DELETE", `users/${u.id}`); renderUsers(); } },
          }, "Verwijderen") : null)))))));
}

window.addEventListener("beforeunload", (e) => {
  if (ed.dirty && location.hash.startsWith("#/rule/")) e.preventDefault();
});

route();
