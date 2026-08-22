/* ── State ─────────────────────────────────────────────────────── */
let pendingParams = null;   // params waiting for user confirmation
let isWaiting     = false;  // prevent double-sends

/* ── Init ──────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  loadConfig();
  checkSSHStatus();
  setInterval(checkSSHStatus, 30000);

  // Mobile sidebar overlay close
  document.addEventListener('click', e => {
    const sidebar = document.getElementById('sidebar');
    const menuBtn  = document.getElementById('menuBtn');
    if (sidebar.classList.contains('open') &&
        !sidebar.contains(e.target) &&
        !menuBtn.contains(e.target)) {
      sidebar.classList.remove('open');
    }
  });
});

/* ── Panel switching ───────────────────────────────────────────── */
function showPanel(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('panel' + name.charAt(0).toUpperCase() + name.slice(1)).classList.add('active');
  document.getElementById('nav'   + name.charAt(0).toUpperCase() + name.slice(1)).classList.add('active');

  // Close mobile sidebar after navigation
  document.getElementById('sidebar').classList.remove('open');
}

/* ── Mobile sidebar ────────────────────────────────────────────── */
document.getElementById('menuBtn').addEventListener('click', () => {
  document.getElementById('sidebar').classList.toggle('open');
});
document.getElementById('sidebarToggle').addEventListener('click', () => {
  document.getElementById('sidebar').classList.remove('open');
});

/* ── SSH Status ────────────────────────────────────────────────── */
async function checkSSHStatus() {
  try {
    const res  = await fetch('/api/config');
    const data = await res.json();
    const dot  = document.getElementById('statusDot');
    const txt  = document.getElementById('statusText');
    const mDot = document.getElementById('mobileStatusDot');

    if (data.configured) {
      dot.className  = 'dot online';
      mDot.className = 'dot-sm online';
      txt.textContent = data.ssh_host || 'Connected';
    } else {
      dot.className  = 'dot offline';
      mDot.className = 'dot-sm';
      txt.textContent = 'Not configured';
    }
  } catch {
    document.getElementById('statusText').textContent = 'Offline';
  }
}

/* ── Config load / save ────────────────────────────────────────── */
async function loadConfig() {
  try {
    const res  = await fetch('/api/config');
    const data = await res.json();
    document.getElementById('cfgHost').value  = data.ssh_host  || '';
    document.getElementById('cfgPort').value  = data.ssh_port  || '22';
    document.getElementById('cfgUser').value  = data.ssh_user  || '';
    document.getElementById('cfgModel').value = data.ollama_model || 'llama3';
    if (data.auth_type === 'key') switchAuth('key');
  } catch {}
}

