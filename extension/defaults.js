/* Shared defaults and helpers. Loaded by the background worker (importScripts),
 * the content script and the popup, so it must not touch window/document. */
(function (root) {
  "use strict";

  const DEFAULTS = {
    serverUrl: "http://127.0.0.1:5003",
    autoAll: false, // auto-translate on every site
    autoSites: [], // hostnames with auto-translate on
    mode: "context", // "context" (LLM) | "fast" (no LLM)
    engine: "auto", // fast-mode engine: auto | deepl | google | offline
    sourceLang: "ja",
    targetLang: "en",
    minNatural: 200, // skip images smaller than this (natural pixels, both sides)
    minRendered: 120, // ... or smaller than this on screen
    font: "", // font name from server/fonts (empty = server default)
  };

  const LANGUAGES = [
    ["ja", "Japanese"],
    ["ko", "Korean"],
    ["zh", "Chinese (Simplified)"],
    ["zh-TW", "Chinese (Traditional)"],
    ["en", "English"],
    ["es", "Spanish"],
    ["fr", "French"],
    ["de", "German"],
    ["it", "Italian"],
    ["pt", "Portuguese"],
    ["ru", "Russian"],
    ["pl", "Polish"],
    ["uk", "Ukrainian"],
    ["tr", "Turkish"],
    ["vi", "Vietnamese"],
    ["id", "Indonesian"],
    ["th", "Thai"],
  ];

  // Languages EasyOCR can read in manga (source); targets can be anything above.
  const SOURCE_LANGS = ["ja", "ko", "zh", "zh-TW", "en", "es", "fr", "de", "it", "pt", "ru", "vi", "id", "th"];

  async function getSettings() {
    const stored = await chrome.storage.local.get(Object.keys(DEFAULTS));
    return Object.assign({}, DEFAULTS, stored);
  }

  function setSettings(patch) {
    return chrome.storage.local.set(patch);
  }

  function hostOf(url) {
    try {
      return new URL(url).hostname.toLowerCase();
    } catch (e) {
      return "";
    }
  }

  function isAutoOn(settings, url) {
    const host = hostOf(url);
    return !!host && (settings.autoAll || settings.autoSites.includes(host));
  }

  /** Same rule as the server: hostname (minus www.) + path minus the last segment. */
  function seriesKey(url) {
    try {
      const u = new URL(url);
      let host = u.hostname.toLowerCase();
      if (host.startsWith("www.")) host = host.slice(4);
      const segs = u.pathname.split("/").filter(Boolean);
      segs.pop();
      return [host].concat(segs).join("/");
    } catch (e) {
      return "";
    }
  }

  function translateOptions(settings, pageUrl, extra) {
    return Object.assign(
      {
        mode: settings.mode,
        engine: settings.engine,
        source_lang: settings.sourceLang,
        target_lang: settings.targetLang,
        session: pageUrl || "",
        font: settings.font || "",
      },
      extra || {}
    );
  }

  root.Fukidashi = {
    DEFAULTS,
    LANGUAGES,
    SOURCE_LANGS,
    getSettings,
    setSettings,
    hostOf,
    isAutoOn,
    seriesKey,
    translateOptions,
  };
})(typeof globalThis !== "undefined" ? globalThis : self);
