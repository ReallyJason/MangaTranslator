/* Small status pill in the bottom-right corner, isolated in a shadow root so
 * page CSS can't touch it (and it can't touch the page). */
(function (root) {
  "use strict";

  const CSS = `
    :host { all: initial; }
    .pill {
      position: fixed; right: 16px; bottom: 16px; z-index: 2147483647;
      display: flex; align-items: center; gap: 8px; max-width: min(420px, calc(100vw - 32px));
      padding: 8px 12px 8px 10px; border-radius: 999px;
      font: 500 12.5px/1.35 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      color: #f5f5f7; background: rgba(28, 28, 34, 0.92);
      box-shadow: 0 4px 18px rgba(0, 0, 0, 0.28); backdrop-filter: blur(6px);
      cursor: pointer; user-select: none;
      opacity: 0; transform: translateY(8px); transition: opacity .18s ease, transform .18s ease;
      pointer-events: none;
    }
    .pill.show { opacity: 1; transform: none; pointer-events: auto; }
    .pill.multiline { border-radius: 14px; }
    .dot { flex: none; width: 9px; height: 9px; border-radius: 50%; background: #8e8e93; }
    .busy .dot { background: transparent; border: 2px solid #ff8a5c; border-right-color: transparent;
                 width: 8px; height: 8px; animation: spin .8s linear infinite; }
    .ok .dot { background: #34c759; }
    .warn .dot { background: #ffcc00; }
    .error .dot { background: #ff453a; }
    .text { overflow-wrap: anywhere; }
    .brand { font-weight: 700; color: #ff8a5c; margin-right: 4px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @media (prefers-reduced-motion: reduce) { .pill { transition: none; } .busy .dot { animation: none; } }
  `;

  let host = null;
  let pill = null;
  let textEl = null;
  let hideTimer = null;

  function ensure() {
    if (pill && host && host.isConnected) return;
    host = document.createElement("fukidashi-status");
    host.style.cssText = "all: initial; position: fixed; z-index: 2147483647;";
    const shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = CSS;
    pill = document.createElement("div");
    pill.className = "pill";
    pill.setAttribute("role", "status");
    pill.setAttribute("aria-live", "polite");
    pill.title = "Fukidashi (click to hide)";
    const dot = document.createElement("span");
    dot.className = "dot";
    const brand = document.createElement("span");
    brand.className = "brand";
    brand.textContent = "Fukidashi";
    textEl = document.createElement("span");
    textEl.className = "text";
    const wrap = document.createElement("span");
    wrap.append(brand, textEl);
    pill.append(dot, wrap);
    pill.addEventListener("click", hide);
    shadow.append(style, pill);
    (document.body || document.documentElement).appendChild(host);
  }

  /**
   * show(text, {kind: "busy"|"ok"|"warn"|"error", timeout: ms | 0 for sticky})
   */
  function show(text, opts) {
    const o = Object.assign({ kind: "busy", timeout: 0 }, opts || {});
    ensure();
    clearTimeout(hideTimer);
    textEl.textContent = text;
    pill.className = "pill show " + o.kind + (text.length > 60 ? " multiline" : "");
    if (o.timeout > 0) hideTimer = setTimeout(hide, o.timeout);
  }

  function hide() {
    clearTimeout(hideTimer);
    if (pill) pill.classList.remove("show");
  }

  function lastText() {
    return pill && pill.classList.contains("show") ? textEl.textContent : "";
  }

  root.FukidashiPill = { show, hide, lastText };
})(globalThis);
