/* 服务器监控 Service Worker
 * 策略: 静态资源 cache-first + 后台更新; /api/* 永远直连(实时数据不缓存),
 * 离线时 API 失败由页面自身显示"最后更新时间"。
 * 使用相对路径匹配, 兼容反向代理前缀 (/server-monitor/)。 */
const CACHE = "mon-static-v2";
const STATIC_ASSETS = [
  "./",
  "./static/index.html",
  "./static/manifest.webmanifest",
  "./static/icons/icon-192.png",
  "./static/icons/icon-512.png"
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  // API 实时数据: 不读缓存, 直接透传(离线时让页面自己报错)
  if (url.pathname.includes("/api/") || url.pathname === "/healthz") return;

  // 静态资源: cache-first, 同时后台刷新
  e.respondWith(
    caches.match(e.request).then(hit => {
      const fetching = fetch(e.request).then(res => {
        if (res && res.ok && url.origin === location.origin){
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return res;
      }).catch(() => hit);
      return hit || fetching;
    })
  );
});
