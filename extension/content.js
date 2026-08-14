// content.js
// The on-page HUD: a small capsule that hovers over every page, reports what the
// last scan concluded, and expands on hover to offer manual scans. Also renders
// the full-page block overlay.
//
// Everything lives inside a Shadow DOM. The previous version injected plain
// elements into the page's own DOM, which meant any site with aggressive CSS
// could restyle, hide, or break the guard UI - including the block overlay,
// which must not be defeatable by the page it is covering.

const HOST_ID = "dhv-guard-host";

const STYLES = `
  :host { all: initial; }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  :host {
    /* The register palette, carried onto the page the child is browsing so the
       guard and the parent's dashboard read as one product. */
    --void:   #14120e;
    --shell:  #191612;
    --raised: #221e18;
    --seam:   #322c23;
    --ink:    #ece4d6;
    --ash:    #a99e8b;
    --faint:  #74695a;
    --ember:  #8aa6dc;
    --clear:  #6aa878;
    --watch:  #d0a13f;
    --halt:   #d7614f;

    --mono: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  }

  /* ---------- the capsule ---------- */

  .hud {
    position: fixed;
    z-index: 2147483646;
    display: flex;
    align-items: stretch;
    overflow: hidden;

    background: color-mix(in srgb, var(--shell) 88%, transparent);
    backdrop-filter: blur(14px) saturate(1.2);
    -webkit-backdrop-filter: blur(14px) saturate(1.2);
    border: 1px solid var(--seam);
    border-radius: 2px;
    box-shadow: 0 6px 22px rgba(0, 0, 0, 0.42);

    font-family: var(--sans);
    color: var(--ink);
    cursor: grab;
    user-select: none;
    touch-action: none;

    /* At rest it should be easy to ignore. It wakes on approach. */
    opacity: 0.42;
    transition: opacity 0.22s ease, box-shadow 0.22s ease;
  }
  .hud:hover,
  .hud:focus-within,
  .hud.is-busy,
  .hud.is-alert {
    opacity: 1;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  }
  .hud.is-dragging { cursor: grabbing; opacity: 1; }

  /* The verdict rail: a coloured spine holding the last judgement. */
  .rail {
    width: 3px;
    flex: none;
    background: var(--faint);
    transition: background 0.3s ease;
  }
  .hud[data-verdict="allow"]   .rail { background: var(--clear); }
  .hud[data-verdict="flag"]    .rail { background: var(--watch); }
  .hud[data-verdict="block"]   .rail { background: var(--halt); }
  .hud[data-verdict="scanning"] .rail { background: var(--ember); }
  /* "Unchecked" must not read as a quiet neutral - a parent has to be able to
     tell it apart from "checked and fine" at a glance. */
  .hud[data-verdict="failed"] .rail {
    background: repeating-linear-gradient(
      -45deg, var(--watch), var(--watch) 3px, transparent 3px, transparent 6px
    );
  }

  .body { display: flex; align-items: center; }

  /* ---------- always-visible core ---------- */

  .core {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 0 11px;
    height: 30px;
    flex: none;
  }

  .status {
    font-family: var(--mono);
    font-size: 9.5px;
    font-weight: 500;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--ash);
    white-space: nowrap;
    transition: color 0.25s ease;
  }
  .hud[data-verdict="block"]  .status { color: var(--halt); }
  .hud[data-verdict="flag"]   .status { color: var(--watch); }
  .hud[data-verdict="failed"] .status { color: var(--watch); }

  /* ---------- the reveal ---------- */

  .reveal {
    display: flex;
    align-items: center;
    max-width: 0;
    opacity: 0;
    overflow: hidden;
    transition: max-width 0.3s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.2s ease;
  }
  .hud:hover .reveal,
  .hud:focus-within .reveal,
  .hud.is-alert .reveal {
    max-width: 460px;
    opacity: 1;
  }

  .divider { width: 1px; align-self: stretch; background: var(--seam); flex: none; }

  .detail {
    font-size: 12px;
    line-height: 1.35;
    color: var(--ash);
    padding: 0 12px;
    max-width: 270px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .detail:empty { display: none; }

  .actions { display: flex; align-items: center; gap: 2px; padding: 0 5px; flex: none; }

  button {
    font-family: var(--sans);
    font-size: 11.5px;
    font-weight: 500;
    color: var(--ash);
    background: transparent;
    border: none;
    border-radius: 2px;
    padding: 6px 9px;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.15s ease, color 0.15s ease;
  }
  button:hover:not(:disabled) { background: rgba(255, 255, 255, 0.07); color: var(--ink); }
  button:disabled { opacity: 0.45; cursor: default; }
  button:focus-visible { outline: 2px solid var(--ember); outline-offset: -1px; }
  .close { color: var(--faint); padding: 6px 8px; }
  .close:hover { color: var(--halt); background: rgba(222, 109, 96, 0.12); }

  /* ---------- signature: the scan sweep ---------- */
  /* A hairline of light travelling the capsule while the page is being judged.
     It mirrors what is actually happening: the screen is being photographed. */

  .sweep {
    position: absolute;
    inset: 0;
    pointer-events: none;
    opacity: 0;
  }
  .hud.is-busy .sweep { opacity: 1; }
  .sweep::after {
    content: "";
    position: absolute;
    top: 0;
    bottom: 0;
    width: 34%;
    background: linear-gradient(
      90deg,
      transparent,
      color-mix(in srgb, var(--ember) 26%, transparent),
      transparent
    );
    animation: sweep 1.15s ease-in-out infinite;
  }
  @keyframes sweep {
    from { transform: translateX(-120%); }
    to   { transform: translateX(390%); }
  }

  /* ---------- curtain ---------- */

  .curtain {
    position: fixed;
    inset: 0;
    z-index: 2147483645;
    background: var(--void);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--faint);
  }
  .curtain .pip {
    width: 5px; height: 5px; border-radius: 50%;
    background: var(--ember);
    animation: pulse 1s ease-in-out infinite;
  }
  @keyframes pulse { 0%, 100% { opacity: 0.25; } 50% { opacity: 1; } }

  /* ---------- block overlay ---------- */

  .overlay {
    position: fixed;
    inset: 0;
    z-index: 2147483647;
    background: var(--void);
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: var(--sans);
    padding: 24px;
  }

  .card {
    max-width: 430px;
    width: 100%;
    color: var(--ink);
    text-align: left;
  }

  /* The rating is the hero. A child stopped mid-click should understand the
     judgement at a glance, in the visual language of a content certificate,
     without reading a sentence and decoding it. */
  .cert {
    display: flex;
    align-items: baseline;
    gap: 3px;
    font-family: var(--mono);
    font-weight: 600;
    color: var(--halt);
    line-height: 0.85;
    margin-bottom: 26px;
  }
  .cert .num { font-size: 84px; letter-spacing: -0.04em; }
  .cert .plus { font-size: 34px; }

  .cert-word {
    font-family: var(--mono);
    font-size: 40px;
    font-weight: 600;
    color: var(--halt);
    letter-spacing: -0.02em;
    line-height: 1.05;
    margin-bottom: 26px;
    text-transform: lowercase;
  }

  .rule { height: 1px; background: var(--seam); margin-bottom: 22px; }

  .card .eyebrow {
    font-family: var(--mono);
    font-size: 9.5px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--faint);
    margin-bottom: 14px;
  }
  .card h1 { font-size: 21px; font-weight: 600; margin-bottom: 10px; letter-spacing: -0.01em; }
  .card p { font-size: 13.5px; line-height: 1.65; color: var(--ash); }
  .card p + p { margin-top: 8px; }

  .queried {
    font-family: var(--mono);
    font-size: 12.5px;
    color: var(--ash);
    background: rgba(255, 255, 255, 0.04);
    border-left: 2px solid var(--seam);
    padding: 9px 12px;
    margin-top: 16px;
    border-radius: 0;
    word-break: break-word;
  }

  .card .row { display: flex; gap: 10px; align-items: center; margin-top: 28px; }
  .card button {
    background: var(--ember);
    color: #10151f;
    font-size: 13px;
    font-weight: 600;
    padding: 11px 24px;
    border-radius: 2px;
  }
  .card button:hover { filter: brightness(1.1); }
  .card .aside { font-size: 12px; color: var(--faint); }

  @media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
  }
`;

