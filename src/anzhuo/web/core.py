"""远窗网页（主体）：对话框、完整过程页、管理页。只听本机端口，经 SSH 反向隧道挂到外网服务器（见 docs/外网访问.md）。

进门：固定网址 + 账号密码，登录一次这台设备 30 天免登录（Cookie）。密码只存 PBKDF2 指纹（钥匙串），
同一来源连错 5 次锁 15 分钟、所有来源一小时内错 30 次全体锁一小时。
扩展（ext.py）能在 /_x/ext/名字/ 下加地址、接住没登记的地址（兜底，排在最后）、往主页面加脚本。

各接口按主题放在同目录的模块里（混入 Web）；这里只留状态、鉴权、路由表、页面框架、实时推送和启动。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from .. import thread, usage
from ..push import Push
from ..speech import Speech
from ..ext import STATIC_FILE, Routes
from .common import COOKIE, PAGE, PREFIX, PUBLIC, USER_ROUTES
from .auth import AuthMixin
from .audio import AudioMixin
from .turns import TurnsMixin
from .threads import ThreadsMixin
from .listing import ListingMixin
from .actions import ActionsMixin
from .rewind import RewindMixin
from .admin import AdminMixin

log = logging.getLogger(__name__)


class Web(AuthMixin, AudioMixin, TurnsMixin, ThreadsMixin, ListingMixin, ActionsMixin, RewindMixin, AdminMixin):
    def __init__(self, base: str, port: int, data_dir: Path,
                 user: str = "", pw_hash: str = "", watch_host: str = "", accounts: dict[str, dict] | None = None,
                 save_hash=None, web_prefix: str = ""):
        # 子路径挂载：被别的面板反代透传时，整站挂在 {web_prefix}/... 下（见 config.example.toml 的 web_prefix）。空 = 根路径，现状不变
        prefix = (web_prefix or "").strip()
        if prefix and not prefix.startswith("/"):
            prefix = "/" + prefix
        self.web_prefix = prefix.rstrip("/")
        self.px = self.web_prefix + PREFIX   # /_x 命名空间的实际地址，比如 /yuanchuang/_x
        # 账号：名字 → {role: admin / user, root: user 的工作文件夹, hash: 密码指纹}。老写法（user + pw_hash）当作唯一的 admin
        self.accounts = dict(accounts) if accounts else ({user: {"role": "admin", "root": "", "hash": pw_hash}} if user else {})
        self.save_hash = save_hash  # 管理页改密码时存指纹（默认存钥匙串）
        from ..config import CONFIG_PATH
        self.config_path = CONFIG_PATH  # 设置页读写的 config.toml（测试里换成临时文件）
        self.usage_path = data_dir / "usage.jsonl"
        self.fails: dict[str, list[float]] = {}
        self.all_fails: list[float] = []
        self.base = base.rstrip("/")
        self.port = port
        self.turns_dir = data_dir / "turns"
        self.store_path = data_dir / "web.json"
        self.push = Push(data_dir / "push.json", self.base or "https://localhost", mirror=watch_host)
        self.store = self._load()
        self.exts: list = []   # 加载了的扩展（ext.py；Bridge 加载好了放进来）
        # 鉴权用的路径集合按前缀重算：PUBLIC / USER_ROUTES 是按 /_x 写的模块常量，挂了子路径就整体平移
        # （/dl 不在 /_x 命名空间里，路由挂到 {web_prefix}/dl，鉴权集合要跟着搬，不然非管理员下文件会被 403）
        def _shift(p: str) -> str:
            if p.startswith(PREFIX):
                return self.px + p[len(PREFIX):]
            if self.web_prefix and p.startswith("/dl"):
                return self.web_prefix + p
            return p
        self.public = tuple(_shift(p) for p in PUBLIC)
        self.user_routes = {_shift(p) for p in USER_ROUTES}
        if self.web_prefix:
            self.user_routes |= {f"{self.web_prefix}/", self.web_prefix}   # 挂载点的入口重定向，谁都能走到主界面
        self._rewind_lock = asyncio.Lock()
        self.speech = Speech(data_dir / "speech")
        self.bridge = None  # 远窗本体（app.Bridge），网页对话框靠它看对话、发话
        self._group_busy: set[str] = set()
        self._group_attempt: dict[str, float] = {}
        self._threads: dict[int, tuple[tuple, list[dict]]] = {}
        self._zh_path = data_dir / "translations.json"
        try:
            self._zh: dict[str, str] = json.loads(self._zh_path.read_text())
        except (OSError, json.JSONDecodeError):
            self._zh = {}
        self._zh_busy: set[str] = set()
        self._uimg: dict[str, tuple[bytes, str]] = {}   # 你发过的图，最近取过的留在内存
        self._uimg_miss: set[str] = set()
        self._zh_failed: set[str] = set()
        self._search_cache: dict[int, tuple] = {}  # 译不出来的不再反复试（免得英文、“翻译中”来回闪）
        self.watch: dict[int, float] = {}  # 你正开着看的对话 → 网页最近一次报“我在看”的时间

    def watching(self, num: int) -> bool:
        """你正开着这个对话、页面在前台（网页每 15 秒报一次）：做完就不再弹通知、不发钉钉。"""
        return time.time() - self.watch.get(num, 0) < 35

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    # ---------- 登录 ----------
    def _load(self) -> dict:
        try:
            d = json.loads(self.store_path.read_text())
        except (OSError, json.JSONDecodeError):
            d = {}
        return {"sessions": d.get("sessions", {}), "devices": d.get("devices", {}), "disabled": d.get("disabled", []),
                "shares": d.get("shares", {}), "rate2": d.get("rate2")}

    def _save(self) -> None:
        now = time.time()
        self.store["sessions"] = {k: v for k, v in self.store["sessions"].items() if v > now}
        self.store["devices"] = {k: v for k, v in self.store.get("devices", {}).items() if k in self.store["sessions"]}
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.store))
        tmp.chmod(0o600)
        tmp.replace(self.store_path)

    def link(self, target: str) -> str:
        """某个页面的固定网址（没登录会先到登录页，登完回到这里）。"""
        return f"{self.base}{target}"

    def _acct_of(self, sid: str) -> str:
        """这台设备登的是哪个账号（这个功能之前登录的都算 admin）。"""
        return self.store.get("devices", {}).get(sid, {}).get("account") or self._admin()

    def _admin(self) -> str:
        return next((n for n, a in self.accounts.items() if a.get("role") == "admin"), "admin")

    # 老写法：只有一个账号（admin）时直接读写它的名字和密码指纹
    @property
    def user(self) -> str:
        return self._admin()

    @user.setter
    def user(self, name: str) -> None:
        old = self.accounts.pop(self._admin(), {"role": "admin", "root": "", "hash": ""})
        self.accounts[name] = old

    @property
    def pw_hash(self) -> str:
        return (self.accounts.get(self._admin()) or {}).get("hash", "")

    @pw_hash.setter
    def pw_hash(self, digest: str) -> None:
        self.accounts.setdefault(self._admin(), {"role": "admin", "root": ""})["hash"] = digest

    def role(self, acct: str) -> str:
        return (self.accounts.get(acct) or {}).get("role", "user")

    def root_of(self, acct: str) -> str:
        return (self.accounts.get(acct) or {}).get("root", "") if self.role(acct) != "admin" else ""

    def owner_key(self, acct: str) -> str:
        """对话登记里记的归属：admin 账号的一律记 "admin"（钉钉开的也是），别的账号记账号名。"""
        return "admin" if self.role(acct) == "admin" else acct

    async def notify(self, owner: str, title: str, body: str, num: int | None = None) -> int:
        """手机通知只发给这个对话所属账号登录的设备。没对上设备的老订阅算 admin 的。"""
        mine, others = set(), set()
        for sid, d in self.store.get("devices", {}).items():
            if d.get("endpoint"):
                (mine if self.owner_key(self._acct_of(sid)) == owner else others).add(d["endpoint"])
        targets = [x["endpoint"] for x in self.push.subs
                   if x["endpoint"] in mine or (owner == "admin" and x["endpoint"] not in others)]
        return await self.push.send(title, body, num, only=set(targets))

    def quota(self, acct: str) -> dict | None:
        """受限账号本周额度用了多少（admin 不限，返回 None）。顺手把推出来的换算比例存下，数据少的时候接着用。"""
        if self.role(acct) == "admin":
            return None
        limits = getattr(getattr(self.bridge, "runner", None), "limits", {}) or {}
        week = limits.get("seven_day")
        share = float(self.store.get("shares", {}).get(acct, (self.accounts.get(acct) or {}).get("share", usage.DEFAULT_SHARE)))
        q = usage.quota(usage.load(self.usage_path, time.time() - 29 * 86400), self.owner_key(acct), share,
                        usage.reset_epoch(week[1]) if week else None, self.store.get("rate2"),
                        spend=getattr(self.bridge, "spend", None))
        if q["rate"] and q["rate"] != self.store.get("rate2"):   # rate2：2026-11-04 之前存的 rate 是按累计值算错的，不用
            self.store["rate2"] = q["rate"]
            self._save()
        t = time.localtime(q["reset_at"]) if q["reset_at"] else None   # 不用 %-m：Windows 的 strftime 不认
        q["reset"] = f"{t.tm_mon}月{t.tm_mday}日 {t.tm_hour:02d}:{t.tm_min:02d}" if t else ""
        return q

    def over_quota(self, acct: str) -> str:
        """额度用完了返回一句说明，没用完返回空。"""
        q = self.quota(acct)
        if not q or q["pct"] < 100:
            return ""
        return "本周额度已用完" + (f"，{q['reset']} 重置后再继续。" if q["reset"] else "，重置后再继续。")

    # ---------- 页面 ----------
    def _page(self, title: str, body: str, status: int = 200) -> web.Response:
        page = PAGE.format(title=html.escape(title), body=body)
        if self.web_prefix:   # PAGE 是按 /_x 写的模板，挂了子路径就把里面的引用平移
            page = page.replace('href="/_x/"', f'href="{self.px}/"')
        return web.Response(text=page, status=status,
                            content_type="text/html", headers={"Cache-Control": "no-store",
                                                               "Referrer-Policy": "no-referrer"})

    def _denied(self, request: web.Request) -> web.Response:
        if request.method == "GET" and "/api/" not in request.path:
            raise web.HTTPFound(f"{self.px}/login?next={quote(request.rel_url.path_qs, safe='')}")
        return web.json_response({"detail": "需要先登录远窗"}, status=401)

    async def _app_page(self, request: web.Request) -> web.Response:
        from ..webui import app_html
        admin = self.role(request.get("acct", "")) == "admin"
        scripts = [f"{self.px}/ext/{e.name}/static/{f}" for e in self.exts for f in e.scripts] if admin else []
        return web.Response(text=app_html(self.web_prefix, scripts), content_type="text/html", headers={"Cache-Control": "no-store"})

    async def _icon(self, request: web.Request) -> web.StreamResponse:
        name = "icon-512.png" if request.path.endswith("512.png") else "icon.png"
        return web.FileResponse(Path(__file__).parent.parent / "static" / name, headers={"Cache-Control": "max-age=86400"})

    async def _manifest(self, request: web.Request) -> web.Response:
        return web.json_response({"name": "远窗", "short_name": "远窗", "start_url": f"{self.px}/", "display": "standalone",
                                  "background_color": "#d5dee5", "theme_color": "#d5dee5",
                                  "icons": [{"src": f"{self.px}/icon.png", "sizes": "180x180", "type": "image/png"},
                                            {"src": f"{self.px}/icon-512.png", "sizes": "512x512", "type": "image/png"}]})

    async def _sw(self, request: web.Request) -> web.Response:
        # 点通知：先把要打开的对话编号记进缓存（iPhone 冷启动时不一定带上地址），再叫醒已开着的页面或新开一个
        js = """self.addEventListener('push', e => {
  const d = e.data ? e.data.json() : {};
  e.waitUntil(self.registration.showNotification(d.title || '远窗', {body: d.body || '', tag: d.tag || undefined, data: {num: d.num || null}, icon: '/_x/icon.png'}));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const n = e.notification.data && e.notification.data.num;
  e.waitUntil((async () => {
    if (n) await (await caches.open('anzhuo')).put('/_x/__open', new Response(String(n)));
    const cs = await clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const c of cs) { if (n) c.postMessage({open: n}); if ('focus' in c) return c.focus(); }
    return clients.openWindow(n ? '/_x/?s=' + n : '/_x/');
  })());
});
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
""".replace(f"'{PREFIX}/", f"'{self.px}/")
        return web.Response(text=js, content_type="application/javascript", headers={"Cache-Control": "no-cache"})

    async def _health(self, request: web.Request) -> web.Response:
        # 外网服务器那边每分钟来问一声“还在吗”；网页也用它判断 Mac 连不连得上。不带任何内容
        return web.json_response({"ok": True})

    async def _api_push_key(self, request: web.Request) -> web.Response:
        return web.json_response({"key": await asyncio.to_thread(self.push.public_key)})

    async def _api_push(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        try:
            sub = await request.json()
            self.push.add(sub)
            sid = request.cookies.get(COOKIE, "")
            if sid in self.store["sessions"]:
                self.store.setdefault("devices", {}).setdefault(sid, {})["endpoint"] = sub.get("endpoint")
                self._save()
        except ValueError:
            return web.json_response({"ok": False}, status=400)
        return web.json_response({"ok": True})

    async def _to_index(self, request: web.Request) -> web.Response:
        # 挂了子路径时根路径友好跳到挂载点；没挂就跳主界面（现状）
        raise web.HTTPFound(f"{self.web_prefix}/" if self.web_prefix else f"{self.px}/")

    async def _to_app(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{self.px}/")

    def _sig_sessions(self, owner: str = "admin") -> str:
        """对话列表的“指纹”：谁在做、做到第几步、等不等你、更新时间；变了就叫网页刷新列表。"""
        b = self.bridge
        out = []
        for s in b.registry.sessions.values():
            if s.owner != owner:
                continue
            t = b.runner.turns.get(s.num)
            out.append((s.num, s.updated, s.awaiting, s.seen > s.updated, bool(t), len(t.steps) if t else 0,
                        bool(t and t.confirm), b.runner.busy(s.num), s.hidden, s.pinned, s.name, s.task_group, s.task_group_manual, s.limit_until,
                        s.rewind_at, s.auto_resume))
        return json.dumps(out, ensure_ascii=False, default=str)

    def _cc_mtime(self, s) -> int:
        """Claude 记录文件的修改时间：Claude Code 那边（终端 / IDE）接着聊也会写这份记录，变了就该刷新。"""
        if not s or not s.claude_id:
            return 0
        p = thread.transcript_path(s.claude_id)
        try:
            return p.stat().st_mtime if p else 0
        except OSError:
            return 0

    def _sig_thread(self, num: int) -> str:
        b = self.bridge
        s, t = b.registry.get(num), b.runner.turns.get(num)
        recs = len(list(self.turns_dir.glob(f"{num}-*.json"))) if s else 0
        cc_mtime = self._cc_mtime(s)
        return json.dumps([s.updated if s else 0, s.relay_to if s else 0, len(t.timeline) if t else -1, bool(t and t.confirm),
                           len(b.outgoing.get(num, [])), recs, s.rewind_at if s else "", len(b.runner.queued(num)),
                           s.limit_until if s else 0, s.auto_resume if s else True, cc_mtime])

    async def _api_events(self, request: web.Request) -> web.StreamResponse:
        """实时推送（Server-Sent Events）：列表变了发 s，打开的对话有新步骤 / 做完了发 t，正在写的回复发 p（带全文）。
        网页收到就当场更新，不用每几秒问一次。每 15 秒发个空行保活（外网服务器那边 120 秒没动静会断）。"""
        num = int(request.query.get("num") or 0)
        owner = request.get("owner", "admin")
        if num and not self.bridge.registry.mine(num, owner):
            num = 0
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        last_s = last_t = last_p = None
        beat = time.time()
        try:
            while True:
                sig_s = self._sig_sessions(owner)
                if sig_s != last_s:
                    if last_s is not None:
                        await resp.write(b"event: s\ndata: 1\n\n")
                    last_s = sig_s
                if num:
                    sig_t = self._sig_thread(num)
                    if sig_t != last_t:
                        if last_t is not None:
                            await resp.write(b"event: t\ndata: 1\n\n")
                        last_t = sig_t
                    t = self.bridge.runner.turns.get(num)
                    part = t.partial if t else ""
                    if part != last_p:
                        await resp.write(b"event: p\ndata: " + json.dumps({"text": part}, ensure_ascii=False).encode() + b"\n\n")
                        last_p = part
                if time.time() - beat > 15:
                    await resp.write(b": ping\n\n")
                    beat = time.time()
                await asyncio.sleep(0.4)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    # ---------- 启动 ----------
    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if request.path in self.public:
            return await handler(request)
        if not self._logged_in(request):
            return self._denied(request)
        if "acct" not in request:   # 测试里直接放行登录的：当作 admin
            request["acct"], request["owner"] = self._admin(), "admin"
        owner, admin = request["owner"], self.role(request["acct"]) == "admin"
        route = request.match_info.route.resource.canonical if request.match_info.route.resource else ""
        if not admin and route not in self.user_routes:
            raise web.HTTPForbidden(text="这个账号用不了这里")
        if route.startswith(f"{self.px}/admin") or route.startswith(f"{self.px}/api/admin"):
            if not admin:
                raise web.HTTPForbidden(text="只有管理员能看")
        # 带对话编号的：只能是自己账号的对话（别人的当作没有）
        raw = request.match_info.get("num") or (request.match_info.get("tid") or "").split("-")[0]
        if raw.isdigit() and self.bridge is not None and hasattr(self.bridge, "registry"):
            s = self.bridge.registry.get(int(raw))
            if (s and s.owner != owner) or (not s and not admin):
                raise web.HTTPNotFound()
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._guard], client_max_size=0)
        app.cleanup_ctx.append(self._speech_context)
        P = self.px   # /_x 的实际地址：空前缀时就是 /_x，挂了子路径时是 {web_prefix}/_x
        app.router.add_get(f"{P}/login", self._login_get)
        app.router.add_post(f"{P}/login", self._login_post)
        app.router.add_get("/", self._to_index)
        app.router.add_get(f"{P}/", self._app_page)
        app.router.add_get(f"{P}/history", self._index)
        app.router.add_get(f"{P}/icon.png", self._icon)
        app.router.add_get(f"{P}/icon-512.png", self._icon)
        app.router.add_get(f"{P}/manifest.json", self._manifest)
        app.router.add_get(f"{P}/sw.js", self._sw)
        app.router.add_get(f"{P}/health", self._health)
        app.router.add_get(f"{P}/api/push/key", self._api_push_key)
        app.router.add_post(f"{P}/api/push", self._api_push)
        app.router.add_post(f"{P}/api/logout", self._api_logout)
        app.router.add_get(f"{P}/api/devices", self._api_devices)
        app.router.add_post(f"{P}/api/devices/logout", self._api_device_logout)
        app.router.add_get(f"{P}/api/sessions", self._api_sessions)
        app.router.add_post(f"{P}/api/task-groups", self._api_task_groups)
        app.router.add_get(f"{P}/api/events", self._api_events)
        app.router.add_get(f"{P}/api/uimg/{{num:\\d+}}/{{uuid}}/{{k:\\d+}}", self._api_uimg)
        app.router.add_get(f"{P}/api/thread/{{num:\\d+}}", self._api_thread)
        app.router.add_post(f"{P}/api/manage/{{num:\\d+}}", self._api_manage)
        app.router.add_post(f"{P}/api/speech/{{num:\\d+}}", self._api_speech)
        app.router.add_post(f"{P}/api/speech/release", self._release_speech)
        app.router.add_get(f"{P}/api/audio/{{key}}", self._api_audio)
        app.router.add_post(f"{P}/api/audio/{{key}}", self._api_audio)
        app.router.add_get(f"{P}/audio/{{key}}.mp3", self._audio)
        app.router.add_get(f"{P}/api/search", self._api_search)
        app.router.add_post(f"{P}/api/send", self._api_send)
        app.router.add_get(f"{P}/api/schedules", self._api_schedules)
        app.router.add_post(f"{P}/api/schedules", self._api_schedules)
        app.router.add_post(f"{P}/api/settings/{{num}}", self._api_settings)
        app.router.add_post(f"{P}/api/compact/{{num:\\d+}}", self._api_compact)
        app.router.add_post(f"{P}/api/relay/{{num:\\d+}}", self._api_relay)
        app.router.add_post(f"{P}/api/confirm/{{num:\\d+}}", self._api_confirm)
        app.router.add_post(f"{P}/api/stop/{{num:\\d+}}", self._api_stop)
        app.router.add_post(f"{P}/api/rewind/{{num:\\d+}}", self._api_rewind)
        app.router.add_post(f"{P}/api/fork/{{num:\\d+}}", self._api_fork)
        app.router.add_get(f"{P}/api/browse", self._api_browse)
        app.router.add_get(f"{P}/api/cc-sessions", self._api_cc_sessions)
        app.router.add_post(f"{P}/api/resume-cc", self._api_resume_cc)
        app.router.add_post(f"{P}/api/unqueue/{{num:\\d+}}", self._api_unqueue)
        app.router.add_post(f"{P}/api/presence", self._api_presence)
        app.router.add_get(f"{P}/admin", self._admin_page)
        app.router.add_get(f"{P}/api/admin", self._api_admin)
        app.router.add_post(f"{P}/api/admin", self._api_admin_act)
        app.router.add_get(f"{P}/api/admin/prefs", self._api_prefs)        # 设置（只有 admin：路径在 /api/admin 下）
        app.router.add_post(f"{P}/api/admin/prefs", self._api_prefs)
        app.router.add_get(f"{P}/admin/settings.js", self._settings_js)
        app.router.add_get(P, self._to_app)
        app.router.add_get(f"{P}/t/{{tid}}", self._turn_page)
        app.router.add_get(f"{P}/t/{{tid}}/img/{{i:\\d+}}", self._turn_image)
        app.router.add_get(f"{P}/t/{{tid}}/file/{{i:\\d+}}", self._turn_file)
        # 网页里下文件走 /_x 外面的地址：iPhone、iPad 从主屏幕打开的远窗，点范围外的链接会弹出带“完成”的浏览页，
        # 看完能回来；范围内的链接直接在远窗窗口里打开文件，没有返回键（10-27 遇到）
        app.router.add_get(f"{self.web_prefix}/dl/{{tid}}/{{i:\\d+}}", self._turn_file)
        app.router.add_get(f"{self.web_prefix}/dl/{{tid}}/{{i:\\d+}}/pages", self._pdf_pages)
        app.router.add_get(f"{self.web_prefix}/dl/{{tid}}/{{i:\\d+}}/p/{{name}}", self._pdf_page)
        # 被反代透传挂到子路径时，挂载点本身也要能落到主界面。
        # 带不带尾斜杠都要注册：浏览器/导航进来的是无斜杠形态，aiohttp 不会自动补斜杠，
        # 已登录用户（守卫放行）会直接 404，未登录的只会被守卫弹走测不出来。
        if self.web_prefix:
            app.router.add_get(self.web_prefix, self._to_app)
            app.router.add_get(f"{self.web_prefix}/", self._to_app)
        # 扩展：各自的地址在 /_x/ext/名字/ 下（只给管理员），static/ 里的文件在 /_x/ext/名字/static/…
        fallbacks = []
        for e in self.exts:
            r = Routes(app, P, e.name, allow_fallback=not self.web_prefix)
            app.router.add_get(r.url("/static/{file:.+}"), self._ext_static)
            try:
                e.routes(r)
            except Exception:  # noqa: BLE001
                log.exception("扩展 %s 加地址出错", e.name)
            fallbacks += r.fallbacks
        if fallbacks:   # 其余地址交给扩展的兜底（必须最后注册；只认第一个）；没有扩展接就 404
            app.router.add_route("*", "/{tail:.*}", fallbacks[0])
        app.on_cleanup.append(self._ext_close)
        return app

    async def _ext_static(self, request: web.Request) -> web.StreamResponse:
        name = request.path[len(self.px) + len("/ext/"):].split("/", 1)[0]
        e = next((x for x in self.exts if x.name == name), None)
        file = request.match_info["file"]
        if not e or not STATIC_FILE.fullmatch(file) or ".." in file.split("/"):
            raise web.HTTPNotFound()
        path = e.static_dir / file
        if not path.is_file():
            raise web.HTTPNotFound()
        if path.suffix == ".js":   # 脚本里写的 /_x/ 地址跟着子路径前缀走（和主页面一样）
            from ..webui import with_prefix
            return web.Response(text=with_prefix(path.read_text(encoding="utf-8"), self.web_prefix),
                                content_type="application/javascript", headers={"Cache-Control": "no-cache"})
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    async def _ext_close(self, app) -> None:
        for e in self.exts:
            try:
                await e.close()
            except Exception:  # noqa: BLE001
                log.exception("扩展 %s 收尾出错", e.name)

    async def start(self) -> None:
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.port).start()
        log.info("远窗网页在 127.0.0.1:%d", self.port)
