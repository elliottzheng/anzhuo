"""远窗本体：收到你的话（网页或钉钉）→ 找对话 → 交给 Claude / Codex → 一轮做完把结果推回网页、手机通知和钉钉。
旁支各在自己的模块里，都持有这个 Bridge：钉钉收消息和文字指令（dingtalk_chat）、钉钉卡片（dingtalk_cards）、
额度监视（quota_watch）、定时任务（schedule_jobs）、后台维护（housekeeping）、接力（relay）、总管（superagent）。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from .localspend import LocalSpend
from . import engines, ext, folders, housekeeping, report, split, superagent, translate, usage
from .events import Bus
from .commands import Cmd
from .config import DATA_DIR, Config, web_accounts, write_web_hash
from .dingtalk import DingTalk, Incoming, NoDingTalk
from .dingtalk_cards import DingCards
from .dingtalk_chat import DingChat
from .quota_watch import QuotaWatch
from .report import Turn
from .relay import RELAY, Relays
from .schedule_jobs import ScheduleJobs
from .web import Web
from .engines import MODELS
from .sessions import COMPACT, Job, Registry, Runner, Session
from .intent import (KIND_CN, _is_reply, _looks_like_project, asks_user, awaiting_kind, inline_overrides, limit_reset,
                     said_engine_model)

try:   # 分派员（判断接着哪个对话、判断任务档位）只在私有版；开源导出不带，直接说话交给当前对话，档位按文件夹判断
    from . import router
except ImportError:
    router = None

RECENT_KEEP = 100  # recent.jsonl 只留最近这么多行（你发的、远窗发的、分给了谁）
CHAT_MEMORY = 30 * 60  # 远窗直接回答的话记 30 分钟，你接着问它还接得上

log = logging.getLogger(__name__)


class Bridge:
    @property
    def cfg(self) -> Config:
        return self._cfg

    @cfg.setter
    def cfg(self, cfg: Config) -> None:
        """配置只有这一份：换配置（设置页保存）时，干活的 Runner 也跟着换，不再一项项拷过去。"""
        self._cfg = cfg
        if runner := getattr(self, "runner", None):
            runner.cfg = cfg

    @property
    def quota_levels(self) -> dict[str, str]:
        """各家额度现在在哪一档（充足 / 偏紧 / 告急 / 撞线），分工表、接力换家时看。"""
        return self.quota.levels

    def __init__(self, cfg: Config, dt: DingTalk | NoDingTalk, data_dir: Path = DATA_DIR):
        self.cfg = cfg
        self.dt = dt
        self.data_dir = data_dir
        from .schedules import Schedules
        self.schedules = Schedules(data_dir / "schedules.json")
        self.registry = Registry(data_dir / "sessions.json")
        self.runner = Runner(self.registry, self._turn_done, cfg.max_parallel, on_start=self._turn_started,
                             on_confirm=self._confirm_asked, cfg=cfg)
        translate.enabled = cfg.translate_english
        from . import schedules as _sch
        _sch.set_timezone(cfg.timezone)
        self.runner.auto_title = True
        self.runner.on_usage = self._usage
        self.runner.role = lambda s: superagent.role(self, s)
        self.spend = LocalSpend(data_dir / "localspend.json")   # 本机全部 Claude Code 的消耗：换算周额度 % 用
        try:
            if n := usage.migrate(data_dir / "usage.jsonl"):
                log.info("消耗记录：%d 行累计值改成每轮的", n)
        except Exception:  # noqa: BLE001
            log.warning("消耗记录迁移失败", exc_info=True)
        self.outgoing: dict[int, list[dict]] = {}  # 你刚发给各对话、Claude 记录里还没有的话（网页对话框先显示出来）
        self._compact_from: dict[int, float | None] = {}  # 压缩前的上下文用量，压完报“59% → 12%”
        self.web = Web(cfg.web_base, cfg.web_port, data_dir,
                       watch_host=cfg.watch_host, accounts=web_accounts(cfg) if cfg.web_base else {}, save_hash=write_web_hash,
                       web_prefix=cfg.web_prefix)
        self.web.bridge = self
        # 事件：一轮做完、额度变档……谁关心谁订阅（events.py）；扩展在这之后加载，也能订阅
        self.events = Bus()
        self.relays = Relays(self)   # 接力（含换家、额度用完时远窗拼交接材料）；订阅 turn_done
        self.jobs = ScheduleJobs(self)   # 定时任务落到对话上；订阅 turn_done 记结果
        superagent.attach(self)   # 总管：回报（turn_report）、接力后交接派活关系（relayed）
        self.quota = QuotaWatch(self)   # 额度分档、撞线后到点接着做；订阅 quota 记日志
        self.dcards = DingCards(self)   # 钉钉卡片：工作台、确认、要你定、回复
        self.ding = DingChat(self)      # 钉钉收消息和文字指令
        self.web.exts = self.exts = ext.load(self)
        self.receipt_delay = 4.0
        self.chat: list[tuple[float, str, str]] = []  # 远窗自己直接回过的几轮（时间、你说的、它答的），接话时带上
        self.chat_at = 0.0  # 最近一次远窗直接回答的时间
        self.work_at = 0.0  # 最近一次把话交给对话的时间
        self.user_id = cfg.allowed_user_ids[0] if cfg.allowed_user_ids else "local"
        self.interrupted = []   # 重启前在做 / 排队的（总管派出去的活重启后接着做）
        for s in self.registry.sessions.values():
            if s.status in ("running", "queued"):
                s.status = "error"
                self.interrupted.append(s.num)
        if cfg.super_agent:
            superagent.ensure(self)
            self.runner.use_spare = True
        self.registry.save()

    def _usage(self, s: Session, turn: Turn) -> None:
        last = None
        if engines.get(s.engine).cum_usage and not s.claude_usage:   # 对话里还没存累计值：用记录里这个对话最后一条的
            last = next((r.get("cum") for r in reversed(usage.load(self.data_dir / "usage.jsonl"))
                         if r.get("num") == s.num and r.get("cum")), None)
        if rec := usage.row(s, turn, engines.get(s.engine).limits(self.runner), last=last):
            usage.append(self.data_dir / "usage.jsonl", rec)
            self.registry.save()

    # ---------- 发消息 ----------
    def record(self, who: str, text: str, **extra) -> None:
        """最近的来往原文存一份在本机（你同意的），排查“远窗为什么这么答”时看原话。只留最近 100 行。"""
        try:
            path = self.data_dir / "recent.jsonl"
            lines = path.read_text().splitlines()[-(RECENT_KEEP - 1):] if path.exists() else []
            row = {"t": time.strftime("%m-%d %H:%M:%S"), "who": who, "text": text[:2000], **extra}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join([*lines, json.dumps(row, ensure_ascii=False)]) + "\n")
        except OSError:
            log.debug("记最近来往失败", exc_info=True)

    async def detail(self, title: str, text: str) -> None:
        """过程类消息（开工、收到、插话、开始压缩）：钉钉安静模式下不发，只记下来（网页里本来就看得到）。"""
        if self.cfg.dingtalk_quiet:
            self.record("远窗（只记不发）", text, title=title)
            return
        await self.say(title, text)

    async def say(self, title: str, text: str, board: bool = True) -> None:
        self.record("远窗", text, title=title)
        for i, part in enumerate(report.split(text)):
            await self.dt.send_markdown(self.user_id, title if i == 0 else f"{title}（续）", part)
        if board:
            await self.dcards.follow()

    async def _turn_started(self, s: Session, turn: Turn, job: Job) -> None:
        """几秒内答完的不打扰；做得久的上工作台（没有就发一张）。网页开着时进度只在网页里看，钉钉只在钉钉里说的话才发。"""
        if job.schedule_id:
            self.schedules.started(job.schedule_id, job.schedule_run_id)
        await asyncio.sleep(self.receipt_delay)
        if job.finished or turn.ended or self.cfg.dingtalk_quiet or (self.web.enabled and job.via != "dingtalk"):
            return
        if self.dcards.board:
            return  # 工作台自己会刷新出这一件
        try:
            await self.dcards.post_board()
        except Exception:  # noqa: BLE001 - 卡片发不出就退回一句文字
            log.warning("发工作台失败", exc_info=True)
            await self.say("收到", f"收到 · {s.label} 在做：{turn.request or '看图'}", board=False)

    async def compact(self, num: int | None, via: str = "dingtalk") -> str:
        """压缩某个对话的上下文：发 /compact 给它；正在做就排到这一轮之后。返回给网页的一句话。"""
        s = self.registry.get(num or self.registry.current)
        if not s or not (s.claude_id or s.codex_id):
            msg = "这个对话还没开始，没什么可压缩的。"
            await self.say("压缩", msg)
            return msg
        if state := self.runner.compacting(s.num):   # 点了两次也只压一次
            return f"{s.label} " + ("正在压缩，大约一分钟。" if state == "running" else "已经排着要压缩了，手上的做完就压。")
        self._compact_from[s.num] = s.context_pct
        ahead = self.runner.submit(s, Job(text=COMPACT, via=via))
        msg = f"{s.label} 开始压缩上下文" + (f"，等手上 {ahead} 件做完再压" if ahead else "") + "，大约一分钟。"
        if via == "dingtalk" or not self.web.enabled:
            await self.detail("压缩", msg)
        return msg

    def _limits(self, s: Session) -> dict:
        """这个对话用的那一家的额度（ACP 引擎查不到额度）。"""
        return engines.get(s.engine).limits(self.runner)

    async def _turn_done(self, s: Session, turn: Turn) -> None:
        await self.events.emit("turn_done", s, turn)   # 接力、定时任务、扩展各自订阅（见 events.py）
        if turn.request == RELAY:
            if self.web.enabled:
                self.web.save_turn(s.label, turn, s.cwd, "接力摘要")
            self.registry.save()
            return  # 内部维护不覆盖原问题、不推送成普通答复；原始记录仍可追溯
        if turn.request == "压缩上下文" and not turn.is_error:
            before = self._compact_from.pop(s.num, None)
            after = s.context_pct
            span = f"{before:.0f}% → {after:.0f}%" if before is not None and after is not None else (f"现在 {after:.0f}%" if after is not None else "")
            if self.web.enabled and turn.via != "dingtalk":
                return   # 网页里点的压缩，网页上看得到结果
            return await self.say("压缩好了", f"{s.label} 压缩好了：上下文 {span}。之前聊过的要点都还记得。")
        if translate.needs_translation(turn.final_text):
            if done := await translate.to_chinese(turn.final_text):
                turn.final_text, turn.translated = done
        title, body = report.build(s.label, turn, s.cwd, self._limits(s))
        if self.web.enabled:
            try:
                tid = self.web.save_turn(s.label, turn, s.cwd, title.split(" · ")[0] if title else "回复")
                if not (turn.changed_files or turn.commands or turn.images or turn.is_error):
                    raise LookupError  # 闲聊、直接问答：记下来给网页对话框用，报告里不附链接
                turn.links = [("完整过程", self.web.link(f"/_x/t/{tid}"))]
                turn.links += [(f"{x['label']}：{x['title']}" if x.get("title") else x["label"], self.web.link(x["url"]))
                               for x in self.web.links_of(self.web._turn(tid) or {})]
                title, body = report.build(s.label, turn, s.cwd, self._limits(s))
            except LookupError:
                pass
            except Exception:  # noqa: BLE001 - 过程页出问题不影响报告
                log.exception("记过程页失败")
        s.last_title = title
        s.last_summary = f"你说“{turn.request}”，它答“{report.brief(turn.final_text, 60)}”"
        s.awaiting = "" if turn.is_error else asks_user(turn.final_text)
        s.awaiting_kind = awaiting_kind(turn.final_text) if s.awaiting else ""
        if turn.is_error and (until := limit_reset(turn.error or turn.final_text)):
            await self.quota.hit_limit(s, until)   # 撞上 5 小时额度：恢复后一分钟自动接着做
        self.registry.save()
        if await self.events.claim("turn_report", s, turn):
            return   # 别人接了（总管派的活：先回报给总管，由总管告诉你）
        # 通知：你正开着这个对话看就都不发；不然先弹手机通知，弹不出去才用钉钉说一句（带链接）。
        # 钉钉里说的话、或没开网页时，照旧把完整结果发回钉钉。
        viewing = self.web.enabled and self.web.watching(s.num)
        full = (turn.via == "dingtalk" or not self.web.enabled) and s.owner == "admin"
        pushed = 0
        if self.web.enabled and not viewing:
            head, what = report.push_message(s.name, turn.request, turn.final_text, turn.is_error, s.awaiting,
                                             s.awaiting_kind, s.limit_until if turn.is_error else 0)
            pushed = await self.web.notify(s.owner, head, what, s.num)
        if not full:
            if not viewing and not pushed and s.owner == "admin":
                head, what = report.push_message(s.name, turn.request, turn.final_text, turn.is_error, s.awaiting,
                                                 s.awaiting_kind, s.limit_until if turn.is_error else 0)
                try:
                    await self.say(f"#{s.num} {what[:20]}", f"**#{s.num} {s.name}**　{what}　[在远窗看]({self.web.link(f'/_x/#s{s.num}')})",
                                   board=False)
                except Exception:  # noqa: BLE001
                    log.exception("钉钉提醒 #%s 失败", s.num)
            return
        await self.dcards.deliver(s, turn, title, body)

    async def _confirm_asked(self, s: Session, turn: Turn) -> None:
        """有危险操作等你点头：手机通知 + 钉钉按钮卡（网页对话里也会出现允许 / 不允许）。"""
        c = turn.confirm
        if not c:
            return
        self.record("确认", c["what"], num=s.num)
        if self.web.enabled:
            asyncio.create_task(self.web.notify(s.owner, s.name, "等你确认：" + c["what"], s.num))
        if s.owner == "admin":
            await self.dcards.ask_confirm(s, c)

    async def answer_confirm(self, num: int, allow: bool, where: str) -> bool:
        """不管在哪点的（网页 / 钉钉），都走这里：交给对话，把钉钉卡改成结果。"""
        if not self.runner.answer_confirm(num, allow):
            return False
        result = ("你允许了" if allow else "你没允许") + f"（{where}）"
        self.record("你", f"确认 #{num}：{result}")
        await self.dcards.confirm_done(num, result)
        return True

    # ---------- 开对话、交话 ----------
    async def _resolve_all(self, tokens: list[str]) -> tuple[list[Path], str | None, list[Path]]:
        """返回 (已确定的目录, 有歧义的词, 该词的候选)。"""
        done: list[Path] = []
        for t in tokens:
            c = folders.candidates(t, self.cfg)
            if not c:
                raise LookupError(t)
            if len(c) > 1:
                return done, t, c
            done.append(c[0])
        return done, None, []

    async def new_session(self, tokens: list[str], text: str, inc: Incoming, resolved: list[Path] | None = None,
                          note: str = "", kind: str | None = None, model: str | None = None, effort: str | None = None) -> None:
        """对外接口：按“新开 文件夹 …”的说法开一个新对话（解析文件夹、没给就用临时文件夹），再交给 start_session。"""
        resolved = list(resolved or [])
        rest = tokens[len(resolved):]
        if not tokens:
            num = self.registry.reserve_num()
            cwd = folders.scratch_dir(self.cfg, num)
            return await self.start_session(num, cwd, [], text, inc, note=note or "（没指定文件夹，用了临时文件夹）", kind=kind,
                                     model=model, effort=effort)
        try:
            more, ambiguous, cands = await self._resolve_all(rest)
        except LookupError as e:
            return await self.say("没找到文件夹", f"没找到“{e.args[0]}”。可以写短名（{'、'.join(self.cfg.folders)}）、完整路径，或文件夹名里更具体的几个字。")
        resolved += more
        if ambiguous:
            return await self.ding.ask_folder(ambiguous, cands, tokens=tokens, resolved=resolved, text=text, inc=inc,
                                              note=note, task_kind=kind)
        num = self.registry.reserve_num()
        await self.start_session(num, resolved[0], resolved[1:], text, inc, note=note, kind=kind, model=model, effort=effort)

    async def start_session(self, num: int, cwd: Path, add_dirs: list[Path], text: str, inc: Incoming, note: str = "",
                            kind: str | None = None, model: str | None = None, effort: str | None = None,
                            owner: str = "admin", root: str = "", boss: int = 0, engine_model: str | None = None,
                            engine: str | None = None) -> None:
        """对外接口：在 cwd 里开好编号为 num 的新对话并交出第一句话。
        model / effort 是网页里选好的（模型全名、深度英文），优先于按任务类型挑的和话里说的；
        engine = 点名了哪家但没点型号（总管写 engine=claude），分工表只在这家里挑。"""
        name = report.brief(text, 16) or "新对话"
        project = _looks_like_project(cwd)
        if kind is None:
            kind = "dev" if project else "simple"   # 判断失败时：代码仓里按开发算，别处按简单任务
            if router and text and not model and not inline_overrides(text)[0]:
                kind = await router.classify_kind(text) or kind   # 不在代码仓也要判断：梳理想法、写方案这类定方案的活照分工表的 design 档
        s = Session(num=num, name=name, cwd=str(cwd), add_dirs=[str(p) for p in add_dirs], owner=owner, root=root, boss=boss)
        said_model, said_effort = inline_overrides(text)
        chosen = None
        if not (model or said_model):   # 没指定时照分工表挑（首选用不了换备选）
            chosen = split.choose(self.cfg, kind, [cwd, *add_dirs], fenced=bool(root), engine=engine, levels=self.quota_levels)
            model, engine_model = MODELS.get(chosen.pick, chosen.pick), engine_model or chosen.model or None
            effort = effort or chosen.effort or None
        alt = engines.engine_of(model or said_model)   # 选的是别家（GPT、Kimi 等）：换引擎，模型那一栏不再是 Claude 的型号
        if root and alt and not engines.get(alt).fenced_ok:   # 受限账号只用 Claude
            model = said_model = alt = None
        if alt:   # ACP 引擎（Kimi 等）的 engine_model 是它自己的模型名（总管指定的）；GPT 用 Codex 自己配置的
            s.engine, model, said_model = alt, None, None
            s.model = (engine_model or said_engine_model(alt, text)) if engines.get(alt).pick_model == "own" else None
        s.model = model or said_model or s.model
        s.effort = effort or said_effort or s.effort
        self.registry.add(s)
        where = folders.display(cwd) + "".join(f" ＋ {folders.display(p)}" for p in add_dirs)
        why = (f"分工表 · {KIND_CN.get(kind, '简单任务')}" + (f" · {chosen.note}" if chosen.note else "") if chosen
               else engines.get(s.engine).brand if alt else "你指定的")
        effort = report.EFFORT_CN.get(s.effort or self.runner.default_effort, "默认")
        meta = f"文件夹：{where}{note}\n\n模型：{report.model_name(s.model)}（{why}）· 深度{effort}"
        if not text and not inc.image_codes and not inc.files:
            if owner != "admin":
                return
            return await self.say("开好了", f"{s.label} 开好了\n\n{meta}\n\n接下来直接说要做什么。")
        await self.submit(s, text, inc, opening=f"开工 · {s.label}\n\n{meta}")

    async def message(self, cmd: Cmd, inc: Incoming, routed: bool = False) -> None:
        """对外接口：一句话交给哪个对话（带编号的直接给；没带的按自动判断 / 总管），最后走 submit。"""
        if not routed and await self.jobs.from_text(cmd, inc):
            return   # 说的是定时安排：建好任务就完了
        num = cmd.num
        if num is None and inc.quoted_text:
            m = re.search(r"#(\d{1,4})", inc.quoted_text)
            num = int(m.group(1)) if m else None
        chatting = self.chat_at > self.work_at and time.time() - self.chat_at < CHAT_MEMORY
        if num is None and cmd.text and not routed and _is_reply(cmd.text) and not chatting:
            waiting = sorted((x for x in self.registry.visible() if x.awaiting), key=lambda x: -x.updated)
            if waiting:  # “按C来做”“好的”这类回复，交给正在等你回答的那个对话（最近问的那个）
                num = waiting[0].num
        # 开了总管（super_agent）就由总管分派，分派员让开，免得两个都来判断
        auto_route = router and self.cfg.auto_route and not self.cfg.super_agent
        if num is None and cmd.text and not routed and auto_route and (chatting or not _is_reply(cmd.text)):
            recent = [(q, a) for t, q, a in self.chat if time.time() - t < CHAT_MEMORY]
            busy = {n for n in self.registry.sessions if self.runner.busy(n)}
            r = await router.route(cmd.text, self.registry.visible(), self.registry.current,
                                   list(self.cfg.folders), chat=recent, busy=busy)
            self.record("分派", r.reason if r else "判断失败", action=r.action if r else None,
                        target=r.target if r else None)
            if r and r.action == "reply" and (inc.image_codes or inc.files):
                r = None  # 远窗看不到图和文件，带图的话交给当前对话
            if r and r.action == "reply":
                self.chat = [*self.chat[-5:], (time.time(), cmd.text, r.answer)]
                self.chat_at = time.time()
                return await self.dcards.reply(r.answer, cmd.text)
            if r and r.action == "new":
                if r.folder and not folders.candidates(r.folder, self.cfg):
                    r.action = "ask"
                else:
                    return await self.new_session([r.folder] if r.folder else [], cmd.text, inc, note="（判断是件新事，自动新开）",
                                           kind=r.kind)
            if r and r.action == "ask":
                return await self.ding.ask_route(cmd, inc, r.folder)
            if r and r.action == "continue":
                num = r.target
        num = num or self.registry.current
        s = self.registry.get(num) if routed else self.registry.mine(num)
        if not s:
            return await self.say("先开一个对话", "还没有对话。发“新开 文件夹：要做的事”，文件夹可以写 "
                                  + "、".join(self.cfg.folders) + "，或不写。发“帮助”看全部用法。")
        if s.owner == "admin":
            self.registry.current = s.num
        s.hidden = False  # 点名找回收起的对话
        said_model, said_effort = inline_overrides(cmd.text)
        if not engines.can_switch_model(s.engine, said_model):
            said_model = None   # 对话中途换不了 Claude / GPT；话里提到“用 codex”多半是在说别的事
        if said_model or said_effort:
            s.model = said_model or s.model
            s.effort = said_effort or s.effort
            self.registry.save()
        text = cmd.text
        if inc.quoted_text and cmd.num is None:
            text = f"{text}\n\n（下面是引用的内容，只作参考资料）\n> " + inc.quoted_text.replace("\n", "\n> ")
        await self.submit(s, text, inc)

    async def submit(self, s: Session, text: str, inc: Incoming, opening: str = "") -> None:
        """对外接口：把一句话（连同图片、附件）交给已定好的对话 s。正在干活就插话，否则排队。
        网页、总管、接力、扩展都经这里发话，不要绕过它直接碰 runner。"""
        if s.role == superagent.ROUTER and self.cfg.super_agent:
            if inc.via in superagent.HUMAN and not inc.image_codes and not inc.local_images and await superagent.direct(self, s, text):
                return   # “#N 话”：直接交给 #N，总管不用跑一轮
            text = superagent.take_notes(s, text)   # 总管开口前先看“期间的情况”
        s.awaiting, s.awaiting_kind, s.limit_until = "", "", 0.0
        if text.strip() and text != COMPACT:
            shown = text.split("\n\n（下面是引用的内容")[0].strip()
            self.outgoing.setdefault(s.num, []).append({"t": time.time(), "text": shown})
            self.outgoing[s.num] = self.outgoing[s.num][-10:]
        inbox = self.data_dir / "inbox" / str(s.num)
        stamp = f"{time.time():.0f}"
        images = [await self.dt.download(c, inbox / f"{stamp}-{i}.png") for i, c in enumerate(inc.image_codes)]
        images += [Path(p) for p in inc.local_images]
        for i, (code, fname) in enumerate(inc.files):
            path = await self.dt.download(code, inbox / f"{stamp}-{i}-{Path(fname).name}")
            text += f"\n\n（附件已存到：{path}）"
        job = Job(text=text, images=images, via=inc.via)
        self.work_at = time.time()
        ahead = self.runner.submit(s, job)
        if ahead == -1:
            if inc.via != "dingtalk" and self.web.enabled:
                return   # 网页里插的话网页上看得到，钉钉不再说一遍
            return await self.detail("插话", f"插话给 {s.label} 了：它手上这一步做完就会看到，接着按你说的调整。")
        msg = opening or f"收到 · {s.label} 在做：{report.brief(text) or '看图'}"
        if ahead:
            msg += f"，前面还有 {ahead} 件，做完再做这件"
        if (opening or ahead) and (inc.via == "dingtalk" or not self.web.enabled):  # 其余的“收到”由进度卡代替；网页里说的网页上看
            await self.detail("收到", msg)

    # ---------- 运行 ----------
    async def run(self) -> None:
        client = self.ding.client() if self.cfg.dingtalk_enabled else None
        if self.web.enabled:
            await self.web.start()
        # 插着电时别让 Mac 睡着（远窗退出就自动失效）；合盖或用电池时照常睡。Windows / Linux 没有 caffeinate，跳过
        try:
            subprocess.Popen(["caffeinate", "-s", "-w", str(os.getpid())])
        except FileNotFoundError:
            pass
        if self.cfg.auto_cleanup:   # 每天清理、每月体检（配置 auto_cleanup）
            asyncio.create_task(housekeeping.cleanup_loop(self))
        asyncio.create_task(self.quota.loop())
        asyncio.create_task(housekeeping.spend_loop(self))
        asyncio.create_task(self.jobs.loop())
        asyncio.create_task(self.runner.title_all(self.data_dir / "turns"))   # 旧对话补上短名字
        from .codex import codex_bin, probe_models
        if codex_bin():   # 记下 Codex 能用哪些型号（Sol / Terra / Luna …），网页、总管选型号用
            asyncio.create_task(probe_models())
        from .acp import probe_all   # 记下各个 ACP 助手有哪些模型（网页选型号、总管派活都能挑），不说话不花额度
        asyncio.create_task(probe_all(str(self.data_dir / "router")))
        if self.cfg.super_agent and self.interrupted:   # 总管派出去、重启前没做完的活：带恢复提示接着做
            asyncio.create_task(superagent.recover(self, self.interrupted))
        self.quota.resume_waiting()   # 重启前撞了额度在等恢复的，接着等
        log.info("远窗启动，已登记 %d 个对话%s", len(self.registry.sessions), "" if client else "（钉钉已停用，只用网页）")
        if client:
            await client.start()
        else:
            await asyncio.Event().wait()   # 只用网页：没有钉钉长连接要等，常驻即可
