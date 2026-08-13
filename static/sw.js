/* Service Worker：vendor 静态资源缓存优先，应用文件网络优先（保证更新及时可见） */
const CACHE = 'ccconsole-v4';
const PRECACHE = [
  '/static/vendor/xterm.js',
  '/static/vendor/xterm.css',
  '/static/vendor/addon-fit.js',
  '/static/icon-192.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});
self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith('/api/') || u.pathname === '/term') return;  // 网络直连
  if (u.pathname.startsWith('/static/vendor/')) {
    // 第三方库基本不变：缓存优先，后台填充
    e.respondWith(
      caches.match(e.request).then((hit) => hit || fetch(e.request).then((resp) => {
        const clone = resp.clone();
        caches.open(CACHE).then((c) => c.put(e.request, clone));
        return resp;
      }))
    );
    return;
  }
  // 应用文件（index/app.js/style.css/manifest）：网络优先，离线回退缓存
  e.respondWith(
    fetch(e.request).then((resp) => resp).catch(() => caches.match(e.request))
  );
});