/* ---------- shadow host ---------- */

let shadow = null;
let hud = null;
let statusEl = null;
let detailEl = null;
let settleTimer = null;

function getShadow() {
  if (shadow) return shadow;
  const host = document.createElement("div");
  host.id = HOST_ID;
  // The host itself must not be affected by page layout.
  host.style.cssText = "all: initial; position: static;";
  shadow = host.attachShadow({ mode: "open" });

  const style = document.createElement("style");
  style.textContent = STYLES;
  shadow.appendChild(style);

  document.documentElement.appendChild(host);
  return shadow;
}

/* ---------- HUD ---------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  // textContent, never innerHTML: everything shown here is model output or a
  // page-supplied string.
  if (text !== undefined) node.textContent = text;
  return node;
}

async function buildHud() {
  const { toolbarHidden } = await chrome.storage.local.get(["toolbarHidden"]);
  if (toolbarHidden) return;

  const root = getShadow();
  if (root.querySelector(".hud")) return;

  hud = el("div", "hud");
  hud.setAttribute("data-verdict", "idle");
  hud.tabIndex = 0;

  hud.appendChild(el("div", "sweep"));
  hud.appendChild(el("div", "rail"));

  const body = el("div", "body");

  const core = el("div", "core");
  statusEl = el("span", "status", "Watching");
  core.appendChild(statusEl);
  body.appendChild(core);

  const reveal = el("div", "reveal");
  detailEl = el("span", "detail");
  reveal.appendChild(detailEl);
  reveal.appendChild(el("div", "divider"));

  const actions = el("div", "actions");
  const scanBtn = el("button", null, "Scan page");
  const selBtn = el("button", null, "Selection");
  const closeBtn = el("button", "close", "×");
  closeBtn.title = "Hide until next reload";
  actions.append(scanBtn, selBtn, closeBtn);
  reveal.appendChild(actions);

  body.appendChild(reveal);
  hud.appendChild(body);
  root.appendChild(hud);

  scanBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    setState("scanning", "Checking", "");
    chrome.runtime.sendMessage({ action: "scanNow" });
  });

  selBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const text = window.getSelection().toString().trim();
    if (!text) {
      setState("idle", "Watching", "Select some text on the page first.");
      return;
    }
    setState("scanning", "Checking", "");
    chrome.runtime.sendMessage({ action: "scanSelection", text });
  });

  closeBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    hud.remove();
    chrome.storage.local.set({ toolbarHidden: true });
  });

  await restorePosition();
  makeDraggable();
}

/* ---------- position: edge-snapped and remembered ---------- */

