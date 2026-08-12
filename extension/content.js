// content.js
// Injected into every page. Shows a floating scan toolbar, a results toast,
// and the full-page blocking overlay when background.js flags a page.

/* ---------- toolbar ---------- */

function injectToolbar() {
  if (document.getElementById("dhv-toolbar")) return;

  chrome.storage.local.get(["toolbarHidden"], (data) => {
    if (data.toolbarHidden) return;

    const bar = document.createElement("div");
    bar.id = "dhv-toolbar";
    bar.innerHTML = `
      <div class="dhv-tb-brand">
        <span class="dhv-tb-dot"></span> Dhvanyartha
      </div>
      <div class="dhv-tb-divider"></div>
      <button class="dhv-tb-btn" id="dhv-scan-selection">Scan Selection</button>
      <div class="dhv-tb-divider"></div>
      <button class="dhv-tb-btn" id="dhv-scan-page">Scan Page</button>
      <div class="dhv-tb-divider"></div>
      <button class="dhv-tb-icon" id="dhv-settings" title="Settings">⚙</button>
      <button class="dhv-tb-icon" id="dhv-close" title="Hide toolbar">×</button>
    `;
    document.documentElement.appendChild(bar);

    document.getElementById("dhv-scan-page").addEventListener("click", (e) => {
      setBtnLoading(e.target, "Scanning...");
      chrome.runtime.sendMessage({ action: "scanNow" });
    });

    document.getElementById("dhv-scan-selection").addEventListener("click", (e) => {
      const text = window.getSelection().toString().trim();
      if (!text) {
        showToast({ blocked: false }, { description: "Select some text on the page first." }, true);
        return;
      }
      setBtnLoading(e.target, "Scanning...");
      chrome.runtime.sendMessage({ action: "scanSelection", text });
    });

    document.getElementById("dhv-settings").addEventListener("click", () => {
      chrome.runtime.sendMessage({ action: "openDashboard" });
    });

    document.getElementById("dhv-close").addEventListener("click", () => {
      bar.remove();
      chrome.storage.local.set({ toolbarHidden: true });
    });
  });
}

function setBtnLoading(btn, label) {
  const original = btn.textContent;
  btn.textContent = label;
  btn.disabled = true;
  setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 2500);
}

/* ---------- results toast ---------- */

function showToast(decision, result, isNote, isAuto) {
  const existing = document.getElementById("dhv-toast");
  if (existing) existing.remove();

  const toast = document.createElement("div");
  toast.id = "dhv-toast";
  if (isAuto) toast.classList.add("dhv-toast-auto");

  const badgeClass = isNote ? "note" : (decision.blocked ? "blocked" : "allowed");
  const badgeText = isNote ? "info" : (decision.blocked ? "blocked" : "safe");
  const message = decision.blocked
    ? (decision.reason || result.reason || "This content was blocked.")
    : (isAuto ? "This page looks fine." : (result.reason || result.description || "Scan complete — nothing concerning found."));

  toast.innerHTML = `
    <span class="dhv-toast-badge ${badgeClass}">${badgeText}</span>
    <span class="dhv-toast-msg">${escapeHTML(message)}</span>
    <button class="dhv-toast-close" id="dhv-toast-close">×</button>
  `;
  document.documentElement.appendChild(toast);

  document.getElementById("dhv-toast-close").addEventListener("click", () => toast.remove());
  setTimeout(() => { if (toast.parentNode) toast.remove(); }, isAuto ? 3200 : 6000);
}

/* ---------- block overlay ---------- */

function showBlockOverlay(reason) {
  if (document.getElementById("dhv-guard-overlay")) return;

  // Freeze the page underneath so scrolling/keyboard can't reveal content past the overlay
  document.documentElement.style.overflow = "hidden";

  const overlay = document.createElement("div");
  overlay.id = "dhv-guard-overlay";
  overlay.innerHTML = `
    <div class="dhv-guard-card">
      <div class="dhv-guard-mark"></div>
      <h1>This page is blocked</h1>
      <p>${escapeHTML(reason || "This content isn't appropriate for the age set on this device.")}</p>
      <button id="dhv-guard-back">Go back</button>
    </div>
  `;
  document.documentElement.appendChild(overlay);

  document.getElementById("dhv-guard-back").addEventListener("click", () => {
    // A page opened in a fresh tab has nothing to go back to, so history.back()
    // would do nothing at all and leave the child facing a dead button on top of
    // a blocked page. Fall back to a blank page in that case.
    if (window.history.length > 1) {
      history.back();
    } else {
      window.location.replace("about:blank");
    }
  });
}

function escapeHTML(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

/* ---------- messages from background.js ---------- */

chrome.runtime.onMessage.addListener((message) => {
  if (message.action === "block") {
    showBlockOverlay(message.reason);
  } else if (message.action === "scanResult") {
    showToast(message.decision, message.result, false, message.auto);
  }
  return false;
});

// Ask background.js right away whether this exact page was already blocked before —
// this is what stops a reload from briefly showing blocked content while a fresh
// scan is still running.
chrome.runtime.sendMessage({ action: "checkBlocked", url: location.href }, (response) => {
  if (response && response.blocked) {
    showBlockOverlay(response.reason);
  }
});

injectToolbar();

// If this page IS the Dhvanyartha web app, sync whichever Google account is
// signed in there into the extension's storage — this is what lets extension
// scans (screenshot scans, selection scans) show up under the right parent
// in the web app's own dashboard.
const isWebAppOrigin = location.hostname === "localhost" || location.hostname === "127.0.0.1";
const isWebAppPort = location.port === "5500";
console.log("[Dhvanyartha Guard] page origin check:", location.origin, "-> web app match:", isWebAppOrigin && isWebAppPort);

if (isWebAppOrigin && isWebAppPort) {
  try {
    const savedUser = JSON.parse(localStorage.getItem("dhv_user") || "null");
    console.log("[Dhvanyartha Guard] syncing parent email from web app:", savedUser ? savedUser.email : null);
    chrome.runtime.sendMessage({ action: "linkParent", email: savedUser ? savedUser.email : null });
  } catch (err) {
    console.error("[Dhvanyartha Guard] failed to read signed-in user from web app:", err.message);
  }
}