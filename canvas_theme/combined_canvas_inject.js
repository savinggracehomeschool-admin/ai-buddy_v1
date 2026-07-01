/* ============================================================================
 * SGEG Canvas Theme — Combined JavaScript
 *
 * 1. Invigilator  — quiz proctoring (camera/mic, blocking overlay, agent)
 * 2. Education Coach badge — floating AI chat badge (all Canvas pages)
 *
 * Upload to: Canvas Admin > Sub-account > Themes > Custom JavaScript
 * ============================================================================ */


/* ── 1. INVIGILATOR ──────────────────────────────────────────────────────── */
(function () {
  "use strict";

  // ============================================================================
  // VERSION & CONFIGURATION
  // ============================================================================
  // Update version in /src/config/canvas-theme-version.js then run: node scripts/sync-canvas-version.js
  const THEME_SCRIPT_VERSION = "1.0.0";

  // UPDATE THIS URL BEFORE UPLOADING TO CANVAS
  const INVIGILATOR_APP_URL = "https://weblti.invigilator.app";
  // ============================================================================
  const INIT_URL = `${INVIGILATOR_APP_URL}/api/lti/canvas/pseudo-launch`;
  const BANNER_ID = "invigilator-lti-banner";
  const IFRAME_ID = "invigilator-lti-iframe";
  const OVERLAY_ID = "invigilator-blocking-overlay";
  const LAUNCH_TIMEOUT_MS = 15000;
  const PROCTORING_TIMEOUT_MS = 60000;
  const BANNER_HEIGHT = 400;
  const REINSERT_INTERVAL_MS = 1000;
  const QUIZ_DEBOUNCE_MS = 300;
  const PERMISSION_POLL_INTERVAL_MS = 30000; // Poll every 30 seconds

  let lastLaunchKey = null;
  let launchInProgress = false;
  let overlayTimeoutId = null;
  let overlayRemoved = false;
  let permissionPollIntervalId = null;
  let quizNotConfigured = false;
  let permissionsGranted = false;
  let agentConnected = false;
  let versionCheckResult = null; // Cache the version check result
  let quizBlockingEnabled = false; // Track if we should block quiz taking

  // ── Theme Agent Socket ─────────────────────────────────────────────────────
  // themeInject connects to the agent on behalf of the studentPage iframe,
  // which cannot do so directly due to Chrome's Private Network Access policy.
  const AGENT_BASE_PORT = 49152;
  const AGENT_PORT_RANGE = 10;
  // Include IPv6 loopback — on some machines "localhost" resolves to ::1 instead of 127.0.0.1
  const AGENT_HOSTS = ["127.0.0.1", "localhost", "[::1]"];
  const AGENT_RECONNECT_MS = 3000;
  const AGENT_TIMEOUT_MS = 2000;

  let themeAgentSocket = null;
  let themeAgentReconnectTimeout = null;
  let themeAgentIframeTarget = null; // the studentPage contentWindow to notify
  let themeAgentIoLoaded = false;

  function notifyIframe(connected) {
    const iframe = document.getElementById(IFRAME_ID);
    const target = themeAgentIframeTarget || (iframe && iframe.contentWindow);
    if (target) {
      target.postMessage(
        { type: "INVIGILATOR_AGENT_CONNECT_RESULT", connected },
        "*",
      );
    }
  }

  function scheduleThemeAgentReconnect() {
    clearTimeout(themeAgentReconnectTimeout);
    themeAgentReconnectTimeout = setTimeout(
      () => connectThemeAgentSocket(),
      AGENT_RECONNECT_MS,
    );
  }

  async function tryThemeAgentPort(host, port) {
    return new Promise((resolve) => {
      const socket = io(`http://${host}:${port}`, {
        transports: ["websocket"],
        upgrade: false,
        reconnection: false,
        timeout: AGENT_TIMEOUT_MS,
        forceNew: true,
      });
      const timer = setTimeout(() => {
        socket.close();
        resolve(null);
      }, AGENT_TIMEOUT_MS);
      socket.on("connect", () => {
        clearTimeout(timer);
        resolve(socket);
      });
      socket.on("connect_error", () => {
        clearTimeout(timer);
        socket.close();
        resolve(null);
      });
    });
  }

  async function connectThemeAgentSocket(target) {
    if (target) themeAgentIframeTarget = target;

    if (themeAgentSocket) {
      themeAgentSocket.removeAllListeners();
      themeAgentSocket.disconnect();
      themeAgentSocket = null;
    }

    const candidates = [];
    for (const host of AGENT_HOSTS) {
      for (
        let port = AGENT_BASE_PORT;
        port < AGENT_BASE_PORT + AGENT_PORT_RANGE;
        port++
      ) {
        candidates.push({ host, port });
      }
    }

    const results = await Promise.all(
      candidates.map(({ host, port }) => tryThemeAgentPort(host, port)),
    );

    let winner = null;
    for (let i = 0; i < results.length; i++) {
      const socket = results[i];
      if (socket && !winner) {
        winner = socket;
      } else if (socket) {
        socket.removeAllListeners();
        socket.disconnect();
      }
    }

    if (winner) {
      themeAgentSocket = winner;
      notifyIframe(true);

      winner.on("disconnect", () => {
        themeAgentSocket = null;
        notifyIframe(false);
        scheduleThemeAgentReconnect();
      });
      winner.on("connect_error", () => {
        themeAgentSocket = null;
        notifyIframe(false);
        scheduleThemeAgentReconnect();
      });
      return;
    }

    notifyIframe(false);
    scheduleThemeAgentReconnect();
  }

  function loadSocketIOThenConnect(target) {
    if (themeAgentIoLoaded) {
      connectThemeAgentSocket(target);
      return;
    }
    const script = document.createElement("script");
    script.src = "https://cdn.socket.io/4.7.2/socket.io.min.js";
    script.onload = () => {
      themeAgentIoLoaded = true;
      connectThemeAgentSocket(target);
    };
    script.onerror = () => notifyIframe(false);
    document.head.appendChild(script);
  }
  // ── End Theme Agent Socket ─────────────────────────────────────────────────

  const VERSION_SESSION_KEY = "invigilator_version_session_checked";

  function getCanvasEnvValue(path, fallback = undefined) {
    try {
      const parts = path.split(".");
      let v = window;
      for (const p of parts) {
        if (v == null) return fallback;
        v = v[p];
      }
      return v === undefined ? fallback : v;
    } catch (e) {
      return fallback;
    }
  }

  function parseCourseIdFromUrl() {
    const m = window.location.pathname.match(/\/courses\/(\d+)/);
    return m ? m[1] : null;
  }

  function getCourseId() {
    return getCanvasEnvValue("ENV.COURSE_ID") || parseCourseIdFromUrl();
  }

  function markQuizAsInvigilated() {
    const quizId = parseQuizIdFromUrl();
    if (!quizId) return;
    const storageKey = `invigilator_quiz_${quizId}_invigilated`;
    try {
      sessionStorage.setItem(storageKey, "true");
    } catch (e) {}
  }

  function isQuizMarkedAsInvigilated() {
    const quizId = parseQuizIdFromUrl();
    if (!quizId) return false;
    const storageKey = `invigilator_quiz_${quizId}_invigilated`;
    try {
      return sessionStorage.getItem(storageKey) === "true";
    } catch (e) {
      return false;
    }
  }

  function clearQuizInvigilatedMark() {
    const quizId = parseQuizIdFromUrl();
    if (!quizId) return;
    const storageKey = `invigilator_quiz_${quizId}_invigilated`;
    try {
      sessionStorage.removeItem(storageKey);
    } catch (e) {}
  }

  function cleanupOldOverlayStorage() {
    try {
      const keys = Object.keys(sessionStorage);
      keys.forEach((key) => {
        if (key.startsWith("invigilator_overlay_shown_")) {
          sessionStorage.removeItem(key);
        }
      });
    } catch (e) {}
  }

  function cleanupOldCacheEntries() {
    try {
      const keys = Object.keys(localStorage);
      keys.forEach((key) => {
        if (key.startsWith("invigilator_course_")) {
          localStorage.removeItem(key);
        }
        if (key.startsWith("invigilator_quiz_")) {
          try {
            const cached = localStorage.getItem(key);
            if (cached) {
              const { timestamp } = JSON.parse(cached);
              const oneHour = 60 * 60 * 1000;
              if (Date.now() - timestamp >= oneHour) {
                localStorage.removeItem(key);
              }
            }
          } catch (e) {
            localStorage.removeItem(key);
          }
        }
      });
    } catch (e) {}
  }

  function parseQuizIdFromUrl() {
    const m = window.location.pathname.match(/\/courses\/\d+\/quizzes\/(\d+)/);
    return m ? m[1] : null;
  }

  function isQuizPage() {
    const p = window.location.pathname;
    return /\/courses\/\d+\/quizzes\/\d+/.test(p);
  }

  function isQuizTakePage() {
    return /\/courses\/\d+\/quizzes\/\d+\/take/.test(window.location.pathname);
  }

  function isQuizInfoPage() {
    const p = window.location.pathname;
    return /\/courses\/\d+\/quizzes\/\d+$/.test(p);
  }

  function isReadyForQuiz() {
    if (isTeacherOrAdmin()) return true;
    if (quizNotConfigured) return true;
    return permissionsGranted && agentConnected;
  }

  function checkQuizReadiness() {
    if ((!isQuizTakePage() && !isQuizInfoPage()) || isTeacherOrAdmin()) {
      return;
    }
    if (quizNotConfigured) {
      enableQuizTaking();
      return;
    }
    if (permissionsGranted && agentConnected) {
      enableQuizTaking();
    } else {
      disableQuizTaking();
    }
  }

  function disableQuizTaking() {
    quizBlockingEnabled = true;
    const quizLink = document.getElementById("take_quiz_link");
    if (quizLink && !quizLink.hasAttribute("data-invigilator-blocked")) {
      quizLink.addEventListener("click", blockQuizAction, true);
      quizLink.removeAttribute("href");
      quizLink.removeAttribute("data-method");
      quizLink.style.opacity = "0.5";
      quizLink.style.cursor = "not-allowed";
      quizLink.setAttribute("data-invigilator-blocked", "true");
      quizLink.setAttribute("title", "Complete Invigilator setup first");
    }
    const fallbackElements = document.querySelectorAll(
      'a[href*="/take"]:not(#preview_quiz_button):not([data-invigilator-blocked]), .take_quiz_button:not([data-invigilator-blocked])',
    );
    fallbackElements.forEach((element) => {
      element.addEventListener("click", blockQuizAction, true);
      element.setAttribute("data-invigilator-blocked", "true");
      element.style.opacity = "0.5";
      element.style.cursor = "not-allowed";
      element.setAttribute("title", "Complete Invigilator setup first");
    });
  }

  function enableQuizTaking() {
    quizBlockingEnabled = false;
    const quizLink = document.getElementById("take_quiz_link");
    if (quizLink && quizLink.hasAttribute("data-invigilator-blocked")) {
      quizLink.removeEventListener("click", blockQuizAction, true);
      const courseId = getCanvasEnvValue("ENV.COURSE_ID") || parseCourseIdFromUrl();
      const quizId = getCanvasEnvValue("ENV.QUIZ.id") || parseQuizIdFromUrl();
      const userId = getCanvasEnvValue("ENV.current_user_id");
      if (courseId && quizId) {
        const href = userId
          ? `/courses/${courseId}/quizzes/${quizId}/take?user_id=${userId}`
          : `/courses/${courseId}/quizzes/${quizId}/take`;
        quizLink.setAttribute("href", href);
        quizLink.setAttribute("data-method", "post");
      }
      quizLink.style.opacity = "";
      quizLink.style.cursor = "";
      quizLink.removeAttribute("data-invigilator-blocked");
      quizLink.removeAttribute("title");
    }
    const blockedElements = document.querySelectorAll("[data-invigilator-blocked]");
    blockedElements.forEach((element) => {
      element.removeEventListener("click", blockQuizAction, true);
      element.removeAttribute("data-invigilator-blocked");
      element.style.opacity = "";
      element.style.cursor = "";
      element.removeAttribute("title");
    });
  }

  function blockQuizAction(event) {
    if (quizBlockingEnabled && !isReadyForQuiz()) {
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      return false;
    }
  }

  function getCurrentUserRole() {
    if (getCanvasEnvValue("ENV.current_user_is_student") === true) return "student";
    if (getCanvasEnvValue("ENV.COURSE.is_instructor") === true) return "teacher";
    if (getCanvasEnvValue("ENV.current_user_is_admin") === true) return "admin";
    const enrollmentType = getCanvasEnvValue("ENV.course_enrollment_type");
    if (enrollmentType) {
      const t = String(enrollmentType).toLowerCase();
      if (t.includes("student") || t.includes("observer")) return "student";
      if (t.includes("teacher") || t.includes("instructor") || t.includes("designer")) return "teacher";
      if (t.includes("ta")) return "ta";
    }
    if (getCanvasEnvValue("ENV.IS_STUDENT") === true) return "student";
    const currentUserRoles = getCanvasEnvValue("ENV.current_user_roles") || [];
    if (Array.isArray(currentUserRoles)) {
      const rolesLower = currentUserRoles.map((r) => String(r).toLowerCase());
      if (rolesLower.some((r) => r.includes("admin"))) return "admin";
      if (rolesLower.some((r) => r.includes("teacher") || r.includes("instructor"))) return "teacher";
      if (rolesLower.some((r) => r.includes("ta"))) return "ta";
    }
    return "student";
  }

  function isTeacherOrAdmin() {
    const role = getCurrentUserRole();
    return role === "admin" || role === "teacher" || role === "ta";
  }

  function hasCheckedVersionThisSession() {
    try {
      return sessionStorage.getItem(VERSION_SESSION_KEY) === "true";
    } catch (e) {
      return false;
    }
  }

  function markVersionCheckedThisSession() {
    try {
      sessionStorage.setItem(VERSION_SESSION_KEY, "true");
    } catch (e) {}
  }

  async function checkVersion() {
    if (hasCheckedVersionThisSession() && versionCheckResult !== null) {
      if (versionCheckResult.isOutdated && isTeacherOrAdmin() && !document.getElementById("invigilator-version-warning")) {
        showVersionWarningBanner(versionCheckResult.serverVersion, versionCheckResult.downloadUrl);
      }
      return versionCheckResult;
    }
    if (versionCheckResult !== null) {
      markVersionCheckedThisSession();
      if (versionCheckResult.isOutdated && isTeacherOrAdmin() && !document.getElementById("invigilator-version-warning")) {
        showVersionWarningBanner(versionCheckResult.serverVersion, versionCheckResult.downloadUrl);
      }
      return versionCheckResult;
    }
    try {
      const response = await fetch(`${INVIGILATOR_APP_URL}/api/lti/canvas/version`, {
        headers: { "ngrok-skip-browser-warning": "true" },
      });
      if (!response.ok) {
        versionCheckResult = { error: true };
        markVersionCheckedThisSession();
        return versionCheckResult;
      }
      const data = await response.json();
      versionCheckResult = {
        serverVersion: data.version,
        downloadUrl: data.downloadUrl,
        isOutdated: data.version !== THEME_SCRIPT_VERSION,
      };
      markVersionCheckedThisSession();
      if (versionCheckResult.isOutdated && isTeacherOrAdmin() && !document.getElementById("invigilator-version-warning")) {
        showVersionWarningBanner(data.version, data.downloadUrl);
      }
      return versionCheckResult;
    } catch (error) {
      versionCheckResult = { error: true, message: error.message };
      markVersionCheckedThisSession();
      return versionCheckResult;
    }
  }

  function showVersionWarningBanner(latestVersion, downloadUrl) {
    if (document.getElementById("invigilator-version-warning")) return;
    const warning = document.createElement("div");
    warning.id = "invigilator-version-warning";
    warning.style.cssText = `position:fixed;top:0;left:0;right:0;background:linear-gradient(135deg,#DC2626 0%,#EF4444 100%);color:white;padding:12px 20px;z-index:10001;text-align:center;font-size:14px;font-weight:500;box-shadow:0 2px 8px rgba(0,0,0,.2);animation:slideDown .3s ease-out;`;
    warning.innerHTML = `<style>@keyframes slideDown{from{transform:translateY(-100%)}to{transform:translateY(0)}}</style><div style="max-width:1200px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;gap:16px;"><div style="flex:1;text-align:left;">⚠️ <strong>Invigilator Theme Script Outdated:</strong> v${THEME_SCRIPT_VERSION} → v${latestVersion}<a href="${downloadUrl || `${INVIGILATOR_APP_URL}/api/lti/canvas/download-theme`}" download style="color:white;text-decoration:underline;margin-left:12px;">Download v${latestVersion}</a></div><button onclick="this.parentElement.parentElement.remove()" style="background:rgba(255,255,255,.2);border:1px solid rgba(255,255,255,.4);color:white;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:bold;">Dismiss</button></div>`;
    document.body.appendChild(warning);
  }

  async function checkPermissions() {
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        permissionsGranted = false;
        checkQuizReadiness();
        return false;
      }
      const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
      stream.getTracks().forEach((track) => track.stop());
      permissionsGranted = true;
      checkQuizReadiness();
      return true;
    } catch (error) {
      permissionsGranted = false;
      checkQuizReadiness();
      return false;
    }
  }

  function showPermissionPrompt() {
    if (isTeacherOrAdmin()) return;
    if (!overlayRemoved) return;
    if (document.getElementById("invigilator-permission-prompt")) return;
    const promptOverlay = document.createElement("div");
    promptOverlay.id = "invigilator-permission-prompt";
    promptOverlay.style.cssText = `position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,.8);z-index:10000;display:flex;align-items:center;justify-content:center;`;
    promptOverlay.innerHTML = `<div style="background:white;border-radius:16px;padding:40px;max-width:500px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.3);"><h2 style="color:#1F2937;font-size:24px;font-weight:bold;margin:0 0 16px;">Camera & Microphone Required</h2><p style="color:#6B7280;font-size:16px;margin:0 0 24px;line-height:1.6;">This exam requires camera and microphone access for invigilation.</p><button id="invigilator-grant-permission-btn" style="background:rgb(54,186,150);color:white;font-weight:bold;padding:14px 32px;border-radius:8px;border:none;cursor:pointer;font-size:16px;width:100%;">Grant Camera & Microphone Access</button><p style="color:#9CA3AF;font-size:13px;margin:16px 0 0;">Click "Allow" when your browser prompts you</p></div>`;
    document.body.appendChild(promptOverlay);
    const grantBtn = document.getElementById("invigilator-grant-permission-btn");
    if (grantBtn) {
      grantBtn.addEventListener("click", async () => {
        const granted = await checkPermissions();
        if (granted) {
          removePermissionPrompt();
          const iframe = document.getElementById(IFRAME_ID);
          if (iframe && iframe.contentWindow) {
            iframe.contentWindow.postMessage({ type: "INVIGILATOR_PERMISSIONS_GRANTED" }, "*");
          }
        }
      });
    }
    promptOverlay.addEventListener("click", (e) => {
      if (e.target === promptOverlay) removePermissionPrompt();
    });
  }

  function removePermissionPrompt() {
    const prompt = document.getElementById("invigilator-permission-prompt");
    if (prompt) {
      prompt.style.opacity = "0";
      setTimeout(() => prompt.remove(), 300);
    }
  }

  function startPermissionPolling() {
    if (isTeacherOrAdmin()) return;
    if (quizNotConfigured) return;
    if (!overlayRemoved) return;
    if (permissionPollIntervalId) return;
    checkPermissions().then((granted) => { if (!granted) showPermissionPrompt(); });
    permissionPollIntervalId = setInterval(async () => {
      if (!isQuizTakePage()) { stopPermissionPolling(); return; }
      const granted = await checkPermissions();
      if (!granted) showPermissionPrompt(); else removePermissionPrompt();
    }, PERMISSION_POLL_INTERVAL_MS);
  }

  function stopPermissionPolling() {
    if (permissionPollIntervalId) {
      clearInterval(permissionPollIntervalId);
      permissionPollIntervalId = null;
    }
    removePermissionPrompt();
  }

  function createBlockingOverlay() {
    if (document.getElementById(OVERLAY_ID)) return;
    if (isTeacherOrAdmin()) return;
    if (overlayRemoved) return;
    const overlay = document.createElement("div");
    overlay.id = OVERLAY_ID;
    overlay.style.cssText = `position:fixed;top:0;left:0;width:100vw;height:100vh;background:linear-gradient(135deg,rgb(54,186,150) 0%,rgb(74,222,128) 100%);z-index:9998;display:flex;align-items:center;justify-content:center;transition:opacity .3s ease-out;`;
    overlay.innerHTML = `<div style="background:white;border-radius:20px;padding:50px;text-align:center;box-shadow:0 20px 40px rgba(0,0,0,.1);animation:slideIn .5s ease-out;width:160px;"><style>@keyframes slideIn{from{opacity:0;transform:translateY(-20px)}to{opacity:1;transform:translateY(0)}}@keyframes spin{to{transform:rotate(360deg)}}</style><div style="width:100px;height:100px;margin:0 auto 24px;background:rgb(54,186,150);border-radius:50%;display:flex;align-items:center;justify-content:center;position:relative;"><div style="position:absolute;width:120px;height:120px;border:3px solid #E5E7EB;border-top-color:rgb(54,186,150);border-radius:50%;animation:spin 1s linear infinite;"></div><img src="${INVIGILATOR_APP_URL}/owl-student.png" alt="Invigilator" style="width:60px;height:60px;object-fit:contain;z-index:1;" onerror="this.style.display='none'" /></div><p style="color:#4B5563;font-size:16px;font-weight:500;margin:0;">Page loading</p></div>`;
    document.body.appendChild(overlay);
  }

  function removeBlockingOverlay() {
    overlayRemoved = true;
    if (overlayTimeoutId) { clearTimeout(overlayTimeoutId); overlayTimeoutId = null; }
    const overlay = document.getElementById(OVERLAY_ID);
    if (overlay) {
      overlay.style.opacity = "0";
      setTimeout(() => {
        overlay.remove();
        if (!isTeacherOrAdmin() && !quizNotConfigured) startPermissionPolling();
      }, 300);
    }
  }

  function ensureOverlayPersistence() {
    if (overlayRemoved || isTeacherOrAdmin()) return;
    setInterval(() => {
      if (!isQuizTakePage()) return;
      if (overlayRemoved) return;
      if (!document.getElementById(OVERLAY_ID)) createBlockingOverlay();
    }, REINSERT_INTERVAL_MS);
  }

  function ensureBodyPadding() {
    const existing = document.documentElement.style.getPropertyValue("--invigilator-top-offset");
    if (!existing) {
      document.documentElement.style.setProperty("--invigilator-top-offset", `${BANNER_HEIGHT}px`);
      document.body.style.paddingTop = `${BANNER_HEIGHT}px`;
    }
  }

  function removeBodyPadding() {
    document.documentElement.style.removeProperty("--invigilator-top-offset");
    document.body.style.paddingTop = "";
  }

  function injectBannerIfMissing() {
    if (document.getElementById(BANNER_ID)) return;
    const banner = document.createElement("div");
    banner.id = BANNER_ID;
    banner.setAttribute("aria-hidden", "false");
    banner.style.position = "relative";
    banner.style.width = "100%";
    banner.style.height = `${BANNER_HEIGHT}px`;
    banner.style.zIndex = "100";
    banner.style.boxShadow = "0 2px 8px rgba(0,0,0,0.12)";
    banner.style.background = "#fff";
    banner.style.borderBottom = "1px solid rgba(0,0,0,0.08)";
    banner.style.display = "flex";
    banner.style.alignItems = "stretch";
    banner.style.justifyContent = "center";
    banner.style.overflow = "hidden";
    const iframe = document.createElement("iframe");
    iframe.id = IFRAME_ID;
    iframe.name = IFRAME_ID;
    iframe.style.width = "100%";
    iframe.style.height = "100%";
    iframe.style.border = "none";
    iframe.setAttribute("title", "Invigilator tool");
    iframe.setAttribute("allow", `camera ${INVIGILATOR_APP_URL}; microphone ${INVIGILATOR_APP_URL}; display-capture; clipboard-read; clipboard-write; fullscreen; local-network *; loopback-network *`);
    const fallback = document.createElement("div");
    fallback.id = `${BANNER_ID}-fallback`;
    fallback.style.display = "none";
    fallback.style.width = "100%";
    fallback.style.height = "100%";
    fallback.style.padding = "20px";
    fallback.style.boxSizing = "border-box";
    fallback.style.textAlign = "center";
    fallback.style.alignItems = "center";
    fallback.style.justifyContent = "center";
    fallback.innerHTML = `<div style="max-width:900px;margin:0 auto;"><strong>Invigilator</strong><p style="margin:8px 0 0;">Unable to launch the invigilator tool. <button id="invigilator-retry-btn" style="margin-left:8px;padding:.5rem 1rem">Retry</button></p></div>`;
    banner.appendChild(iframe);
    banner.appendChild(fallback);
    const content = document.getElementById("content");
    if (content) content.prepend(banner); else document.body.prepend(banner);
    banner.querySelector("#invigilator-retry-btn")?.addEventListener("click", () => { lastLaunchKey = null; launchLtiForCurrentContext(); });
  }

  function removeBannerIfPresent() {
    const el = document.getElementById(BANNER_ID);
    if (el) el.remove();
  }

  async function fetchLtiLaunchHtml(courseId, quizId, assignmentId, userId, userEmail, instanceUrl, userRole, userName) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), LAUNCH_TIMEOUT_MS);
    try {
      const res = await fetch(INIT_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json", "ngrok-skip-browser-warning": "true" },
        body: JSON.stringify({ courseId, quizId, assignmentId, userId, userEmail, userName, instanceUrl, userRole, themeScriptVersion: THEME_SCRIPT_VERSION }),
        signal: controller.signal,
        credentials: "include",
      });
      clearTimeout(timeout);
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`LTI init returned ${res.status}: ${text}`);
      }
      const json = await res.json();
      if (json && json.notConfigured) return null;
      if (!json || typeof json.launchHtml !== "string") throw new Error("Invalid response from LTI init endpoint (missing launchHtml).");
      return json.launchHtml;
    } catch (err) {
      clearTimeout(timeout);
      throw err;
    }
  }

  function writeHtmlToIframe(iframeEl, html) {
    try {
      const doc = iframeEl.contentWindow?.document;
      if (!doc) { iframeEl.setAttribute("srcdoc", html); return; }
      doc.open(); doc.write(html); doc.close();
    } catch (e) {
      iframeEl.setAttribute("srcdoc", html);
    }
  }

  async function launchLtiForCurrentContext() {
    if (launchInProgress) return;
    launchInProgress = true;
    const courseId = getCanvasEnvValue("ENV.COURSE_ID") || parseCourseIdFromUrl();
    const quizId = getCanvasEnvValue("ENV.QUIZ.id") || parseQuizIdFromUrl();
    const userId = getCanvasEnvValue("ENV.current_user_id") || getCanvasEnvValue("ENV.current_user.id") || getCanvasEnvValue("ENV.current_user.user_id");
    const assignmentId = getCanvasEnvValue("ENV.QUIZ.assignment_id") || getCanvasEnvValue("ENV.CONDITIONAL_RELEASE_ENV.asssignment.id") || null;
    const userEmail = getCanvasEnvValue("ENV.current_user.email") || getCanvasEnvValue("ENV.current_user.login_id") || getCanvasEnvValue("ENV.current_user.primary_email");
    const userName = getCanvasEnvValue("ENV.current_user.display_name") || getCanvasEnvValue("ENV.current_user.short_name") || getCanvasEnvValue("ENV.current_user.name") || null;
    const instanceUrl = window.location.origin;
    const userRole = getCurrentUserRole();
    const launchKey = `${courseId || "c-?"}-${quizId || "q-?"}-${userId || "u-?"}`;
    try {
      if (lastLaunchKey === launchKey) { launchInProgress = false; return; }
      const iframeEl = document.getElementById(IFRAME_ID);
      if (iframeEl) writeHtmlToIframe(iframeEl, `<html><body style="display:flex;align-items:center;justify-content:center;height:100%;"><div>Launching Invigilator...</div></body></html>`);
      const launchHtml = await fetchLtiLaunchHtml(courseId, quizId, assignmentId, userId, userEmail, instanceUrl, userRole, userName);
      if (launchHtml === null) {
        quizNotConfigured = true;
        cacheInvigilationStatus(false);
        clearQuizInvigilatedMark();
        stopPermissionPolling();
        removeBlockingOverlay();
        removeBannerIfPresent();
        removeBodyPadding();
        checkQuizReadiness();
        lastLaunchKey = launchKey;
        launchInProgress = false;
        return;
      }
      quizNotConfigured = false;
      cacheInvigilationStatus(true);
      markQuizAsInvigilated();
      injectBannerIfMissing();
      ensureBannerPersistence();
      const configuredIframeEl = document.getElementById(IFRAME_ID);
      if (configuredIframeEl) writeHtmlToIframe(configuredIframeEl, launchHtml);
      const fallback = document.getElementById(`${BANNER_ID}-fallback`);
      if (fallback) fallback.style.display = "none";
      lastLaunchKey = launchKey;
    } catch (err) {
      console.error("Invigilator LTI launch failed:", err);
      quizNotConfigured = true;
      removeBlockingOverlay();
      checkQuizReadiness();
      const fallback = document.getElementById(`${BANNER_ID}-fallback`);
      if (fallback) fallback.style.display = "flex";
      const iframeEl = document.getElementById(IFRAME_ID);
      if (iframeEl) {
        try { writeHtmlToIframe(iframeEl, `<html><body style="font-family:system-ui,Arial;padding:20px;"><h3>Invigilator failed to load</h3><pre style="white-space:pre-wrap;color:#900;">${String(err.message || err)}</pre></body></html>`); } catch (_e) {}
      }
    } finally {
      launchInProgress = false;
    }
  }

  function ensureBannerPersistence() {
    setInterval(() => {
      if (quizNotConfigured) return;
      if (!isQuizTakePage()) { removeBannerIfPresent(); return; }
      if (!document.getElementById(BANNER_ID)) { injectBannerIfMissing(); launchLtiForCurrentContext(); }
    }, REINSERT_INTERVAL_MS);
    const mo = new MutationObserver(() => {
      if (quizNotConfigured) return;
      if (!document.getElementById(BANNER_ID) && isQuizTakePage()) {
        injectBannerIfMissing();
        setTimeout(() => launchLtiForCurrentContext(), QUIZ_DEBOUNCE_MS);
      }
    });
    mo.observe(document.documentElement || document.body, { childList: true, subtree: true });
  }

  function getCachedInvigilationStatus() {
    const courseId = getCourseId();
    const quizId = parseQuizIdFromUrl();
    if (!courseId || !quizId) return null;
    const cacheKey = `invigilator_quiz_${courseId}_${quizId}`;
    const cached = localStorage.getItem(cacheKey);
    if (cached) {
      try {
        const { timestamp, isInvigilated } = JSON.parse(cached);
        if (Date.now() - timestamp < 60 * 60 * 1000) return isInvigilated;
      } catch (e) {
        localStorage.removeItem(cacheKey);
      }
    }
    return null;
  }

  function cacheInvigilationStatus(isInvigilated) {
    const courseId = getCourseId();
    const quizId = parseQuizIdFromUrl();
    if (!courseId || !quizId) return;
    const cacheKey = `invigilator_quiz_${courseId}_${quizId}`;
    localStorage.setItem(cacheKey, JSON.stringify({ timestamp: Date.now(), isInvigilated }));
  }

  function initIfQuiz() {
    try {
      cleanupOldOverlayStorage();
      cleanupOldCacheEntries();
      if (!hasCheckedVersionThisSession() && (isTeacherOrAdmin() || isQuizTakePage() || isQuizInfoPage())) {
        checkVersion();
      }
      if (isQuizTakePage()) {
        const isMarkedInvigilated = isQuizMarkedAsInvigilated();
        const cachedQuizStatus = getCachedInvigilationStatus();
        if (isMarkedInvigilated || cachedQuizStatus === true) {
          createBlockingOverlay();
          ensureOverlayPersistence();
        }
        setTimeout(() => {
          launchLtiForCurrentContext();
          setTimeout(() => checkQuizReadiness(), 1000);
        }, QUIZ_DEBOUNCE_MS);
      } else if (isQuizInfoPage()) {
        quizNotConfigured = false;
        disableQuizTaking();
        setTimeout(() => checkQuizConfigurationOnly(), QUIZ_DEBOUNCE_MS);
        observeQuizContentChanges();
      } else {
        removeBlockingOverlay();
        stopPermissionPolling();
        return;
      }
    } catch (e) {
      console.error("Invigilator theme script init error", e);
    }
  }

  async function checkQuizConfigurationOnly() {
    const courseId = getCanvasEnvValue("ENV.COURSE_ID") || parseCourseIdFromUrl();
    const quizId = getCanvasEnvValue("ENV.QUIZ.id") || parseQuizIdFromUrl();
    const assignmentId = getCanvasEnvValue("ENV.QUIZ.assignment_id") || getCanvasEnvValue("ENV.CONDITIONAL_RELEASE_ENV.asssignment.id") || null;
    const instanceUrl = window.location.origin;
    try {
      const response = await fetch(`${INVIGILATOR_APP_URL}/api/lti/exam-config-check`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "ngrok-skip-browser-warning": "true" },
        body: JSON.stringify({ courseId, quizId, assignmentId, instanceUrl, platformType: "canvas" }),
        signal: AbortSignal.timeout(5000),
      });
      if (response.ok) {
        const data = await response.json();
        if (data.success) {
          if (data.configured) {
            quizNotConfigured = false; markQuizAsInvigilated(); cacheInvigilationStatus(true); checkQuizReadiness();
          } else {
            quizNotConfigured = true; clearQuizInvigilatedMark(); cacheInvigilationStatus(false); checkQuizReadiness();
          }
          return;
        }
      }
      quizNotConfigured = true; clearQuizInvigilatedMark(); cacheInvigilationStatus(false); checkQuizReadiness();
    } catch (error) {
      quizNotConfigured = true; clearQuizInvigilatedMark(); cacheInvigilationStatus(false); checkQuizReadiness();
    }
  }

  function observeQuizContentChanges() {
    let debounceTimer;
    const mo = new MutationObserver(() => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => { if (isQuizInfoPage() || isQuizTakePage()) checkQuizReadiness(); }, 500);
    });
    const contentArea = document.getElementById("content") || document.getElementById("main") || document.body;
    mo.observe(contentArea, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "style", "disabled"] });
  }

  document.addEventListener("DOMContentLoaded", initIfQuiz);
  (function () {
    const pushState = history.pushState;
    history.pushState = function () { pushState.apply(this, arguments); setTimeout(initIfQuiz, 250); };
    window.addEventListener("popstate", function () { setTimeout(initIfQuiz, 250); });
  })();
  if (document.readyState === "complete" || document.readyState === "interactive") {
    setTimeout(initIfQuiz, 50);
  }

  window.addEventListener("message", async (event) => {
    if (event.data?.type === "lti.not_configured") {
      quizNotConfigured = true; stopPermissionPolling(); removeBlockingOverlay(); removeBannerIfPresent(); removeBodyPadding(); checkQuizReadiness(); return;
    }
    if (event.data?.type === "lti.exam.started") {
      quizNotConfigured = false; removeBlockingOverlay(); stopPermissionPolling(); clearQuizInvigilatedMark(); return;
    }
    if (event.data?.type === "INVIGILATOR_AGENT_STATUS") {
      agentConnected = event.data.connected === true; checkQuizReadiness(); return;
    }
    if (event.data?.type === "INVIGILATOR_AGENT_CONNECT_REQUEST") {
      const iframe = document.getElementById(IFRAME_ID);
      const target = event.source || (iframe && iframe.contentWindow);
      if (!target) return;
      loadSocketIOThenConnect(target); return;
    }
    if (event.data?.type === "INVIGILATOR_QUIZ_READINESS") {
      permissionsGranted = event.data.permissionsGranted === true; agentConnected = event.data.agentConnected === true; checkQuizReadiness(); return;
    }
    if (event.data?.type !== "INVIGILATOR_PERMISSION_REQUEST") return;
    const messageId = event.data.messageId;
    const sourceWindow = event.source;
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error("Camera/microphone not supported in this browser");
      const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
      stream.getTracks().forEach((track) => track.stop());
      permissionsGranted = true; removePermissionPrompt(); checkQuizReadiness();
      sourceWindow.postMessage({ type: "INVIGILATOR_PERMISSION_RESPONSE", messageId, success: true }, "*");
    } catch (error) {
      permissionsGranted = false; checkQuizReadiness();
      let errorMessage = "Camera/microphone access denied or unavailable";
      if (error.name === "NotAllowedError") errorMessage = "You denied permission. Please allow camera and microphone access.";
      else if (error.name === "NotFoundError") errorMessage = "No camera or microphone found on this device.";
      else if (error.name === "NotReadableError") errorMessage = "Camera/microphone is already in use by another application.";
      else if (error.name === "SecurityError") errorMessage = "Permission denied due to security restrictions.";
      sourceWindow.postMessage({ type: "INVIGILATOR_PERMISSION_RESPONSE", messageId, success: false, error: errorMessage }, "*");
    }
  });
})();


