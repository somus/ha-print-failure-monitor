// Print Failure Monitor sidebar panel: stats, charts and a gallery of checked frames.
// Plain web component, no build step and no external requests, so it works offline.

const DOMAIN = "print_failure_monitor";
const RANGES = [
  { hours: 24, label: "24 hours" },
  { hours: 168, label: "7 days" },
  { hours: 720, label: "30 days" },
];
const FILTERS = [
  { key: "all", label: "All", results: [] },
  { key: "failed", label: "Failure seen", results: ["failed"] },
  { key: "skipped", label: "Skipped", results: ["unsure", "not_visible", "error"] },
  { key: "clean", label: "Looks fine", results: ["clean"] },
  { key: "labeled", label: "Labeled", results: [], labeled: true },
];
const RESULTS = {
  failed: { label: "Failure seen", icon: "mdi:alert-octagon", group: "failed" },
  unsure: { label: "Unsure", icon: "mdi:help-circle", group: "skipped" },
  not_visible: { label: "Plate not visible", icon: "mdi:eye-off", group: "skipped" },
  error: { label: "Error", icon: "mdi:alert", group: "skipped" },
  clean: { label: "Looks fine", icon: "mdi:check-circle", group: "clean" },
};
// Daily chart groups, bottom to top. Validated as a set (CVD and normal-vision separation pass in
// both modes); the gray is the intentional neutral, and labels + table view relieve its contrast.
const GROUPS = [
  { key: "failed", label: "Failure seen", results: ["failed"] },
  { key: "skipped", label: "Skipped", results: ["unsure", "not_visible", "error"] },
  { key: "clean", label: "Looks fine", results: ["clean"] },
];
const REFRESH_MS = 60000;

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pct = (p) => (p == null ? "–" : `${Math.round(p * 100)}%`);
const label = (qid) => qid.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

