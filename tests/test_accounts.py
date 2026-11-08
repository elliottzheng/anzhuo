"""两个网页账号：admin 全部权限；user 只在自己的文件夹里干活，两边互相看不到对话；管理页看登录和消耗；user 有周额度上限。"""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from types import SimpleNamespace as NS

from aiohttp.test_utils import TestClient, TestServer

from anzhuo import usage
from anzhuo.app import Bridge
from anzhuo.config import Config
from anzhuo.report import Turn
from anzhuo.sessions import Runner, Session
from anzhuo.web import hash_password


def _bridge(tmp_path):
    b = Bridge(Config(client_id="t", allowed_user_ids=["me"], scratch_root=str(tmp_path / "scratch")), NS(secret="x"),
               tmp_path / "data")
    root = tmp_path / "remote-window"
    b.web.base = "http://x.example"
    b.web.accounts = {"admin": {"role": "admin", "root": "", "hash": hash_password("demo-admin-test", iters=1000)},
                      "user": {"role": "user", "root": str(root), "share": 0.2, "hash": hash_password("demo-guest-test", iters=1000)}}
    a = b.registry.add(Session(num=b.registry.reserve_num(), name="主账号的事", cwd=str(tmp_path)))
    u = b.registry.add(Session(num=b.registry.reserve_num(), name="user 的事", cwd=str(root), owner="user", root=str(root)))
    return b, a, u, root


async def _login(b, name, pw):
    c = TestClient(TestServer(b.web.app()))
    await c.start_server()
    r = await c.post("/_x/login", data={"username": name, "password": pw}, allow_redirects=False)
    assert r.status == 302, await r.text()
    return c


def test_two_accounts_isolated(tmp_path):
    async def go():
        b, a, u, root = _bridge(tmp_path)
        ca, cu = await _login(b, "admin", "demo-admin-test"), await _login(b, "user", "demo-guest-test")
        try:
            # 密码不对、账号不存在都进不来
            c = TestClient(TestServer(b.web.app()))
            await c.start_server()
            assert (await c.post("/_x/login", data={"username": "user", "password": "demo-admin-test"}, allow_redirects=False)).status == 401
            await c.close()
            sa = await (await ca.get("/_x/api/sessions")).json()
            su = await (await cu.get("/_x/api/sessions")).json()
            assert [s["num"] for s in sa["sessions"]] == [a.num] and sa["me"]["role"] == "admin"
            assert [s["num"] for s in su["sessions"]] == [u.num] and su["me"]["role"] == "user"
            assert su["folders"] == [] and su["ext"] == {} and all(m[0] != "gpt" for m in su["models"])
            # 对方的对话当作没有
            assert (await cu.get(f"/_x/api/thread/{a.num}")).status == 404
            assert (await ca.get(f"/_x/api/thread/{u.num}")).status == 404
            assert (await cu.post(f"/_x/api/stop/{a.num}")).status == 404
            assert (await cu.get(f"/_x/t/{a.num}-123")).status == 404
            # user 用不了的地方
            for path in ("/_x/history", "/_x/admin", "/_x/api/admin", "/_x/api/browse", "/_x/api/schedules",
                         "/_x/api/cc-sessions", "/_x/api/course", "/studio-preview.html"):
                assert (await cu.get(path)).status == 403, path
            assert (await ca.get("/_x/api/admin")).status == 200
            # 搜索只搜自己的
            hit = await (await cu.get("/_x/api/search?q=事")).json()
            assert [r["num"] for r in hit["title"]] == [u.num]
            # 设备：各看各的；admin 在管理页看得到两个账号的
            assert len((await (await cu.get("/_x/api/devices")).json())["devices"]) == 1
            adm = await (await ca.get("/_x/api/admin")).json()
            assert {x["name"]: len(x["devices"]) for x in adm["accounts"]} == {"admin": 1, "user": 1}
            assert next(x for x in adm["accounts"] if x["name"] == "user")["quota"]["share"] == 0.2
            # 停用 user：它的设备马上进不来；再启用就好
            assert (await (await ca.post("/_x/api/admin", json={"action": "disable", "account": "user"})).json())["ok"]
            assert (await cu.get("/_x/api/sessions")).status == 401
            assert not (await (await ca.post("/_x/api/admin", json={"action": "disable", "account": "admin"})).json())["ok"]
            await ca.post("/_x/api/admin", json={"action": "enable", "account": "user"})
            assert (await cu.get("/_x/api/sessions")).status == 200
            # 改密码：新密码能登，旧的不行
            saved = {}
            b.web.save_hash = lambda n, d: saved.update({n: d})
            assert (await (await ca.post("/_x/api/admin", json={"action": "password", "account": "user", "value": "newpass1"})).json())["ok"]
            assert "user" in saved and (await _login(b, "user", "newpass1"))
        finally:
            await ca.close()
            await cu.close()
    asyncio.run(go())


