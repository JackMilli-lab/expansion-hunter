/* Offline interface only: API and exchange responses never enter this cache. */
const shellCache = 'expansion-hunter-shell-design-v2';
const shellPaths = new Set(['/cockpit', '/cockpit-assets/chart-engine.js',
  '/cockpit-assets/candidate-ui.js', '/cockpit-assets/icon.svg', '/cockpit-assets/manifest.webmanifest']);
self.addEventListener('install', event => {
  event.waitUntil(caches.open(shellCache).then(cache => cache.addAll([...shellPaths]))
    .catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('expansion-hunter-shell-') && key !== shellCache).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const request = event.request, url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || !shellPaths.has(url.pathname)) return;
  event.respondWith(fetch(request).then(response => {
    if (response.ok && response.type !== 'opaque') {
      const copy = response.clone();
      event.waitUntil(caches.open(shellCache).then(cache => cache.put(new Request(url.origin + url.pathname), copy)));
    }
    return response;
  }).catch(async () => {
    const cached = await caches.match(new Request(url.origin + url.pathname));
    return cached || new Response('Interface unavailable offline. Market data is not cached.', {status:503});
  }));
});
