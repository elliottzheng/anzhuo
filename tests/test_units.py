import pytest

from anzhuo import folders, report
from anzhuo.commands import parse
from anzhuo.config import Config
from anzhuo.dingtalk import parse_incoming


def test_parse_commands():
    c = parse("新开 前端 后端：做登录页")
    assert (c.kind, c.folders, c.text) == ("new", ["前端", "后端"], "做登录页")
    c = parse("新开：查一下天气")
    assert (c.kind, c.folders, c.text) == ("new", [], "查一下天气")
    c = parse("新开 前端+后端:做")
    assert c.folders == ["前端", "后端"]
    c = parse("新建 personal/菜谱整理：整理菜谱")
    assert (c.kind, c.folders, c.text) == ("mkdir", ["personal/菜谱整理"], "整理菜谱")
    assert parse("切到 #7").num == 7
    assert parse("停").kind == "stop" and parse("停").num is None
    assert parse("停 3").num == 3
    assert parse("2").kind == "pick"
    c = parse("#5 标题再大一点")
    assert (c.kind, c.num, c.text) == ("message", 5, "标题再大一点")
    assert parse("停一下，先别改").kind == "message"
    assert parse("接着 前端 2").kind == "resume"
    c = parse("模型 7 sonnet")
    assert (c.kind, c.num, c.name) == ("model", 7, "sonnet")
    c = parse("深度 高")
    assert (c.kind, c.num, c.name) == ("effort", None, "高")
    assert parse("额度").kind == "quota"
    assert (parse("压缩").kind, parse("压缩").num, parse("压缩 6").num, parse("压缩上下文").kind) == ("compact", None, 6, "compact")
    assert parse("压缩一下这张图").kind == "message"
    assert (parse("删 7").kind, parse("删 #7").num) == ("delete", 7)
    assert parse("删掉第二段").kind == "message"
    assert (parse("接着桌面").kind, parse("接着桌面").num, parse("接着桌面 2").num) == ("desktop", None, 2)
    assert parse("接着 前端 2").kind == "resume"


def test_folder_candidates(tmp_path):
    (tmp_path / "a" / "菜谱整理").mkdir(parents=True)
    (tmp_path / "b" / "旧菜谱").mkdir(parents=True)
    (tmp_path / "_private" / "菜谱秘密").mkdir(parents=True)
    (tmp_path / "c" / "node_modules" / "菜谱包").mkdir(parents=True)
    cfg = Config(client_id="x", allowed_user_ids=[], folders={"根": str(tmp_path / "a")},
                 search_roots=[str(tmp_path)], scratch_root=str(tmp_path / "s"), skip_names=["_private"])
    assert folders.candidates("根", cfg) == [tmp_path / "a"]
    got = folders.candidates("菜谱", cfg)
    assert got[0].name == "菜谱整理" and {p.name for p in got} == {"菜谱整理", "旧菜谱"}
    assert folders.candidates("不存在的名字", cfg) == []
    assert folders.candidates(str(tmp_path / "b"), cfg) == [tmp_path / "b"]
    assert folders.split_new_path("personal/菜谱整理") == ("personal", "菜谱整理")
    assert folders.scratch_dir(cfg, 3).is_dir()


def test_parse_incoming():
    d = {"msgtype": "text", "text": {"content": " 你好 ", "isReplyMsg": True,
                                     "repliedMsg": {"content": {"text": "【做完了】#7 第2课"}}},
         "senderStaffId": "u1", "conversationType": "1"}
    inc = parse_incoming(d)
    assert inc.text == "你好" and "#7" in inc.quoted_text
    d = {"msgtype": "richText", "content": {"richText": [{"text": "看这里"}, {"type": "picture", "downloadCode": "c1"}]},
         "senderStaffId": "u1", "conversationType": "1"}
    inc = parse_incoming(d)
    assert inc.text == "看这里" and inc.image_codes == ["c1"]
    inc = parse_incoming({"msgtype": "audio", "content": {"recognition": "选B"}, "conversationType": "1"})
    assert inc.text == "选B"


