/* 服务器监控 Service Worker
 * 策略: 静态资源 cache-first + 后台更新; /api/* 永远直连(实时数据不缓存),
 * 离线时 API 失败由页面自身显示"最后更新时间"。
 * 使用相对路径匹配, 兼容反向代理前缀 (/server-monitor/)。 */
const CACHE = "mon-static-v3";
// 资源路径相对 SW 脚本位置( .../static/sw.js )解析:
//   "../"                     -> 站点根(无前缀时为 /, 带前缀时为 /server-monitor/)
//   "./manifest.webmanifest"  -> .../static/manifest.webmanifest
const STATIC_ASSETS = [
  "../",
  "./manifest.webmanifest",
  "./icons/icon-192.png",
  "./icons/icon-512.png"
];

self.addEventListener("install", e => {
  // 逐个 add 且容忍单个失败: addAll 任一 404 会导致整个 SW 安装失败
  e.waitUntil(
    caches.open(CACHE)
      .then(c => Promise.all(STATIC_ASSETS.map(u => c.add(u).catch(() => null))))
      .then(() => self.skipWaiting())
  );
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