def test_user_starts_in_own_folder_and_quota(tmp_path):
    async def go():
        b, a, u, root = _bridge(tmp_path)
        got = []
        b.runner.submit = lambda s, job: got.append((s, job)) or 0
        cu = await _login(b, "user", "demo-guest-test")
        try:
            r = await (await cu.post("/_x/api/send", json={"text": "整理一下", "folder": "Projects", "model": "gpt"})).json()
            s = b.registry.get(r["num"])
            assert s.owner == "user" and s.root == str(root) and Path(s.cwd) == root and s.engine == "claude" and not s.add_dirs
            assert b.registry.current == a.num   # 钉钉那边的“当前对话”不会变成 user 的
            # 不能往主账号的对话里说话
            assert (await cu.post("/_x/api/send", json={"num": a.num, "text": "hi"})).status == 404
            # 附件存进它自己的文件夹
            await (await cu.post("/_x/api/send", json={"num": s.num, "text": "看看",
                                                     "files": [{"name": "a.txt", "data": "data:text/plain;base64,aGk="}]})).json()
            assert b.registry.current == a.num   # user 新开的对话不会变成 admin 的当前对话
            assert (root / "收到的附件").is_dir() and any((root / "收到的附件").iterdir())
            # 本周用了主账号周额度的 25%（比例：每 $1 = 1%）→ 超过 1/5，不能再开始
            now = time.time()
            lt = time.localtime(now + 86400)
            b.runner.limits = {"seven_day": (0.3, f"{lt.tm_mon}月{lt.tm_mday}日 {lt.tm_hour:02d}:{lt.tm_min:02d}")}
            rows = [{"t": now - 3000, "owner": "admin", "num": a.num, "engine": "claude", "cost": 1, "limits": {"seven_day": 5}},
                    {"t": now - 2000, "owner": "user", "num": s.num, "engine": "claude", "cost": 25, "limits": {"seven_day": 30}}]
            b.web.usage_path.parent.mkdir(parents=True, exist_ok=True)
            b.web.usage_path.write_text("".join(json.dumps(x) + "\n" for x in rows))
            mine = await (await cu.get("/_x/api/sessions")).json()
            assert mine["limits"][0]["pct"] == 125 and mine["limits_gpt"] == []
            r = await cu.post("/_x/api/send", json={"num": s.num, "text": "再来"})
            assert r.status == 429 and "额度" in (await r.json())["error"]
        finally:
            await cu.close()
    asyncio.run(go())


@pytest.mark.skipif(os.name == "nt", reason="受限账号与符号链接钩子仅支持 macOS")
def test_fence_hook_and_settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "remote-window"
    (home / "Projects").mkdir(parents=True)
    (home / ".ssh").mkdir()
    root.mkdir()
    (root / "a.txt").write_text("x")
    (home / "outside-link").symlink_to(home / "Projects")
    (root / "sneaky").symlink_to(home / "Projects")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from anzhuo.engines import CLAUDE
    s = Session(num=1, name="x", cwd=str(root), owner="user", root=str(root))
    hook = CLAUDE.fence(s)
    run = lambda name, args: asyncio.run(hook({"tool_name": name, "tool_input": args}, "id", None))
    assert run("Read", {"file_path": str(root / "a.txt")}) == {}
    assert run("Read", {"file_path": "a.txt"}) == {}   # 相对路径按工作文件夹算
    assert run("Bash", {"command": "ls"}) == {}         # 命令靠沙箱管
    for name, args in [("Read", {"file_path": str(home / "Projects/x")}), ("Read", {"file_path": "../Projects/x"}),
                       ("Read", {"file_path": str(root / "sneaky/x")}), ("Glob", {"pattern": str(home) + "/**/*.md"}),
                       ("Grep", {"pattern": "a", "path": str(home)}), ("Edit", {"file_path": "~/.zshrc"}),
                       ("CronCreate", {}), ("mcp__anzhuo__generate_images", {}), ("Skill", {})]:
        out = run(name, args)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", (name, args)
    opts = CLAUDE.fence_options(s)
    deny = json.loads(opts["settings"])["permissions"]["deny"]
    assert opts["sandbox"]["enabled"] and opts["sandbox"]["allowUnsandboxedCommands"] is False
    assert f"Read(/{home}/Projects/**)" in deny and f"Edit(/{home}/.ssh)" in deny
    assert not any(str(root) in d for d in deny)