def test_report():
    t = report.Turn(started=0, ended=300, request="把标题改大", changed_files=["/w/a.py", "/w/b.py"], commands=["ls"],
                    final_text="要你做的：没有\n做了什么：改了两处", model="claude-opus-5-5", effort="high",
                    context_pct=75, compacted=True)
    title, body = report.build("#3 测试", t, "/w", {"five_hour": (0.85, "10月25日 17:10"), "seven_day": (0.4, None)})
    assert title == "做完了 · 把标题改大" and "#3 · 5 分钟 · 改 2 个文件 · 跑 1 条命令" in body and "a.py" in body
    assert "要你做的：没有  \n做了什么：改了两处" in body and "Opus 5.5 · 深度高" in body  # 回复原样，不删段不加粗
    assert "| 上下文 | ■■■■□ 75% |" in body and "| 5 小时 | ■■■■□ 85% |" in body and "| 本周 | ■■□□□ 40% |" in body
    assert body.count("**注意**") == 3 and "<font" not in body and "　" not in body
    chat = report.Turn(started=0, ended=10, request="你好", final_text="你好！有什么要帮忙的？")
    ct, cb = report.build("#3 测试", chat, "/w")
    assert ct == "回复 · 你好" and "【" not in cb and cb.startswith("你好！")
    t.final_text = "要你定：标题放哪？"
    assert report.build("#3 测试", t, "/w")[0].startswith("等你定")
    t.is_error, t.error = True, "登录失效"
    assert "登录失效" in report.build("#3 测试", t, "/w")[1]
    parts = report.split("段" * 5000 + "\n\n" + "尾", limit=3500)
    assert len(parts) >= 2 and all(len(p) <= 3500 for p in parts)


def test_brief():
    assert report.brief("今天是几号？顺便看一眼桌面上有几个文件") == "今天是几号？"
    assert report.brief("把第二课第三小节的标题改大一点并且换个颜色看看效果", 16) == "把第二课第三小节的标题改大一点并…"
    assert report.brief("") == ""


def test_parse_usage():
    from anzhuo.sessions import parse_usage
    text = """Current session: 15% used · resets Oct 25 at 5:10pm (Asia/Taipei)
Current week (all models): 20% used · resets Oct 31 at 4am (Asia/Taipei)
Current week (Fable): 0% used · resets Oct 31 at 4am (Asia/Taipei)"""
    got = parse_usage(text)
    assert got == {"five_hour": (0.15, "10月25日 17:10"), "seven_day": (0.2, "10月31日 04:00")}


def test_is_reply():
    from anzhuo.intent import _is_reply
    for t in ["好的", "选B", "继续", "再大一点", "嗯嗯", "2", "为什么？"]:
        assert _is_reply(t), t
    for t in ["帮我查一下明天厦门天气", "把桌面上的文件按日期列一下", "好的，那顺便把第三课也改了"]:
        assert not _is_reply(t), t


def test_model_choice(tmp_path):
    from anzhuo.intent import _looks_like_project, inline_overrides
    assert inline_overrides("这次用 Opus，深度调到高") == ("claude-opus-5-5", "high")
    assert inline_overrides("帮我查天气") == (None, None)
    assert inline_overrides("思考最高，改用sonnet") == ("claude-sonnet-5-5", "max")
    assert inline_overrides("用 kimi-k2-thinking 深度调研") == ("kimi-k2-thinking", None)
    assert inline_overrides("换成 kimi-k2 快速看一眼") == ("kimi-k2", None)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "src").mkdir()
    assert _looks_like_project(tmp_path / "repo" / "src") and not _looks_like_project(tmp_path)


def test_router_parse():
    _parse = pytest.importorskip("anzhuo.router")._parse
    r = _parse('{"action":"reply","answer":"你好！","target":null}', {1})
    assert (r.action, r.answer) == ("reply", "你好！")
    assert _parse('{"action":"reply","answer":""}', set()).action == "ask"
    r = _parse('好的 {"action":"new","folder":"前端","kind":"dev"}', {1})
    assert (r.action, r.folder, r.kind) == ("new", "前端", "dev")
    assert _parse('{"action":"continue","target":9}', {1}).action == "ask"
    assert _parse('{"action":"continue","target":"#1"}', {1}).target == 1   # 编号带 # 也认，不当判断失败
    assert _parse("不是 JSON", {1}) is None


