/*
 * SGEG Education Coach — Floating chat badge for Canvas LMS
 *
 * WHERE TO UPLOAD:
 *   Canvas Admin > Sub-account > Themes > [active theme] > Custom JavaScript
 *
 * HOW IT WORKS:
 *   1. Reads Canvas window.ENV for the current user + course context.
 *   2. Injects a fixed floating badge (bottom-right, every Canvas page).
 *   3. Badge click opens our slide-out panel with the Education Coach iframe.
 *   4. Panel iframe loads /widget/launch which creates a server-side LTI session.
 *   5. Open/closed state survives Canvas SPA navigation via sessionStorage.
 *   6. Unread dot appears when the iframe posts AIBUDDY_UNREAD message.
 *   7. Foundation Phase (Grade R–3): larger badge (68px), full-height panel.
 *   8. Canvas mobile apps skip the badge and use the global-nav LTI item.
 */

(function () {
  'use strict';

  /* ── Config ────────────────────────────────────────────────────────────── */
  var HOST      = 'https://savinggraceeducationaicoach.co.za';
  var NAVY      = '#0A2240';   /* SGEG Navy */
  var TEAL      = '#007A87';   /* SGEG Teal */
  var ORANGE    = '#F59E0B';   /* SGEG Orange dot */
  var PANEL_W   = 400;
  var PANEL_H   = 580;
  var BADGE_SZ  = 56;
  var FOUND_SZ  = 68;
  var ANIM_MS   = 280;

  /* ── Canvas ENV ─────────────────────────────────────────────────────────── */
  var ENV         = window.ENV || {};
  var currentUser = ENV.current_user || {};
  var userId      = String(currentUser.id || '');
  var userName    = encodeURIComponent(currentUser.display_name || currentUser.name || 'Student');
  var courseId    = String((ENV.course && ENV.course.id) || '');
  var courseName  = encodeURIComponent((ENV.course && ENV.course.name) || '');

  /* ── Guards ─────────────────────────────────────────────────────────────── */
  if (!userId) return;
  if (/CanvasStudentApp|CanvasTeacherApp/i.test(navigator.userAgent)) return;
  if (/\/assignments\/\d+|\/quizzes\/\d+/i.test(location.pathname)) return;

  /* ── Phase detection ─────────────────────────────────────────────────────── */
  function isFoundation() {
    var ctx = (ENV.context_asset_string || '') + (document.title || '');
    return /grade[\s\-_]*r\b|grade[\s\-_]*[1-3]\b/i.test(ctx);
  }

  /* ── State ───────────────────────────────────────────────────────────────── */
  var panelOpen = sessionStorage.getItem('aiBuddyOpen') === '1';
  var panelEl   = null;
  var iframeEl  = null;

  /* ── Badge SVG (new SGEG-brand design) ──────────────────────────────────── */
  function badgeSVG(sz) {
    var ic = Math.round(sz * 0.54);
    return (
      '<svg width="' + ic + '" height="' + ic + '" viewBox="0 0 44 44" fill="none" aria-hidden="true">' +
        /* teal cap bar — references SGEG E-shape */
        '<rect x="8" y="10" width="28" height="6" rx="3" fill="' + TEAL + '"/>' +
        /* orange eyes — references SGEG logo dot */
        '<circle cx="16" cy="24" r="3" fill="' + ORANGE + '"/>' +
        '<circle cx="28" cy="24" r="3" fill="' + ORANGE + '"/>' +
        /* white smile */
        '<path d="M14 32 Q22 37 30 32" stroke="white" stroke-width="2.5" fill="none" stroke-linecap="round"/>' +
      '</svg>'
    );
  }

  /* ── Styles ──────────────────────────────────────────────────────────────── */
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

      '#ab-badge{position:fixed;bottom:24px;right:24px;z-index:9002;',
        'border:none;padding:0;cursor:pointer;border-radius:50%;',
        'background:' + NAVY + ';',
        'box-shadow:0 4px 18px rgba(10,34,64,.45);',
        'transition:transform .15s ease,box-shadow .15s ease;',
        'animation:abIn .45s cubic-bezier(.34,1.56,.64,1) both,abPulse 4s ease-in-out 1s infinite;}',
      '#ab-badge:hover{transform:scale(1.1)!important;box-shadow:0 6px 26px rgba(0,122,135,.55)!important;}',
      '#ab-badge:focus-visible{outline:3px solid ' + TEAL + ';outline-offset:3px;}',

      '#ab-dot{position:absolute;top:0;right:0;',
        'background:#EF4444;border:2.5px solid #fff;border-radius:50%;',
        'font:700 9px/1 -apple-system,sans-serif;color:#fff;',
        'display:none;align-items:center;justify-content:center;',
        'animation:abDot 1.2s ease-in-out infinite;min-width:18px;height:18px;padding:0 3px;}',
      '#ab-dot.visible{display:flex;}',

      '#ab-panel{position:fixed;right:0;z-index:9001;',
        'background:#fff;border-left:1.5px solid #B8D8DC;',
        'box-shadow:-4px 0 32px rgba(10,34,64,.16);',
        'display:none;flex-direction:column;overflow:hidden;}',

      '#ab-panel-head{display:flex;align-items:center;justify-content:space-between;',
        'padding:10px 14px;flex-shrink:0;background:' + NAVY + ';color:#fff;}',
      '#ab-panel-head-inner{display:flex;align-items:center;gap:9px;}',
      '#ab-panel-head span{font-size:.88rem;font-weight:700;letter-spacing:-.1px;}',

      '#ab-close{background:none;border:none;color:rgba(255,255,255,.8);font-size:1.2rem;',
        'cursor:pointer;line-height:1;padding:2px 6px;border-radius:6px;transition:background .12s;}',
      '#ab-close:hover{background:rgba(255,255,255,.18);}',

      '#ab-iframe{flex:1;border:none;width:100%;display:block;}',

      '#ab-loading{position:absolute;inset:0;display:flex;flex-direction:column;',
        'align-items:center;justify-content:center;background:#E8F4F6;',
        'gap:12px;font-size:.85rem;color:' + NAVY + ';pointer-events:none;}',

      '@media(max-width:768px){',
        '#ab-panel{width:100%!important;right:0;left:0;bottom:0!important;height:100dvh!important;border-radius:0;border-left:none;}',
        '#ab-badge{bottom:80px;right:16px;}',
      '}',

      '.back-to-top{bottom:90px!important;}',
    ].join('');
    document.head.appendChild(s);
  }

  /* ── Build panel ─────────────────────────────────────────────────────────── */
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
    panelEl.style.cssText = 'width:' + w + 'px;height:' + h + 'px;bottom:' + bottom + 'px;' +
                            'top:' + (foundation ? '0' : 'auto');

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

    iframeEl.addEventListener('load', function () {
      iframeEl.style.opacity = '1';
      loading.style.display  = 'none';
    });

    head.querySelector('#ab-close').addEventListener('click', closePanel);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && panelOpen) closePanel();
    });

    document.body.appendChild(panelEl);
  }

  /* ── Open / close ────────────────────────────────────────────────────────── */
  function openPanel() {
    buildPanel();
    panelEl.style.display   = 'flex';
    panelEl.style.animation = 'abPanelIn ' + ANIM_MS + 'ms ease both';
    panelOpen = true;
    sessionStorage.setItem('aiBuddyOpen', '1');
    clearUnread();
    // Hide badge while panel is open — panel header has its own close button
    var badge = document.getElementById('ab-badge');
    if (badge) badge.style.display = 'none';
    setTimeout(function () {
      var btn = document.getElementById('ab-close');
      if (btn) btn.focus();
    }, ANIM_MS);
  }

  function closePanel() {
    if (!panelEl) return;
    panelEl.style.animation = 'abPanelOut ' + ANIM_MS + 'ms ease both';
    setTimeout(function () { if (panelEl) panelEl.style.display = 'none'; }, ANIM_MS);
    panelOpen = false;
    sessionStorage.setItem('aiBuddyOpen', '0');
    // Restore badge when panel closes
    var badge = document.getElementById('ab-badge');
    if (badge) { badge.style.display = ''; badge.focus(); }
  }

  function togglePanel() {
    if (panelOpen) closePanel(); else openPanel();
  }

  /* ── Unread dot ──────────────────────────────────────────────────────────── */
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
      window.open(e.data.url, '_blank', 'noopener,noreferrer');
    }
  });

  /* ── Inject badge ────────────────────────────────────────────────────────── */
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

    badge.addEventListener('click', function () {
      badge.setAttribute('aria-expanded', String(!panelOpen));
      togglePanel();
    });

    document.body.appendChild(badge);

    if (panelOpen) openPanel();
  }

  /* ── Boot ────────────────────────────────────────────────────────────────── */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectBadge);
  } else {
    injectBadge();
  }
  window.addEventListener('load', function () {
    if (!document.getElementById('ab-badge')) injectBadge();
  });

})();
