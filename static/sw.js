// Минимальный служебный скрипт: данные тревог (/api/) никогда не кешируются и не подменяются старыми.
const C = 'uav31-v2';
self.addEventListener('install', e => { self.skipWaiting(); e.waitUntil(caches.open(C).then(c => c.addAll(['/']))); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin || u.pathname.startsWith('/api/')) return;
  if (e.request.mode === 'navigate') {   // страница: сначала сеть, при отсутствии связи открываем последнюю копию (главная и /test хранятся отдельно)
    const key = u.pathname.replace(/\/+$/, '') === '/test' ? '/test' : '/';
    e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(C).then(x => x.put(key, c)); return r; })
                                  .catch(() => caches.match(key)));
  }
});