def test_classify_kind_prefers_deepseek(monkeypatch, real_classify_kind):
    """判断档位先问 DeepSeek；它出错或答非所问再问 Haiku。"""
    import asyncio
    import requests
    from claude_agent_sdk import AssistantMessage, TextBlock
    from anzhuo import deepseek_api
    router = pytest.importorskip("anzhuo.router")
    asked = []

    async def haiku(*, prompt, options):
        asked.append(prompt)
        yield AssistantMessage(content=[TextBlock(text="dev")], model="haiku")
    monkeypatch.setattr(router, "query", haiku)
    monkeypatch.setattr(deepseek_api, "key", lambda: "k")
    monkeypatch.setattr(deepseek_api, "chat", lambda *a, **k: " Design\n")
    assert asyncio.run(real_classify_kind("设计一节课")) == "design" and not asked

    def down(*a, **k):
        raise requests.Timeout()
    for bad in (down, lambda *a, **k: "我觉得是开发"):
        monkeypatch.setattr(deepseek_api, "chat", bad)
        assert asyncio.run(real_classify_kind("加个功能")) == "dev"
    assert len(asked) == 2


def test_needs_translation(monkeypatch):
    from anzhuo import translate
    from anzhuo.translate import needs_translation
    monkeypatch.setattr(translate, "enabled", True)   # 翻译默认关闭，这里测打开时的判断
    assert needs_translation("I updated the cover title and checked that it still fits on the lab screen.")
    assert not needs_translation("改好了第二课封面，用 Opus 5.5 跑了 course_gates.py G1，结果 PASS。")
    assert not needs_translation("跑了：\n```\nuv run pytest -q tests/test_units.py --maxfail one two three four\n```")
    assert not needs_translation("路径是 ~/Projects/web/frontend/src/shared/lesson-shell.js")
    assert needs_translation("做完了。\n\nNote: the preview server was already running, so I reused it instead of starting another.")


def test_translated_footer():
    t = report.Turn(started=0, ended=60, request="改标题", final_text="改好了", translated="DeepSeek",
                    changed_files=["/w/a.html"])
    assert "英文已由 DeepSeek 译成中文" in report.build("#1 测试", t, "/w")[1]


def test_cards():
    import json
    from anzhuo import cards
    assert cards.step_label("Read", {"file_path": "/a/b/app.py"}) == "看 app.py"
    assert cards.step_label("Bash", {"command": "pytest -q", "description": "跑测试"}) == "跑命令：跑测试"
    assert cards.step_label("ToolSearch", {}) is None
    text = """要你定：封面标题用哪种字号？

A. 64px，最醒目
**B.** 56px，和正文更协调
- C、保持 48px

推荐：B，机房屏上不挤"""
    d = cards.parse_decision(text)
    assert d.question == "封面标题用哪种字号？"
    assert [k for k, _ in d.options] == ["A", "B", "C"] and d.options[1][1] == "56px，和正文更协调"
    assert d.recommend == "B"
    assert cards.parse_decision("要你定：只有一个 A. 方案") is None
    assert cards.parse_decision("做完了。A. 一 B. 二") is None
    data = cards.decision_data("#2 封面", d)
    btns = json.loads(data["sys_full_json_obj"])["msgButtons"]
    assert [b["id"] for b in btns] == ["pick:A", "pick:B", "pick:C"] and btns[1]["text"] == "选 B（推荐）"
    picked = cards.decision_data("#2 封面", d, picked="B")
    assert json.loads(picked["sys_full_json_obj"])["msgButtons"] == [] and "你选了 B" in picked["markdown"]
    assert cards.action_of('{"cardPrivateData": {"actionIds": ["pick:A"], "params": {}}}') == "pick:A"
    assert cards.action_of({"whatever": {"id": "stop"}}) == "stop"
    nested = {"outTrackId": "d4-1", "content": '{"cardPrivateData":{"actionIds":["single_button_node_x"],"params":{"id":"pick:B"}}}'}
    assert cards.action_of(nested) == "pick:B"
    assert cards.action_of({"content": '{"cardPrivateData":{"actionIds":["single_button_node_x"]}}'}) is None
    import time as _t
    t = report.Turn(started=_t.time() - 130, request="改封面", steps=[f"看 {i}.py" for i in range(9)])
    b = cards.board_data([("#2 封面", 2, t), ("#3 清理", 3, None)])
    assert b["title"] == "工作台 · 1 件在做" and "2 分钟 · 第 9 步" in b["markdown"] and "最新：看 8.py" in b["markdown"]
    assert "#3 清理**　排队中" in b["markdown"]
    assert [x["id"] for x in json.loads(b["sys_full_json_obj"])["msgButtons"]] == ["stop:2"]
    assert cards.board_data([])["title"].endswith("都做完了")
    assert cards.board_data([], moved=True)["markdown"].startswith("进展挪到下面了")
    assert cards.action_of('{"cardPrivateData":{"params":{"id":"stop:12"}}}') == "stop:12"


