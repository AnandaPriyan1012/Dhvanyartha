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

async function getEffectiveSettings() {
  const local = await chrome.storage.local.get(["childAge", "blockedCategories", "guardEnabled", "parentEmail"]);

  if (!local.parentEmail) {
    // Not linked to a parent account yet — fall back to whatever's stored locally
    // (usually just guardEnabled; age/categories will be defaults until linked).
    console.log("[Dhvanyartha Guard] no parent linked yet, using local fallback settings");
    return local;
  }

  try {
    const res = await fetch(`${API_BASE}/settings?user_email=${encodeURIComponent(local.parentEmail)}`);
    const remote = await res.json();
    console.log("[Dhvanyartha Guard] fetched live settings from backend:", remote);
    return {
      childAge: remote.child_age,
      blockedCategories: remote.blocked_categories || [],
      guardEnabled: remote.guard_enabled !== false && local.guardEnabled !== false,
      parentEmail: local.parentEmail
    };
  } catch (err) {
    console.error("[Dhvanyartha Guard] failed to fetch live settings, using local fallback:", err.message);
    return local;
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

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.active && tab.url && tab.url.startsWith("http")) {
    console.log("[Dhvanyartha Guard] page loaded, scheduling auto-scans:", tab.url);
    // Two passes: an early one for fast pages, and a later one to catch
    // content that finishes rendering after the initial page load event fires
    // (search results, infinite-scroll feeds, ads, etc).
    setTimeout(() => scanTab(tabId, false), 700);
    setTimeout(() => scanTab(tabId, false, true), 2800);
  }
});

async function sendBlockMessage(tabId, reason, attempt = 1) {
  try {
    await chrome.tabs.sendMessage(tabId, { action: "block", reason });
    console.log("[Dhvanyartha Guard] block message delivered to tab", tabId);
  } catch (err) {
    console.error(`[Dhvanyartha Guard] block message failed (attempt ${attempt}):`, err.message);
    if (attempt < 3) {
      setTimeout(() => sendBlockMessage(tabId, reason, attempt + 1), 600);
    } else {
      console.error("[Dhvanyartha Guard] giving up on blocking this tab after 3 attempts");
    }
  }
}

const lastScanAt = {}; // tabId -> timestamp, prevents redundant back-to-back scans

// Tab ids are never reused, so without this the map grows for the whole life of
// the service worker.
chrome.tabs.onRemoved.addListener((tabId) => {
  delete lastScanAt[tabId];
});

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

    // Let the HUD start its scan animation, so the user can see that something is
    // happening during the second or two the model takes to answer.
    chrome.tabs.sendMessage(tabId, { action: "scanStarted" }).catch(() => {});

    const screenshotUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 55 });
    const rawBlob = await (await fetch(screenshotUrl)).blob();
    const blob = await shrinkImage(rawBlob);

    const formData = new FormData();
    formData.append("file", blob, "screenshot.jpg");
    if (settings.parentEmail) formData.append("user_email", settings.parentEmail);

    const res = await fetch(`${API_BASE}/analyze-image`, { method: "POST", body: formData });
    const result = await res.json();
    console.log("[Dhvanyartha Guard] scan result:", result);

    const decision = evaluateForChild(result, settings);
    console.log("[Dhvanyartha Guard] decision:", decision);

    if (decision.blocked) {
      sendBlockMessage(tabId, decision.reason);
      if (tab && tab.url) rememberBlocked(tab.url, decision.reason);
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

function evaluateForChild(result, settings) {
  const childAge = settings.childAge || 18;
  const blockedCategories = (settings.blockedCategories || []).map(c => String(c).trim().toLowerCase());
  const selfHarmSignal = !!result.self_harm_signal;

  if (result.min_age && childAge < result.min_age) {
    return { blocked: true, reason: `Rated for age ${result.min_age}+, child is ${childAge}`, selfHarmSignal };
  }

  const categories = (result.categories || []).map(c => String(c).trim().toLowerCase());
  console.log("[Dhvanyartha Guard] category check — page categories:", categories, "| blocked list:", blockedCategories);

  const hit = categories.find(c => blockedCategories.includes(c));
  if (hit) {
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
    sendResponse({ cleared: true });
  }

  if (message.action === "linkParent") {
    chrome.storage.local.set({ parentEmail: message.email || null });
    console.log("[Dhvanyartha Guard] linked parent email:", message.email);
    sendResponse({ linked: true });
  }

  return true;
});
