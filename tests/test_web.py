"""远窗网页：登录、过程页、对话框接口（做课扩展的在 test_ext_course.py）。"""
import asyncio
import re
import time
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from anzhuo.events import Bus
from anzhuo.relay import Relays
from anzhuo.report import Turn
from anzhuo.web import Web, check_password, hash_password


def test_password_hash():
    h = hash_password("020928", iters=1000)
    assert h.startswith("pbkdf2$1000$") and "020928" not in h
    assert check_password("020928", h) and not check_password("020929", h) and not check_password("x", "坏的")


def test_login_pages(tmp_path):
    seen = {}

    async def upstream(request):
        seen.setdefault("xff", {})[request.path] = request.headers.get("X-Forwarded-For")
        seen["cookie"] = request.headers.get("Cookie")
        seen["path"] = request.path_qs
        return web.Response(text="PREVIEW", content_type="text/html")

    async def go():
        up_app = web.Application()
        up_app.router.add_route("*", "/{tail:.*}", upstream)
        up = TestServer(up_app)
        await up.start_server()
        w = Web("http://x.example", 0, tmp_path, user="18000000000", pw_hash=hash_password("pw-ok", iters=1000))
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG")
        t = Turn(started=time.time() - 120, ended=time.time(), request="改<封面>", final_text="**做了什么**：改了标题",
                 images=[str(img)], timeline=[{"t": time.time(), "kind": "do", "text": "跑命令：测试", "detail": "pytest -q"}])
        tid = w.save_turn("#4 开发", t, str(tmp_path), "做完了")
        assert w.link(f"/_x/t/{tid}") == f"http://x.example/_x/t/{tid}"   # 固定网址
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            r = await c.get(f"/_x/t/{tid}", allow_redirects=False)            # 没登录 → 登录页，带着回来的地址
            assert r.status == 302 and r.headers["Location"].startswith("/_x/login?next=")
            assert (await c.get("/whatever", allow_redirects=False)).status == 302   # 没登记的地址：没登录也先要登录
            login = await (await c.get(r.headers["Location"])).text()
            assert 'autocomplete="current-password"' in login
            bad = await c.post("/_x/login", data={"username": "18000000000", "password": "错", "next": "/x"},
                               allow_redirects=False)
            assert bad.status == 401
            r = await c.post("/_x/login", data={"username": "18000000000", "password": "pw-ok", "next": f"/_x/t/{tid}"},
                             allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == f"/_x/t/{tid}"
            page = await (await c.get(f"/_x/t/{tid}")).text()
            assert "改&lt;封面&gt;" in page and "<b>做了什么</b>" in page and "pytest -q" in page
            assert (await c.get(f"/_x/t/{tid}/img/0")).status == 200
            assert (await c.get(f"/_x/t/{tid}/img/5")).status == 404
            assert (await c.get("/", allow_redirects=False)).headers["Location"] == "/_x/"
            assert "开发" in await (await c.get("/_x/history")).text()
            assert (await c.get("/studio-preview.html")).status == 404      # 没有扩展接：没登记的地址一律 404
            # 登录后跳转只认本站路径，不能借登录页跳到外站
            r = await c.get("/_x/login?next=//evil.example/x", allow_redirects=False)
            assert r.headers["Location"] == "/_x/"
            # 换个浏览器（没 Cookie）：连错 5 次锁住，锁住时对的密码也不行
            c.session.cookie_jar.clear()
            for _ in range(5):
                await c.post("/_x/login", data={"username": "18000000000", "password": "错"},
                             headers={"X-Forwarded-For": "6.6.6.6"})
            r = await c.post("/_x/login", data={"username": "18000000000", "password": "pw-ok"},
                             headers={"X-Forwarded-For": "6.6.6.6"}, allow_redirects=False)
            assert r.status == 429
            r = await c.post("/_x/login", data={"username": "18000000000", "password": "pw-ok"},
                             headers={"X-Forwarded-For": "6.6.6.6, 7.7.7.7"}, allow_redirects=False)
            assert r.status == 302                                           # 只认最后一个来源，别的地址不受牵连
        finally:
            await c.close()
            await up.close()

    asyncio.run(go())


def test_thread_history():
    from types import SimpleNamespace as M
    from anzhuo import thread
    msgs = [
        M(type="user", message={"content": "This session is being continued from a previous conversation"}),
        M(type="user", message={"content": "<command-name>/compact</command-name>"}),
        M(type="user", message={"content": [{"type": "text", "text": "把封面标题改大"}, {"type": "image", "source": {}}]}),
        M(type="assistant", message={"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "我先看看"}, {"type": "tool_use", "name": "Read"}]}),
        M(type="user", message={"content": [{"type": "tool_result", "content": "..."}]}),
        M(type="assistant", message={"content": [{"type": "tool_use", "name": "Edit"}]}),
        M(type="assistant", message={"content": [{"type": "text", "text": "改好了"}]}),
        M(type="user", message={"content": "好的"}),
        M(type="user", message={"content": "[SYSTEM NOTIFICATION - NOT USER INPUT] 后台任务结束"}),
        M(type="assistant", message={"content": [{"type": "text", "text": "No response requested."}]}),
    ]
    items = thread.history(msgs)
    assert items == [{"who": "note", "text": "（更早的对话内容已压缩）"},
                     {"who": "you", "text": "[图片]\n把封面标题改大"},
                     {"who": "claude", "text": "改好了", "steps": 2,
                      "process": [{"k": "say", "text": "我先看看"}, {"k": "do", "text": "看 文件", "detail": ""},
                                  {"k": "do", "text": "改 文件", "detail": ""}]},
                     {"who": "you", "text": "好的"}]
    thread.attach_records(items, [{"id": "6-1", "title": "做完了", "links": [{"label": "预览", "url": "/p"}]}])
    assert items[2]["tid"] == "6-1" and items[2]["links"] == [{"label": "预览", "url": "/p"}]


def test_chat_api(tmp_path, monkeypatch):
    """网页对话框：列表、读对话、发话（带图）、新开、停下；没登录给 401；外站来源的 POST 拒绝。"""
    from types import SimpleNamespace as NS
    from anzhuo.sessions import Session

    class FakeRunner:
        def __init__(self):
            self.turns, self.stopped = {}, []
            self.limits, self.default_effort = {"five_hour": (0.37, "10月25日 21:00"), "seven_day": (0.22, None)}, "medium"

        def busy(self, n):
            return n in self.turns

        async def stop(self, n):
            self.stopped.append(n)
            return True

        def compacting(self, n):
            return ""

        def queued(self, n):
            return []

    class FakeBridge:
        def __init__(self):
            s6 = Session(num=6, name="做课", cwd=str(tmp_path), awaiting="要我按 C 来做吗？", claude_id="sid6")
            s7 = Session(num=7, name="清理", cwd=str(tmp_path))
            self.registry = NS(sessions={6: s6, 7: s7}, visible=lambda owner="admin": [s6, s7], get=lambda n: {6: s6, 7: s7}.get(n),
                               current=6, next_num=8, save=lambda: None)
            self.runner = FakeRunner()
            self.runner.turns[7] = Turn(started=time.time() - 130, request="清理下载", steps=["看 a", "跑命令：du"])
            self.cfg = NS(folders={"前端": "/x"}, scratch_root="/tmp/anzhuo-test-scratch", search_roots=[], skip_names=[])
            self.user_id, self.sent, self.new, self.outgoing, self.said = "me", [], [], {}, []
            self.relays = NS(writing={}, targets=lambda s: [])

        async def say(self, title, text):
            self.said.append((title, text))

        def record(self, *a, **k):
            pass

        async def message(self, cmd, inc, routed=False):
            self.sent.append((cmd.num, cmd.text, list(inc.local_images)))
            self.outgoing.setdefault(cmd.num, []).append({"t": 0, "text": cmd.text})

        async def new_session(self, tokens, text, inc, note="", kind=None, model=None, effort=None):
            self.new.append((tokens, text))
            self.new_model = (model, effort)
            self.registry.sessions[8] = Session(num=8, name="新", cwd="/x")

    async def go():
        w = Web("http://x.example", 0, tmp_path, user="u", pw_hash=hash_password("p", iters=1000))
        w.bridge = b = FakeBridge()
        b.registry.get = lambda n: b.registry.sessions.get(n)
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            assert (await c.get("/_x/api/sessions")).status == 401
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            d = await (await c.get("/_x/api/sessions")).json()
            assert [r["num"] for r in d["sessions"]] == [6, 7]                 # 等你回答的排最前
            assert d["sessions"][0]["unread"] is True                          # 做完了还没看
            await c.get("/_x/api/thread/6")
            d = await (await c.get("/_x/api/sessions")).json()
            assert d["sessions"][0]["unread"] is False                         # 打开看过就不算了
            assert d["sessions"][1]["detail"].startswith("在做 · 2 分钟 · 第 2 步")
            assert [{k: v for k, v in l.items() if k in ("name", "pct", "reset")} for l in d["limits"]] == [{"name": "5 小时", "pct": 37, "reset": "10月25日 21:00"}, {"name": "本周", "pct": 22, "reset": ""}]
            assert "远窗" in await (await c.get("/_x/")).text()
            t7 = await (await c.get("/_x/api/thread/7")).json()
            assert t7["live"]["steps"] == ["看 a", "跑命令：du"] and t7["decision"] is None
            assert t7["usage"] == {"model": "默认模型", "effort": "中", "context": None, "mkey": "", "ekey": "medium", "compacting": "", "busy": True, "tokens": None, "relay": None, "relay_suggested": False}
            png = "data:image/png;base64," + __import__("base64").b64encode(b"\x89PNG").decode()
            r = await (await c.post("/_x/api/send", json={"num": 7, "text": "顺便看看桌面", "images": [png]})).json()
            assert r == {"ok": True, "injected": True}
            assert b.sent[0][:2] == (7, "顺便看看桌面") and b.sent[0][2][0].endswith(".png")
            assert b.said == []                                                    # 钉钉不转述你在网页说的话
            t7 = await (await c.get("/_x/api/thread/7")).json()
            assert {k: v for k, v in t7["items"][-1].items() if k != "age"} == {"who": "you", "text": "顺便看看桌面", "pending": True}   # 记录里还没有，也先显示
            r = await (await c.post("/_x/api/send", json={"text": "查一下天气", "folder": ""})).json()
            assert r["num"] == 8 and b.new == [([], "查一下天气")]
            # 新对话“在哪做”：远窗自动判断 / 不碰文件（“接着做某门课”见 test_ext_course.py）
            d = await (await c.get("/_x/api/sessions")).json()
            assert d["ext"] == {}
            from anzhuo import folders
            router = pytest.importorskip("anzhuo.router")
            async def fake_route(text, *a, **k):
                return router.Route("new", None, "前端", "", "dev", "")
            monkeypatch.setattr(router, "route", fake_route)
            monkeypatch.setattr(folders, "candidates", lambda tok, cfg: [Path("/x")] if tok == "前端" else [])
            await c.post("/_x/api/send", json={"text": "新做一节课", "folder": "auto"})
            assert b.new[-1] == (["前端"], "新做一节课")
            await c.post("/_x/api/send", json={"text": "查天气", "folder": "scratch"})
            assert b.new[-1] == ([], "查天气")
            # 手输的路径或文件夹名：找到一个就在那里开；找不到、找到好几个都当场说清楚
            monkeypatch.undo()
            proj = tmp_path / "proj"
            proj.mkdir()
            await c.post("/_x/api/send", json={"text": "改一下", "folder": str(proj)})
            assert b.new[-1] == ([str(proj)], "改一下")
            r = await (await c.post("/_x/api/send", json={"text": "改一下", "folder": str(tmp_path / "没有这个")})).json()
            assert not r["ok"] and "没找到文件夹" in r["error"]
            monkeypatch.setattr(folders, "search", lambda kw, roots, skip, **k: [tmp_path / "a" / "demo", tmp_path / "b" / "demo"])
            r = await (await c.post("/_x/api/send", json={"text": "改一下", "folder": "demo"})).json()
            assert not r["ok"] and "找到好几个" in r["error"]
            monkeypatch.undo()
            # 选模型和思考深度：新开时带上；已有对话改了存下来，下一轮生效；不认得的不理
            await c.post("/_x/api/send", json={"text": "查天气", "folder": "scratch", "model": "sonnet", "effort": "high"})
            assert b.new_model == ("claude-sonnet-5-5", "high")
            await c.post("/_x/api/send", json={"text": "查天气", "folder": "scratch", "model": "llama", "effort": "超高"})
            assert b.new_model == (None, None)
            await c.post("/_x/api/send", json={"text": "查天气", "folder": "scratch", "model": "gpt"})
            assert b.new_model == ("gpt", None)                       # 用 Codex（GPT）新开
            b.registry.save = lambda: None
            assert (await (await c.post("/_x/api/settings/7", json={"model": "haiku", "effort": "xhigh"})).json())["ok"]
            assert (b.registry.sessions[7].model, b.registry.sessions[7].effort) == ("claude-haiku-5-5", "xhigh")
            u = (await (await c.get("/_x/api/thread/7")).json())["usage"]
            assert (u["mkey"], u["ekey"]) == ("haiku", "xhigh")
            # 手机通知：后台脚本不登录也能取；订阅要像样（https 地址 + keys）才收
            assert "showNotification" in await (await c.get("/_x/sw.js")).text()
            sub = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "x", "auth": "y"}}
            assert (await c.post("/_x/api/push", json=sub)).status == 200
            assert (await c.post("/_x/api/push", json=sub)).status == 200
            assert (await c.post("/_x/api/push", json={"endpoint": "http://evil"})).status == 400
            assert w.push.subs == [sub]
            # 退出登录：这台设备的登录和通知都清掉，再访问要重新登录
            r = await c.post("/_x/api/logout", json={"endpoint": sub["endpoint"]})
            assert r.status == 200 and w.push.subs == [] and w.store["sessions"] == {}
            assert (await c.get("/_x/api/sessions")).status == 401
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            assert len(w.store["sessions"]) == 2
            await c.post("/_x/api/logout", json={"all": True})
            assert w.store["sessions"] == {}
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            assert (await (await c.post("/_x/api/stop/7", json={})).json())["stopped"] and b.runner.stopped == [7]
            assert (await c.post("/_x/api/send", json={"text": ""})).status == 400
        finally:
            await c.close()

    asyncio.run(go())


def test_files_delivered_and_uploaded(tmp_path):
    """交给你的文件：复制到远窗自己的文件夹、过程页和对话里能下载（原文件名）；网页传上来的文件存进收件箱。"""
    import base64 as b64
    from types import SimpleNamespace as NS

    async def go():
        w = Web("http://x.example", 0, tmp_path, user="u", pw_hash=hash_password("p", iters=1000))
        src = tmp_path / "tmpdir" / "研究生毕业证.pdf"
        src.parent.mkdir()
        src.write_bytes(b"%PDF-1.4 fake")
        t = Turn(started=time.time() - 60, ended=time.time(), request="发毕业证", final_text="发了", files=[str(src)])
        tid = w.save_turn("#7 毕业证", t, str(tmp_path), "做完了")
        src.unlink()                                                      # 原处被清掉也不影响下载
        rec = w._records(7)[0]
        assert rec["files"][0]["name"] == "研究生毕业证.pdf" and rec["files"][0]["size"] == 13
        sent = []

        async def _message(cmd, inc, routed=False):
            sent.append(cmd.text)
        async def say(*a):
            pass
        w.bridge = NS(runner=NS(turns={}), user_id="me", record=lambda *a, **k: None, message=_message,
                      outgoing={}, say=say, registry=NS(get=lambda n: None))
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            r = await c.get(f"/_x/t/{tid}/file/0")
            assert r.status == 200 and await r.read() == b"%PDF-1.4 fake"
            assert "inline" in r.headers["Content-Disposition"] and "%E7%A0%94" in r.headers["Content-Disposition"]
            assert r.headers["Content-Type"] == "application/pdf"
            r = await c.get(f"/dl/{tid}/0?download=1")
            assert r.status == 200 and await r.read() == b"%PDF-1.4 fake"
            assert "attachment" in r.headers["Content-Disposition"]
            r = await c.get(f"/dl/{tid}/0", headers={"Range": "bytes=0-3"})
            assert r.status == 206 and await r.read() == b"%PDF"
            assert "研究生毕业证.pdf" in await (await c.get(f"/_x/t/{tid}")).text()
            r = await c.get(f"/dl/{tid}/0")   # 网页里的下载地址在 /_x 外（主屏幕打开的案头能返回）
            assert r.status == 200 and await r.read() == b"%PDF-1.4 fake"
            data = "data:application/pdf;base64," + b64.b64encode(b"hello").decode()
            r = await (await c.post("/_x/api/send", json={"num": 7, "text": "看看这个", "files": [{"name": "../合同.pdf", "data": data}]})).json()
            assert r["ok"] and "附件已存到" in sent[0] and sent[0].endswith("合同.pdf）")
            saved = sent[0].split("附件已存到：")[1].rstrip("）")
            assert Path(saved).read_bytes() == b"hello" and Path(saved).parent == tmp_path / "inbox" / "web"
            batch = [{"name": f"photo-{i:02d}.jpg", "data": data} for i in range(18)]
            response = await c.post("/_x/api/send", json={"num": 7, "text": "全部原件", "files": batch})
            assert response.status == 200
            assert (await response.json())["ok"]
            uploaded_paths = re.findall(r"附件已存到：([^）]+)", sent[-1])
            assert len(uploaded_paths) == 18
            assert all(Path(p).read_bytes() == b"hello" for p in uploaded_paths)
            # 新网页直接传原始文件，不能再把整个 ZIP 转成 base64 JSON。
            import json
            from aiohttp import FormData
            form = FormData()
            form.add_field("message", json.dumps({"num": 7, "text": "原始文件直传"}))
            for i in range(18):
                form.add_field("files", bytes([i]) * 4096, filename=f"{i:02d}.zip", content_type="application/zip")
            response = await c.post("/_x/api/send", data=form)
            assert response.status == 200
            assert (await response.json())["ok"]
            paths = re.findall(r"附件已存到：([^）]+)", sent[-1])
            assert len(paths) == 18
            assert [Path(p).read_bytes() for p in paths] == [bytes([i]) * 4096 for i in range(18)]
            assert all(Path(p).name.endswith(f"{i:02d}.zip") for i, p in enumerate(paths))
            before = set((tmp_path / "inbox" / "web").iterdir())
            calls = len(sent)
            form = FormData()
            form.add_field("message", json.dumps({"num": 7, "text": "坏请求"}))
            form.add_field("files", b"partial", filename="partial.zip")
            form.add_field("unexpected", "bad")
            response = await c.post("/_x/api/send", data=form)
            assert response.status == 400 and len(sent) == calls
            assert set((tmp_path / "inbox" / "web").iterdir()) == before
        finally:
            await c.close()

    asyncio.run(go())


def test_process_translation_no_flip(tmp_path, monkeypatch):
    """工作过程里还在译的英文先不给（显示“翻译中…”），译好直接给中文；译不出的给原文、不再反复试。"""
    from anzhuo import translate
    monkeypatch.setattr(translate, "enabled", True)   # 翻译默认关闭，这里测打开时的行为
    import hashlib
    w = Web("http://x.example", 0, tmp_path)
    en = "Let me check the file first and then run the tests to see what fails here"
    k = hashlib.sha1(en.encode()).hexdigest()
    w._zh_busy.add(k)
    assert w._zh_proc({"k": "think", "text": en}) == {"k": "think", "text": "", "wait": True}
    w._zh_busy.discard(k)
    w._zh[k] = "我先看看文件"
    assert w._zh_proc({"k": "think", "text": en})["text"] == "我先看看文件"
    del w._zh[k]
    w._zh_failed.add(k)
    assert w._zh_proc({"k": "think", "text": en}) == {"k": "think", "text": en}


def test_devices(tmp_path):
    """登录的设备：列出几台、哪台是这台；退出别的那台它就进不来了；不能用这个退出自己。"""
    async def go():
        w = Web("http://x.example", 0, tmp_path, user="u", pw_hash=hash_password("p", iters=1000))
        phone = TestClient(TestServer(w.app()))
        await phone.start_server()
        pc = TestClient(phone.server)
        try:
            await phone.post("/_x/login", data={"username": "u", "password": "p"},
                             headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0) AppleWebKit Safari/605"})
            await pc.post("/_x/login", data={"username": "u", "password": "p"},
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/150 Safari/537 Edg/150"})
            d = (await (await phone.get("/_x/api/devices")).json())["devices"]
            assert len(d) == 2 and d[0]["me"] and d[0]["name"] == "iPhone · Safari" and d[1]["name"] == "Windows 电脑 · Edge"
            assert (await phone.post("/_x/api/devices/logout", json={"id": d[0]["id"]})).status == 404   # 自己这台不走这里
            assert (await (await phone.post("/_x/api/devices/logout", json={"id": d[1]["id"]})).json())["ok"]
            assert (await pc.get("/_x/api/sessions")).status == 401
            assert len((await (await phone.get("/_x/api/devices")).json())["devices"]) == 1
        finally:
            await pc.close()
            await phone.close()

    asyncio.run(go())


def test_presence_needs_idle_aware_page(tmp_path):
    """“我在看”只认会判断人走开的新页面（v=2）：老页面在 Mac 屏幕灭了照样报，会把手机通知全挡掉。"""
    from types import SimpleNamespace as NS

    async def go():
        w = Web("http://x.example", 0, tmp_path, user="u", pw_hash=hash_password("p", iters=1000))
        w.bridge = NS(registry=NS(mine=lambda n, owner: True))
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            await c.post("/_x/api/presence", json={"num": 5, "visible": True})
            assert not w.watching(5)                                                   # 老页面报的不算
            await c.post("/_x/api/presence", json={"num": 5, "visible": True, "v": 2})
            assert w.watching(5)
            await c.post("/_x/api/presence", json={"num": 5, "visible": False, "v": 2})  # 走开了
            assert not w.watching(5)
        finally:
            await c.close()

    asyncio.run(go())


def test_records_match_by_time_while_running():
    """正在做的那轮还没有记录：按时间对，文件挂在它所属的那条回复上，不整体错一位。"""
    from anzhuo import thread
    items = [{"who": "you", "text": "a"}, {"who": "claude", "text": "A", "ts": 100.0},
             {"who": "you", "text": "b"}, {"who": "claude", "text": "B", "ts": 200.0},
             {"who": "you", "text": "c"}, {"who": "claude", "text": "正在写…", "ts": 300.0}]
    recs = [{"id": "4-90", "started": 90, "ended": 101, "files": []},
            {"id": "4-190", "started": 190, "ended": 201, "files": [{"name": "设计.md"}]}]
    out = thread.attach_records(items, recs)
    cl = [i for i in out if i["who"] == "claude"]
    assert [i.get("tid") for i in cl] == ["4-90", "4-190", None]
    assert cl[1]["files"] == [{"name": "设计.md"}] and "files" not in cl[2]


def test_manage_and_search(tmp_path):
    """列表管理：置顶、改名（之后不再自动起名）、删除（在做的删不了）、找回；搜索标题、说过的话、已删除。"""
    from types import SimpleNamespace as NS
    from anzhuo.sessions import Registry, Session

    folders = {"平台": str(tmp_path / "plat")}

    async def go():
        reg = Registry(tmp_path / "sessions.json")
        for n, name in [(1, "大数据第二节 · 封面"), (2, "天气"), (3, "在做的活")]:
            reg.add(Session(num=n, name=name, cwd=str(tmp_path / "plat"), engine="codex"))
        w = Web("http://x.example", 0, tmp_path, user="u", pw_hash=hash_password("p", iters=1000))
        t = Turn(started=time.time() - 60, ended=time.time(), request="明天要不要带伞", final_text="不用带伞，多云")
        w.save_turn("#2 天气", t, str(tmp_path), "做完了")
        w.bridge = NS(registry=reg, cfg=NS(folders=folders, scratch_root="/tmp/anzhuo-test-scratch"),
                      runner=NS(turns={3: Turn(started=time.time(), request="x")}, busy=lambda n: n == 3, compacting=lambda n: None, limits={}, limits_gpt={}, last={}))
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            post = lambda n, a, v=None: c.post(f"/_x/api/manage/{n}", json={"action": a, "value": v})
            assert (await (await post(1, "pin", True)).json())["ok"] and reg.get(1).pinned
            await post(2, "rename", "明天的天气")
            assert reg.get(2).name == "明天的天气" and reg.get(2).renamed
            r = await (await post(3, "delete")).json()
            assert not r["ok"] and not reg.get(3).hidden                     # 在做的删不了
            await post(1, "delete")
            assert reg.get(1).hidden and reg.get(1).hidden_at and not reg.get(1).pinned
            rows = (await (await c.get("/_x/api/sessions")).json())["sessions"]
            assert 1 not in [x["num"] for x in rows] and {x["num"] for x in rows} == {2, 3}
            s = await (await c.get("/_x/api/search", params={"q": "封面"})).json()
            assert [x["num"] for x in s["deleted"]] == [1] and not s["title"]
            s = await (await c.get("/_x/api/search", params={"q": "带伞"})).json()
            assert s["said"][0]["num"] == 2 and "带伞" in s["said"][0]["snippet"]
            reg.get(2).awaiting, reg.get(2).awaiting_kind = "要不要做？", "decide"
            r = await (await post(3, "dismiss")).json()
            assert not r["ok"]                                              # 在做的没有可撤的状态
            assert (await (await post(2, "dismiss")).json())["ok"] and not reg.get(2).awaiting and not reg.get(2).awaiting_kind
            await post(1, "restore")
            assert not reg.get(1).hidden
            s = await (await c.get("/_x/api/search", params={"q": "封面"})).json()
            assert [x["num"] for x in s["title"]] == [1]
        finally:
            await c.close()

    asyncio.run(go())


def test_model_names_follow_configuration(tmp_path, monkeypatch):
    from anzhuo.web import display_model, model_options
    from anzhuo.engines import MODELS
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('model = "gpt-6.1-sol"\n')
    monkeypatch.setitem(MODELS, "opus", "claude-opus-7-2")
    opts = dict(model_options())
    assert opts["opus"] == "Claude Opus 7.2"
    assert opts["gpt"] == "GPT-6.1 Sol"
    assert opts["fable"] == "Claude Fable 5.1"
    assert display_model("GPT（gpt-6.1-sol）") == "GPT-6.1 Sol"
    assert display_model("claude-haiku-5-5") == "Claude Haiku 5.5"
    assert display_model("unknown-version") == "unknown-version"
    (tmp_path / "config.toml").write_text("bad = [")
    assert dict(model_options())["gpt"] == "GPT"
    (tmp_path / "config.toml").unlink()
    assert dict(model_options())["gpt"] == "GPT"


def test_fork(tmp_path, monkeypatch):
    """分叉：另开一个对话，带着到那条回复为止的记录、从那里另起；原对话不动。从第一句分叉 = 从头另开。"""
    from types import SimpleNamespace as NS
    from anzhuo import thread as th
    from anzhuo.sessions import Session

    p = Session(num=6, name="大数据第二节磨课", cwd=str(tmp_path), claude_id="sid6", model="m", worked=True)
    sessions = {6: p}
    nums = iter([9, 10])
    reg = NS(sessions=sessions, get=sessions.get, visible=lambda owner="admin": list(sessions.values()), save=lambda: None,
             reserve_num=lambda: next(nums), add=lambda s: sessions.setdefault(s.num, s))
    monkeypatch.setattr(th, "full_messages", lambda sid, cwd, times=None: sid)
    monkeypatch.setattr(th, "history", lambda msgs, limit=300, times=None: [
        {"who": "you", "text": "一", "uuid": "u1"}, {"who": "claude", "text": "好", "end": "a1"},
        {"who": "you", "text": "二", "uuid": "u2"}, {"who": "claude", "text": "好", "end": "a2"}])

    async def go():
        w = Web("http://x", 0, tmp_path)
        w.bridge = NS(registry=reg, record=lambda *a, **k: None)
        w._logged_in = lambda r: True
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            r = await (await c.post("/_x/api/fork/6", json={"to": "a1"})).json()
            s = sessions[r["num"]]
            assert (s.claude_id, s.rewind_at, s.forking, s.fork_from, s.cwd, s.model) == ("sid6", "a1", True, 6, p.cwd, "m")
            assert p.claude_id == "sid6" and not p.rewind_at                       # 原对话不动
            assert (await (await c.post("/_x/api/rewind/" + str(s.num), json={"cancel": True})).json())["ok"] is False
            assert (await (await c.post("/_x/api/fork/6", json={"to": "zz"})).json())["ok"] is False
            r = await (await c.post("/_x/api/fork/6", json={"to": ""})).json()
            assert sessions[r["num"]].claude_id is None and not sessions[r["num"]].forking
        finally:
            await c.close()

    asyncio.run(go())


def test_codex_rewind_fork_cancel_and_persist(tmp_path):
    import json
    from types import SimpleNamespace as NS
    from anzhuo.sessions import Registry, Session
    async def go():
        registry = Registry(tmp_path / "sessions.json")
        session = registry.add(Session(num=registry.reserve_num(), name="原对话", cwd=str(tmp_path), engine="codex", codex_id="native-parent"))
        runner = NS(unqueue_last=lambda n: None, busy=lambda n: False, last={})
        w = Web("http://x", 0, tmp_path)
        w.bridge = NS(registry=registry, runner=runner, outgoing={})
        w.bridge.relays = NS(writing={}, targets=lambda s: [])   # 不接力
        for i in (100, 200, 300):
            w.save_turn(session.label, Turn(started=i, ended=i + 10, request=f"问{i}", final_text=f"答{i}",
                        codex_turn_id=f"native-{i}"), str(tmp_path), "测试")
        assert [x.get("end") for x in w._codex_history(1) if x["who"] == "claude"] == ["1-100", "1-200", "1-300"]
        async def request(data):
            async def read(): return data
            return NS(headers={}, match_info={"num": "1"}, json=read)
        result = json.loads((await w._api_fork(await request({"to": "1-200"}))).text)
        fork = registry.get(result["num"])
        assert fork.engine == "codex" and fork.codex_id == "native-parent" and fork.codex_cut_turn == "native-200"
        assert [x["id"] for x in w._records(fork.num)] == ["1-100", "1-200"]
        assert len(w._records(1)) == 3
        nofiles = json.loads((await w._rewind_codex(session, {"to": "1-100", "files": True})).text)
        assert not nofiles["ok"] and session.rewind_at == ""
        result = json.loads((await w._rewind_codex(session, {"to": "1-100"})).text)
        assert result["ok"] and result["text"] == "问200" and session.codex_id == "native-parent"
        assert [x["id"] for x in w._records(1)] == ["1-100"]
        saved = Registry(tmp_path / "sessions.json").get(1)
        assert saved.codex_cut_turn == "native-100" and saved.codex_pending_hidden == ["1-200", "1-300"]
        w._threads[1] = ((), [])
        result = json.loads((await w._api_rewind(await request({"cancel": True}))).text)
        assert result["ok"] and not session.rewind_at and len(w._records(1)) == 3 and 1 not in w._threads
        result = json.loads((await w._rewind_codex(session, {"to": "foreign-id"})).text)
        assert not result["ok"] and len(w._records(1)) == 3
        result = json.loads((await w._api_fork(await request({"to": ""}))).text)
        empty = registry.get(result["num"])
        assert empty.engine == "codex" and empty.codex_id is None and not w._records(empty.num)
    asyncio.run(go())


def test_codex_native_fork_failure_keeps_original():
    from anzhuo.codex import CodexConn, CodexError
    async def go():
        c = CodexConn("/tmp", lambda *a: None)
        c.thread_id = "parent"
        async def request(method, params):
            assert method == "thread/fork" and params["lastTurnId"] == "turn-1"
            return {"thread": {"id": "parent"}}
        c.request = request
        try:
            await c.fork_thread("parent", "turn-1")
            assert False
        except CodexError:
            assert c.thread_id == "parent"
    asyncio.run(go())


def test_codex_next_message_uses_selected_branch(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from anzhuo import codex, sessions
    from anzhuo.engines import CODEX, base
    async def go():
        registry = sessions.Registry(tmp_path / "sessions.json")
        s = registry.add(sessions.Session(num=1, name="测试", cwd=str(tmp_path), engine="codex",
            codex_id="parent", rewind_at="1-100", codex_cut_turn="turn-100", codex_hidden=["1-200"],
            codex_pending_hidden=["1-200"]))
        calls = []
        class Conn:
            def __init__(self, *args):
                self.done = asyncio.Event()
                self.model = "gpt-6.1-sol"
                self.turn_status = "completed"
            async def start(self): pass
            async def fork_thread(self, parent, cut):
                calls.append((parent, cut))
                return "new-branch"
            async def open_thread(self, *args):
                raise AssertionError("回退后不能直接接着原分支")
            async def start_turn(self, text, images, effort, model=None):
                assert s.codex_id == "new-branch" and not s.rewind_at
                calls.append(text)
                self.done.set()
                return "new-turn"
            async def close(self): pass
        monkeypatch.setattr(codex, "CodexConn", Conn)
        monkeypatch.setattr(base, "blocked", lambda *a: "")
        runner = NS(default_effort="medium", live={}, registry=registry)
        turn = Turn(started=time.time())
        await CODEX.run(runner, s, sessions.Job("新问题"), turn)
        assert calls == [("parent", "turn-100"), "新问题"]
        assert turn.codex_turn_id == "new-turn" and not turn.is_error
        restored = sessions.Registry(tmp_path / "sessions.json").get(1)
        assert restored.codex_id == "new-branch" and restored.codex_hidden == ["1-200"]
        assert not restored.codex_pending_hidden and not restored.rewind_at
    asyncio.run(go())


def test_codex_file_rewind_and_cancel_through_api(tmp_path):
    import json
    from types import SimpleNamespace as NS
    from anzhuo.checkpoints import Checkpoints
    from anzhuo.sessions import Registry, Session
    async def go():
        work = tmp_path / "work"; work.mkdir()
        data = tmp_path / "data"; data.mkdir()
        registry = Registry(data / "sessions.json")
        s = registry.add(Session(num=registry.reserve_num(), name="课", cwd=str(work), engine="codex", codex_id="native"))
        runner = NS(unqueue_last=lambda n: None, busy=lambda n: False, last={})
        w = Web("http://x", 0, data)
        w.bridge = NS(registry=registry, runner=runner, outgoing={})
        w.bridge.relays = NS(writing={}, targets=lambda s: [])   # 不接力
        f = work / "课件.txt"; f.write_text("第一轮")
        store = Checkpoints(data / "checkpoints")
        before = store.capture([str(work)])
        w.save_turn(s.label, Turn(started=100, ended=110, request="第一问", final_text="第一答", codex_turn_id="t1"), str(work), "测试")
        f.write_text("第二轮")
        after = store.capture([str(work)])
        w.save_turn(s.label, Turn(started=200, ended=210, request="第二问", final_text="第二答", codex_turn_id="t2",
                    checkpoint_before=before, checkpoint_after=after), str(work), "测试")
        async def request(data):
            async def read(): return data
            return NS(headers={}, match_info={"num": "1"}, json=read)
        result = json.loads((await w._api_rewind(await request({"to": "1-100", "files": True}))).text)
        assert result["ok"] and f.read_text() == "第一轮" and s.codex_file_undo
        # 撤销时有后续人工修改，不能把人工编辑覆盖。
        f.write_text("人工修改")
        result = json.loads((await w._api_rewind(await request({"cancel": True}))).text)
        assert not result["ok"] and f.read_text() == "人工修改" and s.rewind_at
        f.write_text("第一轮")
        result = json.loads((await w._api_rewind(await request({"cancel": True}))).text)
        assert result["ok"] and f.read_text() == "第二轮" and not s.rewind_at and not s.codex_file_undo
        assert len(w._records(1)) == 2
        # 同目录另一个对话忙时，拒绝文件恢复。
        other = registry.add(Session(num=registry.reserve_num(), name="另一个", cwd=str(work), engine="codex"))
        runner.busy = lambda n: n == other.num
        result = json.loads((await w._api_rewind(await request({"to": "1-100", "files": True}))).text)
        assert not result["ok"] and f.read_text() == "第二轮" and not s.rewind_at
    asyncio.run(go())


def test_quota_reset_epoch_is_absolute(monkeypatch):
    import datetime
    import time
    now = datetime.datetime(2026, 12, 31, 23, 0).timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    rows = Web._limits({"seven_day": (.99, "1月1日 04:00"), "five_hour": (.3, "12月31日 22:00"), "unknown": (.1, None)})
    assert rows[0]["reset_at"] == datetime.datetime(2027, 1, 1, 4).timestamp()
    assert rows[1]["reset_at"] < now  # 已过期的时间不能被误认成明年
    assert rows[2]["reset_at"] is None

def test_schedule_management_and_natural_request_api(tmp_path):
    from datetime import datetime, timedelta
    from types import SimpleNamespace as NS
    from anzhuo.app import Bridge
    from anzhuo.config import Config
    from anzhuo.schedules import TZ
    from anzhuo.sessions import Session
    async def go():
        b=Bridge(Config(client_id='test',allowed_user_ids=['me'],scratch_root=str(tmp_path/'scratch')),NS(secret='x'),tmp_path/'data')
        w=b.web;w.base='http://x.example';w.user='u';w.pw_hash=hash_password('p',iters=1000)
        s=b.registry.add(Session(num=b.registry.reserve_num(),name='现有对话',cwd=str(tmp_path)))
        c=TestClient(TestServer(w.app()));await c.start_server()
        try:
            assert (await c.get('/_x/api/schedules')).status==401
            await c.post('/_x/login',data={'username':'u','password':'p'})
            w.base='https://x.example'  # HTTP TestServer 已取得测试 Cookie，再验证生产同源规则
            data={'action':'save','prompt':'整理要闻','kind':'once','model':'gpt','run_at':(datetime.now(TZ)+timedelta(hours=1)).isoformat()}
            created=await (await c.post('/_x/api/schedules',json=data)).json()
            assert created['ok'];key=created['task']['id']
            rows=await (await c.get('/_x/api/schedules')).json();assert len(rows['tasks'])==1
            assert (await c.post('/_x/api/schedules',json={'id':key,'action':'pause'},headers={'Origin':'https://evil.example'})).status==403
            for action in ['pause','resume']:
                assert (await (await c.post('/_x/api/schedules',json={'id':key,'action':action})).json())['ok']
            data.update(id=key,prompt='修改后的内容')
            assert (await (await c.post('/_x/api/schedules',json=data)).json())['task']['prompt']=='修改后的内容'
            natural=await (await c.post('/_x/api/send',json={'num':s.num,'text':'两小时后接着这个对话做'})).json()
            assert natural['scheduled']['target']==s.num and not b.runner.busy(s.num)
            bad=await (await c.post('/_x/api/send',json={'text':'每天早上25点整理'})).json()
            assert not bad['ok']
            assert (await (await c.post('/_x/api/schedules',json={'id':key,'action':'delete'})).json())['ok']
            assert key not in b.schedules.tasks
        finally:await c.close()
    asyncio.run(go())


def test_turn_duration_matches_record_identity_not_next_reply():
    from anzhuo import thread
    items = [{"who": "claude", "text": "第一轮", "end": "1-100", "ts": 110},
             {"who": "claude", "text": "刚完成但还没存档", "ts": 112, "elapsed": 2}]
    records = [{"id": "1-100", "started": 100, "ended": 110}]
    out = thread.attach_records(items, records)
    assert out[0]["elapsed"] == 10 and out[0]["tid"] == "1-100"
    assert out[1]["elapsed"] == 2 and "tid" not in out[1]
    for rec in [{"id": "x", "started": 10}, {"id": "x", "started": 10, "ended": 9}]:
        assert "elapsed" not in thread.attach_records([{"who": "claude", "text": "旧记录"}], [rec])[0]


def test_claude_late_archive_refreshes_attachments(tmp_path, monkeypatch):
    """先读到最终文字、稍后才归档附件：缓存与实时事件都必须更新。"""
    from types import SimpleNamespace as NS
    from anzhuo.sessions import Session
    from anzhuo import thread

    async def go():
        s = Session(num=6, name="交付", cwd=str(tmp_path), claude_id="sid6", updated=120)
        w = Web("http://x.example", 0, tmp_path, user="u", pw_hash=hash_password("p", iters=1000))
        w.bridge = NS(registry=NS(get=lambda n: s, save=lambda: None), outgoing={},
                      runner=NS(turns={}, limits={}, default_effort="medium", compacting=lambda n: "",
                                busy=lambda n: False, queued=lambda n: []), relays=NS(writing={}, targets=lambda s: []))
        monkeypatch.setattr(thread, "full_messages", lambda *a: [])
        monkeypatch.setattr(thread, "history", lambda *a, **kw: [{"who": "claude", "text": "图片和文件已做好", "ts": 119}])
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            before = w._sig_thread(6)
            data = await (await c.get("/_x/api/thread/6")).json()
            assert not data["items"][0].get("files")
            img, pdf = tmp_path / "图.png", tmp_path / "文件.pdf"
            img.write_bytes(b"\x89PNG")
            pdf.write_bytes(b"%PDF-1.4")
            tid = w.save_turn("#6 交付", Turn(started=100, ended=120, final_text="图片和文件已做好",
                                         images=[str(img)], files=[str(pdf)]), str(tmp_path), "完成")
            assert s.updated == 120 and w._sig_thread(6) != before
            data = await (await c.get("/_x/api/thread/6")).json()
            reply = data["items"][0]
            assert reply["tid"] == tid and len(reply["images"]) == len(reply["files"]) == 1
            for url, expected in [(reply["images"][0], img.read_bytes()), (reply["files"][0]["url"], pdf.read_bytes())]:
                r = await c.get(url)
                assert r.status == 200 and await r.read() == expected
        finally:
            await c.close()
    asyncio.run(go())


def test_all_selected_images_reach_model_in_order(tmp_path):
    """网页选 18 张必须原样送出，忙碌时插话也不能静默只留前 6 张。"""
    import base64
    from types import SimpleNamespace as NS
    from anzhuo.codex import CodexConn

    async def go():
        w = Web('http://x.example', 0, tmp_path, user='u', pw_hash=hash_password('p', iters=1000))
        received = []

        async def message(cmd, inc, routed=False):
            received.append(list(inc.local_images))

        runner = NS(turns={})
        w.bridge = NS(runner=runner, user_id='me', record=lambda *a, **k: None, message=message)
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            await c.post('/_x/login', data={'username': 'u', 'password': 'p'})
            payloads = [f'photo-{i}'.encode() for i in range(18)]
            images = ['data:image/jpeg;base64,' + base64.b64encode(p).decode() for p in payloads]
            for busy in [False, True]:
                runner.turns = {7: object()} if busy else {}
                r = await c.post('/_x/api/send', json={'num': 7, 'text': '检查这些照片', 'images': images})
                assert r.status == 200
                assert (await r.json())['injected'] == busy
                paths = received[-1]
                assert len(paths) == 18
                assert len(set(paths)) == 18
                assert [Path(p).read_bytes() for p in paths] == payloads
                model_input = CodexConn._input("检查这些照片", [Path(p) for p in paths])
                assert [p["path"] for p in model_input if p["type"] == "localImage"] == paths
        finally:
            await c.close()
    asyncio.run(go())


def test_web_hash_env_first_and_file_private(tmp_path, monkeypatch):
    """ANZHUO_WEB_HASH 优先（演示、测试靠它）；非 macOS 的 web_hash.json 只给自己读写。"""
    import os
    import sys
    from anzhuo import config
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ANZHUO_WEB_HASH", "env-hash")
    assert config.read_web_hash("u") == "env-hash"
    monkeypatch.delenv("ANZHUO_WEB_HASH")
    assert config.read_web_hash("u") == ""
    config.write_web_hash("u", "h1")
    config.write_web_hash("v", "h2")
    assert config.read_web_hash("u") == "h1" and config.read_web_hash("v") == "h2"
    if os.name != "nt":
        assert (tmp_path / "web_hash.json").stat().st_mode & 0o777 == 0o600
    (tmp_path / "web_hash.json").write_text("[]")
    assert config.read_web_hash("u") == ""


def test_browse_drive_level(tmp_path, monkeypatch):
    """Windows 的“此电脑”层：列出所有盘符（这台机器没有 listdrives 就是空列表），不能选它当文件夹。"""
    async def go():
        w = Web("http://x.example", 0, tmp_path / "data", user="u", pw_hash=hash_password("p", iters=1000))
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            d = await (await c.get("/_x/api/browse", params={"path": "__drives__"})).json()
            assert d["drive_level"] and d["absolute"] and isinstance(d["dirs"], list)
        finally:
            await c.close()
    asyncio.run(go())


def test_browse_defaults_to_projects_and_lists_only_visible_dirs(tmp_path, monkeypatch):
    """新对话的“浏览文件夹…”：默认从 ~/Projects 开始，只列看得见的子文件夹，要登录。"""
    root = tmp_path / "Projects"
    for d in ["乙", "甲", ".藏起来"]:
        (root / d).mkdir(parents=True)
    (root / "文件.txt").write_text("x")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    async def go():
        w = Web("http://x.example", 0, tmp_path / "data", user="u", pw_hash=hash_password("p", iters=1000))
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            assert (await c.get("/_x/api/browse", allow_redirects=False)).status in (302, 401, 403)
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            d = await (await c.get("/_x/api/browse")).json()
            assert d["raw"] == str(root.resolve()) and d["dirs"] == sorted(["乙", "甲"], key=str.lower)
            assert d["parent"] == str(tmp_path.resolve())
            d = await (await c.get("/_x/api/browse", params={"path": str(root / "甲")})).json()
            assert d["dirs"] == [] and d["parent"] == str(root.resolve())
            assert (await c.get("/_x/api/browse", params={"path": str(root / "没有这个")})).status == 400
        finally:
            await c.close()
    asyncio.run(go())


def test_resume_claude_code_history(tmp_path, monkeypatch):
    """电脑端对话：不分文件夹列出时去掉远窗自己开的、临时文件夹和家目录里的；只接这个文件夹里真有的；接过的（含收起的）回到原来那个，不重复开；写接口校验同源。"""
    from types import SimpleNamespace as NS
    from anzhuo.sessions import Registry, Session

    proj = tmp_path / "proj"
    proj.mkdir()
    sid_a, sid_b = "aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444"
    scratch = tmp_path / "scratch"
    sid_own, sid_tmp, sid_home = ("dddddddd-0000-0000-0000-00000000000" + c for c in "123")
    infos = [NS(session_id=sid_a, custom_title="", summary="修登录\n报错", first_prompt="", last_modified=1_700_000_000_000, cwd=str(proj)),
             NS(session_id=sid_b, custom_title="周报", summary="", first_prompt="", last_modified=1_700_000_100_000, cwd=str(proj))]
    sid_live = "eeeeeeee-1111-2222-3333-444444444444"
    live = [NS(session_id=sid_live, custom_title="电脑上刚写过", summary="", first_prompt="", last_modified=int(time.time() * 1000) - 30_000, cwd=str(proj))]
    others = [NS(session_id=sid_own, custom_title="远窗自己开的", summary="", first_prompt="", last_modified=1, cwd=str(proj)),
              NS(session_id=sid_tmp, custom_title="临时", summary="", first_prompt="", last_modified=1, cwd=str(scratch / "20261103-1")),
              NS(session_id=sid_home, custom_title="家目录", summary="", first_prompt="", last_modified=1, cwd=str(Path.home()))]
    monkeypatch.setattr(Web, "_cc_list", staticmethod(lambda d, limit: infos + others if d is None else infos + live if Path(d) == proj else []))

    async def go():
        reg = Registry(tmp_path / "sessions.json")
        old = reg.add(Session(num=reg.reserve_num(), name="早接过的", cwd=str(proj), claude_id=sid_b, hidden=True, hidden_at=time.time(), from_desktop=True))
        reg.add(Session(num=reg.reserve_num(), name="远窗开的", cwd=str(proj), claude_id=sid_own))
        w = Web("http://x.example", 0, tmp_path, user="u", pw_hash=hash_password("p", iters=1000))
        w.bridge = NS(registry=reg, cfg=NS(scratch_root=str(scratch)))
        c = TestClient(TestServer(w.app()))
        await c.start_server()
        try:
            await c.post("/_x/login", data={"username": "u", "password": "p"})
            d = await (await c.get("/_x/api/cc-sessions")).json()               # 电脑端对话：不分文件夹
            assert [(x["id"], x["cwd"], x["num"]) for x in d["sessions"]] == [(sid_a, str(proj), None), (sid_b, str(proj), old.num)]
            d = await (await c.get("/_x/api/cc-sessions", params={"path": str(proj)})).json()
            assert [(x["id"], x["title"], x["num"]) for x in d["sessions"]] == [(sid_a, "修登录 报错", None), (sid_b, "周报", old.num), (sid_live, "电脑上刚写过", None)]
            resume = lambda sid, path=str(proj), **kw: c.post("/_x/api/resume-cc", json={"path": path, "session_id": sid, "name": "接着"}, **kw)
            w.base = "https://x.example"                                                       # 外网地址才校验同源
            assert (await resume(sid_a, headers={"Origin": "https://evil.example"})).status == 403
            assert (await resume("cccccccc-0000-0000-0000-000000000000")).status == 400      # 不在这个文件夹里
            assert (await resume(sid_a, path=str(tmp_path / "别处"))).status == 400
            assert (await resume("../../etc")).status == 400
            r = await (await resume(sid_a)).json()
            s = reg.get(r["num"])
            assert s.claude_id == sid_a and s.cwd == str(proj) and s.from_desktop
            assert (await (await resume(sid_a)).json())["num"] == r["num"]                  # 再接一次不重复开
            r = await (await resume(sid_b)).json()
            assert r["num"] == old.num and r["existing"] and not old.hidden                 # 收起的找回来
            r = await (await resume(sid_live)).json()
            assert r["warn"] and 25 <= r["ago"] <= 60 and len(reg.sessions) == 3              # 电脑上可能还开着：先提醒，不接
            r = await (await c.post("/_x/api/resume-cc", json={"path": str(proj), "session_id": sid_live, "force": True})).json()
            assert reg.get(r["num"]).claude_id == sid_live                                       # 确认关掉了再接
            assert len(reg.sessions) == 4
        finally:
            await c.close()
    asyncio.run(go())


def test_relay_continuous_history_and_old_entry(tmp_path):
    import json
    from types import SimpleNamespace as NS
    from anzhuo.sessions import Registry, Session
    async def go():
        reg = Registry(tmp_path / "sessions.json")
        sessions = [reg.add(Session(num=reg.reserve_num(), name=name, cwd=str(tmp_path),
                                   engine="codex", codex_id=f"native-{i}"))
                    for i, name in enumerate(["完整标题", "旧的·接力", "旧的·接力"], 1)]
        for parent, child in zip(sessions, sessions[1:]):
            parent.relay_to, child.relay_from = child.num, parent.num
            child.relay_note = f"交接{parent.num}"
        runner = NS(turns={}, last={}, limits={}, default_effort="medium",
                    compacting=lambda n: "", busy=lambda n: False, queued=lambda n: [])
        w = Web("http://x", 0, tmp_path)
        w.bridge = NS(registry=reg, runner=runner, outgoing={}, cfg=NS(folders={}, scratch_root=str(tmp_path / "scratch")))
        w.bridge.relays = NS(writing={}, targets=lambda s: [])   # 不接力
        for session in sessions:
            w.save_turn(session.label, Turn(started=session.num * 100, ended=session.num * 100 + 1,
                        request=f"问题{session.num}", final_text=f"回复{session.num}",
                        codex_turn_id=f"turn-{session.num}"), str(tmp_path), "测试")
        old = json.loads((await w._api_thread(NS(match_info={"num": "1"}))).text)
        assert old == {"num": 1, "redirect": 3}
        listing = json.loads((await w._api_sessions({})).text)
        assert [(r["num"], r["name"]) for r in listing["sessions"]] == [(3, "完整标题")]
        before = [(s.unsent, s.codex_id, s.claude_id) for s in sessions]
        result = json.loads((await w._api_thread(NS(match_info={"num": "3"}))).text)
        assert result["name"] == "完整标题"
        assert [i["text"] for i in result["items"] if i["who"] == "you"] == ["问题1", "问题2", "问题3"]
        assert [i["handoff"] for i in result["items"] if "handoff" in i] == ["交接1", "交接2"]
        assert [i["source_num"] for i in result["items"] if i.get("historical") and i["who"] == "claude"] == [1, 2]
        assert before == [(s.unsent, s.codex_id, s.claude_id) for s in sessions]
        assert not any(i.get("historical") for i in (await w._saved_items(sessions[0])))
        # 跨账号关系不允许串入历史，也不能跳到别人的会话。
        sessions[1].owner = "user"
        assert w._relay_latest(sessions[0]).num == 1
        assert [s.num for s in w._relay_chain(sessions[2])] == [3]
        sessions[1].owner = "admin"
        sessions[0].relay_from = 3
        sessions[2].relay_to = 1
        assert len(w._relay_chain(sessions[2])) == 3
        assert w._relay_latest(sessions[0]) is not None
    asyncio.run(go())


def test_relay_only_after_advice_and_between_answers(tmp_path):
    import json
    from types import SimpleNamespace as NS
    from anzhuo.sessions import Registry, Session
    async def go():
        reg=Registry(tmp_path/'sessions.json')
        s=reg.add(Session(num=reg.reserve_num(), name='长对话', cwd=str(tmp_path), engine='codex', codex_id='native',context_tokens=300_000))
        turns={1:Turn(started=time.time())}
        queued=[]
        runner=NS(turns=turns,last={},limits={},default_effort='medium',compacting=lambda n:'',
                  busy=lambda n:bool(turns or queued),queued=lambda n:queued)
        def rejected(*args,**kwargs): raise AssertionError('忙碌时不应提交接力')
        w=Web('http://x',0,tmp_path)
        w.bridge=NS(registry=reg,runner=runner,outgoing={},cfg=None,quota_levels={},events=Bus())
        w.bridge.relays=Relays(w.bridge); w.bridge.relays.start=rejected
        request=NS(match_info={'num':'1'},headers={},can_read_body=False)
        result=json.loads((await w._api_thread(request)).text)
        assert not result['can_relay'] and not result['usage']['relay_suggested']
        turns.clear()
        result=json.loads((await w._api_thread(request)).text)
        assert result['can_relay'] and result['usage']['relay_suggested']
        assert Registry(tmp_path/'sessions.json').get(1).relay_suggested
        turns[1]=Turn(started=time.time())
        result=json.loads((await w._api_thread(request)).text)
        assert not result['can_relay'] and result['usage']['relay_suggested']
        assert (await w._api_relay(request)).status==409
        turns.clear()
        from anzhuo.sessions import Job
        queued.append(Job(text='排队消息'))
        result=json.loads((await w._api_thread(request)).text)
        assert not result['can_relay']
        assert (await w._api_relay(request)).status==409
        queued.clear()
        s.context_tokens = 1000  # 建议触发过，即使上下文下降也保留入口
        result = json.loads((await w._api_thread(request)).text)
        assert result['can_relay'] and result['usage']['relay_suggested']
    asyncio.run(go())


def test_relay_hides_maintenance_and_preserves_choice(tmp_path):
    import json
    from types import SimpleNamespace as NS
    from anzhuo.sessions import Registry, Session
    from anzhuo.relay import RELAY, display_items
    question = "要你定：选哪一个？\nA. 红色\nB. 蓝色\n推荐：A，醒目"
    assert display_items([{"who": "you", "text": RELAY}, {"who": "claude", "text": "内部摘要"},
                          {"who": "you", "text": "（这是接力的新对话：摘要\n用户这次说：\n\nA"}]) == [{"who": "you", "text": "A"}]
    async def go():
        reg = Registry(tmp_path / "sessions.json")
        parent = reg.add(Session(num=1, name="设计", cwd=str(tmp_path), engine="codex", codex_id="old", relay_to=2))
        child = reg.add(Session(num=2, name="设计", cwd=str(tmp_path), engine="codex", relay_from=1,
                               relay_note="内部摘要", awaiting="选哪一个", awaiting_kind="decide", relay_pending={"text": question}))
        runner = NS(turns={}, last={}, limits={}, default_effort="medium", compacting=lambda n: "", busy=lambda n: False, queued=lambda n: [])
        w = Web("http://x", 0, tmp_path)
        w.bridge = NS(registry=reg, runner=runner, outgoing={}, cfg=NS(folders={}, scratch_root=str(tmp_path / "scratch")))
        w.bridge.relays = NS(writing={}, targets=lambda s: [])   # 不接力
        w.save_turn(parent.label, Turn(started=100, ended=101, request="设计", final_text=question), str(tmp_path), "设计")
        w.save_turn(parent.label, Turn(started=110, ended=111, request=RELAY, final_text="内部摘要"), str(tmp_path), "接力")
        req = NS(match_info={"num": "2"})
        result = json.loads((await w._api_thread(req)).text)
        assert [i["text"] for i in result["items"] if i["who"] == "you"] == ["设计"]
        assert [i["text"] for i in result["items"] if i["who"] == "claude"] == [question]
        assert result["decision"]["options"] == [["A", "红色"], ["B", "蓝色"]]
        assert result["decision"]["recommend"] == "A" and result["awaiting_kind"] == "decide"
        assert [i["handoff"] for i in result["items"] if "handoff" in i] == ["内部摘要"]
        # 下一棒拿到摘要，聊天只呈现用户的选择；原始存档不修改。
        wrapped = "（这是接力的新对话：" + "长摘要" * 2000 + "\n用户这次说：\n\nA"
        w.save_turn(child.label, Turn(started=120, ended=121, request="接力首句…", final_text="按红色继续",
                                     timeline=[{"kind": "you", "text": wrapped}]), str(tmp_path), "继续")
        w.bridge.outgoing[2] = [{"text": "A", "t": 119}]
        child.awaiting = ""
        result = json.loads((await w._api_thread(req)).text)
        assert [i["text"] for i in result["items"] if i["who"] == "you"] == ["设计", "A"]
        assert result["decision"] is None
        assert not any(i.get("pending") for i in result["items"])
        assert w.bridge.outgoing[2] == []
        assert w._turn("2-120")["timeline"][0]["text"] == wrapped
        assert w._turn("1-110")["request"] == RELAY
    asyncio.run(go())

def test_quota_rows_carry_eta_and_pace(monkeypatch):
    """额度格带上“按这速度何时用完”：本周过了一半已用 80% → 重置前会用完；5 小时用得慢 → 没有 eta、给到重置时的用量。"""
    import datetime
    import time
    now = datetime.datetime(2026, 11, 8, 12, 0).timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    fmt = lambda ts: (lambda t: f"{t.month}月{t.day}日 {t.hour:02d}:{t.minute:02d}")(datetime.datetime.fromtimestamp(ts))
    week_reset = fmt(now + 3.5 * 86400)
    five_reset = fmt(now + 4 * 3600)
    week, five = Web._limits({"seven_day": (.8, week_reset), "five_hour": (.1, five_reset)})
    assert week["eta"] and now < week["eta"] < week["reset_at"] and week["level"] == "告急"
    assert five["eta"] is None and five["pace"] == 50 and five["level"] == "充足"   # 已过 20%（下限）用了 10% → 到重置约 50%


def test_codex_history_shows_sent_images(tmp_path):
    """GPT 对话里你发的图：记在那一轮的 timeline 里，历史带上 imgs，uimg 只从收件目录取文件。"""
    from types import SimpleNamespace as NS
    from anzhuo.sessions import Registry, Session
    registry = Registry(tmp_path / "sessions.json")
    session = registry.add(Session(num=registry.reserve_num(), name="看图", cwd=str(tmp_path), engine="codex"))
    w = Web("http://x", 0, tmp_path)
    w.bridge = NS(registry=registry, runner=NS(last={}), outgoing={})
    (tmp_path / "inbox" / "web").mkdir(parents=True)
    pic = tmp_path / "inbox" / "web" / "a.jpg"
    pic.write_bytes(b"\xff\xd8jpg")
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")
    turn = Turn(started=100, ended=110, request="（见图）", final_text="看到了")
    turn.timeline = [{"t": 100, "kind": "you", "text": "（见图）", "imgs": [str(pic), str(outside)]}]
    w.save_turn(session.label, turn, str(tmp_path), "测试")
    you = [x for x in w._codex_history(1) if x["who"] == "you"]
    assert you == [{"who": "you", "text": "", "imgs": ["1-100/0", "1-100/1"]}]
    assert w._record_image(1, "1-100/0") == (b"\xff\xd8jpg", "image/jpeg")
    assert w._record_image(1, "1-100/1") is None        # 收件目录以外的文件不给
    assert w._record_image(2, "1-100/0") is None        # 别的对话的编号不认


def test_codex_legacy_truncated_relay_recovers_said(tmp_path):
    """旧接力首句截断时，用独立保存的原话认领待发消息，保留图片和后续插话。"""
    from types import SimpleNamespace as NS
    from anzhuo.sessions import Registry, Session
    from anzhuo.web.common import _said_has
    reg = Registry(tmp_path / "sessions.json")
    session = reg.add(Session(num=reg.reserve_num(), name="接力", cwd=str(tmp_path), engine="codex"))
    w = Web("http://x", 0, tmp_path)
    w.bridge = NS(registry=reg, runner=NS(last={}), outgoing={})
    said = "这些细节看不清楚，调整一下"
    turn = Turn(started=100, ended=110, request="这些细节…", said=said, final_text="已调整")
    turn.timeline = [{"kind": "you", "text": "（这是接力的新对话" + "摘要" * 1500, "imgs": ["a.jpg"]},
                     {"kind": "you", "text": "再看一下状态灯"}]
    w.save_turn(session.label, turn, str(tmp_path), "测试")
    you = [x for x in w._codex_history(1) if x["who"] == "you"]
    assert you[0]["text"] == said and you[0]["imgs"] == ["1-100/0"]
    assert _said_has(you[0]["text"], said)
    assert you[1]["text"] == "再看一下状态灯"