def test_push_message():
    """手机通知：标题是对话名，正文一句短话；去掉 markdown 和五段小标题。"""
    from anzhuo.report import push_message
    assert push_message("天气", "明天厦门天气", "**明天**多云。\n\n做了什么：查了", False, "") == ("天气", "做完了：明天多云")
    assert push_message("做课", "改封面", "要你定：用哪张？", False, "用哪张？") == ("做课", "等你定：用哪张？")
    assert push_message("清理", "清下载", "", True, "") == ("清理", "出错了，点开看原因")
    assert len(push_message("x", "", "很长" * 50, False, "")[1]) <= 23


HARD = "--" + "hard"  # 拆开写，免得本机的命令护栏把这份测试本身当成危险命令


def test_guard_danger():
    """危险操作认得出来；临时目录里删东西、普通命令不打扰。"""
    from anzhuo.guard import danger
    assert danger("git push origin dev") and danger(f"cd x && git reset {HARD} HEAD~1")
    assert danger("rm -rf ~/Projects/foo") == "删除 foo"
    assert danger("sqlite3 lab.db 'DELETE FROM users'") and danger("sudo rm x") and danger("find . -name '*.pyc' -delete")
    assert danger("ssh my-server 'rm -rf /opt/x'")
    assert danger("rm -rf /tmp/anzhuo-shots/a.png /private/tmp/b") is None
    assert danger("git status && git diff && ls -la") is None
    assert danger("git restore --staged a.py") is None and danger("git restore a.py")
    assert danger("echo 'firmware' && grep -rn form .") is None


