// shared.js — 所有页面共用的基础工具函数
const API = '/api';

async function api(path, body) {
  const r = await fetch(API + path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (r.status === 401) { location.href = '/login.html'; throw new Error('未登录'); }
  const j = await r.json();
  if (!r.ok || j.ok === false || j.error) {
    const err = new Error(j.error || ('HTTP ' + r.status));
    err.payload = j;
    throw err;
  }
  return j;
}

function esc(s) {
  return (s || '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function toast(m) {
  const t = document.getElementById('toast');
  t.textContent = m;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2600);
}
