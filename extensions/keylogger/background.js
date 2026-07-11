// Input Capture Background Script
// Manages content script injection and handles extension lifecycle

const INJECTED_TABS = new Set();

browser.runtime.onInstalled.addListener(() => {
  console.log("[Input Capture] Background script started");
});

// Ensure content script is injected on all pages
browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url && tab.url.startsWith("http")) {
    // Content script is injected automatically via manifest.json matches
    // This just logs that it happened
    if (!INJECTED_TABS.has(tabId)) {
      INJECTED_TABS.add(tabId);
    }
  }
});

// Clean up closed tabs
browser.tabs.onRemoved.addListener((tabId) => {
  INJECTED_TABS.delete(tabId);
});

// Handle messages from content scripts
browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Can be used for popup communication if needed
  if (message.action === "status") {
    sendResponse({ status: "active", tabId: sender.tab ? sender.tab.id : null });
  }
  return true;
});
