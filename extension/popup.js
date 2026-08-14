// popup.js

const guardStatus = document.getElementById("guardStatus");
const guardDot = document.getElementById("guardDot");
const toolbarToggle = document.getElementById("toolbarToggle");
const scanBtn = document.getElementById("scanBtn");
const clearBlockedBtn = document.getElementById("clearBlockedBtn");
const scanStatus = document.getElementById("scanStatus");
const dashboardLink = document.getElementById("dashboardLink");

chrome.storage.local.get(["toolbarHidden"], (data) => {
  toolbarToggle.checked = !data.toolbarHidden;
});

// Protection state is read from the parent's saved settings and only displayed.
// There is no control to change it here on purpose - see README.
async function showGuardState() {
  try {
    const { parentEmail } = await chrome.storage.local.get(["parentEmail"]);
    if (!parentEmail) {
      guardStatus.textContent = "Protection on";
      guardDot.className = "status-dot on";
      document.querySelector(".status-note").textContent = "Sign in on the dashboard to link this device";
      return;
    }
    const res = await fetch(`http://localhost:8000/settings?user_email=${encodeURIComponent(parentEmail)}`);
    const settings = await res.json();
    const on = settings.guard_enabled !== false;
    guardStatus.textContent = on ? "Protection on" : "Protection off";
    guardDot.className = `status-dot ${on ? "on" : "off"}`;
  } catch (err) {
    guardStatus.textContent = "Can't reach the backend";
    guardDot.className = "status-dot off";
    document.querySelector(".status-note").textContent = "Start the backend to see live status";
  }
}
showGuardState();

toolbarToggle.addEventListener("change", () => {
  chrome.storage.local.set({ toolbarHidden: !toolbarToggle.checked });
  // Reload the active tab so the toolbar actually appears/disappears right away
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) chrome.tabs.reload(tabs[0].id);
  });
});

scanBtn.addEventListener("click", () => {
  scanStatus.textContent = "Scanning...";
  chrome.runtime.sendMessage({ action: "scanNow" }, () => {
    setTimeout(() => { scanStatus.textContent = "Scan complete."; }, 1500);
  });
});

// A page stays blocked on reload via the background script's blocked-URL memory.
// Without this there was no way at all to undo a block the parent disagreed with.
clearBlockedBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({ action: "clearBlockedUrls" }, () => {
    scanStatus.textContent = "Blocked pages cleared. Reload the tab to see it.";
    setTimeout(() => { scanStatus.textContent = ""; }, 2500);
  });
});

dashboardLink.addEventListener("click", () => {
  chrome.tabs.create({ url: "http://localhost:5500" });
});