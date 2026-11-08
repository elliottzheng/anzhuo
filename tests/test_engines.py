"""引擎抽象：能力表、选模型的键 → 引擎、统一的批准入口（policy）、Runner 的调度收尾。"""
import asyncio
import time
from types import SimpleNamespace as NS

from anzhuo import acp, guard
from anzhuo import engines
from anzhuo.engines import CLAUDE, CODEX, policy
from anzhuo.engines.acp import permission_target
from anzhuo.report import Turn
from anzhuo.sessions import Job, Runner, Session


def test_capability_table(monkeypatch):
    monkeypatch.setitem(acp.ENGINES, "kimi", {"name": "Kimi", "cmd": ["kimi", "acp"]})
    kimi = engines.get("kimi")
    assert engines.get("claude") is CLAUDE and engines.get("codex") is CODEX and engines.get("kimi") is kimi
    assert engines.get("") is CLAUDE and engines.get("没登记的") is CLAUDE   # 早先没写引擎的都是 Claude
    assert engines.is_acp("kimi") and not engines.is_acp("codex") and not engines.is_acp(None)
    assert (CLAUDE.label, CODEX.label, kimi.label) == ("Claude", "GPT", "Kimi")
    assert CODEX.full == "GPT（Codex）" and CODEX.brand == "GPT · Codex" and kimi.full == kimi.brand == "Kimi"
    caps = lambda e: (e.can_steer, e.can_rewind, e.can_fork, e.has_quota, e.has_tools, e.stop_hook, e.fenced_ok)  # noqa: E731
    assert caps(CLAUDE) == (True,) * 7
    assert caps(CODEX) == (True, True, True, True, True, True, False)   # 受限账号只用 Claude
    assert caps(kimi) == (False,) * 7                                   # 不能插话、回退、分叉，查不到额度，带不上工具
    assert (CLAUDE.history, CODEX.history, kimi.history) == ("transcript", "records", "records")
    assert CLAUDE.cache_hint and CLAUDE.cum_usage and not CODEX.cache_hint and not CODEX.cum_usage


def test_pick_keys_to_engines(monkeypatch):
    monkeypatch.setitem(acp.ENGINES, "kimi", {"name": "Kimi", "cmd": ["kimi", "acp"]})
    assert engines.engine_of("gpt") == "codex" and engines.engine_of("kimi") == "kimi"
    assert engines.engine_of("claude-opus-5-5") is None and engines.engine_of("claude") is None and engines.engine_of(None) is None
    assert engines.by_key("gpt") is CODEX and engines.by_key("nope") is None
    # 已有对话不能换家（定时任务）
    assert not engines.switches("opus", "claude") and engines.switches("gpt", "claude")
    assert not engines.switches("gpt", "codex") and engines.switches("opus", "kimi") and not engines.switches("kimi", "kimi")
    # 中途换型号：只有 Claude 能换，而且只能换成 Claude 的型号
    assert engines.can_switch_model("claude", "claude-sonnet-5-5")
    assert not engines.can_switch_model("claude", "gpt") and not engines.can_switch_model("claude", "kimi")
    assert not engines.can_switch_model("codex", "claude-sonnet-5-5") and not engines.can_switch_model("kimi", "claude-sonnet-5-5")
    assert not engines.can_switch_model("claude", None)
    # ACP/Kimi：启动时探到的具体型号，中途也能换；别家的型号不行
    monkeypatch.setitem(acp.ENGINES, "kimi", {"name": "Kimi", "cmd": ["kimi", "acp"]})
    monkeypatch.setattr(acp, "MODELS_SEEN", {"kimi": {"models": ["kimi-k2", "kimi-k2-thinking"], "current": "kimi-k2"}})
    assert engines.engine_of("kimi-k2") == "kimi" and engines.engine_of("kimi-k2-thinking") == "kimi"
    assert engines.can_switch_model("kimi", "kimi-k2") and engines.can_switch_model("kimi", "kimi-k2-thinking")
    assert not engines.can_switch_model("kimi", "claude-sonnet-5-5") and not engines.can_switch_model("claude", "kimi-k2")


