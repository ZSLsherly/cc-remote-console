/* Service Worker：静态资源缓存（API 与终端 WS 直连网络） */
const CACHE = 'ccconsole-v1';
const STATIC = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/static/vendor/xterm.js',
  '/static/vendor/xterm.css',
  '/static/vendor/addon-fit.js',
  '/static/icon-192.png',
  '/manifest.json',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC)));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith('/api/') || u.pathname === '/term') return; // 网络直连
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((resp) => {
      if (u.pathname.startsWith('/static/') && resp.ok) {
        const clone = resp.clone();
        caches.open(CACHE).then((c) => c.put(e.request, clone));
      }
      return resp;
    }))
  );
});