/* ── 2. SGEG EDUCATION COACH BADGE ──────────────────────────────────────── */
(function () {
  'use strict';

  var HOST      = 'https://savinggraceeducationaicoach.co.za';
  var NAVY      = '#0A2240';
  var TEAL      = '#007A87';
  var ORANGE    = '#F59E0B';
  var PANEL_W   = 400;
  var PANEL_H   = 580;
  var BADGE_SZ  = 56;
  var FOUND_SZ  = 68;
  var ANIM_MS   = 280;

  var ENV         = window.ENV || {};
  var currentUser = ENV.current_user || {};
  var userId      = String(currentUser.id || '');
  var userName    = encodeURIComponent(currentUser.display_name || currentUser.name || 'Student');
  var courseId    = String((ENV.course && ENV.course.id) || '');
  var courseName  = encodeURIComponent((ENV.course && ENV.course.name) || '');

  if (!userId) return;
  if (/CanvasStudentApp|CanvasTeacherApp/i.test(navigator.userAgent)) return;
  // Don't show badge on quiz/assignment pages — Invigilator handles those
  if (/\/assignments\/\d+|\/quizzes\/\d+/i.test(location.pathname)) return;

  function isFoundation() {
    var ctx = (ENV.context_asset_string || '') + (document.title || '');
    return /grade[\s\-_]*r\b|grade[\s\-_]*[1-3]\b/i.test(ctx);
  }

  var panelOpen = sessionStorage.getItem('aiBuddyOpen') === '1';
  var panelEl   = null;
  var iframeEl  = null;

  function badgeSVG(sz) {
    var ic = Math.round(sz * 0.54);
    return (
      '<svg width="' + ic + '" height="' + ic + '" viewBox="0 0 44 44" fill="none" aria-hidden="true">' +
        '<rect x="8" y="10" width="28" height="6" rx="3" fill="' + TEAL + '"/>' +
        '<circle cx="16" cy="24" r="3" fill="' + ORANGE + '"/>' +
        '<circle cx="28" cy="24" r="3" fill="' + ORANGE + '"/>' +
        '<path d="M14 32 Q22 37 30 32" stroke="white" stroke-width="2.5" fill="none" stroke-linecap="round"/>' +
      '</svg>'
    );
  }

  function injectStyles() {
    if (document.getElementById('ab-styles')) return;
    var s = document.createElement('style');
    s.id = 'ab-styles';
    s.textContent = [
      '@keyframes abIn{from{transform:scale(0) rotate(-180deg);opacity:0}to{transform:scale(1) rotate(0);opacity:1}}',
      '@keyframes abPulse{0%,100%{box-shadow:0 4px 18px rgba(10,34,64,.5)}50%{box-shadow:0 4px 26px rgba(0,122,135,.6)}}',
      '@keyframes abDot{0%,100%{transform:scale(1)}50%{transform:scale(1.3)}}',
      '@keyframes abPanelIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}',
      '@keyframes abPanelOut{from{transform:translateX(0);opacity:1}to{transform:translateX(100%);opacity:0}}',
      '#ab-badge{position:fixed;bottom:24px;right:24px;z-index:9002;border:none;padding:0;cursor:pointer;border-radius:50%;background:' + NAVY + ';box-shadow:0 4px 18px rgba(10,34,64,.45);transition:transform .15s ease,box-shadow .15s ease;animation:abIn .45s cubic-bezier(.34,1.56,.64,1) both,abPulse 4s ease-in-out 1s infinite;}',
      '#ab-badge:hover{transform:scale(1.1)!important;box-shadow:0 6px 26px rgba(0,122,135,.55)!important;}',
      '#ab-badge:focus-visible{outline:3px solid ' + TEAL + ';outline-offset:3px;}',
      '#ab-dot{position:absolute;top:0;right:0;background:#EF4444;border:2.5px solid #fff;border-radius:50%;font:700 9px/1 -apple-system,sans-serif;color:#fff;display:none;align-items:center;justify-content:center;animation:abDot 1.2s ease-in-out infinite;min-width:18px;height:18px;padding:0 3px;}',
      '#ab-dot.visible{display:flex;}',
      '#ab-panel{position:fixed;right:0;z-index:9001;background:#fff;border-left:1.5px solid #B8D8DC;box-shadow:-4px 0 32px rgba(10,34,64,.16);display:none;flex-direction:column;overflow:hidden;}',
      '#ab-panel-head{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;flex-shrink:0;background:' + NAVY + ';color:#fff;}',
      '#ab-panel-head-inner{display:flex;align-items:center;gap:9px;}',
      '#ab-panel-head span{font-size:.88rem;font-weight:700;letter-spacing:-.1px;}',
      '#ab-close{background:none;border:none;color:rgba(255,255,255,.8);font-size:1.2rem;cursor:pointer;line-height:1;padding:2px 6px;border-radius:6px;transition:background .12s;}',
      '#ab-close:hover{background:rgba(255,255,255,.18);}',
      '#ab-iframe{flex:1;border:none;width:100%;display:block;}',
      '#ab-loading{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#E8F4F6;gap:12px;font-size:.85rem;color:' + NAVY + ';pointer-events:none;}',
      '@media(max-width:768px){#ab-panel{width:100%!important;right:0;left:0;bottom:0!important;height:100dvh!important;border-radius:0;border-left:none;}#ab-badge{bottom:80px;right:16px;}}',
      '.back-to-top{bottom:90px!important;}',
    ].join('');
    document.head.appendChild(s);
  }

  function buildPanel() {
    if (document.getElementById('ab-panel')) {
      panelEl  = document.getElementById('ab-panel');
      iframeEl = document.getElementById('ab-iframe');
      return;
    }
    var foundation = isFoundation();
    var w      = Math.min(PANEL_W, window.innerWidth);
    var h      = foundation ? window.innerHeight : PANEL_H;
    var bottom = foundation ? 0 : 24;
    panelEl = document.createElement('div');
    panelEl.id = 'ab-panel';
    panelEl.setAttribute('role', 'dialog');
    panelEl.setAttribute('aria-label', 'Education Coach');
    panelEl.style.cssText = 'width:' + w + 'px;height:' + h + 'px;bottom:' + bottom + 'px;top:' + (foundation ? '0' : 'auto');
    var head = document.createElement('div');
    head.id = 'ab-panel-head';
    head.innerHTML =
      '<div id="ab-panel-head-inner">' +
        '<svg width="26" height="26" viewBox="0 0 44 44" fill="none" aria-hidden="true">' +
          '<circle cx="22" cy="22" r="21" fill="white" fill-opacity=".12"/>' +
          '<rect x="8" y="10" width="28" height="6" rx="3" fill="' + TEAL + '"/>' +
          '<circle cx="16" cy="24" r="3" fill="' + ORANGE + '"/>' +
          '<circle cx="28" cy="24" r="3" fill="' + ORANGE + '"/>' +
          '<path d="M14 32 Q22 37 30 32" stroke="white" stroke-width="2.5" fill="none" stroke-linecap="round"/>' +
        '</svg>' +
        '<span>Education Coach</span>' +
      '</div>' +
      '<button id="ab-close" aria-label="Close Education Coach" title="Close">&#x2715;</button>';
    panelEl.appendChild(head);
    var loading = document.createElement('div');
    loading.id = 'ab-loading';
    loading.innerHTML =
      '<svg width="44" height="44" viewBox="0 0 44 44" fill="none">' +
        '<circle cx="22" cy="22" r="21" fill="#E8F4F6"/>' +
        '<rect x="8" y="10" width="28" height="6" rx="3" fill="' + TEAL + '"/>' +
        '<circle cx="16" cy="24" r="3" fill="' + NAVY + '"/>' +
        '<circle cx="28" cy="24" r="3" fill="' + NAVY + '"/>' +
        '<path d="M14 32 Q22 37 30 32" stroke="' + NAVY + '" stroke-width="2.5" fill="none" stroke-linecap="round"/>' +
      '</svg>' +
      '<span>Loading…</span>';
    panelEl.appendChild(loading);
    iframeEl = document.createElement('iframe');
    iframeEl.id    = 'ab-iframe';
    iframeEl.title = 'Education Coach chat';
    iframeEl.allow = 'clipboard-write; popups';
    iframeEl.style.opacity = '0';
    iframeEl.src = HOST + '/widget/launch?user_id=' + userId +
                   '&user_name=' + userName +
                   '&course_id=' + courseId +
                   '&course_name=' + courseName;
    panelEl.appendChild(iframeEl);
    iframeEl.addEventListener('load', function () { iframeEl.style.opacity = '1'; loading.style.display = 'none'; });
    head.querySelector('#ab-close').addEventListener('click', closePanel);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && panelOpen) closePanel(); });
    document.body.appendChild(panelEl);
  }

  function openPanel() {
    buildPanel();
    panelEl.style.display   = 'flex';
    panelEl.style.animation = 'abPanelIn ' + ANIM_MS + 'ms ease both';
    panelOpen = true;
    sessionStorage.setItem('aiBuddyOpen', '1');
    clearUnread();
    var badge = document.getElementById('ab-badge');
    if (badge) badge.style.display = 'none';
    setTimeout(function () { var btn = document.getElementById('ab-close'); if (btn) btn.focus(); }, ANIM_MS);
  }

  function closePanel() {
    if (!panelEl) return;
    panelEl.style.animation = 'abPanelOut ' + ANIM_MS + 'ms ease both';
    setTimeout(function () { if (panelEl) panelEl.style.display = 'none'; }, ANIM_MS);
    panelOpen = false;
    sessionStorage.setItem('aiBuddyOpen', '0');
    var badge = document.getElementById('ab-badge');
    if (badge) { badge.style.display = ''; badge.focus(); }
  }

  function togglePanel() { if (panelOpen) closePanel(); else openPanel(); }

  function clearUnread() {
    var dot = document.getElementById('ab-dot');
    if (dot) { dot.classList.remove('visible'); dot.textContent = ''; }
  }

  function showUnread(n) {
    if (panelOpen) return;
    var dot = document.getElementById('ab-dot');
    if (!dot) return;
    dot.textContent = n > 9 ? '9+' : String(n || '');
    dot.classList.add('visible');
  }

  window.addEventListener('message', function (e) {
    if (!e.data || typeof e.data !== 'object') return;
    if (e.data.type === 'AIBUDDY_UNREAD') showUnread(e.data.count);
    if (e.data.type === 'AIBUDDY_CLEAR')  clearUnread();
    // Open links from inside the chat iframe — window.open from Canvas context
    // is never blocked by browsers, unlike window.open from inside an iframe.
    if (e.data.type === 'AIBUDDY_OPEN_URL' && e.data.url) {
      var safeUrl = String(e.data.url);
      if (/^https:\/\//.test(safeUrl)) window.open(safeUrl, '_blank', 'noopener,noreferrer');
    }
  });

  function injectBadge() {
    if (document.getElementById('ab-badge')) return;
    injectStyles();
    var foundation = isFoundation();
    var size = foundation ? FOUND_SZ : BADGE_SZ;
    var badge = document.createElement('button');
    badge.id = 'ab-badge';
    badge.setAttribute('aria-label', 'Open Education Coach');
    badge.setAttribute('aria-expanded', String(panelOpen));
    badge.setAttribute('aria-haspopup', 'dialog');
    badge.style.width  = size + 'px';
    badge.style.height = size + 'px';
    badge.innerHTML = badgeSVG(size) + '<span id="ab-dot"></span>';
    badge.addEventListener('click', function () { badge.setAttribute('aria-expanded', String(!panelOpen)); togglePanel(); });
    document.body.appendChild(badge);
    if (panelOpen) openPanel();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectBadge);
  } else {
    injectBadge();
  }
  window.addEventListener('load', function () { if (!document.getElementById('ab-badge')) injectBadge(); });

})();