def test_policy_judges_commands_and_files():
    assert policy.danger("execute", "git push origin main") == guard.danger("git push origin main")
    assert policy.danger("execute", "ls -la") is None
    assert policy.danger("execute", "") is None                          # Codex 往终端里写输入：没有命令，照旧放行
    assert "看不出" in policy.danger("execute", "", strict=True)          # ACP 看不出跑什么：问你
    # 删、挪文件：和 rm <路径> 同一套规则
    for p in ("/Users/demo/重要.txt", "~/文稿/a b.txt", "相对/路径.txt", "/tmp/x.txt", "/private/var/folders/ab/x"):
        assert policy.danger("delete", p) == guard.danger(f"rm '{p}'"), p
    assert policy.danger("delete", "/Users/demo/重要.txt") == "删除 重要.txt"
    assert policy.danger("delete", ["/tmp/a", "/var/folders/b"]) is None   # 临时目录里的放行
    assert policy.danger("move", ["/tmp/a.txt", "/Users/demo/b.txt"]) == "挪走 b.txt"
    assert "看不出" in policy.danger("delete", []) and "看不出" in policy.danger("move", None)
    assert policy.danger("edit", "/Users/demo/a.txt") is None and policy.danger("read", "") is None
    assert policy.shown("delete", ["/a", "/b"]) == "删除：/a /b" and policy.shown("execute", "ls") == "ls"


def test_policy_approve_asks_only_when_dangerous():
    asked = []

    async def decide(s, turn, what, cmd):
        asked.append((what, cmd))
        return len(asked) == 1   # 第一次同意，之后不同意

    async def go():
        r, s, t = NS(decide=decide), Session(num=1, name="x", cwd="/tmp"), Turn(started=0)
        assert await policy.approve(r, s, t, "execute", "ls") is None and not asked   # 不危险：不打扰
        assert await policy.approve(r, s, t, "execute", "sudo ls") is True
        assert await policy.approve(r, s, t, "delete", "/Users/demo/a.txt") is False
        assert await policy.approve(r, s, t, "execute", "", strict=True, title="Bash") is False
        assert [c for _, c in asked] == ["sudo ls", "删除：/Users/demo/a.txt", "Bash"]

    asyncio.run(go())


def test_acp_permission_target():
    import os
    norm = lambda ps: [p.replace(os.sep, "/") for p in ps]
    known = {"kind": "move", "rawInput": {"source": "a.txt", "destination": "/Users/demo/b.txt"}}
    assert norm(permission_target("move", known, known, "/work")) == ["/work/a.txt", "/Users/demo/b.txt"]
    titled = {"kind": "delete", "title": "Deleting /Users/demo/c.txt"}
    assert norm(permission_target("delete", {}, titled, "/work")) == ["/Users/demo/c.txt"]
    run = {"kind": "execute", "title": "Bash", "rawInput": {"command": "git push"}}
    assert permission_target("execute", run, run, "/work") == "git push"
    assert permission_target("edit", {}, {}, "/work") == ""


def test_unfinished_follows_role_and_capability(monkeypatch):
    monkeypatch.setitem(acp.ENGINES, "kimi", {"name": "Kimi", "cmd": ["kimi", "acp"]})
    r = Runner(None, None)
    s, t = Session(num=1, name="x", cwd="/tmp"), Turn(started=0)
    assert CLAUDE.unfinished(r, s, t, False) == ""   # 普通对话：没有角色
    r.role = lambda s_: {"on_stop": lambda turn, again: "" if again else "先汇报"}
    assert CLAUDE.unfinished(r, s, t, False) == "先汇报" and CLAUDE.unfinished(r, s, t, True) == ""
    assert CODEX.unfinished(r, s, t, False) == "先汇报"
    assert engines.get("kimi").unfinished(r, s, t, False) == ""   # ACP 带不上汇报工具：不拦