function bytes(n) {
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

class PrintFailureMonitorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._entries = null;
    this._entryId = null;
    this._hours = 24;
    this._filter = "all";
    this._summary = null;
    this._frames = [];
    this._more = false;
    // Signed URL per frame, kept across refreshes so the browser cache works (each signing makes a new URL).
    this._urls = new Map();
    this._error = null;
    this._timer = null;
    // Bumped by every load; a response that arrives after a newer load started is dropped, so switching
    // printer, range or filter mid-request can't show (or label) another selection's frames.
    this._load = 0;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._init();
    this._applyTheme();
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = hass;
  }

  set narrow(narrow) {
    this._narrow = narrow;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.narrow = narrow;
  }

  connectedCallback() {
    this._timer = setInterval(() => this._refresh(false), REFRESH_MS);
    this._onLocation = () => this._openFromUrl();
    window.addEventListener("location-changed", this._onLocation);
    this._resize = new ResizeObserver(() => this._drawCharts());
    this._resize.observe(this);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    window.removeEventListener("location-changed", this._onLocation);
    this._resize?.disconnect();
  }

  _applyTheme() {
    this.toggleAttribute("dark", Boolean(this._hass?.themes?.darkMode));
  }

  _ws(msg) {
    return this._hass.callWS({ ...msg, type: `${DOMAIN}/${msg.type}` });
  }

  async _init() {
    this._renderShell();
    try {
      this._entries = await this._ws({ type: "entries" });
    } catch (err) {
      this._error = err.message || String(err);
    }
    this._entryId = this._entries?.[0]?.entry_id ?? null;
    if (!(await this._openFromUrl())) {
      this._renderControls();
      await this._refresh(true);
    }
  }

  // Opened from a notification: ?entry=<id>&frame=<id> selects the printer, picks a range that
  // contains the frame, and opens it. The query is then cleared so a reload doesn't reopen it.
  async _openFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const entry = params.get("entry");
    if (!entry || !this._entries) return false;
    const frameId = params.get("frame");
    try {
      window.history.replaceState(window.history.state, "", window.location.pathname);
    } catch {
      // Not allowed on some origins (e.g. file:// previews); reopening on reload is harmless.
    }
    if (!this._entries.some((e) => e.entry_id === entry)) {
      this._notice = "That printer is no longer monitored.";
      return false;
    }
    this._entryId = entry;
    let frame = null;
    if (frameId) {
      try {
        frame = await this._ws({ type: "frame", entry_id: entry, frame_id: frameId });
        const ageHours = (Date.now() - new Date(frame.at).getTime()) / 3600000;
        this._hours = (RANGES.find((r) => r.hours >= ageHours) ?? RANGES[RANGES.length - 1]).hours;
        this._filter = "all";
      } catch {
        this._notice = "The frame from that alert is no longer in the history.";
      }
    }
    this._renderControls();
    await this._refresh(true);
    if (frame) this._openFrame(frame);
    return true;
  }

  async _refresh(resetFrames) {
    if (!resetFrames && !(await this._refreshEntries())) return;
    if (!this._entryId) return this._renderBody();
    const load = ++this._load;
    const entryId = this._entryId;
    if (!resetFrames && this._frames.length > 48) {
      // The user paged further; refresh only the stats so the gallery doesn't jump.
      try {
        const summary = await this._ws({ type: "summary", entry_id: entryId, hours: this._hours });
        if (load !== this._load) return;
        this._summary = summary;
      } catch (err) {
        console.warn("Print Failure Monitor: refreshing stats failed", err);
        return;
      }
      return this._renderStats();
    }
    let error = null;
    try {
      const [summary, page] = await Promise.all([
        this._ws({ type: "summary", entry_id: entryId, hours: this._hours }),
        this._ws({ type: "frames", entry_id: entryId, hours: this._hours, ...this._query() }),
      ]);
      if (load !== this._load) return;
      this._summary = summary;
      this._frames = this._keepUrls(page.frames, true);
      this._more = page.more;
    } catch (err) {
      if (load !== this._load) return;
      error = err.message || String(err);
    }
    const inPlace = !resetFrames && !error && !this._error && this.shadowRoot.getElementById("tiles");
    this._error = error;
    // The periodic refresh updates what's on screen in place, so an open table or the scroll position stays.
    if (inPlace) {
      this._renderStats();
      this._renderGallery();
    } else {
      this._renderBody();
    }
  }

  // Printers, their plate areas and retention can change elsewhere; picks them up on the periodic refresh.
  // Returns false when the selected printer is gone and the view was reset instead.
  async _refreshEntries() {
    let entries;
    try {
      entries = await this._ws({ type: "entries" });
    } catch {
      return true;  // keep showing what's there; the next refresh tries again
    }
    const changed = JSON.stringify(entries.map((e) => [e.entry_id, e.title, e.retention_days])) !==
      JSON.stringify((this._entries || []).map((e) => [e.entry_id, e.title, e.retention_days]));
    this._entries = entries;
    if (!entries.some((e) => e.entry_id === this._entryId)) {
      this._entryId = entries[0]?.entry_id ?? null;
      this._renderControls();
      await this._refresh(true);
      return false;
    }
    if (changed) this._renderControls();
    return true;
  }

  async _loadMore() {
    const load = this._load;
    const last = this._frames[this._frames.length - 1];
    const page = await this._ws({
      type: "frames",
      entry_id: this._entryId,
      hours: this._hours,
      ...this._query(),
      before: last.id,
    });
    if (load !== this._load) return;  // the selection changed meanwhile
    this._frames = this._frames.concat(this._keepUrls(page.frames, false));
    this._more = page.more;
    this._renderGallery();
  }

  _keepUrls(frames, replace) {
    const urls = replace ? new Map() : this._urls;
    for (const f of frames) {
      if (!f.url) continue;
      const key = `${this._entryId}/${f.id}`;
      f.url = this._urls.get(key) ?? f.url;
      urls.set(key, f.url);
    }
    this._urls = urls;
    return frames;
  }

  // Signed URLs last an hour; when one has expired, sign it again once.
  _resignOnError(root) {
    root.querySelectorAll("img[data-id]").forEach((img) =>
      img.addEventListener(
        "error",
        async () => {
          try {
            const f = await this._ws({ type: "frame", entry_id: this._entryId, frame_id: img.dataset.id });
            if (!f.url) return;
            this._urls.set(`${this._entryId}/${f.id}`, f.url);
            for (const x of this._frames) if (x.id === f.id) x.url = f.url;
            img.src = f.url;
          } catch {
            // The frame was cleaned up; the broken image stays.
          }
        },
        { once: true }
      )
    );
  }

  _query() {
    const filter = FILTERS.find((f) => f.key === this._filter);
    return { results: filter.results, labeled: Boolean(filter.labeled) };
  }

  // ---- rendering -------------------------------------------------------------------------------

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <h1>Print failures</h1>
      </div>
      <main>
        <div class="controls" id="controls"></div>
        <div id="body"><p class="muted">Loading…</p></div>
      </main>
      <dialog id="dialog"></dialog>
      <div class="tooltip" id="tooltip" role="status" hidden></div>`;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    menu.hass = this._hass;
    menu.narrow = this._narrow;
    this.shadowRoot.getElementById("dialog").addEventListener("click", (e) => {
      if (e.target.id === "dialog" || e.target.closest("[data-close]")) e.currentTarget.close();
    });
  }

  _renderControls() {
    const el = this.shadowRoot.getElementById("controls");
    const entries = this._entries || [];
    const retention = entries.find((e) => e.entry_id === this._entryId)?.retention_days ?? 30;
    el.innerHTML = `
      ${
        entries.length > 1
          ? `<label class="field">Printer
               <select id="entry">${entries
                 .map((e) => `<option value="${esc(e.entry_id)}" ${e.entry_id === this._entryId ? "selected" : ""}>${esc(e.title)}</option>`)
                 .join("")}</select></label>`
          : ""
      }
      <div class="segmented" role="group" aria-label="Time range">
        ${RANGES.map(
          (r) => `<button data-hours="${r.hours}" aria-pressed="${r.hours === this._hours}" ${
            r.hours > retention * 24 ? 'title="Longer than the history kept"' : ""
          }>${r.label}</button>`
        ).join("")}
      </div>
      <div class="segmented" role="group" aria-label="Show frames">
        ${FILTERS.map(
          (f) => `<button data-filter="${f.key}" aria-pressed="${f.key === this._filter}">${f.label}</button>`
        ).join("")}
      </div>
      ${
        this._entryId && this._hass.user?.is_admin
          ? `<div class="tools">
               <button class="secondary" id="crop"><ha-icon icon="mdi:crop"></ha-icon>Plate area</button>
               <button class="secondary" id="test"><ha-icon icon="mdi:tune-variant"></ha-icon>Test settings</button>
             </div>`
          : ""
      }`;
    el.querySelector("#crop")?.addEventListener("click", () => this._openCrop());
    el.querySelector("#test")?.addEventListener("click", () => this._openTest());
    el.querySelector("#entry")?.addEventListener("change", (e) => {
      this._entryId = e.target.value;
      this._renderControls();
      this._refresh(true);
    });
    el.querySelectorAll("[data-hours]").forEach((b) =>
      b.addEventListener("click", () => {
        this._hours = Number(b.dataset.hours);
        this._renderControls();
        this._refresh(true);
      })
    );
    el.querySelectorAll("[data-filter]").forEach((b) =>
      b.addEventListener("click", () => {
        this._filter = b.dataset.filter;
        this._renderControls();
        this._refresh(true);
      })
    );
  }

  _renderBody() {
    const body = this.shadowRoot.getElementById("body");
    if (this._error) {
      body.innerHTML = `<p class="error">Couldn't load history: ${esc(this._error)}</p>`;
      return;
    }
    if (!this._entryId) {
      body.innerHTML = `<p class="muted">No printers are being monitored. Add one under Settings → Devices &amp; services → Add integration → Print Failure Monitor.</p>`;
      return;
    }
    const notice = this._notice;
    this._notice = null;
    body.innerHTML = `
      ${notice ? `<p class="notice" role="status">${esc(notice)}</p>` : ""}
      <section class="tiles" id="tiles"></section>
      <section class="charts">
        <figure class="card">
          <figcaption>Failure probability</figcaption>
          <div class="chart" id="line"></div>
          <details><summary>Show as table</summary><div id="line-table"></div></details>
        </figure>
        <figure class="card">
          <figcaption>Frames per day</figcaption>
          <div class="legend">${GROUPS.map((g) => `<span><i class="swatch ${g.key}"></i>${g.label}</span>`).join("")}</div>
          <div class="chart" id="bars"></div>
          <details><summary>Show as table</summary><div id="bars-table"></div></details>
        </figure>
      </section>
      <section>
        <h2>Checked frames</h2>
        <div class="gallery" id="gallery"></div>
        <div class="more" id="more"></div>
      </section>`;
    this._renderStats();
    this._renderGallery();
  }

  _renderStats() {
    const s = this._summary?.stats;
    const tiles = this.shadowRoot.getElementById("tiles");
    if (!s || !tiles) return;
    const skipped = s.by_result.unsure + s.by_result.not_visible + s.by_result.error;
    const tile = (value, name, note = "") =>
      `<div class="tile"><div class="value">${value}</div><div class="name">${name}</div>${note ? `<div class="note">${note}</div>` : ""}</div>`;
    tiles.innerHTML = [
      tile(s.checks.toLocaleString(), "Checks", `${s.prints} print${s.prints === 1 ? "" : "s"}`),
      tile(s.by_result.failed.toLocaleString(), "Failure seen", `${skipped.toLocaleString()} skipped`),
      tile(s.alerts, "Alerts", `${s.dismissed} dismissed as false alarm`),
      s.cost == null
        ? tile("n/a", "Cost", "not reported by this provider")
        : tile(`$${s.cost.toFixed(s.cost < 1 ? 4 : 2)}`, "Cost", "reported by the provider"),
      tile(bytes(s.storage_bytes), "Saved frames"),
      tile(
        s.labels.total.toLocaleString(),
        "Labeled",
        s.labels.total ? `${s.labels.false_alarms} false alarm${s.labels.false_alarms === 1 ? "" : "s"}, ${s.labels.missed} missed` : "label frames to measure accuracy"
      ),
    ].join("");
    this._drawCharts();
    this._renderTables();
  }

  _renderTables() {
    const summary = this._summary;
    const lineTable = this.shadowRoot.getElementById("line-table");
    if (!summary || !lineTable) return;
    const fmt = (iso) => new Date(iso).toLocaleString();
    lineTable.innerHTML = `<table><thead><tr><th>Time</th><th>Highest failure probability</th><th>Result</th></tr></thead><tbody>${summary.series
      .slice()
      .reverse()
      .slice(0, 200)
      .map((p) => `<tr><td>${fmt(p.at)}</td><td>${pct(p.p)}</td><td>${RESULTS[p.result]?.label ?? p.result}</td></tr>`)
      .join("")}</tbody></table>`;
    this.shadowRoot.getElementById("bars-table").innerHTML = `<table><thead><tr><th>Day</th>${Object.values(RESULTS)
      .map((r) => `<th>${r.label}</th>`)
      .join("")}</tr></thead><tbody>${summary.daily
      .map((d) => `<tr><td>${d.day}</td>${Object.keys(RESULTS).map((k) => `<td>${d[k]}</td>`).join("")}</tr>`)
      .join("")}</tbody></table>`;
  }

  _renderGallery() {
    const gallery = this.shadowRoot.getElementById("gallery");
    if (!gallery) return;
    if (!this._frames.length) {
      gallery.innerHTML = `<p class="muted">No checks in this range${this._filter === "all" ? "" : " match this filter"}.</p>`;
    } else {
      gallery.innerHTML = this._frames
        .map((f, i) => {
          const r = RESULTS[f.result] ?? { label: f.result, icon: "mdi:circle", group: "skipped" };
          const image = f.url
            ? `<img src="${esc(f.url)}" data-id="${esc(f.id)}" alt="Frame at ${esc(new Date(f.at).toLocaleString())}" loading="lazy">`
            : `<div class="noimage">${f.result === "error" ? "No frame (error)" : "Frame not kept"}</div>`;
          return `<button class="frame" data-index="${i}">
              ${image}
              <span class="badge ${r.group}"><ha-icon icon="${r.icon}"></ha-icon>${r.label}</span>
              ${f.label ? `<span class="tag ${f.label}" role="img" aria-label="You labeled this: ${f.label === "failure" ? "a failure" : "nothing wrong"}" title="You labeled this: ${f.label === "failure" ? "a failure" : "nothing wrong"}"><ha-icon icon="${f.label === "failure" ? "mdi:account-alert" : "mdi:account-check"}"></ha-icon></span>` : ""}
              <span class="meta"><span>${esc(
                new Date(f.at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })
              )}</span><span title="Highest failure probability">${f.top == null ? "" : pct(f.top)}</span></span>
            </button>`;
        })
        .join("");
      gallery.querySelectorAll(".frame").forEach((b) =>
        b.addEventListener("click", () => this._openFrame(this._frames[Number(b.dataset.index)]))
      );
      this._resignOnError(gallery);
    }
    const more = this.shadowRoot.getElementById("more");
    more.innerHTML = this._more ? `<button class="secondary">Load more</button>` : "";
    more.querySelector("button")?.addEventListener("click", () => this._loadMore());
  }

  _openFrame(f) {
    const r = RESULTS[f.result] ?? { label: f.result, icon: "mdi:circle", group: "skipped" };
    // The plate-visible gate first, then the failure questions from most to least likely.
    const answers = Object.entries(f.answers || {}).sort((a, b) =>
      a[0] === "plate_visible" ? -1 : b[0] === "plate_visible" ? 1 : b[1] - a[1]
    );
    const dialog = this.shadowRoot.getElementById("dialog");
    dialog.innerHTML = `
      <div class="dialog">
        <header>
          <span class="badge ${r.group}"><ha-icon icon="${r.icon}"></ha-icon>${r.label}</span>
          <span>${esc(new Date(f.at).toLocaleString())}</span>
          <button class="close" data-close aria-label="Close">✕</button>
        </header>
        ${f.url ? `<img src="${esc(f.url)}" data-id="${esc(f.id)}" alt="Checked frame">` : `<div class="noimage large">Frame not kept</div>`}
        ${f.error ? `<p class="error">${esc(f.error)}</p>` : ""}
        <table class="answers"><tbody>${answers
          .map(
            ([q, p]) =>
              `<tr><th scope="row">${esc(label(q))}${q === "plate_visible" ? ' <span class="muted">(gate)</span>' : ""}</th><td class="meter"><span style="width:${(p * 100).toFixed(1)}%"></span></td><td>${pct(p)}</td></tr>`
          )
          .join("")}</tbody></table>
        <p class="muted">${esc(f.model || "")}${f.counted ? "" : " · not counted toward alerts"}${typeof f.cost === "number" && f.cost ? ` · $${f.cost.toFixed(5)}` : ""}</p>
        ${
          this._hass.user?.is_admin
            ? `<div class="labeling" role="group" aria-label="What did this frame really show?">
                <span>What did it really show?</span>
                ${["failure", "fine"]
                  .map((l) => `<button class="secondary" data-label="${l}" aria-pressed="${f.label === l}">${l === "failure" ? "A failure" : "Nothing wrong"}</button>`)
                  .join("")}
                ${f.label ? `<button class="link" data-label="">Clear</button>` : ""}
              </div>`
            : ""
        }
      </div>`;
    dialog.querySelectorAll("[data-label]").forEach((b) =>
      b.addEventListener("click", () => this._setLabel(f, b.dataset.label || null))
    );
    this._resignOnError(dialog);
    if (!dialog.open) dialog.showModal();
  }

  async _setLabel(f, value) {
    try {
      await this._ws({ type: "label", entry_id: this._entryId, frame_id: f.id, label: value });
    } catch (err) {
      console.warn("Print Failure Monitor: saving the label failed", err);
      return;
    }
    f.label = value;
    for (const x of this._frames) if (x.id === f.id) x.label = value;
    this._openFrame(f);
    this._renderGallery();
    try {
      this._summary = await this._ws({ type: "summary", entry_id: this._entryId, hours: this._hours });
      this._renderStats();
    } catch {
      // The stats catch up on the next refresh.
    }
  }

  // ---- plate area --------------------------------------------------------------------------------

  // Drag a box over the live camera picture; checks then send only that part of the frame.
  async _openCrop() {
    await this._refreshEntries();  // the plate area may have been changed elsewhere
    const entry = this._entries.find((e) => e.entry_id === this._entryId);
    if (!entry) return;
    const picture = this._hass.states[entry.camera]?.attributes?.entity_picture;
    let crop = entry.crop ? [...entry.crop] : null;
    const dialog = this.shadowRoot.getElementById("dialog");
    dialog.innerHTML = `
      <div class="dialog">
        <header><strong>Plate area</strong><button class="close" data-close aria-label="Close">✕</button></header>
        <p class="muted">Drag a box over the build plate and the space above it, up to and including the nozzle. Checks
          then send only that part of the frame. From a low camera, prints appear above the plate, not on it, so
          keep everything below the nozzle in the box, and leave a margin at the sides: a failure can spill past the
          plate's edge.</p>
        ${
          picture
            ? `<div class="cropper"><img src="${esc(picture)}" alt="Live camera picture" draggable="false"><div class="crop-dim" hidden></div><div class="crop-box" hidden></div></div>`
            : `<p class="error">The camera has no picture right now, so the area can't be drawn.</p>`
        }
        <div class="actions">
          <button class="link" id="whole" ${entry.crop ? "" : "hidden"}>Use the whole frame</button>
          <button class="secondary" data-close>Cancel</button>
          <button class="primary" id="save" disabled>Save</button>
        </div>
      </div>`;
    const area = dialog.querySelector(".cropper");
    const box = dialog.querySelector(".crop-box");
    const dim = dialog.querySelector(".crop-dim");
    const save = dialog.querySelector("#save");
    const draw = () => {
      box.hidden = dim.hidden = !crop;
      if (!crop) return;
      const [l, t, r, b] = crop.map((v) => `${v * 100}%`);
      Object.assign(box.style, { left: l, top: t, right: `${100 - crop[2] * 100}%`, bottom: `${100 - crop[3] * 100}%` });
      // Darken everything outside the box: the whole area, minus the box, with the even-odd rule.
      dim.style.clipPath = `polygon(evenodd, 0 0, 100% 0, 100% 100%, 0 100%, 0 0, ${l} ${t}, ${r} ${t}, ${r} ${b}, ${l} ${b}, ${l} ${t})`;
      save.disabled = crop[2] - crop[0] < 0.1 || crop[3] - crop[1] < 0.1;
    };
    if (area) {
      const at = (e) => {
        const rect = area.getBoundingClientRect();
        return [
          Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 1),
          Math.min(Math.max((e.clientY - rect.top) / rect.height, 0), 1),
        ];
      };
      let start = null;
      area.addEventListener("pointerdown", (e) => {
        start = at(e);
        area.setPointerCapture(e.pointerId);
        e.preventDefault();
      });
      area.addEventListener("pointermove", (e) => {
        if (!start) return;
        const [x, y] = at(e);
        crop = [Math.min(start[0], x), Math.min(start[1], y), Math.max(start[0], x), Math.max(start[1], y)];
        draw();
      });
      area.addEventListener("pointerup", () => (start = null));
      draw();
    }
    const send = async (value) => {
      try {
        await this._ws({ type: "set_crop", entry_id: this._entryId, crop: value });
      } catch (err) {
        dialog.querySelector(".actions").insertAdjacentHTML("beforebegin", `<p class="error">${esc(err.message || err)}</p>`);
        return;
      }
      entry.crop = value;
      this._notice = value ? "Saved. The next check uses the plate area." : "Saved. Checks use the whole frame again.";
      dialog.close();
      this._renderBody();
    };
    save.addEventListener("click", () => send(crop));
    dialog.querySelector("#whole").addEventListener("click", () => send(null));
    if (!dialog.open) dialog.showModal();
  }

  // ---- testing settings against labels -----------------------------------------------------------

  async _openTest() {
    const dialog = this.shadowRoot.getElementById("dialog");
    let s;
    try {
      s = await this._ws({ type: "settings", entry_id: this._entryId });
    } catch (err) {
      this._notice = `Couldn't load the settings: ${err.message || err}`;
      return this._renderBody();
    }
    dialog.innerHTML = `
      <div class="dialog">
        <header><strong>Test settings</strong><button class="close" data-close aria-label="Close">✕</button></header>
        ${
          s.labeled
            ? `<p class="muted">Try other thresholds or questions on your ${s.labeled} labeled frame${s.labeled === 1 ? "" : "s"}
                 and compare the false alarms and misses before saving.</p>`
            : `<p class="notice">Label some frames first: open a frame and mark what it really showed. Include a few
                 false alarms and real failures.</p>`
        }
        <div class="form">
          <label>Yes threshold<input type="number" id="yes" min="0.05" max="0.98" step="0.01" value="${s.yes}"></label>
          <label>No threshold<input type="number" id="no" min="0.02" max="0.95" step="0.01" value="${s.no}"></label>
          <label class="wide">Questions (YAML, as in the options; empty uses the built-ins)
            <textarea id="questions" rows="6" spellcheck="false">${esc(s.questions)}</textarea></label>
        </div>
        <p class="muted" id="cost" hidden>Changed questions are asked again on the ${s.with_frames} labeled frame${s.with_frames === 1 ? "" : "s"}
          with a saved image: ${s.with_frames} model call${s.with_frames === 1 ? "" : "s"}.</p>
        <div class="actions">
          <button class="secondary" id="run" ${s.labeled ? "" : "disabled"}>Run test</button>
          <button class="primary" id="save">Save these settings</button>
        </div>
        <div id="results"></div>
      </div>`;
    const values = () => ({
      entry_id: this._entryId,
      yes: Number(dialog.querySelector("#yes").value),
      no: Number(dialog.querySelector("#no").value),
      questions: dialog.querySelector("#questions").value,
    });
    const results = dialog.querySelector("#results");
    dialog.querySelector("#questions").addEventListener("input", (e) => {
      dialog.querySelector("#cost").hidden = e.target.value.trim() === s.questions.trim();
    });
    const run = dialog.querySelector("#run");
    run.addEventListener("click", async () => {
      run.disabled = true;
      results.innerHTML = `<p class="muted">Testing…</p>`;
      try {
        this._renderTest(results, await this._ws({ type: "try_settings", ...values() }));
      } catch (err) {
        results.innerHTML = `<p class="error">${esc(err.message || err)}</p>`;
      }
      run.disabled = false;
    });
    dialog.querySelector("#save").addEventListener("click", async () => {
      try {
        await this._ws({ type: "save_settings", ...values() });
      } catch (err) {
        results.innerHTML = `<p class="error">${esc(err.message || err)}</p>`;
        return;
      }
      this._notice = "Saved. The printer reloaded with the new settings.";
      dialog.close();
      this._renderBody();
    });
    if (!dialog.open) dialog.showModal();
  }

  _renderTest(host, result) {
    const errs = (e) => `${e.false_alarms} false alarm${e.false_alarms === 1 ? "" : "s"}, ${e.missed} missed`;
    const name = (r) => RESULTS[r]?.label ?? r;
    const changed = result.rows.filter((r) => r.before !== r.after);
    host.innerHTML = `
      <div class="compare">
        <div class="tile"><div class="name">Current settings</div><div class="note">${errs(result.current)}</div></div>
        <div class="tile"><div class="name">These settings</div><div class="note">${errs(result.proposed)}</div></div>
      </div>
      <p class="muted">${result.rows.length} labeled frame${result.rows.length === 1 ? "" : "s"} tested${
        result.asked ? `, ${result.asked} asked again` : ", using the stored answers"
      }. ${changed.length ? `${changed.length} changed:` : "No frame's result changed."}</p>
      ${
        changed.length
          ? `<div class="scroll"><table class="changes"><thead><tr><th>Frame</th><th>Your label</th><th>Now</th><th>With these</th></tr></thead><tbody>${changed
              .map(
                (r) => `<tr>
                  <td>${r.url ? `<img src="${esc(r.url)}" data-id="${esc(r.id)}" alt="">` : ""}</td>
                  <td>${r.label === "failure" ? "A failure" : "Nothing wrong"}</td>
                  <td>${esc(name(r.before))}</td>
                  <td>${esc(name(r.after))}${r.error ? `<div class="error">${esc(r.error)}</div>` : ""}</td>
                </tr>`
              )
              .join("")}</tbody></table></div>`
          : ""
      }`;
    this._resignOnError(host);
  }

  // ---- charts ----------------------------------------------------------------------------------

  _drawCharts() {
    if (!this._summary) return;
    this._drawLine();
    this._drawBars();
  }

  _drawLine() {
    const host = this.shadowRoot.getElementById("line");
    if (!host) return;
    const series = this._summary.series;
    if (!series.length) {
      host.innerHTML = `<p class="muted">No checks in this range.</p>`;
      return;
    }
    const W = Math.max(host.clientWidth, 280);
    const H = 200;
    const m = { l: 40, r: 12, t: 10, b: 24 };
    const times = series.map((p) => new Date(p.at).getTime());
    const t0 = Math.min(...times);
    const t1 = Math.max(Math.max(...times), t0 + 60000);
    const x = (iso) => m.l + ((new Date(iso).getTime() - t0) / (t1 - t0)) * (W - m.l - m.r);
    const y = (p) => m.t + (1 - p) * (H - m.t - m.b);
    const yes = this._summary.yes_threshold;
    // Break the line between prints: a gap much longer than the usual check interval has no data.
    const gaps = times.slice(1).map((t, i) => t - times[i]).sort((a, b) => a - b);
    const breakAfter = Math.max((gaps[Math.floor(gaps.length / 2)] || 0) * 5, 10 * 60000);
    const path = series
      .map((p, i) => `${i && times[i] - times[i - 1] <= breakAfter ? "L" : "M"}${x(p.at).toFixed(1)},${y(p.p).toFixed(1)}`)
      .join("");
    const dots = series
      .filter((p) => p.result === "failed")
      .map((p) => `<circle class="dot-failed" cx="${x(p.at).toFixed(1)}" cy="${y(p.p).toFixed(1)}" r="4"></circle>`)
      .join("");
    const ticks = [0, 0.5, 1]
      .map(
        (p) =>
          `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(p)}" y2="${y(p)}"></line><text class="axis" x="${m.l - 6}" y="${y(p) + 4}" text-anchor="end">${p * 100}%</text>`
      )
      .join("");
    const span = t1 - t0;
    const timeFmt = span > 2 * 86400000 ? { month: "short", day: "numeric" } : { hour: "2-digit", minute: "2-digit" };
    const xTicks = [0, 0.5, 1]
      .map((f) => {
        const t = new Date(t0 + f * span);
        const anchor = f === 0 ? "start" : f === 1 ? "end" : "middle";
        return `<text class="axis" x="${m.l + f * (W - m.l - m.r)}" y="${H - 6}" text-anchor="${anchor}">${t.toLocaleString([], timeFmt)}</text>`;
      })
      .join("");
    host.innerHTML = `
      <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Highest failure probability per check over time; dots mark frames where a failure was seen">
        ${ticks}${xTicks}
        <line class="threshold" x1="${m.l}" x2="${W - m.r}" y1="${y(yes)}" y2="${y(yes)}"></line>
        <text class="threshold-label" x="${W - m.r}" y="${y(yes) - 5}" text-anchor="end">alert threshold ${pct(yes)}</text>
        <path class="line" d="${path}"></path>
        ${dots}
        <line class="crosshair" id="cross" y1="${m.t}" y2="${H - m.b}" visibility="hidden"></line>
        <circle class="hover-dot" id="hdot" r="5" visibility="hidden"></circle>
        <rect class="hit" x="${m.l}" y="0" width="${W - m.l - m.r}" height="${H}"></rect>
      </svg>`;
    const svg = host.querySelector("svg");
    const xs = series.map((p) => x(p.at));
    const cross = svg.getElementById("cross");
    const hdot = svg.getElementById("hdot");
    svg.querySelector(".hit").addEventListener("mousemove", (e) => {
      const px = e.offsetX;
      let i = 0;
      for (let k = 1; k < xs.length; k++) if (Math.abs(xs[k] - px) < Math.abs(xs[i] - px)) i = k;
      const p = series[i];
      cross.setAttribute("x1", xs[i]);
      cross.setAttribute("x2", xs[i]);
      cross.setAttribute("visibility", "visible");
      hdot.setAttribute("cx", xs[i]);
      hdot.setAttribute("cy", y(p.p));
      hdot.setAttribute("visibility", "visible");
      this._tooltip(e, `<b>${pct(p.p)}</b> highest failure probability<br>${RESULTS[p.result]?.label ?? p.result}<br><span class="muted">${new Date(p.at).toLocaleString()}</span>`);
    });
    svg.querySelector(".hit").addEventListener("mouseleave", () => {
      cross.setAttribute("visibility", "hidden");
      hdot.setAttribute("visibility", "hidden");
      this._tooltip(null);
    });
  }

  _drawBars() {
    const host = this.shadowRoot.getElementById("bars");
    if (!host) return;
    const days = this._summary.daily;
    if (!days.length) {
      host.innerHTML = `<p class="muted">No checks in this range.</p>`;
      return;
    }
    const W = Math.max(host.clientWidth, 280);
    const H = 200;
    const m = { l: 40, r: 12, t: 10, b: 24 };
    const totals = days.map((d) => Object.keys(RESULTS).reduce((s, k) => s + d[k], 0));
    const max = niceMax(Math.max(...totals, 1));
    const slot = (W - m.l - m.r) / days.length;
    const bw = Math.min(Math.max(slot - 6, 4), 48);
    const y = (v) => m.t + (1 - v / max) * (H - m.t - m.b);
    let bars = "";
    days.forEach((d, i) => {
      const cx = m.l + slot * i + slot / 2;
      let base = 0;
      GROUPS.forEach((g, gi) => {
        const v = g.results.reduce((s, k) => s + d[k], 0);
        if (!v) return;
        const top = y(base + v);
        const bottom = y(base);
        const isTop = GROUPS.slice(gi + 1).every((g2) => g2.results.reduce((s, k) => s + d[k], 0) === 0);
        const h = Math.max(bottom - top - (base ? 2 : 0), 1); // 2px surface gap between segments
        const r = isTop ? Math.min(4, bw / 2, h / 2) : 0;
        bars += `<path class="seg ${g.key}" data-day="${i}" data-group="${gi}" d="${roundedTop(cx - bw / 2, top, bw, h, r)}"></path>`;
        base += v;
      });
    });
    const yTicks = [0, max / 2, max]
      .map(
        (v) =>
          `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"></line><text class="axis" x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`
      )
      .join("");
    const every = Math.ceil(days.length / Math.max(Math.floor((W - m.l - m.r) / 56), 1));
    const xTicks = days
      .map((d, i) =>
        i % every
          ? ""
          : `<text class="axis" x="${m.l + slot * i + slot / 2}" y="${H - 6}" text-anchor="middle">${new Date(`${d.day}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric" })}</text>`
      )
      .join("");
    host.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Checked frames per day by result">${yTicks}${xTicks}${bars}</svg>`;
    host.querySelectorAll(".seg").forEach((seg) => {
      seg.addEventListener("mousemove", (e) => {
        const d = days[Number(seg.dataset.day)];
        const g = GROUPS[Number(seg.dataset.group)];
        const parts = g.results.filter((k) => d[k]).map((k) => `${RESULTS[k].label}: ${d[k]}`);
        this._tooltip(e, `<b>${g.label}</b> · ${new Date(`${d.day}T12:00:00`).toLocaleDateString()}<br>${parts.join("<br>")}`);
        seg.classList.add("hover");
      });
      seg.addEventListener("mouseleave", () => {
        seg.classList.remove("hover");
        this._tooltip(null);
      });
    });
  }

  _tooltip(e, html) {
    const tip = this.shadowRoot.getElementById("tooltip");
    if (!e) {
      tip.hidden = true;
      return;
    }
    tip.innerHTML = html;
    tip.hidden = false;
    const pad = 12;
    const { innerWidth } = window;
    const w = tip.offsetWidth;
    tip.style.left = `${Math.min(e.clientX + pad, innerWidth - w - pad)}px`;
    tip.style.top = `${e.clientY + pad}px`;
  }
}