const EDGE_GAP = 14;

async function restorePosition() {
  const { hudPosition } = await chrome.storage.local.get(["hudPosition"]);
  const pos = hudPosition || { side: "right", topRatio: 0.82 };
  applyPosition(pos);
}

function applyPosition(pos) {
  const height = hud.offsetHeight || 32;
  const maxTop = Math.max(EDGE_GAP, window.innerHeight - height - EDGE_GAP);
  const top = Math.min(maxTop, Math.max(EDGE_GAP, pos.topRatio * window.innerHeight));

  hud.style.top = `${top}px`;
  if (pos.side === "left") {
    hud.style.left = `${EDGE_GAP}px`;
    hud.style.right = "auto";
  } else {
    hud.style.right = `${EDGE_GAP}px`;
    hud.style.left = "auto";
  }
}

function makeDraggable() {
  let startX = 0, startY = 0, originLeft = 0, originTop = 0, dragging = false;

  hud.addEventListener("pointerdown", (e) => {
    // Buttons keep their own behaviour.
    if (e.target.closest("button")) return;
    dragging = true;
    hud.classList.add("is-dragging");
    hud.setPointerCapture(e.pointerId);

    const rect = hud.getBoundingClientRect();
    originLeft = rect.left;
    originTop = rect.top;
    startX = e.clientX;
    startY = e.clientY;

    // Switch to left-anchored while dragging so movement maps 1:1.
    hud.style.left = `${originLeft}px`;
    hud.style.right = "auto";
  });

  hud.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    hud.style.left = `${originLeft + (e.clientX - startX)}px`;
    hud.style.top = `${originTop + (e.clientY - startY)}px`;
  });

  const finish = (e) => {
    if (!dragging) return;
    dragging = false;
    hud.classList.remove("is-dragging");

    // Snap to whichever vertical edge is nearer, the way a persistent overlay
    // should behave - it never ends up stranded mid-screen over content.
    const rect = hud.getBoundingClientRect();
    const side = rect.left + rect.width / 2 < window.innerWidth / 2 ? "left" : "right";
    const topRatio = rect.top / window.innerHeight;

    const pos = { side, topRatio };
    applyPosition(pos);
    chrome.storage.local.set({ hudPosition: pos });
    if (e && e.pointerId !== undefined && hud.hasPointerCapture(e.pointerId)) {
      hud.releasePointerCapture(e.pointerId);
    }
  };

  hud.addEventListener("pointerup", finish);
  hud.addEventListener("pointercancel", finish);

  window.addEventListener("resize", async () => {
    const { hudPosition } = await chrome.storage.local.get(["hudPosition"]);
    if (hud && hud.isConnected) applyPosition(hudPosition || { side: "right", topRatio: 0.82 });
  });
}

