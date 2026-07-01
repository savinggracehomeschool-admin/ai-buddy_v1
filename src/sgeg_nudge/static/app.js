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
      icon.textContent = "🤖";
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
      <div class="msg-icon" aria-hidden="true">🤖</div>
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
    head.innerHTML = `<span class="comp-head-icon">📚</span><span class="comp-head-title">Your courses</span>`;
    card.appendChild(head);

    const grid = make("div", "comp-btn-grid");
    (c.courses || []).forEach(course => {
      const btn = document.createElement("button");
      btn.className = "comp-btn";
      btn.setAttribute("type", "button");
      btn.innerHTML = `<span class="btn-icon">📘</span><span>${escHtml(course.name || course.id)}</span>`;
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
      <span class="comp-head-icon">📊</span>
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
      <span class="comp-head-icon">📝</span>
      <span class="comp-head-title">${escHtml(c.title || "Assignments")}</span>`;
    card.appendChild(head);

    const items = c.items || [];
    if (!items.length) {
      const p = make("p", "comp-more");
      p.style.padding = "10px 13px";
      p.textContent = "Nothing here right now ✅";
      card.appendChild(p);
      return card;
    }

    const list = make("ul", "comp-assign-list");
    const STATUS_ICON = { submitted: "✅", upcoming: "🕐", overdue: "⚠️" };

    items.forEach(item => {
      const icon = STATUS_ICON[item.status] || "📄";
      const pts  = item.points_possible ? `${item.points_possible} pts` : "";
      const li   = document.createElement("li");
      const inner = `
        <span class="comp-assign-icon">${icon}</span>
        <span class="comp-assign-body">
          <span class="comp-assign-name">${escHtml(item.name || "")}</span>
          <span class="comp-assign-due">${escHtml(item.due_friendly || "")}</span>
        </span>
        ${pts ? `<span class="comp-assign-pts">${escHtml(pts)}</span>` : ""}
        ${item.url ? `<a class="comp-assign-open" href="${escHtml(item.url)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">Open ↗</a>` : '<span class="comp-assign-chevron">›</span>'}`;

      const row = make("div", `comp-assign-item ${item.status || ""}`);
      row.style.cursor = item.url ? "pointer" : "default";
      row.innerHTML = inner;
      if (item.url) {
        row.addEventListener("click", e => {
          if (e.target.closest("a")) return;
          window.parent.postMessage({ type: "AIBUDDY_OPEN_URL", url: item.url }, "*");
        });
      }
      li.appendChild(row);
      list.appendChild(li);
    });
    card.appendChild(list);
    return card;
  }

  // ── Module section (Phase 3: expand in-place) ──────────────────────────────
  function buildModuleSection(c) {
    const card = make("div", "comp-card");
    const head = make("div", "comp-head");
    head.innerHTML = `
      <span class="comp-head-icon">📁</span>
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

    const TYPE_ICON = { File:"📄", Assignment:"📝", Quiz:"📋", Page:"📖",
                        Discussion:"💬", ExternalUrl:"🔗", ExternalTool:"🛠" };

    // Phase 4: respect grade-level density from server (max_visible field)
    const maxVisible = c.max_visible || 8;
    const grid    = make("div", "comp-btn-grid");
    const visible = items.slice(0, maxVisible);
    const hidden  = items.slice(maxVisible);

    function addButton(item) {
      const icon = TYPE_ICON[item.type] || "📄";
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
    const TYPE_ICON = { File:"📄", Assignment:"📝", Quiz:"📋", Page:"📖",
                        Discussion:"💬", ExternalUrl:"🔗", ExternalTool:"🛠" };
    const card = make("div", "comp-card");
    const head = make("div", "comp-head");
    head.innerHTML = `<span class="comp-head-icon">🔍</span>
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
      const icon = item.icon || TYPE_ICON[item.type] || "📄";
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

  // ── Assignment detail drawer (Phase 3) ─────────────────────────────────────
  // Tapping an assignment card shows description inline (no Canvas redirect needed
  // for browsing). External link is still available via "Open in Canvas" button.
  function buildAssignmentDrawer(item) {
    const drawer = make("div", "assign-drawer");
    drawer.innerHTML = `
      <div class="assign-drawer-head">
        <span>${escHtml(item.name || "Assignment")}</span>
        <button class="drawer-close" aria-label="Close">✕</button>
      </div>
      <div class="assign-drawer-body">
        <p class="assign-meta">Due: ${escHtml(item.due_friendly || "No due date")}
          ${item.points_possible ? ` · ${escHtml(String(item.points_possible))} pts` : ""}</p>
        <p class="assign-status">Status: ${{submitted:"✅ Submitted", overdue:"⚠️ Overdue", upcoming:"🕐 Not yet submitted"}[item.status] || "—"}</p>
        ${item.url ? `<a class="assign-canvas-link" href="${escHtml(item.url)}" target="_blank" rel="noopener">Open in Canvas ↗</a>` : ""}
      </div>`;
    drawer.querySelector(".drawer-close").addEventListener("click", () => drawer.remove());
    return drawer;
  }

  // ── Module item drawer ─────────────────────────────────────────────────────
  // Opens when a student taps a module button — shows item details in-app.
  // "Open in Canvas" button is the explicit exit point, never the default action.
  function buildItemDrawer(item, moduleName) {
    const TYPE_LABEL = {
      File: "File / Video", Assignment: "Assignment", Quiz: "Quiz",
      Page: "Page", Discussion: "Discussion",
      ExternalUrl: "External link", ExternalTool: "External tool",
    };
    const TYPE_ICON = {
      File:"📄", Assignment:"📝", Quiz:"📋", Page:"📖",
      Discussion:"💬", ExternalUrl:"🔗", ExternalTool:"🛠",
    };

    const typeLabel = TYPE_LABEL[item.type] || item.type || "Item";
    const icon      = TYPE_ICON[item.type]  || "📄";

    const drawer = make("div", "item-drawer");
    drawer.innerHTML = `
      <div class="item-drawer-head">
        <span class="item-drawer-title">${icon} ${escHtml(item.title || "Item")}</span>
        <button class="drawer-close" aria-label="Close">✕</button>
      </div>
      <div class="item-drawer-body">
        <p class="item-drawer-meta">Type: ${escHtml(typeLabel)}</p>
        ${moduleName ? `<p class="item-drawer-meta">Module: ${escHtml(moduleName)}</p>` : ""}
        ${item.url
          ? `<a class="item-open-btn" href="${escHtml(item.url)}" target="_blank" rel="noopener noreferrer">
               Open in Canvas ↗
             </a>`
          : `<p class="item-drawer-meta" style="color:#B91C1C">No Canvas link available for this item.</p>`
        }
      </div>`;

    drawer.querySelector(".drawer-close").addEventListener("click", () => drawer.remove());
    return drawer;
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
          ["📊 My grades",    "What are my grades?"],
          ["📝 Assignments",  "What assignments do I have coming up?"],
          ["📁 Course content","Show me the course modules and lessons."],
        ].forEach(([label, msg]) => {
          const btn = document.createElement("button");
          btn.className = "action-sheet-btn";
          btn.textContent = label;
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
          notice.textContent = "Your teacher has been notified and will follow up with you.";
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

  // Route all external link clicks through the parent Canvas frame.
  // target=_blank from inside a cross-origin iframe is blocked by Safari and
  // some Chrome popup-blocker configurations. Sending AIBUDDY_OPEN_URL lets
  // the top-level Canvas page call window.open() with no blocker friction.
  document.addEventListener("click", function(e) {
    const a = e.target.closest("a[href]");
    if (!a) return;
    const href = a.href;
    if (!href || !/^https?:\/\//.test(href)) return;
    e.preventDefault();
    window.parent.postMessage({ type: "AIBUDDY_OPEN_URL", url: href }, "*");
  }, false);


  modalConfirm.addEventListener("click", async () => {
    const msg = escalateMsg.value.trim();
    escalateModal.setAttribute("hidden", "");
    escalateMsg.value = "";
    chatIntro?.remove();
    quickActions.style.display = "none";
    try {
      const res  = await fetch(`/api/chat/escalate?session=${SESSION}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "other", message: msg }),
      });
      const data = await res.json();
      appendBubble("assistant", data.message || "Your teacher has been notified.");
    } catch {
      appendBubble("assistant", "Couldn't send that right now. Please try again.");
    }
    scrollToBottom();
  });

})();