async function saveConfig() {
  const btn = event.currentTarget;
  btn.disabled = true;

  const body = {
    ssh_host:     document.getElementById('cfgHost').value.trim(),
    ssh_port:     parseInt(document.getElementById('cfgPort').value) || 22,
    ssh_user:     document.getElementById('cfgUser').value.trim(),
    ssh_password: document.getElementById('cfgPassword').value,
    ssh_key_path: document.getElementById('cfgKeyPath').value.trim(),
    ollama_model: document.getElementById('cfgModel').value,
  };

  try {
    const res  = await fetch('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const data = await res.json();
    showFeedback('configFeedback', 'success', data.message);
    checkSSHStatus();
    showToast('Configuration saved', 'success');
  } catch (e) {
    showFeedback('configFeedback', 'error', 'Failed to save: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

async function testSSH() {
  const btn = event.currentTarget;
  btn.disabled = true;
  showFeedback('configFeedback', 'loading', 'Testing SSH connection…');

  // Save first so test uses latest values
  const body = {
    ssh_host:     document.getElementById('cfgHost').value.trim(),
    ssh_port:     parseInt(document.getElementById('cfgPort').value) || 22,
    ssh_user:     document.getElementById('cfgUser').value.trim(),
    ssh_password: document.getElementById('cfgPassword').value,
    ssh_key_path: document.getElementById('cfgKeyPath').value.trim(),
    ollama_model: document.getElementById('cfgModel').value,
  };
  await fetch('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });

  try {
    const res  = await fetch('/api/test-ssh', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showFeedback('configFeedback', 'success', '✅ ' + data.message);
      checkSSHStatus();
    } else {
      showFeedback('configFeedback', 'error', '❌ ' + data.message);
    }
  } catch (e) {
    showFeedback('configFeedback', 'error', 'Request failed: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

function switchAuth(type) {
  document.getElementById('authPassword').classList.toggle('hidden', type !== 'password');
  document.getElementById('authKey').classList.toggle('hidden', type !== 'key');
  document.getElementById('tabPassword').classList.toggle('active', type === 'password');
  document.getElementById('tabKey').classList.toggle('active', type === 'key');
}

function togglePw(id, btn) {
  const input = document.getElementById(id);
  input.type = input.type === 'password' ? 'text' : 'password';
  btn.style.color = input.type === 'text' ? 'var(--accent)' : '';
}

/* ── Chat ──────────────────────────────────────────────────────── */
function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

function fillExample(btn) {
  const input = document.getElementById('chatInput');
  input.value = btn.textContent.trim();
  autoResize(input);
  input.focus();
}

async function sendMessage() {
  if (isWaiting) return;
  const input = document.getElementById('chatInput');
  const text  = input.value.trim();
  if (!text) return;

  appendMessage('user', text);
  input.value = '';
  input.style.height = 'auto';

  setWaiting(true);
  const typingId = appendTyping();

  try {
    const res  = await fetch('/api/chat', {
      method:  'POST',
      headers: {'Content-Type':'application/json'},
      body:    JSON.stringify({ message: text, stage: 'parse' })
    });
    const data = await res.json();
    removeTyping(typingId);
    handleChatResponse(data);
  } catch (e) {
    removeTyping(typingId);
    appendBotError('Network error: ' + e.message);
  } finally {
    setWaiting(false);
  }
}

function handleChatResponse(data) {
  if (data.stage === 'confirm') {
    pendingParams = data.params;
    appendConfirmMessage(data);
  } else if (data.stage === 'success') {
    appendSuccessMessage(data);
    pendingParams = null;
  } else if (data.stage === 'error' || data.error) {
    appendBotError(data.message || data.error);
    pendingParams = null;
  } else {
    appendBotMessage(data.message || JSON.stringify(data));
  }
}

async function confirmExecution() {
  if (!pendingParams) return;
  setWaiting(true);

  appendMessage('user', '✅ Yes, create the user.');
  removeConfirmButtons();

  const typingId = appendTyping();
  try {
    const res  = await fetch('/api/chat', {
      method:  'POST',
      headers: {'Content-Type':'application/json'},
      body:    JSON.stringify({ stage: 'execute', params: pendingParams, message: '' })
    });
    const data = await res.json();
    removeTyping(typingId);
    handleChatResponse(data);
  } catch (e) {
    removeTyping(typingId);
    appendBotError('Execution failed: ' + e.message);
  } finally {
    setWaiting(false);
  }
}

function cancelExecution() {
  pendingParams = null;
  removeConfirmButtons();
  appendBotMessage('Cancelled. No changes were made to the server.');
}

/* ── Message renderers ─────────────────────────────────────────── */
function appendMessage(role, text) {
  const wrap = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = `message ${role}`;

  const avatarSvg = role === 'bot'
    ? `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/><circle cx="12" cy="16" r="1"/></svg>`
    : `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`;

  div.innerHTML = `
    <div class="avatar ${role}-avatar">${avatarSvg}</div>
    <div class="bubble"><p>${escHtml(text)}</p></div>
  `;
  wrap.appendChild(div);
  scrollBottom();
  return div;
}

function appendBotMessage(text) {
  return appendMessage('bot', text);
}

function appendBotError(text) {
  const wrap = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = 'message bot';
  div.innerHTML = `
    <div class="avatar bot-avatar">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
    </div>
    <div class="bubble error"><p>${markdownLite(escHtml(text))}</p></div>
  `;
  wrap.appendChild(div);
  scrollBottom();
}

function appendConfirmMessage(data) {
  const wrap = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = 'message bot';

  const descHtml = data.description
    ? data.description.split('\n').map(l => `<p>${markdownLite(escHtml(l))}</p>`).join('')
    : '';

  const cmdsHtml = (data.commands || [])
    .map(c => `<div class="cmd-line">${escHtml(c)}</div>`)
    .join('');

  div.innerHTML = `
    <div class="avatar bot-avatar">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/><circle cx="12" cy="16" r="1"/></svg>
    </div>
    <div class="bubble">
      <p><strong>${escHtml(data.message)}</strong></p>
      <div class="desc-list">${descHtml}</div>
      <div class="cmd-preview">
        <div class="cmd-preview-header">Commands to execute</div>
        <div class="cmd-preview-body">${cmdsHtml}</div>
      </div>
      <div class="confirm-actions" id="confirmActions">
        <button class="btn btn-success btn-sm" onclick="confirmExecution()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
          Confirm & Execute
        </button>
        <button class="btn btn-danger btn-sm" onclick="cancelExecution()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          Cancel
        </button>
      </div>
    </div>
  `;
  wrap.appendChild(div);
  scrollBottom();
}

function appendSuccessMessage(data) {
  const wrap = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = 'message bot';

  const detailsHtml = (data.details || [])
    .map(d => `<p>${escHtml(d)}</p>`)
    .join('');

  div.innerHTML = `
    <div class="avatar bot-avatar">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
    </div>
    <div class="bubble success">
      <p><strong>${markdownLite(escHtml(data.message))}</strong></p>
      <div class="result-list">${detailsHtml}</div>
    </div>
  `;
  wrap.appendChild(div);
  scrollBottom();
}

function appendTyping() {
  const wrap = document.getElementById('messages');
  const div  = document.createElement('div');
  const id   = 'typing-' + Date.now();
  div.id     = id;
  div.className = 'message bot';
  div.innerHTML = `
    <div class="avatar bot-avatar">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/><circle cx="12" cy="16" r="1"/></svg>
    </div>
    <div class="bubble">
      <div class="typing-dots"><span></span><span></span><span></span></div>
    </div>
  `;
  wrap.appendChild(div);
  scrollBottom();
  return id;
}

function removeTyping(id) {
  document.getElementById(id)?.remove();
}

function removeConfirmButtons() {
  document.getElementById('confirmActions')?.remove();
}

/* ── Helpers ───────────────────────────────────────────────────── */
function setWaiting(state) {
  isWaiting = state;
  document.getElementById('sendBtn').disabled  = state;
  document.getElementById('chatInput').disabled = state;
}

function scrollBottom() {
  const el = document.getElementById('messages');
  el.scrollTop = el.scrollHeight;
}

function clearChat() {
  const wrap = document.getElementById('messages');
  wrap.innerHTML = '';
  pendingParams = null;
  appendBotMessage('Chat cleared. What user would you like to create?');
}

function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

/** Minimal markdown: **bold**, `code` */
function markdownLite(s) {
  return s
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

function showFeedback(containerId, type, message) {
  const icons = {
    success: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>`,
    error:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
    loading: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>`,
    info:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`,
  };
  document.getElementById(containerId).innerHTML = `
    <div class="feedback ${type}">${icons[type] || ''}${escHtml(message)}</div>
  `;
}

function showToast(message, type = '') {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.className   = 'toast show ' + type;
  setTimeout(() => { toast.className = 'toast'; }, 3000);
}
