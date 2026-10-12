/* Fukidashi popup. */
(function () {
  "use strict";

  const F = globalThis.Fukidashi;
  const $ = (id) => document.getElementById(id);

  let settings = Object.assign({}, F.DEFAULTS);
  let tab = null;
  let health = null;

  async function getTab() {
    // ?tab=<id> lets the popup be opened as a normal page for testing.
    const id = Number(new URLSearchParams(location.search).get("tab"));
    if (id) return chrome.tabs.get(id);
    const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
    return t || null;
  }

  async function sendToTab(msg) {
    if (!tab) return null;
    try {
      return await chrome.tabs.sendMessage(tab.id, msg, { frameId: 0 });
    } catch (e) {
      return null; // no content script (chrome:// page, or opened before install)
    }
  }

  function setText(el, text, cls) {
    el.textContent = text || "";
    el.className = "detail" + (cls ? " " + cls : "") + (el.classList.contains("mono") ? " mono" : "");
  }

  // ------------------------------------------------------------- rendering

  function fillLanguages() {
    const src = $("source-lang");
    const tgt = $("target-lang");
    for (const [code, name] of F.LANGUAGES) {
      if (F.SOURCE_LANGS.includes(code)) src.add(new Option(name, code));
      tgt.add(new Option(name, code));
    }
  }

  function renderSettings() {
    const host = tab && tab.url ? F.hostOf(tab.url) : "";
    $("site-host").textContent = host || "this site";
    $("auto-site").checked = !!host && settings.autoSites.includes(host);
    $("auto-site").disabled = !host;
    $("auto-all").checked = settings.autoAll;
    $("mode-context").setAttribute("aria-checked", String(settings.mode === "context"));
    $("mode-fast").setAttribute("aria-checked", String(settings.mode === "fast"));
    $("engine").value = settings.engine;
    $("source-lang").value = settings.sourceLang;
    $("target-lang").value = settings.targetLang;
    $("min-natural").value = settings.minNatural;
    $("min-rendered").value = settings.minRendered;
    $("server-url").value = settings.serverUrl;
    $("font").value = settings.font;
    $("series-key").textContent = tab && tab.url ? F.seriesKey(tab.url) : "";
    renderModeHint();
  }

  function renderHealth() {
    const status = $("server-status");
    if (!health) {
      status.className = "status";
      $("server-label").textContent = "Checking…";
      return;
    }
    if (!health.ok) {
      status.className = "status offline";
      $("server-label").textContent = "Offline";
      setText($("server-detail"), health.error, "bad");
      renderEngines(null);
      renderModeHint();
      return;
    }
    const h = health.health;
    status.className = "status online";
    $("server-label").textContent = "Online";
    const bits = [h.gpu ? `GPU: ${h.device}` : "CPU", `inpaint: ${h.inpainter === "lama" ? "LaMa" : "OpenCV"}`];
    if (!h.ocr.easyocr) bits.unshift("EasyOCR missing, can't detect text");
    setText($("server-detail"), bits.join(" · "), h.ocr.easyocr ? "" : "bad");
    renderEngines(h.engines);
    const list = $("font-list");
    list.textContent = "";
    (h.fonts || []).forEach((f) => list.appendChild(new Option(f)));
    renderModeHint();
  }

  function renderEngines(engines) {
    const sel = $("engine");
    const names = { deepl: "DeepL", google: "Google", offline: "Offline (Argos)" };
    for (const opt of sel.options) {
      if (opt.value === "auto") {
        const first = engines && ["deepl", "google", "offline"].find((e) => engines[e]);
        opt.textContent = engines ? `Auto${first ? ` (${names[first]})` : " (none set up)"}` : "Auto";
        continue;
      }
      const ok = !engines || engines[opt.value];
      opt.textContent = names[opt.value] + (ok ? "" : " (not set up)");
      opt.disabled = !ok;
    }
  }

  function renderModeHint() {
    const el = $("mode-hint");
    if (settings.mode === "fast") {
      setText(el, "Each text block is translated on its own. No LLM, no story memory.");
      return;
    }
    if (!health || !health.ok) {
      setText(el, "Translates the whole page at once with a running story summary and name glossary.");
      return;
    }
    const llm = health.health.llm;
    if (llm.configured) setText(el, `Story memory on · ${llm.provider} · ${llm.model}`);
    else setText(el, `LLM not configured (${llm.reason}). Pages will use fast mode until it is set in server/.env.`, "warn");
  }

  async function renderPage() {
    const st = await sendToTab({ type: "getState" });
    for (const id of ["translate-all", "retranslate", "show-originals"]) $(id).disabled = !st;
    if (!st) {
      const web = tab && tab.url && /^(https?|file):/.test(tab.url);
      setText($("page-status"), web ? "Reload this page to use Fukidashi here." : "Fukidashi works on regular web pages.", "warn");
      return;
    }
    $("show-originals").checked = st.showingOriginals;
    const s = st.stats;
    const parts = [];
    if (s.working || s.queued) parts.push(`${s.working + s.queued} in progress`);
    if (s.done) parts.push(`${s.done} translated`);
    if (s.failed) parts.push(`${s.failed} failed`);
    if (s.notext) parts.push(`${s.notext} without text`);
    setText($("page-status"), parts.length ? parts.join(" · ") : st.enabled ? "Auto-translate is on for this page." : "");
  }

  async function refreshHealth() {
    health = null;
    renderHealth();
    health = await chrome.runtime.sendMessage({ type: "health" });
    renderHealth();
  }

  // ------------------------------------------------------------- events

  async function save(patch) {
    Object.assign(settings, patch);
    await F.setSettings(patch);
    renderSettings();
  }

  function bind() {
    $("auto-site").addEventListener("change", (e) => {
      const host = F.hostOf(tab.url);
      const sites = new Set(settings.autoSites);
      if (e.target.checked) sites.add(host);
      else sites.delete(host);
      save({ autoSites: Array.from(sites) }).then(() => setTimeout(renderPage, 300));
    });
    $("auto-all").addEventListener("change", (e) => save({ autoAll: e.target.checked }).then(() => setTimeout(renderPage, 300)));
    $("translate-all").addEventListener("click", async () => {
      await sendToTab({ type: "translateAll" });
      renderPage();
    });
    $("retranslate").addEventListener("click", async () => {
      await sendToTab({ type: "retranslate" });
      renderPage();
    });
    $("show-originals").addEventListener("change", (e) => sendToTab({ type: "showOriginals", value: e.target.checked }));
    for (const btn of document.querySelectorAll(".segmented button")) {
      btn.addEventListener("click", () => save({ mode: btn.dataset.mode }));
    }
    $("engine").addEventListener("change", (e) => save({ engine: e.target.value }));
    $("source-lang").addEventListener("change", (e) => save({ sourceLang: e.target.value }));
    $("target-lang").addEventListener("change", (e) => save({ targetLang: e.target.value }));
    $("min-natural").addEventListener("change", (e) => save({ minNatural: Math.max(0, Number(e.target.value) || 0) }));
    $("min-rendered").addEventListener("change", (e) => save({ minRendered: Math.max(0, Number(e.target.value) || 0) }));
    $("font").addEventListener("change", (e) => save({ font: e.target.value.trim() }));
    $("server-url").addEventListener("change", async (e) => {
      const value = e.target.value.trim() || F.DEFAULTS.serverUrl;
      try {
        new URL(value);
      } catch (err) {
        setText($("server-detail"), "That doesn't look like a URL (e.g. http://127.0.0.1:5003)", "bad");
        return;
      }
      await save({ serverUrl: value });
      refreshHealth();
    });
    $("reset-memory").addEventListener("click", async () => {
      if (!tab || !tab.url) return;
      const r = await chrome.runtime.sendMessage({ type: "resetContext", pageUrl: tab.url });
      const el = $("series-key");
      if (r && r.ok) setText(el, r.existed ? `Cleared story memory for ${r.series}` : `No story memory yet for ${r.series}`);
      else setText(el, (r && r.error) || "Could not reset", "bad");
    });
  }

  async function init() {
    fillLanguages();
    [settings, tab] = await Promise.all([F.getSettings(), getTab()]);
    renderSettings();
    bind();
    refreshHealth();
    renderPage();
    setInterval(renderPage, 1000);
  }

  init();
})();
