// popup.js

const guardToggle = document.getElementById("guardToggle");
const toolbarToggle = document.getElementById("toolbarToggle");
const scanBtn = document.getElementById("scanBtn");
const clearBlockedBtn = document.getElementById("clearBlockedBtn");
const scanStatus = document.getElementById("scanStatus");
const dashboardLink = document.getElementById("dashboardLink");

// Load current guard state (defaults to ON if never set)
chrome.storage.local.get(["guardEnabled", "toolbarHidden"], (data) => {
  guardToggle.checked = data.guardEnabled !== false;
  toolbarToggle.checked = !data.toolbarHidden;
});

guardToggle.addEventListener("change", () => {
  chrome.storage.local.set({ guardEnabled: guardToggle.checked });
});

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