def test_usage_rows_and_summary():
    s = Session(num=3, name="做课", cwd="/x")
    t = Turn(started=time.time(), request="改第 3 屏", ended=time.time())
    t.usage = {"cost": 0.42, "models": {"claude-opus-5-5": {"inputTokens": 10, "outputTokens": 200, "cacheReadInputTokens": 5000,
                                                              "cacheCreationInputTokens": 300}}}
    rec = usage.row(s, t, {"seven_day": (0.31, "x"), "five_hour": (0.1, "y")})
    assert rec["cost"] == 0.42 and rec["tokens"]["total"] == 5510 and rec["limits"] == {"seven_day": 31.0, "five_hour": 10.0}
    # Codex 报会话累计：记差值
    g = Session(num=4, name="g", cwd="/x", engine="codex", codex_tokens=1000)
    tg = Turn(started=1, ended=time.time())
    tg.usage = {"codex": {"total": 1600}}
    assert usage.row(g, tg, {})["tokens"]["total"] == 600 and g.codex_tokens == 1600
    now = time.time()
    rows = [rec, {**rec, "owner": "user", "num": 5, "cost": 0.58, "t": now - 86400}, usage.row(g, Turn(started=1, ended=now, usage={"codex": {"total": 1700}}), {})]
    out = usage.summary(rows, {3: "做课（改名后）"}, days=7, now=now)
    assert out["total_cost"] == 1.0 and out["conversations"][0]["num"] == 5 and out["conversations"][0]["share"] == 58.0
    assert next(c for c in out["conversations"] if c["num"] == 3)["name"] == "做课（改名后）"
    assert out["days"][-1]["by_owner"] == {"admin": 0.42} and out["days"][-1]["gpt_tokens"] == 100
    assert {o["owner"]: o["cost"] for o in out["owners"]} == {"admin": 0.42, "user": 0.58}


def test_quota_rate_fallbacks():
    now = time.time()
    rows = [{"t": now - 100, "owner": "user", "engine": "claude", "cost": 1.0, "limits": {"seven_day": 2}}]
    q = usage.quota(rows, "user", 0.2, now + 3600, None, now=now)
    assert q["calibrating"] and q["pct"] == 0          # 数据不够：先按 0 算
    q = usage.quota(rows, "user", 0.2, now + 3600, 2.0, now=now)
    assert q["used"] == 2.0 and q["pct"] == 10.0       # 用上次存下的比例：$1 × 2%/$ = 2%，占自己 20% 额度的 10%
    old = [{"t": now - 8 * 86400, "owner": "user", "engine": "claude", "cost": 50, "limits": {"seven_day": 90}}]
    assert usage.quota(old + rows, "user", 0.2, now + 3600, 2.0, now=now)["cost"] == 1.0   # 上周的不算


