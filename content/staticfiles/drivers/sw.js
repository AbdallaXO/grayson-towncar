const CACHE = 'gt-driver-v2';
const OFFLINE_URL = '/static/drivers/offline.html';

self.addEventListener('install', (e) => {
    e.waitUntil(
        caches.open(CACHE).then((c) => c.add(OFFLINE_URL).catch(() => {}))
    );
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (e) => {
    if (e.request.mode === 'navigate') {
        e.respondWith(
            fetch(e.request).catch(() => caches.match(OFFLINE_URL))
        );
    }
});
