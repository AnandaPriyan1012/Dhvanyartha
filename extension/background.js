// background.js
// Runs invisibly. Watches for page loads, screenshots the tab, sends it
// to the backend for analysis, and decides whether to block it for the child.
// Also handles on-demand scans triggered by the toolbar (page or selection),
// and remembers already-blocked URLs so a reload can't bypass the block.

const API_BASE = "http://localhost:8000";

/* ---------- blocked-URL memory (survives reloads instantly, no re-scan needed) ---------- */

let blockedUrls = {};
const blockedUrlsReady = chrome.storage.local.get(["blockedUrls"]).then((data) => {
  // Merge, don't replace. A scan can finish and call rememberBlocked() before this
  // load resolves — a plain assignment would silently discard that fresh block,
  // letting a just-blocked page come straight back on reload.
  blockedUrls = { ...(data.blockedUrls || {}), ...blockedUrls };
});

// Pages the extension must never try to screenshot: browser-internal pages cannot
// be captured at all (the call throws), and the parent dashboard is our own UI.
const NEVER_SCAN = [
  /^chrome:\/\//i,
  /^chrome-extension:\/\//i,
  /^edge:\/\//i,
  /^about:/i,
  /^devtools:\/\//i,
  /^https?:\/\/(localhost|127\.0\.0\.1)(:|\/)/i,
];

function shouldNeverScan(url) {
  return !url || NEVER_SCAN.some((pattern) => pattern.test(url));
}

function normalizeUrl(url) {
  try {
    const u = new URL(url);
    return u.origin + u.pathname + u.search; // ignore #hash only
  } catch {
    return url;
  }
}

/* ---------- live settings from the backend (source of truth is the web app's Dashboard) ---------- */

// Settings are re-fetched at most this often. Every scan used to make its own
// round trip to the backend before it could even start, which added latency to
// every single page load for data that changes maybe twice a month.
const SETTINGS_TTL_MS = 30_000;
let settingsCache = { value: null, fetchedAt: 0 };

function invalidateSettingsCache() {
  settingsCache = { value: null, fetchedAt: 0 };
}

async function getEffectiveSettings(force = false) {
  const fresh = Date.now() - settingsCache.fetchedAt < SETTINGS_TTL_MS;
  if (!force && fresh && settingsCache.value) return settingsCache.value;

  const local = await chrome.storage.local.get(["parentEmail"]);

  if (!local.parentEmail) {
    // No parent linked yet. Protection stays ON: this is a child-safety tool, so
    // an unconfigured state must fail safe rather than fail open.
    console.log("[Dhvanyartha Guard] no parent linked yet, using safe defaults");
    const fallback = { childAge: 18, blockedCategories: [], guardEnabled: true, parentEmail: null };
    settingsCache = { value: fallback, fetchedAt: Date.now() };
    return fallback;
  }

  try {
    const res = await fetch(`${API_BASE}/settings?user_email=${encodeURIComponent(local.parentEmail)}`);
    const remote = await res.json();
    const value = {
      childAge: remote.child_age,
      blockedCategories: remote.blocked_categories || [],
      // Deliberately NOT combined with any local flag. Protection is the parent's
      // decision, made in the dashboard; nothing on the child's device may
      // override it. See the note in the popup.
      guardEnabled: remote.guard_enabled !== false,
      parentEmail: local.parentEmail
    };
    settingsCache = { value, fetchedAt: Date.now() };
    return value;
  } catch (err) {
    console.error("[Dhvanyartha Guard] could not reach the backend for settings:", err.message);
    // Never fall open on a network error. Keep the last known settings, or the
    // strictest sensible defaults if we have never had any.
    const value = settingsCache.value || { childAge: 18, blockedCategories: [], guardEnabled: true, parentEmail: local.parentEmail };
    return value;
  }
}

async function rememberBlocked(url, reason) {
  await blockedUrlsReady;
  blockedUrls[normalizeUrl(url)] = { reason, timestamp: Date.now() };
  chrome.storage.local.set({ blockedUrls });
}

/* ---------- image downscaling (big speed win — screenshots are often huge) ---------- */