def test_claude_usage_is_cumulative_so_record_delta(tmp_path):
    """Claude 报的金额、token 是会话累计的：每轮记差值；换了新会话（累计变小）记全部；旧记录迁移成每轮的。"""
    s = Session(num=7, name="x", cwd="/x")
    m = lambda i, o: {"claude-opus-5-5": {"inputTokens": i, "outputTokens": o}, "claude-haiku-5-5": {"inputTokens": 5}}
    t = lambda c, mm: Turn(started=1, ended=time.time(), usage={"cost": c, "models": mm})
    assert usage.row(s, t(1.0, m(100, 10), ), {})["cost"] == 1.0
    r = usage.row(s, t(1.5, {**m(300, 30)}), {})
    assert r["cost"] == 0.5 and r["tokens"]["total"] == 220 and r["model"] == "claude-opus-5-5"   # haiku 这轮没动
    assert usage.row(s, t(0.2, m(50, 5)), {})["cost"] == 0.2                                       # 新会话
    # 远窗刚升级、对话里没存累计值：用记录里上一条的 cum
    s2 = Session(num=8, name="y", cwd="/x")
    assert usage.row(s2, t(3.0, m(1000, 0)), {}, last={"cost": 2.0, "tok": {"input": 900}})["tokens"]["input"] == 105
    p = tmp_path / "u.jsonl"
    old = [{"t": 1, "num": 9, "engine": "claude", "cost": 7.0, "tokens": {"input": 10, "total": 10}},
           {"t": 2, "num": 9, "engine": "claude", "cost": 9.5, "tokens": {"input": 25, "total": 25}},
           {"t": 3, "num": 9, "engine": "claude", "cost": 1.0, "tokens": {"input": 4, "total": 4}}]
    p.write_text("".join(json.dumps(x) + "\n" for x in old))
    assert usage.migrate(p) == 3 and usage.migrate(p) == 0
    assert [(x["cost"], x["tokens"]["total"]) for x in usage.load(p)] == [(7.0, 10), (2.5, 15), (1.0, 4)]
    # 换算比例：相邻两轮周 % 涨了多少 ÷ 后一轮花了多少，跨重置的不算
    rows = [{"t": 1, "cost": 1, "limits": {"seven_day": 10}}, {"t": 2, "cost": 2, "limits": {"seven_day": 12}},
            {"t": 3, "cost": 2, "limits": {"seven_day": 14}}, {"t": 4, "cost": 5, "limits": {"seven_day": 1}}]
    assert usage.rate(rows, 0) == 1.0


def test_local_spend_feeds_rate(tmp_path):
    """本机 Claude Code 记录：一条回复分几行写只算一次，只读新写的部分；换算比例的分母用本机全部消耗。"""
    from datetime import datetime, timezone
    from anzhuo.localspend import LocalSpend
    now = time.time()
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")
    line = lambda t, mid, out: json.dumps({"type": "assistant", "timestamp": iso(t), "message": {
        "id": mid, "model": "claude-opus-5-5", "usage": {"input_tokens": 0, "output_tokens": out}}}) + "\n"
    f = tmp_path / "proj" / "a.jsonl"
    f.parent.mkdir()
    f.write_text(line(now - 3000, "m1", 100_000) + line(now - 3000, "m1", 100_000) + '{"type":"user"}\n')   # $2，写了两行
    sp = LocalSpend(tmp_path / "ls.json", tmp_path / "proj")
    assert sp.scan(now) == 1 and abs(sp.between(now - 4000, now) - 2.0) < 1e-9
    with f.open("a") as h:
        h.write(line(now - 1000, "m2", 200_000) + line(now - 900, "m1", 100_000)[:20])   # $4；最后半行没写完
    assert sp.scan(now) == 1 and abs(sp.between(now - 4000, now) - 6.0) < 1e-9
    sp.save()
    assert LocalSpend(tmp_path / "ls.json", tmp_path / "proj").scan(now) == 0   # 记得读到哪了
    # 远窗只记了 $1，但这段时间本机一共花了 $6，周额度涨了 3% → 每美元 0.5%
    sp.snap(10, now - 3500)
    rows = [{"t": now - 500, "cost": 1.0, "limits": {"seven_day": 13}}]
    assert usage.rate(rows, 0, sp) == 0.5 and usage.rate(rows, 0) is None


