/* ── Shared shell bootstrap + utilities for CIR BIDS tabs ────────────────────
   Extracted from index.html. Loaded once by the shell; every tab's own JS
   (mrBids, petBids, megBids, ...) relies on these globals. */

/* ── Shared utilities ── */
let authToken = null;

function addTokenToUrl(url) {
  if (!authToken) return url;
  const sep = url.includes('?') ? '&' : '?';
  return url + sep + 'token=' + encodeURIComponent(authToken);
}

function _handleAuthError() {
  alert('Authentication failed. Please refresh and enter a valid token.');
  window.location.href = '/';
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escAttr(s) {
  return esc(s).replace(/"/g, '&quot;');
}

/* ── Tab switching ── */
let _activeTabId = null;
let _registeredTabModules = {};

/* Tabs register themselves here (instead of the shell guessing global names)
   e.g. registerTabModule('mr-bids', mrBids) at the end of a tab's own JS. */
function registerTabModule(tabId, moduleObj) {
  _registeredTabModules[tabId] = moduleObj;
}

function switchTab(id) {
  _activeTabId = id;
  document.querySelectorAll('.tab-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.target === id)
  );
  document.querySelectorAll('[id^="tabwrap-"]').forEach(w => {
    w.style.display = (w.id === 'tabwrap-' + id) ? '' : 'none';
  });
  // Notify active tab module
  const mod = _registeredTabModules[id];
  if (mod && mod.onTabSwitch) mod.onTabSwitch(id);
}

/* Execute the <script> tags found in an injected tab-content fragment.
   Inline scripts run synchronously as soon as they're appended (unchanged
   behavior). External <script src="..."> tags are awaited via their load
   event so tabInit_<id>() is only called once the tab's own JS module has
   actually finished loading and registered itself. */
function _runFragmentScripts(wrap) {
  const waits = [];
  wrap.querySelectorAll('script').forEach(old => {
    const s = document.createElement('script');
    if (old.src) {
      s.src = old.src;
      s.async = false;
      waits.push(new Promise(resolve => {
        s.addEventListener('load', resolve);
        s.addEventListener('error', () => {
          console.warn('Failed to load tab script:', old.src);
          resolve();
        });
      }));
    } else {
      s.textContent = old.textContent;
    }
    document.head.appendChild(s);
  });
  return Promise.all(waits);
}

/* ── Bootstrap ── */
window.addEventListener('DOMContentLoaded', async () => {
  authToken = new URLSearchParams(window.location.search).get('token');

  const tabBar   = document.getElementById('tab-bar');
  const container = document.getElementById('tab-container');

  let tabs;
  try {
    const res = await fetch(addTokenToUrl('/api/tabs'));
    if (res.status === 401) { _handleAuthError(); return; }
    tabs = await res.json();
  } catch (e) {
    container.innerHTML =
      '<p class="warn">⚠ Failed to connect to server.</p>';
    return;
  }

  if (!tabs.length) {
    tabBar.innerHTML = '';
    container.innerHTML =
      '<p class="warn">⚠ No BIDS tabs are available for this project. ' +
      'Create <code>raw/mri</code> to show MR BIDS and <code>raw/pet</code> to show PET BIDS.</p>';
    return;
  }

  for (const [i, tab] of tabs.entries()) {
    // Tab button
    const btn = document.createElement('button');
    btn.className = 'tab-btn' + (i === 0 ? ' active' : '');
    btn.dataset.target = tab.id;
    btn.textContent = tab.label;
    btn.onclick = () => switchTab(tab.id);
    tabBar.appendChild(btn);

    const wrap = document.createElement('div');
    wrap.id = 'tabwrap-' + tab.id;
    wrap.style.display = (i === 0) ? '' : 'none';
    container.appendChild(wrap);

    if (tab.type === 'overview') {
      // Render as a full-height iframe preserving the file's own styles
      const iframe = document.createElement('iframe');
      iframe.src = addTokenToUrl('/overview-file');
      iframe.style.cssText =
        'display:block;width:100%;height:calc(100vh - 140px);border:none;background:#fff;border-radius:3px;';
      iframe.title = 'Project Overview';
      wrap.appendChild(iframe);
      continue;
    }

    // Load + inject tab fragment
    let html = '';
    try {
      const r = await fetch(addTokenToUrl('/tab-content?id=' + encodeURIComponent(tab.id)));
      if (r.ok) html = await r.text();
    } catch (_) {}

    wrap.innerHTML = html;

    // Execute inline/external scripts from the injected fragment, waiting
    // for any external <script src> to finish loading before continuing.
    await _runFragmentScripts(wrap);

    // Call tab's init function (module has had a chance to define it by now)
    const initFn = window['tabInit_' + tab.id.replace(/-/g, '_')];
    if (initFn) initFn(authToken);
  }

  if (tabs.length > 0) {
    _activeTabId = tabs[0].id;
  }
});
