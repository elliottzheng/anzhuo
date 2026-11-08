"""ACP 引擎（Kimi 等）：用一个假的 ACP 助手（tests/fake_acp_agent.py）走一遍，不连真的 Kimi。"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from anzhuo.engines import policy
from anzhuo import acp
from anzhuo.app import Bridge
from anzhuo.intent import inline_overrides
from anzhuo.config import Config
from anzhuo.dingtalk import Incoming
from anzhuo.sessions import Job, Runner, Session

from test_bridge import FakeDT

AGENT = Path(__file__).with_name("fake_acp_agent.py")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """登记一个叫 fake 的 ACP 引擎，启动的是假助手；它收到的请求记在 log 里。"""
    log = tmp_path / "acp.log"
    monkeypatch.setenv("FAKE_ACP_LOG", str(log))
    monkeypatch.setitem(acp.ENGINES, "fake", {"name": "假助手", "cmd": [sys.executable, str(AGENT)]})

    async def no_usage(self, force=False):
        pass
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)
    monkeypatch.setattr(Runner, "refresh_gpt_usage", no_usage)
    return lambda: [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def _bridge(tmp_path):
    cfg = Config(client_id="x", allowed_user_ids=["me"], scratch_root=str(tmp_path / "s"), web_base="https://xe.example")
    b = Bridge(cfg, FakeDT(), data_dir=tmp_path / "data")

    async def push(title, body, num=None, only=None):
        return 1
    b.web.push.send = push
    return b


def web(t):
    return Incoming(sender_id="me", sender_nick="", conversation_type="1", kind="text", text=t, via="web")


async def _wait(b, num, timeout=20):
    for _ in range(int(timeout * 20)):
        await asyncio.sleep(0.05)
        if not b.runner.busy(num) and num in b.runner.last:
            return b.runner.last[num]
    raise AssertionError("这一轮没做完")


def test_full_turn_and_resume(tmp_path, fake):
    async def go():
        b = _bridge(tmp_path)
        s = Session(num=1, name="t", cwd=str(tmp_path), engine="fake", effort="low")
        b.registry.add(s)
        b.runner.submit(s, Job(text="整理一下", via="web"))
        t = await _wait(b, 1)
        assert not t.is_error, t.error
        assert t.final_text.startswith("做完了（批准：yes）")         # 最后一段话是回复；普通命令放行一次
        assert "跑命令：ls -la" in t.steps and "改 a.txt" in t.steps
        assert t.steps.count("跑命令：ls -la") == 1 and "跑命令" not in t.steps   # 参数后到：原地改这一步，不另起
        assert t.changed_files == [str(tmp_path / "a.txt")] and t.commands == ["ls -la"]
        assert [p["status"] for p in t.plan] == ["done", "doing"]
        assert s.context_pct == 25.0 and t.model == "假助手（fake-1）"
        kinds = [e["kind"] for e in t.timeline]
        assert "think" in kinds and kinds.count("say") == 2
        assert s.acp_id and s.status == "idle"
        first = s.acp_id
        b.runner.submit(s, Job(text="再来一次", via="web"))           # 第二轮：接着同一个会话
        await asyncio.sleep(0.2)
        await _wait(b, 1)
        calls = fake()
        assert [c["method"] for c in calls].count("session/new") == 1
        assert any(c["method"] == "session/resume" and c["params"]["sessionId"] == first for c in calls)
        prompts = [c for c in calls if c["method"] == "session/prompt"]
        assert "远窗的约定" in prompts[0]["params"]["prompt"][0]["text"]       # 第一句带上约定
        assert "远窗的约定" not in prompts[1]["params"]["prompt"][0]["text"]   # 接着聊不再带
        assert any(c["method"] == "session/set_config_option" and c["params"]["value"] == "off" for c in calls)   # 深度低 = 不思考

    asyncio.run(go())


def test_dangerous_command_waits_for_you(tmp_path, fake, monkeypatch):
    asked = []

    async def decide(self, s, turn, what, cmd):
        asked.append(cmd)
        return False   # 你没同意
    monkeypatch.setattr(Runner, "decide", decide)

    async def go():
        b = _bridge(tmp_path)
        s = Session(num=1, name="t", cwd=str(tmp_path), engine="fake")
        b.registry.add(s)
        b.runner.submit(s, Job(text="push 一下", via="web"))
        t = await _wait(b, 1)
        assert asked == ["git push origin main"] and t.final_text == "没推送"

    asyncio.run(go())


def test_stop_cancels(tmp_path, fake):
    async def go():
        b = _bridge(tmp_path)
        s = Session(num=1, name="t", cwd=str(tmp_path), engine="fake")
        b.registry.add(s)
        b.runner.submit(s, Job(text="慢慢做", via="web"))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if 1 in b.runner.live and b.runner.turns.get(1) and b.runner.turns[1].partial:
                break
        assert await b.runner.stop(1)
        t = await _wait(b, 1)
        assert t.interrupted and not t.is_error
        assert any(c["method"] == "session/cancel" for c in fake())

    asyncio.run(go())


def test_error_is_shown(tmp_path, fake):
    async def go():
        b = _bridge(tmp_path)
        s = Session(num=1, name="t", cwd=str(tmp_path), engine="fake")
        b.registry.add(s)
        b.runner.submit(s, Job(text="订阅问题", via="web"))
        t = await _wait(b, 1)
        assert t.is_error and "subscription" in t.error and s.status == "error"

    asyncio.run(go())


def test_missing_cli(tmp_path, monkeypatch):
    monkeypatch.setitem(acp.ENGINES, "nope", {"name": "没装的", "cmd": ["definitely-not-installed-acp"]})
    assert not acp.available("nope") and acp.available("kimi") in (True, False)

    async def go():
        b = _bridge(tmp_path)
        s = Session(num=1, name="t", cwd=str(tmp_path), engine="nope")
        b.registry.add(s)
        b.runner.submit(s, Job(text="x", via="web"))
        t = await _wait(b, 1)
        assert t.is_error and "没找到" in t.error

    asyncio.run(go())


def test_pick_kimi_when_opening(tmp_path, monkeypatch):
    assert inline_overrides("用 kimi 帮我看看")[0] == "kimi"
    seen = []

    async def fake_run(self, s, job, turn):
        seen.append(s.engine)
        turn.final_text = "好"
    monkeypatch.setattr(Runner, "_run", fake_run)

    async def no_usage(self, force=False):
        pass
    monkeypatch.setattr(Runner, "refresh_usage", no_usage)

    async def go():
        b = _bridge(tmp_path)
        await b.new_session([], "用 kimi 整理一下", web("用 kimi 整理一下"))
        await asyncio.sleep(0.2)
        s = b.registry.get(1)
        assert s.engine == "kimi" and s.model is None and seen == ["kimi"]
        await b.submit(s, "用 opus 接着做", web("用 opus 接着做"))   # 对话中途不换家
        await asyncio.sleep(0.2)
        assert s.engine == "kimi" and s.model is None
        await b.start_session(b.registry.reserve_num(), tmp_path, [], "x", web("x"), model="kimi", owner="u", root=str(tmp_path))
        await asyncio.sleep(0.1)
        assert b.registry.get(2).engine == "claude"   # 受限账号只用 Claude

    asyncio.run(go())


def test_inject_while_running_is_queued(tmp_path, fake):
    """ACP 没有中途插话：正在做时再说一句，排到下一轮。"""
    async def go():
        b = _bridge(tmp_path)
        s = Session(num=1, name="t", cwd=str(tmp_path), engine="fake")
        b.registry.add(s)
        b.runner.submit(s, Job(text="慢慢做", via="web"))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if b.runner.turns.get(1) and b.runner.turns[1].partial:   # 它已经开口了（在等）再插话、再取消
                break
        assert b.runner.submit(s, Job(text="整理一下", via="web")) == 1   # 前面还有 1 件
        assert b.runner.queued(1)
        await b.runner.live[1].cancel()   # 只结束正在做的这一轮（“停”会连排着的一起清掉）
        for _ in range(400):
            await asyncio.sleep(0.05)
            t = b.runner.last.get(1)
            if t and t.final_text.startswith("做完了"):
                break
        assert t.final_text.startswith("做完了"), t   # 排着的那句在下一轮做了

    asyncio.run(go())


def test_unknown_command_asks(tmp_path, fake, monkeypatch):
    """跑命令却看不出是什么：不自动放行，交给你点头。"""
    asked = []

    async def decide(self, s, turn, what, cmd):
        asked.append(what)
        return False
    monkeypatch.setattr(Runner, "decide", decide)

    async def go():
        b = _bridge(tmp_path)
        s = Session(num=1, name="t", cwd=str(tmp_path), engine="fake")
        b.registry.add(s)
        b.runner.submit(s, Job(text="神秘命令", via="web"))
        t = await _wait(b, 1)
        assert asked and "看不出" in asked[0] and t.final_text == "没跑"

    asyncio.run(go())


@pytest.mark.skipif(os.name == "nt", reason="/tmp 临时目录语义仅 POSIX；Windows 上保守地转为询问")
def test_acp_move_and_delete_paths():
    """挪文件源和目标都判、看不出挪到哪也问你；参数里的路径列表也算。"""
    from anzhuo.engines.acp import permission_target
    norm = lambda ps: [p.replace(os.sep, "/") for p in ps]   # Windows 上还原成 POSIX 形态好比较
    home = str(Path.home())
    one = permission_target("move", {"rawInput": {"source": "/tmp/a"}}, {}, "/w")
    assert norm(one) == ["/tmp/a"] and "看不出" in policy.danger("move", one)
    two = permission_target("move", {"rawInput": {"source": "/tmp/a", "destination": "/tmp/b"}}, {}, "/w")
    assert policy.danger("move", two) is None
    out = permission_target("move", {"rawInput": {"src": "/tmp/a", "to": f"{home}/Documents/b"}}, {}, "/w")
    assert policy.danger("move", out).startswith("挪走")
    many = permission_target("delete", {"rawInput": {"paths": ["/tmp/a", f"{home}/Documents/c.md"]}}, {}, "/w")
    assert policy.danger("delete", many)


def test_acp_close_cancels_tasks(fake):
    """关连接时读输出的后台任务一起收掉，不挂在事件循环里。"""
    async def go():
        conn = acp.AcpConn("fake", str(Path.cwd()), lambda u: None)
        await conn.start()
        tasks = list(conn._tasks)
        assert tasks
        await conn.close()
        await asyncio.sleep(0)         # 做完的回调下一拍才跑
        assert all(t.done() for t in tasks) and not conn._tasks

    asyncio.run(go())


def test_codex_close_cancels_tasks():
    from anzhuo.codex import CodexConn

    async def go():
        conn = CodexConn("/tmp", lambda *a: None)
        t = conn._spawn(asyncio.sleep(60))
        await conn.close()
        await asyncio.sleep(0)
        assert t.cancelled() and not conn._tasks
@pytest.mark.skipif(os.name == "nt", reason="/Users/demo 与 /tmp 判定语义仅 macOS")
def test_delete_judged_like_rm(tmp_path, fake, monkeypatch):
    """删文件和 Claude 那边的 rm 同一套规则：工作区外的要问你，不同意就拒；临时目录里的照旧放行。"""
    asked = []

    async def decide(self, s, turn, what, cmd):
        asked.append((what, cmd))
        return False
    monkeypatch.setattr(Runner, "decide", decide)

    async def go():
        b = _bridge(tmp_path)
        s = Session(num=1, name="t", cwd=str(tmp_path), engine="fake")
        b.registry.add(s)
        b.runner.submit(s, Job(text="删掉那个文件", via="web"))
        t = await _wait(b, 1)
        assert asked == [("删除 重要.txt", "删除：/Users/demo/重要.txt")] and t.final_text == "没删"
        assert t.steps == ["删 重要.txt"] and t.changed_files == ["/Users/demo/重要.txt"]
        asked.clear()
        b.runner.submit(s, Job(text="删掉临时文件", via="web"))   # /tmp 里的：和 rm 一样放行
        await asyncio.sleep(0.2)
        t = await _wait(b, 1)
        assert not asked and t.final_text == "删了"

    asyncio.run(go())


def test_conn_keeps_and_cancels_its_tasks(tmp_path, fake):
    """读输出、读 stderr 的协程留着引用，关闭连接时一并收掉，不留在事件循环里。"""
    async def go():
        conn = acp.AcpConn("fake", str(tmp_path), lambda u: None)
        await conn.start()
        tasks = list(conn._tasks)
        assert len(tasks) == 2 and not any(t.done() for t in tasks)
        await conn.close()
        assert all(t.done() for t in tasks)

    asyncio.run(go())