def test_relay_usage_groups_preserve_totals():
    from types import SimpleNamespace
    sessions = {n: SimpleNamespace(relay_from=p, owner=o) for n, p, o in
                [(1, 0, "admin"), (2, 1, "admin"), (3, 2, "admin"),
                 (4, 1, "user"), (5, 99, "admin"), (6, 7, "admin"), (7, 6, "admin")]}
    groups = usage.relay_groups(sessions)
    assert groups == {1: 1, 2: 1, 3: 1, 4: 4, 5: 5, 6: 6, 7: 6}
    now = time.time()
    rows = [{"num": n, "t": now, "cost": n, "tokens": {"total": n * 100},
             "owner": "admin", "model": "opus"} for n in (1, 2, 3, 5)]
    before = usage.summary(rows, {1: "原对话"}, now=now)
    after = usage.summary(rows, {1: "原对话"}, now=now, groups=groups)
    merged = next(c for c in after["conversations"] if c["num"] == 1)
    assert (merged["name"], merged["cost"], merged["tokens"], merged["turns"]) == ("原对话", 6, 600, 3)
    for key in ("days", "owners", "total_cost"):
        assert before[key] == after[key]
    assert len(after["conversations"]) == 2


def test_usage_current_cycle_exact_boundary_and_relay():
    now=time.mktime((2026,11,7,17,0,0,0,0,-1))
    start=time.mktime((2026,11,5,10,30,0,0,0,-1))
    assert usage.week_start(start+usage.WEEK,now)==start
    assert usage.week_start(start,now)==start
    assert usage.week_start(start+usage.WEEK,start+usage.WEEK)==start+usage.WEEK
    assert usage.week_start(None,now) is None
    rows=[{'t':t,'num':n,'cost':cost,'tokens':{'total':100},'engine':'claude'}
          for t,n,cost in [(start-1,1,50),(start,1,1),(now,2,2),(now+1,2,99)]]
    out=usage.summary(rows,{1:'整条任务'},now=now,since=start,groups={2:1},rate=2)
    assert out['since']==start and out['total_cost']==3
    assert len(out['days'])==3 and sum(d['cost'] for d in out['days'])==3
    assert len(out['conversations'])==1 and out['conversations'][0]['turns']==2
    assert out['total_cost']*out['rate']==6


def test_admin_week_range_and_unknown_reset(tmp_path,monkeypatch):
    from anzhuo.web import Web
    from anzhuo.sessions import Registry, Session
    now=time.mktime((2026,11,7,17,0,0,0,0,-1))
    start=time.mktime((2026,11,5,10,30,0,0,0,-1))
    monkeypatch.setattr('time.time',lambda:now)
    async def go():
        reg=Registry(tmp_path/'sessions.json')
        reg.add(Session(num=reg.reserve_num(),name='任务',cwd=str(tmp_path)))
        w=Web('http://x',0,tmp_path)
        runner=NS(limits={'seven_day':(.3,time.strftime('%m月%d日 %H:%M',time.localtime(start+usage.WEEK)))},limits_gpt={'seven_day':(.1,'')})
        w.bridge=NS(registry=reg,runner=runner)
        w.store['rate2']=2
        for t,cost in [(start-1,50),(start,1),(now,2),(now+1,99)]:
            usage.append(w.usage_path,{'t':t,'num':1,'cost':cost,'tokens':{'total':100}})
        week=json.loads((await w._api_admin(NS(query={'period':'week'},cookies={}))).text)
        rolling=json.loads((await w._api_admin(NS(query={'days':'7'},cookies={}))).text)
        assert week['week_available'] and week['period_start']==start
        assert week['usage']['total_cost']==3 and week['usage']['rate']==2
        assert rolling['usage']['total_cost']==53 and rolling['period']=='days'
        runner.limits={}
        unknown=json.loads((await w._api_admin(NS(query={'period':'week'},cookies={}))).text)
        assert not unknown['week_available'] and unknown['period_start'] is None
        assert unknown['usage']['conversations']==[]
    asyncio.run(go())


def test_rate_does_not_count_end_of_turn_cost_twice(tmp_path):
    from anzhuo.localspend import LocalSpend
    spend=LocalSpend(tmp_path/'local.json',tmp_path/'projects')
    # 同一轮花费6：前后两段各3，回答结束时另记一条整轮6。
    spend.buckets={300:3,600:3}
    spend.snaps=[[0,0],[600,3]]
    rows=[{'t':900,'cost':6,'limits':{'seven_day':6}}]
    assert usage.rate(rows,0,spend)==1  # 6个百分点 / 6，不能除以重复累计的9
    spend.buckets={};spend._idx=None
    assert usage.rate(rows,0,spend)==1  # 本机记录缺失时才整体退回对话口径
