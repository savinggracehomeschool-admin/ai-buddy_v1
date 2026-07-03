/* AI Buddy — chat frontend */
(() => {

  // ── Session ────────────────────────────────────────────────────────────────
  const SESSION = new URLSearchParams(location.search).get("session") || "";
  if (!SESSION) {
    document.body.innerHTML =
      '<p style="padding:2rem;text-align:center;color:#6B7280">' +
      "No session found. Please relaunch AI Buddy from Canvas.</p>";
    return;
  }

  // ── DOM refs ───────────────────────────────────────────────────────────────
  const chatWindow    = document.getElementById("chat-window");
  const chatIntro     = document.getElementById("chat-intro");
  const introGreeting = document.getElementById("intro-greeting");
  const courseLabel   = document.getElementById("course-label");
  const gradeBadge    = document.getElementById("grade-badge");
  const input         = document.getElementById("message-input");
  const sendBtn       = document.getElementById("send-btn");
  const quickActions  = document.getElementById("quick-actions");
  const escalateModal = document.getElementById("escalate-modal");
  const modalCancel   = document.getElementById("modal-cancel");
  const modalConfirm  = document.getElementById("modal-confirm");
  const escalateMsg   = document.getElementById("escalate-msg");
  const btnEscalate   = document.getElementById("btn-escalate");

  const ICONS = {
    ai:          `<svg viewBox="0 0 44 44" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="22" cy="22" r="21" fill="#0A2240"/><rect x="8" y="10" width="28" height="6" rx="3" fill="#007A87"/><circle cx="16" cy="24" r="3" fill="#F59E0B"/><circle cx="28" cy="24" r="3" fill="#F59E0B"/><path d="M14 32 Q22 37 30 32" stroke="white" stroke-width="2.5" fill="none" stroke-linecap="round"/></svg>`,
    grades:      `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2.5" y="10" width="4" height="7" rx="1" fill="#007A87"/><rect x="8" y="6" width="4" height="11" rx="1" fill="#007A87"/><rect x="13.5" y="3" width="4" height="14" rx="1" fill="#0A2240"/></svg>`,
    assignments: `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="3" y="2" width="14" height="16" rx="2" stroke="#0A2240" stroke-width="1.3" fill="none"/><path d="M7 7h7M7 11h7M7 15h4" stroke="#0A2240" stroke-width="1.1" stroke-linecap="round"/><circle cx="5" cy="7" r="1" fill="#007A87"/><circle cx="5" cy="11" r="1" fill="#007A87"/><circle cx="5" cy="15" r="1" fill="#007A87"/></svg>`,
    folder:      `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M2 7a2 2 0 012-2h3.17a2 2 0 011.42.59l.82.82A2 2 0 0010.83 7H16a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V7z" fill="#007A87" opacity=".9"/></svg>`,
    search:      `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="8.5" cy="8.5" r="5.5" stroke="#0A2240" stroke-width="1.4"/><path d="M13.5 13.5L17 17" stroke="#0A2240" stroke-width="1.4" stroke-linecap="round"/></svg>`,
    books:       `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="4" width="8" height="12" rx="1.5" fill="#007A87"/><rect x="7" y="3" width="9" height="13" rx="1.5" fill="#0A2240"/><path d="M10 6h4M10 9h4M10 12h3" stroke="white" stroke-width="1" stroke-linecap="round"/></svg>`,
    warning:     `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9.104 3.5L2.5 15.5h15L10.896 3.5a1 1 0 00-1.792 0z" fill="#EF4444"/><path d="M10 8v3.5M10 13.5h.01" stroke="white" stroke-width="1.5" stroke-linecap="round"/></svg>`,
    clock:       `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="10" cy="10" r="7.5" stroke="#F59E0B" stroke-width="1.5"/><path d="M10 5.5v5l3 2" stroke="#F59E0B" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
    check:       `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="10" cy="10" r="8" fill="#10B981"/><path d="M6 10l3 3 5-5.5" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
    doc:         `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M5 2h7l4 4v12a1 1 0 01-1 1H5a1 1 0 01-1-1V3a1 1 0 011-1z" stroke="#6B7280" stroke-width="1.3" fill="none"/><path d="M12 2v4h4" stroke="#6B7280" stroke-width="1.3"/><path d="M7 9h6M7 12h6M7 15h4" stroke="#6B7280" stroke-width="1.2" stroke-linecap="round"/></svg>`,
    page:        `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="4" y="2" width="12" height="16" rx="1.5" stroke="#0A2240" stroke-width="1.3" fill="none"/><path d="M7 7h6M7 10h6M7 13h4" stroke="#0A2240" stroke-width="1.1" stroke-linecap="round"/></svg>`,
    quiz:        `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="3" y="2" width="14" height="16" rx="2" stroke="#0A2240" stroke-width="1.3" fill="none"/><path d="M8 7.5a2 2 0 114 0c0 1.5-2 2-2 3.5" stroke="#0A2240" stroke-width="1.3" stroke-linecap="round"/><circle cx="10" cy="14.5" r=".75" fill="#0A2240"/></svg>`,
    chat:        `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M3 4h14a1 1 0 011 1v8a1 1 0 01-1 1H6l-4 3V5a1 1 0 011-1z" stroke="#0A2240" stroke-width="1.3" fill="none"/></svg>`,
    link:        `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 4H5a2 2 0 00-2 2v9a2 2 0 002 2h9a2 2 0 002-2v-4" stroke="#0A2240" stroke-width="1.3" stroke-linecap="round"/><path d="M13 3h4v4M11 9l6-6" stroke="#0A2240" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
    tool:        `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M14.5 2.5a3.5 3.5 0 00-3.36 4.43L3.5 14.5a1 1 0 000 1.41l.59.59a1 1 0 001.41 0l7.57-7.64A3.5 3.5 0 1014.5 2.5z" stroke="#0A2240" stroke-width="1.3" fill="none"/></svg>`,
    raise:       `<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M10 2v7M7 4v5M4 6v3M13 4v5M16 6v3" stroke="#B91C1C" stroke-width="1.4" stroke-linecap="round"/><rect x="3" y="11" width="14" height="4" rx="1.5" fill="#B91C1C"/></svg>`,
  };

  let isWaiting    = false;
  let userInitials = "?";

  function getInitials(name) {
    const parts = (name || "").trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return "?";
    if (parts.length === 1) return parts[0][0].toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  // ── Panel mode (loaded inside badge iframe) ────────────────────────────────
  // When panel=1 is in the URL, hide the internal header — the badge provides its own.
  if (new URLSearchParams(location.search).get("panel") === "1") {
    document.body.classList.add("panel-mode");
  }

  // ── Boot ───────────────────────────────────────────────────────────────────
  loadSession();
  loadAlerts();
  input.focus();

  // ── Session info ───────────────────────────────────────────────────────────
  async function loadSession() {
    try {
      const res = await fetch(`/api/chat/session?session=${SESSION}`);
      if (!res.ok) return;
      const info = await res.json();
      const firstName = (info.user_name || "").split(" ")[0] || "there";
      userInitials = getInitials(info.user_name);

      courseLabel.textContent   = info.course_title || "Canvas";
      gradeBadge.textContent    = info.grade_label  || "";
      introGreeting.textContent = `Hi ${firstName}! I'm your Education Coach`;

      const g = info.grade_level;
      if (g !== null && g !== undefined) {
        if      (g <= 3) document.body.classList.add("grade-foundation");
        else if (g <= 7) document.body.classList.add("grade-intermediate");
        else             document.body.classList.add("grade-senior");
      }
    } catch { /* silent */ }
  }

  // ── Missing assignments alert ──────────────────────────────────────────────
  const COACH_AVATAR_SM = `<svg viewBox="0 0 44 44" fill="none" xmlns="http://www.w3.org/2000/svg">
    <circle cx="22" cy="22" r="21" fill="#0A2240"/>
    <rect x="8" y="10" width="28" height="6" rx="3" fill="#007A87"/>
    <circle cx="16" cy="24" r="3" fill="#F59E0B"/>
    <circle cx="28" cy="24" r="3" fill="#F59E0B"/>
    <path d="M14 32 Q22 37 30 32" stroke="white" stroke-width="2.5" fill="none" stroke-linecap="round"/>
  </svg>`;

  async function loadAlerts() {
    if (sessionStorage.getItem("alerts_dismissed")) return;
    try {
      const res = await fetch(`/api/chat/alerts?session=${SESSION}`);
      if (!res.ok) return;
      const data = await res.json();
      if (data.count > 0) showMissingAlert(data.missing, data.count);
    } catch { /* silent */ }
  }

  function showMissingAlert(items, count) {
    if (document.getElementById("missing-alert")) return;
    const el = document.createElement("div");
    el.className = "missing-alert";
    el.id = "missing-alert";

    const listItems = items.map(i =>
      `<li>
        <a href="${escHtml(i.url || "#")}" target="_blank" rel="noopener">${escHtml(i.name)}</a>
        ${i.due_friendly ? `<span class="missing-due">${escHtml(i.due_friendly)}</span>` : ""}
      </li>`
    ).join("");

    el.innerHTML = `
      <div class="missing-alert-avatar">${COACH_AVATAR_SM}</div>
      <div class="missing-alert-body">
        <strong>You have ${count} unsubmitted assignment${count !== 1 ? "s" : ""}</strong>
        <ul class="missing-alert-list">${listItems}</ul>
      </div>
      <button class="missing-alert-close" aria-label="Dismiss">&times;</button>
    `;

    el.querySelector(".missing-alert-close").addEventListener("click", () => {
      el.remove();
      sessionStorage.setItem("alerts_dismissed", "1");
    });

    chatWindow.insertBefore(el, chatIntro);
  }

  // ── Safe HTML ──────────────────────────────────────────────────────────────
  function escHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ── Render assistant text ─────────────────────────────────────────────────
  function renderText(raw) {
    let s = escHtml(raw);
    s = s.replace(/\*\*([^*\n]{1,120})\*\*/g, "<strong>$1</strong>");
    // Make URLs clickable, opening in parent frame (not inside the panel iframe)
    s = s.replace(/(https?:\/\/[^\s<"]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/\n/g, "<br>");
    return s;
  }

  // ── Append message bubble ──────────────────────────────────────────────────
  function appendBubble(role, text, escalated = false) {
    const row    = document.createElement("div");
    row.className = `msg-row ${role}${escalated ? " escalated" : ""}`;

    const icon   = document.createElement("div");
    icon.setAttribute("aria-hidden", "true");
    if (role === "user") {
      icon.className = "msg-icon msg-icon-user";
      icon.textContent = userInitials;
    } else {
      icon.className = "msg-icon msg-icon-ai";
      icon.innerHTML = ICONS.ai;
    }

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML  = role === "assistant"
      ? renderText(text)
      : escHtml(text).replace(/\n/g, "<br>");

    row.append(icon, bubble);
    chatWindow.appendChild(row);
    return row;
  }

  // ── Typing indicator ───────────────────────────────────────────────────────
  function showTyping() {
    const row = document.createElement("div");
    row.className = "msg-row assistant";
    row.id = "typing-indicator";
    row.innerHTML = `
      <div class="msg-icon msg-icon-ai" aria-hidden="true">${ICONS.ai}</div>
      <div class="typing-bubble" aria-label="AI Buddy is thinking">
        <div class="typing-dots"><span></span><span></span><span></span></div>
      </div>`;
    chatWindow.appendChild(row);
    scrollToBottom();
  }
  function removeTyping() { document.getElementById("typing-indicator")?.remove(); }
  function scrollToBottom() { chatWindow.scrollTop = chatWindow.scrollHeight; }

  // ══════════════════════════════════════════════════════════════════════════
  //  Rich component renderer
  //  Types: grades_card | assignment_list | module_section
  // ══════════════════════════════════════════════════════════════════════════

  function renderComponents(components) {
    if (!components?.length) return null;
    const block = document.createElement("div");
    block.className = "comp-block";
    components.forEach(c => {
      let el = null;
      if      (c.type === "grades_card")           el = buildGradesCard(c);
      else if (c.type === "assignment_card")        el = buildAssignmentCard(c);
      else if (c.type === "assignment_list")        el = buildAssignmentList(c);
      else if (c.type === "module_section")         el = buildModuleSection(c);
      else if (c.type === "course_picker")          el = buildCoursePicker(c);
      else if (c.type === "content_search_results") el = buildContentSearchResults(c);
      if (el) block.appendChild(el);
    });
    return block.childElementCount ? block : null;
  }

  // ── Course picker ─────────────────────────────────────────────────────────
  // Shown when AI Buddy is launched without a course context (e.g. from the
  // Canvas dashboard). Tapping a course sends a message that sets the context.
  function buildCoursePicker(c) {
    const card = make("div", "comp-card");
    const head = make("div", "comp-head");
    head.innerHTML = `<span class="comp-head-icon">${ICONS.books}</span><span class="comp-head-title">Your courses</span>`;
    card.appendChild(head);

    const grid = make("div", "comp-btn-grid");
    (c.courses || []).forEach(course => {
      const btn = document.createElement("button");
      btn.className = "comp-btn";
      btn.setAttribute("type", "button");
      btn.innerHTML = `<span class="btn-icon">${ICONS.books}</span><span>${escHtml(course.name || course.id)}</span>`;
      btn.addEventListener("click", () => {
        // Send a follow-up message that includes the chosen course name —
        // the router / Claude will resolve it and set context
        sendMessage(`Show me content for ${course.name || 'course ' + course.id}`);
      });
      grid.appendChild(btn);
    });

    card.appendChild(grid);
    return card;
  }

  // ── Grades card ────────────────────────────────────────────────────────────
  function buildGradesCard(c) {
    const score = c.current_score != null ? `${c.current_score}%` : null;
    const cls   = scoreClass(c.current_score);
    const card  = make("div", "comp-card");

    const head = make("div", "comp-head");
    head.innerHTML = `
      <span class="comp-head-icon">${ICONS.grades}</span>
      <span class="comp-head-title">${escHtml(c.course_name || "Course")}</span>
      ${c.course_url ? `<a class="comp-head-link" href="${escHtml(c.course_url)}" target="_blank" rel="noopener">View grades ↗</a>` : ""}`;
    card.appendChild(head);

    const body = make("div", "comp-grade-body");
    if (score) {
      const s = make("span", `comp-grade-score ${cls}`);
      s.textContent = score;
      body.appendChild(s);
      if (c.current_grade) {
        const b = make("span", `comp-grade-badge ${cls}`);
        b.textContent = c.current_grade;
        body.appendChild(b);
      }
    } else {
      const n = make("span", "comp-grade-none");
      n.textContent = "No mark recorded yet";
      body.appendChild(n);
    }
    card.appendChild(body);
    return card;
  }

  function scoreClass(score) {
    if (score == null) return "";
    if (score >= 80) return "high";
    if (score >= 50) return "mid";
    return "low";
  }

  // ── Assignment list ────────────────────────────────────────────────────────
  function buildAssignmentList(c) {
    const card  = make("div", "comp-card");
    const head  = make("div", "comp-head");
    head.innerHTML = `
      <span class="comp-head-icon">${ICONS.assignments}</span>
      <span class="comp-head-title">${escHtml(c.title || "Assignments")}</span>`;
    card.appendChild(head);

    const items = c.items || [];
    if (!items.length) {
      const p = make("p", "comp-more");
      p.style.padding = "10px 13px";
      p.textContent = "Nothing here right now";
      card.appendChild(p);
      return card;
    }

    const list = make("ul", "comp-assign-list");
    const STATUS_ICON = { submitted: ICONS.check, upcoming: ICONS.clock, overdue: ICONS.warning };

    items.forEach(item => {
      const icon = STATUS_ICON[item.status] || ICONS.doc;
      const pts  = item.points_possible ? `${item.points_possible} pts` : "";
      const li   = document.createElement("li");
      const inner = `
        <span class="comp-assign-icon">${icon}</span>
        <span class="comp-assign-body">
          <span class="comp-assign-name">${escHtml(item.name || "")}</span>
          <span class="comp-assign-due">${escHtml(item.due_friendly || "")}</span>
        </span>
        ${pts ? `<span class="comp-assign-pts">${escHtml(pts)}</span>` : ""}
        ${item.url ? `<a class="comp-assign-open" href="${escHtml(item.url)}" target="_blank" rel="noopener noreferrer">Open ↗</a>` : '<span class="comp-assign-chevron">›</span>'}`;

      const row = make("div", `comp-assign-item ${item.status || ""}`);
      row.style.cursor = item.url ? "pointer" : "default";
      row.innerHTML = inner;
      if (item.url) {
        row.addEventListener("click", e => {
          if (e.target.closest("a")) return;
          window.open(item.url, "_blank");
        });
      }
      li.appendChild(row);
      list.appendChild(li);
    });
    card.appendChild(list);
    return card;
  }

  // ── Single assignment card ─────────────────────────────────────────────────
  function buildAssignmentCard(c) {
    const STATUS_ICON  = { overdue: ICONS.warning, upcoming: ICONS.warning, floating: ICONS.clock, submitted: ICONS.check };
    const STATUS_LABEL = { overdue: "Overdue", upcoming: "Not yet submitted", floating: "Open — no due date", submitted: "Submitted" };
    const icon  = STATUS_ICON[c.status]  || ICONS.doc;
    const label = STATUS_LABEL[c.status] || "";
    const pts   = c.points_possible ? `${c.points_possible} pts` : "";

    const card = make("div", `asgn-card asgn-${c.status || "upcoming"}`);
    card.innerHTML = `
      <div class="asgn-top">
        <span class="asgn-icon">${icon}</span>
        <span class="asgn-name">${escHtml(c.name || "Assignment")}</span>
      </div>
      <div class="asgn-meta">
        ${label ? `<span class="asgn-status-label">${escHtml(label)}</span>` : ""}
        ${c.due_friendly ? `<span class="asgn-due">${escHtml(c.due_friendly)}</span>` : ""}
        ${pts ? `<span class="asgn-pts">${escHtml(pts)}</span>` : ""}
      </div>
      ${c.url ? `<a class="asgn-open" href="${escHtml(c.url)}" target="_blank" rel="noopener noreferrer">Open in Canvas ↗</a>` : ""}`;

    if (c.url) {
      card.style.cursor = "pointer";
      card.addEventListener("click", e => {
        if (e.target.closest("a")) return;
        window.open(c.url, "_blank");
      });
    }
    return card;
  }

  // ── Module section (Phase 3: expand in-place) ──────────────────────────────
  function buildModuleSection(c) {
    const card = make("div", "comp-card");
    const head = make("div", "comp-head");
    head.innerHTML = `
      <span class="comp-head-icon">${ICONS.folder}</span>
      <span class="comp-head-title">${escHtml(c.module_name || "Module")}</span>`;
    card.appendChild(head);

    const items = (c.items || []).filter(i => i.url);
    if (!items.length) {
      const p = make("p", "comp-more");
      p.style.padding = "10px 13px";
      p.textContent = "No items in this module yet.";
      card.appendChild(p);
      return card;
    }

    const TYPE_ICON = {
      File: ICONS.doc, Assignment: ICONS.assignments, Quiz: ICONS.quiz,
      Page: ICONS.page, Discussion: ICONS.chat, ExternalUrl: ICONS.link, ExternalTool: ICONS.tool,
    };

    // Phase 4: respect grade-level density from server (max_visible field)
    const maxVisible = c.max_visible || 8;
    const grid    = make("div", "comp-btn-grid");
    const visible = items.slice(0, maxVisible);
    const hidden  = items.slice(maxVisible);

    function addButton(item) {
      const icon = TYPE_ICON[item.type] || ICONS.doc;
      if (item.url) {
        const a = document.createElement("a");
        a.className = "comp-btn";
        a.href = item.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.innerHTML = `<span class="btn-icon">${icon}</span><span>${escHtml(item.title || "Item")}</span>`;
        grid.appendChild(a);
      } else {
        const btn = document.createElement("button");
        btn.className = "comp-btn";
        btn.setAttribute("type", "button");
        btn.innerHTML = `<span class="btn-icon">${icon}</span><span>${escHtml(item.title || "Item")}</span>`;
        grid.appendChild(btn);
      }
    }

    visible.forEach(addButton);

    // Phase 3: "+N more" is now a real button that expands in-place
    if (hidden.length > 0) {
      const expandBtn = make("button", "comp-expand-btn");
      expandBtn.textContent = `+${hidden.length} more`;
      expandBtn.addEventListener("click", () => {
        hidden.forEach(addButton);
        expandBtn.remove();
      });
      grid.appendChild(expandBtn);
    }

    card.appendChild(grid);
    return card;
  }

  // ── Content search results (knowledge base) ───────────────────────────────
  function buildContentSearchResults(c) {
    const TYPE_ICON = {
      File: ICONS.doc, Assignment: ICONS.assignments, Quiz: ICONS.quiz,
      Page: ICONS.page, Discussion: ICONS.chat, ExternalUrl: ICONS.link, ExternalTool: ICONS.tool,
    };
    const card = make("div", "comp-card");
    const head = make("div", "comp-head");
    head.innerHTML = `<span class="comp-head-icon">${ICONS.search}</span>
      <span class="comp-head-title">Results for "${escHtml(c.query || "")}"</span>`;
    card.appendChild(head);

    const items = (c.items || []).filter(i => i.url);
    if (!items.length) {
      const p = make("p", "comp-more");
      p.style.padding = "10px 13px";
      p.textContent = "No matching content found.";
      card.appendChild(p);
      return card;
    }

    const grid = make("div", "comp-btn-grid");
    items.forEach(item => {
      const icon = item.icon || TYPE_ICON[item.type] || ICONS.doc;
      const el = document.createElement(item.url ? "a" : "button");
      el.className = "comp-btn";
      if (item.url) {
        el.href = item.url;
        el.target = "_blank";
        el.rel = "noopener noreferrer";
      } else {
        el.setAttribute("type", "button");
      }
      el.innerHTML = `<span class="btn-icon">${icon}</span><span>${escHtml(item.title || "Item")}</span>`;
      grid.appendChild(el);
    });
    card.appendChild(grid);
    return card;
  }

  function make(tag, cls) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    return e;
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  Send message
  // ══════════════════════════════════════════════════════════════════════════

  async function sendMessage(text) {
    text = text.trim();
    if (!text || isWaiting) return;
    isWaiting = true;
    sendBtn.disabled = true;

    chatIntro?.remove();
    quickActions.style.display = "none";

    // Phase 4: show floating "+" button so quick actions are always accessible
    if (!document.getElementById("fab-actions")) {
      const fab = document.createElement("button");
      fab.id        = "fab-actions";
      fab.className = "fab-btn";
      fab.setAttribute("aria-label", "Quick actions");
      fab.textContent = "+";
      fab.addEventListener("click", () => {
        // Toggle a compact action sheet above the input bar
        let sheet = document.getElementById("action-sheet");
        if (sheet) { sheet.remove(); return; }
        sheet = document.createElement("div");
        sheet.id = "action-sheet";
        sheet.className = "action-sheet";
        [
          [`<span class="sheet-icon">${ICONS.grades}</span> My grades`,       "What are my grades?"],
          [`<span class="sheet-icon">${ICONS.assignments}</span> Assignments`, "What assignments do I have coming up?"],
          [`<span class="sheet-icon">${ICONS.folder}</span> Course content`,  "Show me the course modules and lessons."],
        ].forEach(([label, msg]) => {
          const btn = document.createElement("button");
          btn.className = "action-sheet-btn";
          btn.innerHTML = label;
          btn.addEventListener("click", () => { sheet.remove(); sendMessage(msg); });
          sheet.appendChild(btn);
        });
        document.querySelector(".input-bar").insertAdjacentElement("beforebegin", sheet);
      });
      document.body.appendChild(fab);
    }

    appendBubble("user", text);
    input.value = "";
    autoResize();
    scrollToBottom();
    showTyping();

    try {
      const res = await fetch(`/api/chat/message?session=${SESSION}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      removeTyping();

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        appendBubble("assistant", err.detail || "Something went wrong. Please try again.");
      } else {
        const data = await res.json();

        // Phase 4: sequential delivery — text bubble first, then component block
        appendBubble("assistant", data.reply || "", data.escalated);

        if (data.components?.length) {
          const compEl = renderComponents(data.components);
          if (compEl) {
            // Small delay so text lands before the card block (feels more natural)
            await new Promise(r => setTimeout(r, 220));
            chatWindow.appendChild(compEl);
          }
        }

        if (data.escalated) {
          const notice = make("div", "escalation-notice");
          notice.textContent = "A support ticket has been logged — the SGEG support team will follow up with you.";
          chatWindow.appendChild(notice);
        }
      }
    } catch {
      removeTyping();
      appendBubble("assistant", "I'm having trouble connecting right now. Please try again in a moment.");
    }

    scrollToBottom();
    isWaiting = false;
    sendBtn.disabled = false;
    input.focus();
  }

  // ── Input resize ───────────────────────────────────────────────────────────
  function autoResize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  }

  // ── Events ─────────────────────────────────────────────────────────────────
  input.addEventListener("input", () => {
    autoResize();
    sendBtn.disabled = !input.value.trim() || isWaiting;
  });

  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!sendBtn.disabled) sendMessage(input.value);
    }
  });

  sendBtn.addEventListener("click", () => sendMessage(input.value));

  quickActions.addEventListener("click", e => {
    const chip = e.target.closest(".chip[data-msg]");
    if (chip) sendMessage(chip.dataset.msg);
  });

  btnEscalate.addEventListener("click", () => {
    escalateModal.removeAttribute("hidden");
    escalateMsg.focus();
  });

  modalCancel.addEventListener("click", () => {
    escalateModal.setAttribute("hidden", "");
    escalateMsg.value = "";
  });

  escalateModal.addEventListener("click", e => {
    if (e.target === escalateModal) {
      escalateModal.setAttribute("hidden", "");
      escalateMsg.value = "";
    }
  });

  document.addEventListener("click", function(e) {
    var a = e.target.closest("a[href]");
    if (!a) return;
    var href = a.href;
    if (!href || !/^https:\/\//.test(href)) return;
    e.preventDefault();
    e.stopPropagation();
    window.open(href, "_blank");
  }, true);



  modalConfirm.addEventListener("click", async () => {
    const msg = escalateMsg.value.trim();
    escalateModal.setAttribute("hidden", "");
    escalateMsg.value = "";
    chatIntro?.remove();
    quickActions.style.display = "none";
    try {
      const lastPage = document.referrer || window.parent?.location?.href || window.location.href;
      const res  = await fetch(`/api/chat/escalate?session=${SESSION}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "other", message: msg, last_page: lastPage }),
      });
      const data = await res.json();
      appendBubble("assistant", data.message || "Your request has been logged with the SGEG support team.");
    } catch {
      appendBubble("assistant", "Couldn't send that right now. Please try again.");
    }
    scrollToBottom();
  });

})();
