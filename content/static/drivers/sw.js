const CACHE = 'gt-driver-v3';
const OFFLINE_URL = '/static/drivers/offline.html';
const ICON = '/static/drivers/icons/icon-192.png';

self.addEventListener('install', (e) => {
    e.waitUntil(
        caches.open(CACHE).then((c) => c.add(OFFLINE_URL).catch(() => {}))
    );
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(
        caches.keys().then((names) =>
            Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)))
        ).then(() => self.clients.claim())
    );
});

// Offline strategy:
// - Driver pages (navigations): network-first, falling back to the last
//   successfully loaded copy of THAT page, then the generic offline page.
//   A driver in a parking-garage dead zone still sees his trip addresses.
// - Static assets: cache-first with background refresh.
self.addEventListener('fetch', (e) => {
    const req = e.request;
    if (req.method !== 'GET') return;

    if (req.mode === 'navigate') {
        e.respondWith(
            fetch(req)
                .then((resp) => {
                    if (resp && resp.ok) {
                        const copy = resp.clone();
                        caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
                    }
                    return resp;
                })
                .catch(() =>
                    caches.match(req).then((hit) => hit || caches.match(OFFLINE_URL))
                )
        );
        return;
    }

    const url = new URL(req.url);
    if (url.origin === self.location.origin && url.pathname.startsWith('/static/')) {
        e.respondWith(
            caches.match(req).then((hit) => {
                const refresh = fetch(req)
                    .then((resp) => {
                        if (resp && resp.ok) {
                            const copy = resp.clone();
                            caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
                        }
                        return resp;
                    })
                    .catch(() => hit);
                return hit || refresh;
            })
        );
    }
});

// ── Web Push ──
self.addEventListener('push', (e) => {
    let data = {};
    try { data = e.data ? e.data.json() : {}; } catch (err) { /* non-JSON push */ }
    const title = data.title || 'Grayson Towncar';
    const options = {
        body: data.body || 'Your schedule was updated.',
        icon: ICON,
        badge: ICON,
        tag: data.tag || 'gt-driver',
        renotify: true,
        data: { url: data.url || '/drivers/' },
    };
    e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (e) => {
    e.notification.close();
    const target = (e.notification.data && e.notification.data.url) || '/drivers/';
    e.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
            for (const win of wins) {
                if (win.url.includes('/drivers/') && 'focus' in win) {
                    win.navigate(target).catch(() => {});
                    return win.focus();
                }
            }
            return self.clients.openWindow(target);
        })
    );
});