def test_codex_follow_up_turn_when_unfinished(tmp_path, monkeypatch):
    """Codex 没有 Stop 钩子：做完后问 unfinished，该汇报没汇报就再开一轮；回复仍用补之前那段。"""
    from anzhuo import codex, sessions
    from anzhuo.engines import base
    turns = []

    class Conn:
        def __init__(self, *args, **kw):
            self.done, self.model, self.turn_status = asyncio.Event(), "", ""
            self.on_event = args[1]

        async def start(self): pass
        async def open_thread(self, *a): return "th"
        async def close(self): pass

        async def start_turn(self, text, images, effort, model=None):
            turns.append(text)
            self.on_event("item/completed", {"item": {"type": "agentMessage", "text": f"答{len(turns)}"}})
            self.turn_status = "completed"
            self.done.set()
            return f"t{len(turns)}"

    async def go():
        monkeypatch.setattr(codex, "CodexConn", Conn)
        monkeypatch.setattr(base, "blocked", lambda *a: "")
        registry = sessions.Registry(tmp_path / "sessions.json")
        s = registry.add(Session(num=1, name="x", cwd=str(tmp_path), engine="codex"))
        runner = Runner(registry, None)
        runner.role = lambda s_: {"on_stop": lambda turn, again: "补上汇报" if len(turns) == 1 else ""}
        turn = Turn(started=time.time())
        await CODEX.run(runner, s, Job("干活"), turn)
        assert turns == ["干活", "补上汇报"] and turn.final_text == "答1" and not turn.is_error
        assert 1 not in runner.live   # 连接用完就拿掉

    asyncio.run(go())


def test_cancel_while_waiting_for_slot_clears_busy(tmp_path):
    """在等并行名额时被取消（关机、重启）：不能一直算“在忙”。"""
    from anzhuo.sessions import Registry

    async def go():
        registry = Registry(tmp_path / "sessions.json")
        s = registry.add(Session(num=1, name="x", cwd=str(tmp_path)))
        r = Runner(registry, None, max_parallel=1)
        await r.sem.acquire()   # 名额被别的对话占着
        r.submit(s, Job(text="排着"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if 1 in r.waiting:
                break
        assert r.busy(1) and 1 in r.waiting
        r.workers[1].cancel()
        await asyncio.gather(r.workers[1], return_exceptions=True)
        assert 1 not in r.waiting and not r.busy(1)

    asyncio.run(go())


def test_steer_dispatch_by_capability(monkeypatch):
    """插话：Claude 连上了就插，Codex 连接开着才插，ACP 一律排队。"""
    monkeypatch.setitem(acp.ENGINES, "kimi", {"name": "Kimi", "cmd": ["kimi", "acp"]})
    sent = []

    class Live:
        async def steer(self, text, images):
            sent.append(("codex", text))

        async def query(self, prompt):
            async for m in prompt:
                sent.append(("claude", m["message"]["content"][-1]["text"]))

    async def go():
        r = Runner(registry=None, on_done=None)
        for num, engine in ((1, "claude"), (2, "codex"), (3, "kimi")):
            r.turns[num], r.live[num] = Turn(started=time.time(), request="干活"), Live()
            r.workers[num] = asyncio.get_running_loop().create_future()   # 假装有人在干活
        assert r.submit(Session(num=1, name="a", cwd="/tmp"), Job(text="插一句")) == -1
        assert r.submit(Session(num=2, name="b", cwd="/tmp", engine="codex"), Job(text="也插")) == -1
        assert r.submit(Session(num=3, name="c", cwd="/tmp", engine="kimi"), Job(text="排队")) == 1
        await asyncio.sleep(0.05)
        r.live.pop(2)
        assert r.submit(Session(num=2, name="b", cwd="/tmp", engine="codex"), Job(text="没连上")) == 1
        assert sorted(sent) == [("claude", "插一句"), ("codex", "也插")]
        assert r.turns[1].injected == r.turns[2].injected == 1 and [j.text for j in r.queued(3)] == ["排队"]

    asyncio.run(go())


def test_other_engines_keep_their_model(tmp_path, monkeypatch):
    """GPT、Kimi 的对话不会被钉钉“模型 N opus”、定时任务改成 Claude 的型号（中途不换家）。"""
    import pytest

    from anzhuo.app import Bridge
    from anzhuo.config import Config
    from test_bridge import FakeDT, msg
    monkeypatch.setitem(acp.ENGINES, "kimi", {"name": "Kimi", "cmd": ["kimi", "acp"]})
    dt = FakeDT()

    async def go():
        b = Bridge(Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), dingtalk_quiet=False),
                   dt, data_dir=tmp_path / "data")
        k = b.registry.add(Session(num=7, name="查资料", cwd=str(tmp_path), engine="kimi", model="k3"))
        await b.ding.on_raw(msg("模型 7 opus"))
        assert k.model == "k3" and "中途换不了" in dt.sent[-1][1]
        with pytest.raises(ValueError, match="不能切换"):
            b.jobs.create({"target": 7, "model": "opus", "prompt": "每天查一下", "kind": "daily", "time": "08:00"})
        c = b.registry.add(Session(num=8, name="写代码", cwd=str(tmp_path)))
        await b.ding.on_raw(msg("模型 8 sonnet"))
        assert c.model == "claude-sonnet-5-5"

    asyncio.run(go())


