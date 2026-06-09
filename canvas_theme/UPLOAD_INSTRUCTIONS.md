# Canvas Theme Upload Instructions

## Files in this directory

| File | Purpose |
|------|---------|
| `ai_buddy_badge.js` | Education Coach floating button (standalone JS) |
| `ai_buddy_badge.css` | Education Coach panel styles (standalone CSS) |

---

## How to upload

### Option A — You have NO existing Canvas theme JS/CSS

Upload `ai_buddy_badge.js` directly as your Custom JS and `ai_buddy_badge.css` as your Custom CSS.

### Option B — You have existing JS (e.g. Invigilator / themeInject.js)

1. Open your current Canvas theme JS file (the Invigilator `themeInject.js`).
2. Scroll to the very bottom of that file.
3. Add a blank line, then paste the entire contents of `ai_buddy_badge.js` below it.
4. Upload the combined file.

The two scripts are fully isolated (separate IIFEs, no shared variables). Education Coach skips quiz/assignment pages so there is zero conflict with Invigilator.

### For CSS

Same approach: open your existing Canvas CSS, scroll to the bottom, paste `ai_buddy_badge.css` contents below it, upload.

---

## Canvas upload steps

1. Go to **Admin → [Your subaccount] → Themes**
2. Click **Edit** (or **Open in Theme Editor**)
3. Under **Custom JS**: upload the combined JS file
4. Under **Custom CSS**: upload the combined CSS file
5. Click **Preview** → verify the floating coach button appears
6. Click **Apply** to save

---

## After upload

- Reload any Canvas course page
- You should see a navy floating circle button (bottom-right corner)
- Click it to open the Education Coach side panel
- The panel fetches a session from the backend and loads the chat

**Note:** The backend must be running (ngrok or AWS) before the panel will load.