async function shrinkImage(blob, maxWidth = 800) {
  try {
    const bitmap = await createImageBitmap(blob);
    const scale = Math.min(1, maxWidth / bitmap.width);
    const w = Math.round(bitmap.width * scale);
    const h = Math.round(bitmap.height * scale);
    const canvas = new OffscreenCanvas(w, h);
    const ctx = canvas.getContext("2d");
    ctx.drawImage(bitmap, 0, 0, w, h);
    return await canvas.convertToBlob({ type: "image/jpeg", quality: 0.5 });
  } catch (err) {
    console.error("[Dhvanyartha Guard] image resize failed, using original:", err.message);
    return blob;
  }
}

/* ---------- page load hook ---------- */

/* ---------- search queries: the fastest and most useful block ---------- */

// Where the search term lives in each engine's URL.
const SEARCH_ENGINES = [
  { host: /(^|\.)google\.[a-z.]+$/i, param: "q" },
  { host: /(^|\.)bing\.com$/i, param: "q" },
  { host: /(^|\.)duckduckgo\.com$/i, param: "q" },
  { host: /(^|\.)ecosia\.org$/i, param: "q" },
  { host: /(^|\.)search\.brave\.com$/i, param: "q" },
  { host: /(^|\.)search\.yahoo\.[a-z.]+$/i, param: "p" },
  { host: /(^|\.)youtube\.com$/i, param: "search_query" },
];

function extractSearchQuery(url) {
  try {
    const u = new URL(url);
    for (const engine of SEARCH_ENGINES) {
      if (engine.host.test(u.hostname)) {
        const q = (u.searchParams.get(engine.param) || "").trim();
        if (q) return q;
      }
    }
  } catch {
    // not a parseable URL
  }
  return null;
}

const lastQueryScanned = {}; // tabId -> query, so re-fires of the same URL cost nothing