def test_deepseek_engine_env_block_and_cost(monkeypatch):
    """DeepSeek：Claude Code 换后端；密钥只从钥匙串来；私密文件夹不开工、中途也碰不到；消耗按 DeepSeek 价重算。"""
    from anzhuo.engines import deepseek
    monkeypatch.setattr(deepseek, "api_key", lambda: "sk-test")
    e = engines.DEEPSEEK
    assert engines.get("deepseek") is e and engines.engine_of("deepseek") == "deepseek" and not e.fenced_ok
    r, s = NS(subagent_model="", deepseek_block=["私密资料"]), Session(num=1, name="x", cwd="/tmp")
    env = e.env(r, s)
    assert env["ANTHROPIC_BASE_URL"] == deepseek.BASE_URL and env["ANTHROPIC_AUTH_TOKEN"] == "sk-test"
    assert env["ANTHROPIC_MODEL"] == "deepseek-flash" and e.model_for(s) == "deepseek-flash"
    s.model = "deepseek-v4-pro"
    assert e.env(r, s)["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
    assert deepseek.blocked_by(["/Users/x/Projects/私密资料/微信"], ["私密资料"]) == "私密资料"
    assert deepseek.blocked_by(["/Users/x/Projects/04_项目"], ["私密资料"]) == ""
    hook = e.extra_hooks(r, s)[0].hooks[0]
    denied = asyncio.run(hook({"tool_input": {"file_path": "/p/私密资料/a.txt"}}, None, None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asyncio.run(hook({"tool_input": {"command": "ls"}}, None, None)) == {}
    usd = deepseek.cost_usd({"deepseek-flash": {"inputTokens": 1_000_000, "cacheReadInputTokens": 0, "outputTokens": 0}})
    assert abs(usd - 1 / deepseek.CNY_PER_USD) < 1e-9


def test_said_engine_model(monkeypatch):
    """话里点别家的型号：gpt luna → Codex 能用的最新 luna；deepseek pro → v4-pro。"""
    from anzhuo import codex
    from anzhuo.intent import inline_overrides, said_engine_model
    monkeypatch.setattr(codex, "MODELS_SEEN", {"gpt-6.1-sol": "", "gpt-6-luna": "", "gpt-5.6-luna": ""})
    assert said_engine_model("codex", "用 gpt luna 改个按钮") == "gpt-6-luna"
    assert said_engine_model("codex", "交给 codex 做") is None
    assert said_engine_model("deepseek", "用 deepseek pro 查查") == "deepseek-v4-pro"
    assert said_engine_model("deepseek", "用 deepseek 查查") is None
    assert inline_overrides("用 deepseek 查个资料")[0] == "deepseek"


def test_deepseek_context_pct_uses_1m_window():
    """Claude Code 不认识 DeepSeek 型号、按 200K 报百分比；DeepSeek 按 1M 自己折，Claude 照它报的。"""
    from anzhuo.engines.catalog import CLAUDE, DEEPSEEK
    usage = {"totalTokens": 100_000, "percentage": 50.0}
    assert DEEPSEEK.context_pct(usage) == 10.0
    assert CLAUDE.context_pct(usage) == 50.0
    assert DEEPSEEK.context_pct({"totalTokens": 0, "percentage": 3.0}) == 3.0