/* ---------- state ---------- */

function setState(verdict, status, detail) {
  if (!hud || !hud.isConnected) return;
  clearTimeout(settleTimer);

  hud.setAttribute("data-verdict", verdict);
  hud.classList.toggle("is-busy", verdict === "scanning");
  // A block holds itself open; everything else stays collapsed unless hovered.
  hud.classList.toggle("is-alert", verdict === "block" || verdict === "flag");

  statusEl.textContent = status;
  detailEl.textContent = detail || "";

  if (verdict === "flag" || verdict === "block") {
    settleTimer = setTimeout(() => hud.classList.remove("is-alert"), 6000);
  }
}

function showVerdict(decision, result, isAuto) {
  if (decision.blocked) {
    setState("block", "Blocked", decision.reason || result.reason || "This page was blocked.");
    return;
  }

  const flagged = result.moderation_decision === "flag";
  if (flagged) {
    setState("flag", "Flagged", result.reason || "Worth a look.");
    return;
  }

  setState("allow", "Safe", isAuto ? "" : (result.reason || result.description || "Nothing concerning found."));
}

/* ---------- block overlay ---------- */

function showBlockOverlay(reason, meta) {
  const root = getShadow();
  if (root.querySelector(".overlay")) return;

  // Freeze the page underneath so scrolling cannot reveal content past the overlay.
  document.documentElement.style.overflow = "hidden";

  meta = meta || {};
  const overlay = el("div", "overlay");
  const card = el("div", "card");

  // Lead with the judgement itself, in the language of a content rating.
  if (meta.minAge) {
    const cert = el("div", "cert");
    cert.appendChild(el("span", "num", String(meta.minAge)));
    cert.appendChild(el("span", "plus", "+"));
    card.appendChild(cert);
  } else if (meta.category) {
    card.appendChild(el("div", "cert-word", meta.category.replace(/_/g, " ")));
  } else {
    card.appendChild(el("div", "cert-word", "blocked"));
  }

  card.appendChild(el("div", "rule"));
  card.appendChild(el("div", "eyebrow", meta.query ? "Search stopped" : "Page stopped"));

  if (meta.minAge && meta.childAge) {
    card.appendChild(el("h1", null, `Meant for ages ${meta.minAge} and up`));
    card.appendChild(el("p", null, `This device is set up for age ${meta.childAge}.`));
  } else if (meta.category) {
    card.appendChild(el("h1", null, `This is about ${meta.category.replace(/_/g, " ")}`));
    card.appendChild(el("p", null, "A parent chose to block this topic on this device."));
  } else {
    card.appendChild(el("h1", null, "This page is blocked"));
    card.appendChild(el("p", null, reason || "This content isn't right for the age set on this device."));
  }

  if (meta.query) {
    card.appendChild(el("div", "queried", meta.query));
  }

  card.appendChild(el("p", null, "If you think this is wrong, ask whoever set up this device."));

  const row = el("div", "row");
  const back = el("button", null, "Go back");
  back.addEventListener("click", () => {
    // A page opened in a fresh tab has nothing to go back to, so history.back()
    // would do nothing and leave a dead button on top of a blocked page.
    if (window.history.length > 1) {
      history.back();
    } else {
      window.location.replace("about:blank");
    }
  });
  row.appendChild(back);
  card.appendChild(row);

  overlay.appendChild(card);
  root.appendChild(overlay);
  back.focus();
}

/* ---------- curtain over search results while the query is judged ---------- */

// Results render in a few hundred milliseconds; the verdict takes about a second.
// Without this, the child sees the results for that gap - which is precisely the
// window that matters for the searches worth blocking at all. So search pages are
// held behind a curtain until the verdict lands.
//
// Only search pages, and only briefly: veiling every page would make the whole
// browser feel broken, and the timeout below guarantees a page can never stay
// hidden if the backend is slow or down.
const CURTAIN_MAX_MS = 2500;

