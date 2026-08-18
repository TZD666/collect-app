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
  const text = await r.text();
  let j = null;
  if (text) {
    try {
      j = JSON.parse(text);
    } catch (e) {
      const err = new Error(text.slice(0, 300) || ('HTTP ' + r.status));
      err.raw = text;
      throw err;
    }
  } else {
    j = {};
  }
  if (!r.ok || j.ok === false || j.error) {
    const err = new Error(j.error || j.detail || ('HTTP ' + r.status));
    err.payload = j;
    throw err;
  }
  return j;
}

function esc(s) {
  return (s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(m) {
  const t = document.getElementById('toast');
  t.textContent = m;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2600);
}