async function scanSearchQuery(tabId, query, url) {
  try {
    if (lastQueryScanned[tabId] === query) return;
    lastQueryScanned[tabId] = query;

    const settings = await getEffectiveSettings();
    if (settings.guardEnabled === false) return;

    console.log("[Dhvanyartha Guard] judging search query:", query);
    chrome.tabs.sendMessage(tabId, { action: "scanStarted" }).catch(() => {});

    const res = await fetch(`${API_BASE}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: query, user_email: settings.parentEmail || null })
    });

    // A failed scan is NOT a safe page. Without this check the error body was
    // handed straight to evaluateForChild(), which found no min_age and no
    // categories in it and duly returned "not blocked" - so a backend that was
    // completely unable to reach the model still told the parent SAFE. Observed
    // live: "how to make a bomb" reported safe while the API was returning 502.
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      console.error("[Dhvanyartha Guard] search scan FAILED", res.status, detail.slice(0, 200));
      reportScanFailure(tabId, res.status);
      return;
    }

    const result = await res.json();

    const decision = evaluateForChild(result, settings);
    console.log("[Dhvanyartha Guard] search decision:", decision);

    if (decision.blocked) {
      sendBlockMessage(tabId, decision.reason, blockMeta(result, settings, query));
      rememberBlocked(url, decision.reason);
    }

    chrome.tabs.sendMessage(tabId, { action: "scanResult", result, decision, auto: true }).catch(() => {});
    logScan({ ...result, description: `Search: ${query}` }, decision);
  } catch (err) {
    console.error("[Dhvanyartha Guard] search scan failed:", err);
  }
}

/* ---------- page load hook ---------- */

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  // A search term is judged the instant the URL appears - long before the page
  // renders. Analyzing the query as text takes a fraction of the time a
  // screenshot plus vision call does, so this is the fastest block available and
  // it catches intent rather than waiting to see what the results looked like.
  if (changeInfo.url) {
    const query = extractSearchQuery(changeInfo.url);
    if (query) scanSearchQuery(tabId, query, changeInfo.url);
  }

  if (changeInfo.status === "complete" && tab.active && tab.url && tab.url.startsWith("http")) {
    console.log("[Dhvanyartha Guard] page loaded, scheduling auto-scan:", tab.url);
    // A single pass, at 900ms - late enough for the text to have rendered, early
    // enough to catch the page quickly. There used to be a second unconditional
    // pass at 2600ms, which doubled the cost of every page in exchange for very
    // little: the text path already reads the DOM as it stands when it runs, and
    // a genuinely new page navigation fires this listener again anyway.
    setTimeout(() => scanTab(tabId, false), 900);
  }
});

// Structured detail for the block screen, so it can show the age rating as a
// rating rather than restating a sentence the child has to parse.
function blockMeta(result, settings, query) {
  return {
    minAge: result.min_age || null,
    childAge: settings.childAge || null,
    category: (result.categories || [])
      .map(c => String(c).trim().toLowerCase())
      .find(c => (settings.blockedCategories || []).map(x => String(x).trim().toLowerCase()).includes(c)) || null,
    query: query || null
  };
}

async function sendBlockMessage(tabId, reason, meta, attempt = 1) {
  try {
    await chrome.tabs.sendMessage(tabId, { action: "block", reason, meta });
    console.log("[Dhvanyartha Guard] block message delivered to tab", tabId);
  } catch (err) {
    console.error(`[Dhvanyartha Guard] block message failed (attempt ${attempt}):`, err.message);
    if (attempt < 3) {
      setTimeout(() => sendBlockMessage(tabId, reason, meta, attempt + 1), 600);
    } else {
      console.error("[Dhvanyartha Guard] giving up on blocking this tab after 3 attempts");
    }
  }
}

/* ---------- verdict cache: the difference between 300 calls a day and 40 ---------- */

// A page that was judged safe an hour ago is still safe now, and a child revisits
// the same handful of sites constantly. Without this, every visit to the same
// YouTube page, every reload, and every scroll-triggered rescan paid for a fresh
// vision call.
//
// Only "allow" verdicts live here. Blocks are kept separately in blockedUrls,
// which is consulted before any scan runs and never expires on its own.
const VERDICT_TTL_MS = 6 * 60 * 60 * 1000; // 6 hours
const VERDICT_CACHE_MAX = 500;

let verdictCache = {};
const verdictCacheReady = chrome.storage.local.get(["verdictCache"]).then((data) => {
  verdictCache = { ...(data.verdictCache || {}), ...verdictCache };
});

// The cache must not outlive the settings it was judged under: raising a child's
// age or ticking a new category has to re-open every previous decision.
function settingsFingerprint(settings) {
  return [
    settings.childAge || 0,
    (settings.blockedCategories || []).slice().sort().join(","),
  ].join("|");
}

async function getCachedVerdict(url, settings) {
  await verdictCacheReady;
  const entry = verdictCache[normalizeUrl(url)];
  if (!entry) return null;
  if (Date.now() - entry.at > VERDICT_TTL_MS) return null;
  if (entry.fp !== settingsFingerprint(settings)) return null;
  return entry;
}

async function cacheVerdict(url, settings, result) {
  await verdictCacheReady;

  const keys = Object.keys(verdictCache);
  if (keys.length >= VERDICT_CACHE_MAX) {
    // Drop the oldest quarter rather than growing without bound.
    keys.sort((a, b) => verdictCache[a].at - verdictCache[b].at)
      .slice(0, Math.floor(VERDICT_CACHE_MAX / 4))
      .forEach(k => delete verdictCache[k]);
  }

  verdictCache[normalizeUrl(url)] = {
    at: Date.now(),
    fp: settingsFingerprint(settings),
    selfHarmSignal: !!result.self_harm_signal,
  };
  chrome.storage.local.set({ verdictCache });
}

function clearVerdictCache() {
  verdictCache = {};
  chrome.storage.local.set({ verdictCache: {} });
}

const lastScanAt = {}; // tabId -> timestamp, prevents redundant back-to-back scans

// Tab ids are never reused, so without this the map grows for the whole life of
// the service worker.
chrome.tabs.onRemoved.addListener((tabId) => {
  delete lastScanAt[tabId];
  delete lastQueryScanned[tabId];
});

// The dashboard writes settings to the backend, not to extension storage, so the
// extension has to be told when they change or it would keep serving decisions
// made under the old age and category list until the 30s settings cache expired
// and every cached verdict aged out.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.parentEmail) {
    invalidateSettingsCache();
    clearVerdictCache();
  }
});

// Below this many characters a page is mostly pictures or video, and its text
// says nothing useful about what is on screen.
const MIN_TEXT_FOR_VERDICT = 220;

// Sites where the words on the page are a poor guide to what is actually being
// shown, so a screenshot is worth its cost even when text was available.
const VISUAL_SITES = /(^|\.)(youtube\.com|youtu\.be|instagram\.com|tiktok\.com|reddit\.com|pinterest\.|imgur\.com|x\.com|twitter\.com)$/i;

function needsVisualCheck(result, url) {
  try {
    if (VISUAL_SITES.test(new URL(url).hostname)) return true;
  } catch {
    // unparseable URL, fall through
  }
  // A text verdict that is close to the line deserves a proper look.
  if (result && typeof result.confidence === "number" && result.confidence < 0.55) return true;
  return false;
}

async function readPageText(tabId) {
  try {
    const res = await chrome.tabs.sendMessage(tabId, { action: "getPageText" });
    return res && res.text ? res.text : null;
  } catch {
    // No content script on this page (or it is still loading).
    return null;
  }
}

async function scanTab(tabId, isManual, bypassDedup = false) {
  try {
    const now = Date.now();
    if (!isManual && !bypassDedup && lastScanAt[tabId] && now - lastScanAt[tabId] < 1500) {
      console.log("[Dhvanyartha Guard] skipping duplicate scan on tab", tabId);
      return;
    }
    lastScanAt[tabId] = now;

    console.log("[Dhvanyartha Guard] scanning tab", tabId, "manual:", isManual);
    const settings = await getEffectiveSettings();
    if (settings.guardEnabled === false) {
      console.log("[Dhvanyartha Guard] guard is turned off, skipping scan");
      return;
    }

    const tab = await chrome.tabs.get(tabId);

    if (shouldNeverScan(tab.url)) {
      console.log("[Dhvanyartha Guard] skipping page we never scan:", tab.url);
      return;
    }

    // captureVisibleTab() photographs whichever tab is ACTIVE in the window — it
    // cannot be pointed at a specific tab id. Auto-scans run on a delay (700ms and
    // 2800ms after load), so by the time we get here the child may have switched
    // tabs. Capturing anyway would judge THIS tab using ANOTHER tab's content,
    // which both blocks innocent pages and lets flagged ones through.
    if (!tab.active) {
      console.log("[Dhvanyartha Guard] tab", tabId, "is no longer the visible tab, skipping scan");
      return;
    }

    // Already judged safe, under these same settings, recently enough.
    const cached = await getCachedVerdict(tab.url, settings);
    if (cached && !isManual) {
      console.log("[Dhvanyartha Guard] cached allow, no model call:", tab.url);
      return;
    }

    // Let the HUD start its scan animation, so the user can see that something is
    // happening during the second or two the model takes to answer.
    chrome.tabs.sendMessage(tabId, { action: "scanStarted" }).catch(() => {});

    // Try the page as TEXT first. It costs roughly a quarter of the tokens a
    // screenshot does, runs on the faster text model, and is enough to judge the
    // large majority of pages. The screenshot is kept for the cases text cannot
    // answer: image and video pages, where the words on screen say nothing about
    // what is actually being shown.
    let result = null;
    const pageText = await readPageText(tabId);

    if (pageText && pageText.length >= MIN_TEXT_FOR_VERDICT) {
      const res = await fetch(`${API_BASE}/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: pageText, user_email: settings.parentEmail || null })
      });
      if (res.ok) {
        result = await res.json();
        console.log("[Dhvanyartha Guard] text scan result:", result);
      }
    }

    if (!result || needsVisualCheck(result, tab.url)) {
      console.log("[Dhvanyartha Guard] falling back to a screenshot for", tab.url);
      const screenshotUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 55 });
      const rawBlob = await (await fetch(screenshotUrl)).blob();
      const blob = await shrinkImage(rawBlob);

      const formData = new FormData();
      formData.append("file", blob, "screenshot.jpg");
      if (settings.parentEmail) formData.append("user_email", settings.parentEmail);

      const res = await fetch(`${API_BASE}/analyze-image`, { method: "POST", body: formData });
      if (!res.ok) {
        const detail = await res.text().catch(() => "");
        console.error("[Dhvanyartha Guard] screenshot scan FAILED", res.status, detail.slice(0, 200));
        reportScanFailure(tabId, res.status);
        return;
      }
      result = await res.json();
      console.log("[Dhvanyartha Guard] screenshot scan result:", result);
    }

    // Belt and braces: a reply that carries none of the fields the decision is
    // made from cannot be treated as a pass.
    if (!result || (result.min_age === undefined && result.moderation_decision === undefined)) {
      console.error("[Dhvanyartha Guard] scan returned no usable verdict, not reporting safe");
      reportScanFailure(tabId, 0);
      return;
    }

    const decision = evaluateForChild(result, settings);
    console.log("[Dhvanyartha Guard] decision:", decision);

    if (decision.blocked) {
      sendBlockMessage(tabId, decision.reason, blockMeta(result, settings, null));
      if (tab && tab.url) rememberBlocked(tab.url, decision.reason);
    } else if (tab && tab.url) {
      // Remember the pass so revisits, reloads and scroll-triggered rescans of
      // this page cost nothing at all.
      cacheVerdict(tab.url, settings, result);
    }

    // Every scan gets a toast — "safe" when allowed, the reason when blocked —
    // whether it was triggered automatically on page load or manually via the toolbar.
    chrome.tabs.sendMessage(tabId, { action: "scanResult", result, decision, auto: !isManual })
      .catch(err => console.error("[Dhvanyartha Guard] toast message failed:", err.message));

    logScan(result, decision);
  } catch (err) {
    console.error("[Dhvanyartha Guard] scan failed:", err);
  }
}