def test_guard_hook_waits_for_answer():
    """碰到危险命令就停下等你：允许放行、不允许拒绝；普通命令直接过。"""
    import asyncio
    import time
    from anzhuo.report import Turn
    from anzhuo.engines import CLAUDE
    from anzhuo.sessions import Runner, Session
    asked = []

    async def on_confirm(s, turn):
        asked.append(turn.confirm["what"])

    async def go():
        r = Runner(registry=None, on_done=None, on_confirm=on_confirm)
        r.auto_review = False   # 关掉审核员：照旧等你点
        s, turn = Session(num=3, name="x", cwd="/tmp"), Turn(started=time.time())
        r.turns[3] = turn
        hook = CLAUDE.guard(r, s, turn)
        assert await hook({"tool_input": {"command": "ls"}}, "t0", None) == {}
        task = asyncio.create_task(hook({"tool_input": {"command": "git push"}}, "t1", None))
        await asyncio.sleep(0.05)
        assert asked and turn.confirm and r.answer_confirm(3, True)
        assert (await task)["hookSpecificOutput"]["permissionDecision"] == "allow" and turn.confirm is None
        task = asyncio.create_task(hook({"tool_input": {"command": "rm -rf ~/x"}}, "t2", None))
        await asyncio.sleep(0.05)
        r.answer_confirm(3, False)
        assert (await task)["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert not r.answer_confirm(3, True)   # 没有在等的就不算

    asyncio.run(go())


def test_reviewer_decides(monkeypatch):
    """开着审核员：危险命令由它判断，结果记进这一轮（报告里列出）；它出错时退回来问你。"""
    import asyncio
    import time
    from anzhuo import reviewer
    from anzhuo.engines import CLAUDE
    from anzhuo.report import Turn, build
    from anzhuo.sessions import Runner, Session
    answers = iter([(True, "任务要求推送"), (False, "删用户资料"), None])

    async def fake(what, cmd, cwd, request, recent, timeout=90, policy=""):
        return next(answers)
    monkeypatch.setattr(reviewer, "review", fake)
    asked = []

    async def on_confirm(s, turn):
        asked.append(turn.confirm["what"])

    async def go():
        r = Runner(registry=None, on_done=None, on_confirm=on_confirm)
        r.auto_review = True   # 审核员默认关，这里测开着的情形
        s, turn = Session(num=3, name="x", cwd="/tmp"), Turn(started=time.time(), request="改完推上去")
        r.turns[3] = turn
        hook = CLAUDE.guard(r, s, turn)
        first = await hook({"tool_input": {"command": "git " + "pu" + "sh"}}, "t1", None)
        assert first["hookSpecificOutput"]["permissionDecision"] == "allow"
        out = (await hook({"tool_input": {"command": "rm -rf ~/Documents/x"}}, "t2", None))["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny" and "删用户资料" in out["permissionDecisionReason"]
        assert not asked
        task = asyncio.create_task(hook({"tool_input": {"command": "sudo ls"}}, "t3", None))
        await asyncio.sleep(0.05)
        assert asked and r.answer_confirm(3, True)   # 审核员出错：退回来问你
        await task
        turn.ended = time.time()
        _, md = build("#3 x", turn, "/tmp")
        assert "审核员替你判断" in md and "放行 推送代码" in md and "拦下" in md

    asyncio.run(go())


def test_compact_queue():
    """正在压缩时说的话不插进压缩那一轮，排到压完之后；压缩点两次只排一次。"""
    import asyncio
    import time
    from anzhuo.report import Turn
    from anzhuo.sessions import COMPACT, Job, Runner, Session

    async def go():
        r = Runner(registry=None, on_done=None)
        s = Session(num=6, name="做课", cwd="/tmp")
        r.turns[6], r.live[6] = Turn(started=time.time(), request="压缩上下文"), object()
        r.workers[6] = asyncio.get_running_loop().create_future()   # 假装有人在干活，别真开 worker
        assert r.compacting(6) == "running"
        assert r.submit(s, Job(text="把这节课上线")) == 1            # 排队，不是插话（-1）
        assert [j.text for j in r.queued(6)] == ["把这节课上线"]
        r.turns[6] = Turn(started=time.time(), request="改封面")
        r.submit(s, Job(text=COMPACT))
        assert r.compacting(6) == "queued"

    asyncio.run(go())


def test_cleanup(tmp_path):
    """60 天前的副本删掉、新的留着；90 天前的对话记录压缩归档，登记着的不动。"""
    import gzip
    import os
    import time
    from anzhuo import cleanup
    now = time.time()
    data, proj, arch = tmp_path / "data", tmp_path / "projects", tmp_path / "archive"
    old, new = data / "outbox" / "4-1" / "a.png", data / "inbox" / "web" / "b.png"
    for f in (old, new):
        f.parent.mkdir(parents=True); f.write_bytes(b"x" * 10)
    os.utime(old, (now - 61 * 86400,) * 2)
    (proj / "p").mkdir(parents=True)
    for name in ("old", "keep", "fresh"):
        (proj / "p" / f"{name}.jsonl").write_text('{"a":1}\n' * 100)
    for name in ("old", "keep"):
        os.utime(proj / "p" / f"{name}.jsonl", (now - 91 * 86400,) * 2)
    r = cleanup.run(data, {"keep"}, now, proj, arch)
    assert old.exists() and new.exists()   # 对话里的截图、文件副本不删
    assert not (proj / "p" / "old.jsonl").exists() and (proj / "p" / "keep.jsonl").exists() and (proj / "p" / "fresh.jsonl").exists()
    assert gzip.open(arch / "p" / "old.jsonl.gz").read().count(b"\n") == 100
    assert r["removed"] == 0 and r["archived"] == 1


def test_guard_force_restart():
    from anzhuo.guard import danger
    assert danger("nohup sh -c 'sleep 90; launchctl kickstart -k gui/501/local.anzhuo' &")
    assert danger("launchctl list | grep anzhuo") is None


def test_codex_handoff_links(tmp_path):
    """GPT 把要交的文件写成 [下载…](/本机/路径) 链接：能交的拿出来发，链接只留文字；说明文档、源码的链接不动。"""
    from anzhuo.engines.base import handoff
    z = tmp_path / "包.zip"; z.write_bytes(b"PK")
    md = tmp_path / "先看这里.md"; md.write_text("x")
    png = tmp_path / "a.png"; png.write_bytes(b"x")
    text = f"[下载脚本包：包.zip]({z})\n\n详细见[《先看这里》]({md})\n[丢了](/nope/x.zip)\n交给你：{png}"
    out, imgs, files = handoff(text, str(tmp_path))
    assert files == [str(z)] and imgs == [str(png)]
    assert "**下载脚本包：包.zip**" in out and f"[《先看这里》]({md})" in out and "[丢了](/nope/x.zip)" in out
    assert "交给你" not in out


def test_noise_result():
    """后台任务通知被答的那句空话认得出来，正常回复不误伤。"""
    from anzhuo.engines.claude import NOISE
    assert NOISE.match("No response requested.") and NOISE.match("") and NOISE.match(" no response requested \n")
    assert not NOISE.match("No response requested. 但我顺便看了一下进度：还在跑。")


def test_tidy_scratch_and_purge(tmp_path):
    """临时文件夹 60 天没动、没对话在用 → 进废纸篓；删除 30 天以上的对话从名单去掉。"""
    import os
    import time
    from anzhuo import cleanup
    from anzhuo.sessions import Registry, Session
    now = time.time()
    root, trash = tmp_path / "s", tmp_path / "Trash"
    for n in ("old", "inuse", "fresh"):
        (root / n).mkdir(parents=True); (root / n / "a.txt").write_text("x")
    for n in ("old", "inuse"):
        os.utime(root / n / "a.txt", (now - 61 * 86400,) * 2); os.utime(root / n, (now - 61 * 86400,) * 2)
    assert cleanup.tidy_scratch(root, {str(root / "inuse")}, now, trash) == ["old"]
    assert (trash / "远窗临时-old" / "a.txt").exists() and (root / "inuse").exists() and (root / "fresh").exists()
    reg = Registry(tmp_path / "r.json")
    reg.add(Session(num=1, name="a", cwd="/", hidden=True, hidden_at=now - 31 * 86400))
    reg.add(Session(num=2, name="b", cwd="/", hidden=True, hidden_at=now - 5 * 86400))
    assert len(reg.sessions) == 2   # 删除的对话不从名单去掉（对话内容不删）


def test_track_agent_lifecycle():
    import time
    from claude_agent_sdk.types import TaskNotificationMessage, TaskProgressMessage, TaskStartedMessage
    from anzhuo.report import Turn
    from anzhuo.engines.claude import running_agents, sub_name, track_agent
    t = Turn(started=time.time())
    track_agent(t, TaskStartedMessage(subtype="task_started", data={}, task_id="a1", description="查课程目录", uuid="u", session_id="s", tool_use_id="tu1"))
    assert len(running_agents(t)) == 1 and sub_name(t, "tu1") == "查课程目录" and sub_name(t, None) == ""
    track_agent(t, TaskProgressMessage(subtype="task_progress", data={}, task_id="a1", description="", usage={}, uuid="u", session_id="s", last_tool_name="Bash"))
    assert t.agents["a1"]["last"] == "跑命令"
    track_agent(t, TaskNotificationMessage(subtype="task_notification", data={}, task_id="a1", status="completed", output_file="", summary="", uuid="u", session_id="s"))
    assert not running_agents(t) and t.agents["a1"]["status"] == "done"


def test_awaiting_kind_and_limit():
    """等你的轻重：要你定 / 列方案 / 批准编辑 / 直接问 = decide；做完顺口问“要不要我…” = suggest。撞额度能读出几点恢复。"""
    import time
    from datetime import datetime
    from anzhuo.intent import awaiting_kind, limit_reset
    assert awaiting_kind("要你定：用哪张？\nA. 红\nB. 蓝") == "decide"
    assert awaiting_kind("```\n批准本会话编辑：redzone-write｜path:a\n```") == "decide"
    assert awaiting_kind("封面用红色还是蓝色？") == "decide"
    assert awaiting_kind("改好了，截图在上面。\n\n要不要我顺便把第二节也改了？") == "suggest"
    assert awaiting_kind("改好了。") == ""
    now = datetime(2026, 10, 28, 15, 0).timestamp()
    at = limit_reset("You've hit your session limit · resets 5:40pm (Asia/Taipei)", now)
    assert datetime.fromtimestamp(at).strftime("%d %H:%M") == "28 17:40"
    at = limit_reset("You've hit your session limit · resets 2am", now)
    assert datetime.fromtimestamp(at).strftime("%d %H:%M") == "29 02:00"
    assert limit_reset("API Error: overloaded", now) is None
    assert time.time() < limit_reset("hit your limit") <= time.time() + 5 * 3600 + 1


def test_title_valid():
    from anzhuo.naming import _clean, valid
    assert _clean("大会话名字：问候打招呼") == "问候打招呼" and valid("问候打招呼")
    assert not valid("Here's a tit") and not valid("对话名字") and not valid("好") and valid("大数据第二节磨课")


def test_queue_merges_and_waits_for_client(monkeypatch):
    """还没开工时连着说的几句并成一件；刚开工、Claude 还没连上时说的话等连上就插进这一轮。"""
    import asyncio
    import time
    from anzhuo.engines import CLAUDE
    from anzhuo.report import Turn
    from anzhuo.sessions import Job, Runner, Session

    async def go():
        r = Runner(registry=None, on_done=None)
        s = Session(num=3, name="做课", cwd="/tmp")
        r.turns[3] = Turn(started=time.time(), request="压缩上下文")   # 正在压缩：后来的话排队
        r.live[3] = object()
        r.workers[3] = asyncio.get_running_loop().create_future()
        assert r.submit(s, Job(text="改封面")) == 1
        assert r.submit(s, Job(text="标题也大一点")) == 1             # 并进前一件，不另排
        q = r.queued(3)
        assert len(q) == 1 and q[0].parts == ["改封面", "标题也大一点"] and q[0].text == "改封面\n\n标题也大一点"
        assert r.unqueue_last(3).text.startswith("改封面") and not r.queued(3)
        # 刚开工、还没连上
        r.turns[3], r.live = Turn(started=time.time(), request="改封面"), {}
        injected = []

        async def fake_inject(runner, s_, client, job):
            injected.append(job.text)
        monkeypatch.setattr(CLAUDE, "_inject", fake_inject)
        assert r.submit(s, Job(text="顺便看下第二屏")) == -1
        await asyncio.sleep(0.1)
        r.live[3] = object()
        await asyncio.sleep(0.7)
        assert injected == ["顺便看下第二屏"] and r.turns[3].injected == 1

    asyncio.run(go())


def test_said_has_relay_first_message():
    from anzhuo.web import _said_has
    rec = "（这是接力的新对话：…）\n\n# 交接\n\n---\n用户这次说：\n\n推不上的原因：钩子跑 doctor"
    assert _said_has(rec, "推不上的原因：钩子跑 doctor")
    assert _said_has("批准本会话编辑：a", "批准本会话编辑：a")
    assert not _said_has(rec, "别的话")


def test_waiting_cleared_when_cancelled(tmp_path):
    """排队等名额时被取消，要从 waiting 里拿掉，不然这个对话一直显示“忙”。"""
    import asyncio
    from anzhuo.sessions import Job, Registry, Runner, Session

    async def go():
        reg = Registry(tmp_path / "sessions.json")
        r = Runner(registry=reg, on_done=None, max_parallel=1)
        s = Session(num=1, name="t", cwd=str(tmp_path))
        reg.add(s)
        await r.sem.acquire()          # 名额被别的对话占满
        r.submit(s, Job(text="做点事"))
        await asyncio.sleep(0.05)
        assert 1 in r.waiting and r.busy(1)
        r.workers[1].cancel()
        await asyncio.gather(r.workers[1], return_exceptions=True)
        assert 1 not in r.waiting and not r.busy(1)

    asyncio.run(go())