function niceMax(v) {
  // Round an axis maximum up to 1, 2, 4 or 5 times a power of ten, so the midpoint tick is whole.
  const base = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 4, 5, 10].map((m) => m * base).find((n) => n >= v && Number.isInteger(n / 2)) ?? 10 * base;
}

function roundedTop(x, y, w, h, r) {
  // Rounded data-end on top, square at the baseline.
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

const STYLE = `
  :host {
    display: block;
    min-height: 100vh;
    background: var(--primary-background-color, #fcfcfb);
    color: var(--primary-text-color, #0b0b0b);
    font-family: var(--paper-font-body1_-_font-family, system-ui, sans-serif);
    --pfm-line: #2a78d6;
    --pfm-failed: #d03b3b;
    --pfm-skipped: #a3a29c;
    --pfm-clean: #0ca30c;
    --pfm-surface: var(--card-background-color, #fff);
    --pfm-muted: var(--secondary-text-color, #52514e);
    --pfm-divider: var(--divider-color, rgba(0, 0, 0, 0.12));
  }
  :host([dark]) { --pfm-line: #3987e5; }
  .toolbar { display: flex; align-items: center; gap: 4px; height: 56px; padding: 0 12px; border-bottom: 1px solid var(--pfm-divider); background: var(--app-header-background-color, var(--pfm-surface)); color: var(--app-header-text-color, inherit); }
  .toolbar h1 { font-size: 20px; font-weight: 400; margin: 0 0 0 8px; }
  main { padding: 16px; max-width: 1400px; margin: 0 auto; box-sizing: border-box; }
  h2 { font-size: 16px; font-weight: 500; margin: 24px 0 12px; }
  .muted { color: var(--pfm-muted); }
  .error { color: var(--error-color, #d03b3b); }
  .notice { background: var(--pfm-surface); border: 1px solid var(--pfm-divider); border-left: 4px solid var(--warning-color, #fab219); border-radius: 8px; padding: 10px 12px; margin: 0 0 12px; }
  .controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 16px; }
  .field { display: flex; gap: 8px; align-items: center; color: var(--pfm-muted); }
  select, button { font: inherit; color: inherit; }
  select { background: var(--pfm-surface); border: 1px solid var(--pfm-divider); border-radius: 8px; padding: 6px 8px; }
  .segmented { display: inline-flex; max-width: 100%; border: 1px solid var(--pfm-divider); border-radius: 8px; overflow-x: auto; scrollbar-width: none; }
  .segmented button { flex: none; white-space: nowrap; background: var(--pfm-surface); border: 0; border-right: 1px solid var(--pfm-divider); padding: 6px 12px; cursor: pointer; }
  .segmented button:last-child { border-right: 0; }
  .segmented button[aria-pressed="true"] { background: var(--primary-color, #2a78d6); color: var(--text-primary-color, #fff); }
  button.secondary { background: var(--pfm-surface); border: 1px solid var(--pfm-divider); border-radius: 8px; padding: 8px 16px; cursor: pointer; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
  .tile, .card { background: var(--pfm-surface); border-radius: var(--ha-card-border-radius, 12px); border: 1px solid var(--pfm-divider); }
  .tile { padding: 12px 16px; }
  .tile .value { font-size: 28px; font-variant-numeric: tabular-nums; }
  .tile .name { font-weight: 500; }
  .tile .note { color: var(--pfm-muted); font-size: 13px; margin-top: 2px; }
  .charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr)); gap: 12px; margin-top: 12px; }
  .card { margin: 0; padding: 12px 16px; min-width: 0; }
  figcaption { font-weight: 500; margin-bottom: 8px; }
  .chart { width: 100%; min-height: 200px; }
  .chart svg { display: block; }
  .legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 13px; color: var(--pfm-muted); margin-bottom: 4px; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
  .swatch.failed, .seg.failed { background: var(--pfm-failed); fill: var(--pfm-failed); }
  .swatch.skipped, .seg.skipped { background: var(--pfm-skipped); fill: var(--pfm-skipped); }
  .swatch.clean, .seg.clean { background: var(--pfm-clean); fill: var(--pfm-clean); }
  .seg.hover { opacity: 0.8; }
  .grid { stroke: var(--pfm-divider); stroke-width: 1; }
  .axis { fill: var(--pfm-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
  .line { fill: none; stroke: var(--pfm-line); stroke-width: 2; stroke-linejoin: round; }
  .dot-failed { fill: var(--pfm-failed); stroke: var(--pfm-surface); stroke-width: 2; }
  .threshold { stroke: var(--pfm-muted); stroke-dasharray: 4 4; stroke-width: 1; }
  .threshold-label { fill: var(--pfm-muted); font-size: 11px; }
  .crosshair { stroke: var(--pfm-muted); stroke-width: 1; }
  .hover-dot { fill: var(--pfm-line); stroke: var(--pfm-surface); stroke-width: 2; }
  .hit { fill: transparent; }
  details { margin-top: 8px; font-size: 13px; }
  summary { cursor: pointer; color: var(--pfm-muted); }
  table { border-collapse: collapse; width: 100%; margin-top: 8px; font-variant-numeric: tabular-nums; }
  th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--pfm-divider); }
  details > div { max-height: 240px; overflow: auto; }
  .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
  .frame { position: relative; padding: 0; border: 1px solid var(--pfm-divider); border-radius: 12px; overflow: hidden; background: var(--pfm-surface); cursor: pointer; text-align: left; }
  .frame:focus-visible { outline: 2px solid var(--primary-color, #2a78d6); outline-offset: 2px; }
  .frame img, .noimage { display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: cover; background: var(--secondary-background-color, #e5e5e0); }
  .noimage { display: grid; place-items: center; color: var(--pfm-muted); font-size: 13px; }
  .noimage.large { aspect-ratio: 16 / 9; }
  .badge { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; font-weight: 500; padding: 2px 8px 2px 4px; border-radius: 999px; background: var(--pfm-surface); border: 1px solid var(--pfm-divider); }
  .frame .badge { position: absolute; top: 8px; left: 8px; }
  .tag { position: absolute; top: 8px; right: 8px; display: inline-flex; padding: 3px; border-radius: 999px; background: var(--pfm-surface); border: 1px solid var(--pfm-divider); --mdc-icon-size: 16px; }
  .tag.failure ha-icon { color: var(--pfm-failed); }
  .tag.fine ha-icon { color: var(--pfm-clean); }
  .labeling { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; }
  .labeling span { margin-right: auto; }
  .labeling button[aria-pressed="true"] { border-color: var(--primary-color); color: var(--primary-color); font-weight: 500; }
  .tools { display: flex; gap: 8px; margin-left: auto; }
  .controls button.secondary { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; --mdc-icon-size: 18px; }
  button.primary { background: var(--primary-color, #2a78d6); color: var(--text-primary-color, #fff); border: 0; border-radius: 8px; padding: 8px 16px; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  .actions { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 12px; }
  .actions .link { margin-right: auto; }
  .cropper { position: relative; overflow: hidden; border-radius: 8px; touch-action: none; user-select: none; cursor: crosshair; line-height: 0; }
  .dialog .cropper img { border-radius: 0; }
  .crop-dim { position: absolute; inset: 0; background: rgba(0, 0, 0, 0.5); pointer-events: none; }
  .crop-box { position: absolute; box-sizing: border-box; border: 2px solid var(--primary-color, #2a78d6); pointer-events: none; }
  .form { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
  .form label { display: flex; flex-direction: column; gap: 4px; color: var(--pfm-muted); font-size: 13px; }
  .form .wide { grid-column: 1 / -1; }
  .form input, .form textarea { font: inherit; color: var(--primary-text-color, inherit); background: var(--pfm-surface); border: 1px solid var(--pfm-divider); border-radius: 8px; padding: 6px 8px; }
  .form textarea { font-family: var(--code-font-family, monospace); font-size: 13px; }
  .compare { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 16px; }
  .dialog .changes img { width: min(96px, 18vw); border-radius: 4px; }
  .scroll { overflow-x: auto; }
  button.link { background: none; border: 0; color: var(--pfm-muted); cursor: pointer; padding: 8px; text-decoration: underline; }
  .badge ha-icon { --mdc-icon-size: 16px; }
  .badge.failed ha-icon { color: var(--pfm-failed); }
  .badge.skipped ha-icon { color: var(--pfm-muted); }
  .badge.clean ha-icon { color: var(--pfm-clean); }
  .meta { display: flex; justify-content: space-between; gap: 8px; padding: 8px 10px; font-size: 13px; color: var(--pfm-muted); font-variant-numeric: tabular-nums; }
  .more { display: flex; justify-content: center; margin: 16px 0 32px; }
  dialog { border: 0; padding: 0; border-radius: 16px; background: var(--pfm-surface); color: inherit; width: min(960px, calc(100vw - 32px)); max-height: calc(100vh - 32px); }
  dialog::backdrop { background: rgba(0, 0, 0, 0.5); }
  .dialog { padding: 16px; }
  .dialog header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
  .dialog .close { margin-left: auto; background: none; border: 0; font-size: 18px; cursor: pointer; color: var(--pfm-muted); padding: 4px 8px; }
  .dialog img { width: 100%; border-radius: 8px; display: block; }
  .answers th { font-weight: 400; width: 30%; }
  .answers td:last-child { width: 4em; text-align: right; }
  .meter { width: 100%; }
  .meter span { display: block; height: 8px; border-radius: 4px; background: var(--pfm-line); min-width: 2px; }
  .tooltip { position: fixed; z-index: 10; pointer-events: none; background: var(--pfm-surface); border: 1px solid var(--pfm-divider); border-radius: 8px; padding: 8px 10px; font-size: 13px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15); max-width: 260px; }
  @media (max-width: 600px) { main { padding: 12px; } .tile .value { font-size: 22px; } .changes th, .changes td { padding: 4px; } }
`;

customElements.define("print-failure-monitor-panel", PrintFailureMonitorPanel);