async function scanSelection(tabId, text) {
  try {
    const settings = await getEffectiveSettings();

    const res = await fetch(`${API_BASE}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, user_email: settings.parentEmail || null })
    });
    const result = await res.json();
    console.log("[Dhvanyartha Guard] selection scan result:", result);

    const decision = evaluateForChild(result, settings);
    chrome.tabs.sendMessage(tabId, { action: "scanResult", result, decision })
      .catch(err => console.error("[Dhvanyartha Guard] selection toast message failed:", err.message));

    logScan({ ...result, description: text.slice(0, 80) }, decision);
  } catch (err) {
    console.error("[Dhvanyartha Guard] selection scan failed:", err);
  }
}

// A scan that could not complete must never look like a pass. The HUD shows an
// unmistakable "can't check" state instead of "safe", so a parent can tell the
// difference between "this page was checked and is fine" and "this page was never
// checked at all".
function reportScanFailure(tabId, status) {
  chrome.tabs.sendMessage(tabId, {
    action: "scanFailed",
    status,
    reason: status === 429
      ? "Too many scans just now."
      : "Couldn't check this page."
  }).catch(() => {});
}


function evaluateForChild(result, settings) {
  const childAge = settings.childAge || 18;
  const blockedCategories = (settings.blockedCategories || []).map(c => String(c).trim().toLowerCase());
  const selfHarmSignal = !!result.self_harm_signal;
  const categories = (result.categories || []).map(c => String(c).trim().toLowerCase());
  const educational = result.educational === true;

  // Self-harm splits in two, and the two need opposite responses.
  //
  // A child typing "i want to kill myself" is in crisis. The page they are being
  // sent to is the one showing helplines, so blocking it would cut them off from
  // help at the moment they reached for it, and teach them that asking gets them
  // caught. That case is never blocked - it goes to the parent instead, urgently.
  //
  // Content that supplies methods, encouragement, or a community pushing people
  // toward self-harm is the opposite: it is the single most dangerous thing this
  // tool can encounter, and it is blocked regardless of age or category settings.
  const selfHarmKind = String(result.self_harm_kind || "none").toLowerCase();

  // Crisis is checked FIRST, ahead of the category test below, and that ordering
  // is load-bearing. The model tags a crisis search with the "self_harm" category
  // too, so checking categories first blocked the child anyway and threw away the
  // whole distinction. Measured: "i feel so alone and worthless" was blocked by
  // the category even after the crisis correction had been applied.
  if (selfHarmKind === "crisis") {
    console.log("[Dhvanyartha Guard] self-harm CRISIS - deliberately NOT blocking, alerting the parent");
    return { blocked: false, reason: null, selfHarmSignal: true };
  }

  if (selfHarmKind === "promotion" || categories.includes("self_harm") || categories.includes("eating_disorder")) {
    console.log("[Dhvanyartha Guard] self-harm PROMOTION - blocking, and alerting the parent");
    return {
      blocked: true,
      reason: "This page encourages self-harm.",
      selfHarmSignal: true
    };
  }

  if (selfHarmSignal) {
    console.log("[Dhvanyartha Guard] self-harm signal, kind unclear - not blocking, alerting the parent");
    return { blocked: false, reason: null, selfHarmSignal: true };
  }

  // Categories that are never acceptable for a minor, whatever the parent has
  // ticked and whatever age is configured. These are not matters of taste.
  const ALWAYS_BLOCK = ["sexual_content", "predatory_contact", "extremism", "dangerous_challenges"];
  const alwaysHit = categories.find(c => ALWAYS_BLOCK.includes(c));
  if (alwaysHit) {
    return { blocked: true, reason: `Contains ${alwaysHit.replace(/_/g, " ")}`, selfHarmSignal };
  }

  if (result.min_age && childAge < result.min_age) {
    return { blocked: true, reason: `Rated for age ${result.min_age}+, child is ${childAge}`, selfHarmSignal };
  }

  console.log("[Dhvanyartha Guard] category check — page categories:", categories,
    "| blocked list:", blockedCategories, "| educational:", educational);

  // A ticked category blocks the subject - unless the treatment is genuinely
  // educational. The model tags subject matter, so "world war 2 battle history"
  // and "boxing highlights" both come back tagged violence, and a parent who
  // ticked violence did not mean to block history homework and sport. Measured:
  // both were blocked outright before this carve-out existed.
  //
  // The exemption is narrow on purpose. "educational" is false for entertainment
  // that merely features the topic, and false for anything that teaches how to DO
  // harm - so "how to make a bomb" stays blocked however factually it is worded.
  const hit = categories.find(c => blockedCategories.includes(c));
  if (hit && !educational) {
    return { blocked: true, reason: `Contains ${hit.replace(/_/g, " ")}`, selfHarmSignal };
  }

  if (result.moderation_decision === "block") {
    return { blocked: true, reason: result.reason, selfHarmSignal };
  }

  // Self-harm signals are never used to block the page — the page may be showing
  // legitimate crisis resources, and blocking it could cut a child off from help.
  // Instead this gets carried through to logScan() so it surfaces clearly for the parent.
  return { blocked: false, reason: null, selfHarmSignal };
}

async function logScan(result, decision) {
  const { scanLog = [] } = await chrome.storage.local.get("scanLog");
  scanLog.unshift({
    timestamp: Date.now(),
    description: result.description || "Page content",
    blocked: decision.blocked,
    reason: decision.reason || result.reason,
    min_age: result.min_age || null,
    selfHarmSignal: !!decision.selfHarmSignal
  });
  await chrome.storage.local.set({ scanLog: scanLog.slice(0, 200) });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "scanNow") {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) scanTab(tabs[0].id, true);
    });
    sendResponse({ started: true });
  }

  if (message.action === "scanSelection") {
    const tabId = sender.tab ? sender.tab.id : null;
    if (tabId) scanSelection(tabId, message.text);
    sendResponse({ started: true });
  }

  if (message.action === "openDashboard") {
    chrome.tabs.create({ url: "http://localhost:5500" });
    sendResponse({ opened: true });
  }

  if (message.action === "checkBlocked") {
    // Honour the protection toggle here too. This memory is consulted on every
    // page load before any scan runs, so without this check a page blocked earlier
    // stays blocked forever even after a parent switches protection off — with no
    // way to undo it from the UI.
    Promise.all([blockedUrlsReady, chrome.storage.local.get(["guardEnabled"])])
      .then(([, local]) => {
        if (local.guardEnabled === false) {
          sendResponse({ blocked: false, reason: null });
          return;
        }
        const entry = blockedUrls[normalizeUrl(message.url)];
        sendResponse({ blocked: !!entry, reason: entry ? entry.reason : null });
      });
    return true; // keep the message channel open for the async response above
  }

  if (message.action === "clearBlockedUrls") {
    blockedUrls = {};
    chrome.storage.local.set({ blockedUrls: {} });
    clearVerdictCache();
    sendResponse({ cleared: true });
  }

  if (message.action === "settingsChanged") {
    // A new age or category list re-opens every previous decision.
    invalidateSettingsCache();
    clearVerdictCache();
    sendResponse({ cleared: true });
  }

  if (message.action === "linkParent") {
    chrome.storage.local.set({ parentEmail: message.email || null });
    console.log("[Dhvanyartha Guard] linked parent email:", message.email);
    sendResponse({ linked: true });
  }

  return true;
});