const SEARCH_HOSTS = [
  [/(^|\.)google\.[a-z.]+$/i, "q"],
  [/(^|\.)bing\.com$/i, "q"],
  [/(^|\.)duckduckgo\.com$/i, "q"],
  [/(^|\.)ecosia\.org$/i, "q"],
  [/(^|\.)search\.brave\.com$/i, "q"],
  [/(^|\.)search\.yahoo\.[a-z.]+$/i, "p"],
  [/(^|\.)youtube\.com$/i, "search_query"],
];

let curtainTimer = null;

function isSearchPage() {
  try {
    const u = new URL(location.href);
    return SEARCH_HOSTS.some(([host, param]) => host.test(u.hostname) && (u.searchParams.get(param) || "").trim());
  } catch {
    return false;
  }
}

function raiseCurtain() {
  const root = getShadow();
  if (root.querySelector(".curtain")) return;
  const curtain = el("div", "curtain");
  curtain.appendChild(el("span", "pip"));
  curtain.appendChild(el("span", null, "Checking this search"));
  root.appendChild(curtain);
  curtainTimer = setTimeout(dropCurtain, CURTAIN_MAX_MS);
}

function dropCurtain() {
  clearTimeout(curtainTimer);
  const root = getShadow();
  const curtain = root.querySelector(".curtain");
  if (curtain) curtain.remove();
}

if (isSearchPage()) raiseCurtain();

/* ---------- reading the page as text ---------- */

// The cheap path. A screenshot costs about 1,760 input tokens and a vision call;
// the same page as text is roughly 450 and a much faster text call. The content
// script is already inside the page, so this needs no scraping service and no
// network request at all - the DOM is right here.
function extractPageText() {
  const parts = [document.title || ""];

  const meta = document.querySelector('meta[name="description"]');
  if (meta && meta.content) parts.push(meta.content);

  const ogTitle = document.querySelector('meta[property="og:title"]');
  if (ogTitle && ogTitle.content) parts.push(ogTitle.content);

  // innerText rather than textContent: it reflects what is actually visible,
  // skipping script, style and hidden elements.
  const body = document.body ? document.body.innerText : "";
  parts.push(body);

  return parts.join("\n").replace(/\s+/g, " ").trim().slice(0, 3000);
}

/* ---------- messages from background.js ---------- */

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "getPageText") {
    sendResponse({ text: extractPageText(), url: location.href });
    return false;
  }

  if (message.action === "block") {
    // The block overlay replaces the curtain, so the results underneath are never
    // shown even for a frame.
    dropCurtain();
    showBlockOverlay(message.reason, message.meta);
    setState("block", "Blocked", message.reason || "");
  } else if (message.action === "scanResult") {
    dropCurtain();
    showVerdict(message.decision || {}, message.result || {}, message.auto);
  } else if (message.action === "scanStarted") {
    setState("scanning", "Checking", "");
  } else if (message.action === "scanFailed") {
    // Deliberately NOT "safe". This page was never actually checked.
    dropCurtain();
    setState("failed", "Not checked", message.reason || "Couldn't check this page.");
  }
  return false;
});

/* ---------- boot ---------- */

buildHud();

// Ask background.js immediately whether this exact page was already blocked.
// This is what stops a reload briefly showing blocked content while a fresh scan
// is still running.
chrome.runtime.sendMessage({ action: "checkBlocked", url: location.href }, (response) => {
  if (chrome.runtime.lastError) return;
  if (response && response.blocked) showBlockOverlay(response.reason);
});

// If this page IS the Dhvanyartha dashboard, sync whichever Google account is
// signed in there into the extension's storage. This is what lets extension scans
// show up under the right parent in the dashboard.
// The dashboard posts a message when settings are saved, so cached verdicts made
// under the old age and category list can be thrown away immediately.
window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const data = event.data;
  if (data && data.source === "dhvanyartha-dashboard" && data.action === "settingsChanged") {
    chrome.runtime.sendMessage({ action: "settingsChanged" }).catch(() => {});
  }
});

if ((location.hostname === "localhost" || location.hostname === "127.0.0.1") && location.port === "5500") {
  try {
    const savedUser = JSON.parse(localStorage.getItem("dhv_user") || "null");
    chrome.runtime.sendMessage({ action: "linkParent", email: savedUser ? savedUser.email : null });
  } catch (err) {
    console.error("[Dhvanyartha Guard] failed to read signed-in user from web app:", err.message);
  }
}
