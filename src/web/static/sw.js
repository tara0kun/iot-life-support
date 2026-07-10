// Service Worker: タブレットの最後の表示をキャッシュし、オフライン時に表示する
// stale-while-revalidate 戦略

const CACHE_NAME = 'tablet-v5';  // 今日の食事写真セクション追加
const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      // 静的アセットは事前キャッシュ
      return cache.addAll(STATIC_ASSETS).catch(() => {});
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // GET以外はキャッシュしない
  if (request.method !== 'GET') return;

  // タブレット本体（/tablet）と静的アセット → stale-while-revalidate
  const isTablet = url.pathname === '/tablet' || url.pathname === '/';
  const isStatic = url.pathname.startsWith('/static/');

  if (!isTablet && !isStatic) return;  // API / WebSocket は素通し

  event.respondWith(
    caches.open(CACHE_NAME).then(async (cache) => {
      const cached = await cache.match(request);
      const networkPromise = fetch(request)
        .then((response) => {
          if (response && response.status === 200) {
            cache.put(request, response.clone());
          }
          return response;
        })
        .catch(() => null);

      // ネットワーク優先、失敗時にキャッシュ
      const networkResponse = await networkPromise;
      if (networkResponse) return networkResponse;
      if (cached) {
        // オフライン時にキャッシュを返却
        return cached;
      }
      // どちらもなければ最低限のフォールバック
      return new Response(
        '<html><body style="font-family:sans-serif;text-align:center;padding:40px;"><h2>オフライン中</h2><p>ネットワークに接続後、自動で再読み込みされます。</p></body></html>',
        { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
      );
    })
  );
});
