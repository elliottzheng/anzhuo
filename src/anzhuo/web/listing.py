"""对话列表与开新对话的周边：列表、任务分组、管理、选文件夹、接电脑端对话、定时任务。"""
from __future__ import annotations

import asyncio
import os
import logging
import re
import time
from pathlib import Path

from aiohttp import web

from .. import quota, schedules, superagent, usage, task_groups
from ..sessions import Session
from ..folders import display, readable
from ..report import LIMIT_CN
from .common import DESK_FRESH, model_options

log = logging.getLogger(__name__)


class ListingMixin:
    async def _api_task_groups(self, request):
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        owner = request.get("owner", "admin")
        if owner != "admin":
            raise web.HTTPForbidden()
        if not getattr(self.bridge.cfg, "task_grouping", False):   # 没开自动分组：不把标题、摘要发给模型
            return web.json_response({"ok": True, "count": 0})
        data = await request.json()
        if owner in self._group_busy:
            return web.json_response({"ok": False, "busy": True})
        if not data.get("force") and time.time() - self._group_attempt.get(owner, 0) < 300:
            return web.json_response({"ok": False, "retry_later": True})
        sessions = [s for s in self.bridge.registry.sessions.values()
                    if s.owner == owner and not s.hidden and self._relay_latest(s).num == s.num]
        targets = sorted([s for s in sessions if not s.task_group_manual and (data.get("force") or not s.task_group)],
                         key=lambda s: -s.updated)[:80]
        if not targets:
            return web.json_response({"ok": True, "count": 0})
        self._group_busy.add(owner)
        self._group_attempt[owner] = time.time()
        try:
            items = [{"num": s.num, "title": self._relay_chain(s)[0].name[:60],
                      "folder": Path(s.cwd).name[:80], "summary": s.last_summary[:400]}
                     for s in targets]
            result, model = await task_groups.classify(items, sorted({s.task_group for s in sessions if s.task_group}),
                                                       cache=None if data.get("force") else self.store_path.with_name("task_groups.json"),
                                                       rules=self.bridge.cfg.task_group_rules)
            count = 0
            for s in targets:
                # 分类期间用户可能改了分组、删除或完成接力；以当前现场为准。
                if s.task_group_manual or s.hidden or s.owner != owner:
                    continue
                latest = self._relay_latest(s)
                if latest.task_group_manual:
                    continue
                for member in self._relay_chain(latest):
                    member.task_group = result[s.num]
                count += 1
            self.bridge.registry.save()
            return web.json_response({"ok": True, "count": count, "model": model})
        except Exception:
            log.warning("任务归组失败，保留原分组")
            return web.json_response({"ok": False, "error": "暂时没整理成功，原有分组已保留"})
        finally:
            self._group_busy.discard(owner)

    async def _api_sessions(self, request: web.Request) -> web.Response:
        b = self.bridge
        now = time.time()
        rows = []
        owner = request.get("owner", "admin")
        fenced = owner != "admin"
        regrouped = False
        for member in b.registry.sessions.values():
            if member.owner == owner and member.task_group and not member.task_group_manual:
                group = task_groups.normalize(member.task_group)
                if group != member.task_group:
                    member.task_group = group
                    regrouped = True
        if regrouped:
            b.registry.save()

        for s in [x for x in b.registry.sessions.values() if not x.hidden and x.owner == owner]:
            if self._relay_latest(s).num != s.num:
                continue
            turn = b.runner.turns.get(s.num)
            if turn and turn.confirm:
                status, detail = "awaiting", f"等你确认：{turn.confirm['what']}"
            elif turn and b.runner.compacting(s.num) == "running":
                status, detail = "running", "正在压缩上下文"
            elif turn:
                status, detail = "running", f"在做 · {int((now - turn.started) // 60)} 分钟 · 第 {len(turn.steps)} 步"
            elif b.runner.busy(s.num):
                status, detail = "queued", "排队中"
            elif s.limit_until > now:
                status = "paused"
                detail = f"额度用完 · {time.strftime('%H:%M', time.localtime(s.limit_until))} 恢复" + ("后接着做" if s.auto_resume else "")
            elif s.awaiting and s.awaiting_kind == "suggest":
                status, detail = "idle", f"有建议：{s.awaiting}"
            elif s.awaiting:
                status, detail = "awaiting", f"等你定：{s.awaiting}"
            elif s.status == "error":
                status, detail = "error", "上次出错了"
            else:
                status, detail = "idle", s.last_title or "空闲"
            unread = status in ("idle", "awaiting", "error", "paused") and bool(s.claude_id or s.codex_id) and s.updated > s.seen + 1
            rows.append({"num": s.num, "name": self._relay_chain(s)[0].name, "cwd": display(Path(s.cwd)), "status": status, "unread": unread,
                         "detail": detail, "updated": s.updated, "steps": len(turn.steps) if turn else 0,
                         "engine": getattr(s, "engine", "claude"),
                         "task_group": s.task_group or self._relay_chain(s)[0].task_group, "task_group_manual": s.task_group_manual,
                         "pinned": s.pinned, "role": s.role,
                         "suggest": s.awaiting_kind == "suggest" and status == "idle",
                         "last_step": turn.steps[-1] if turn and turn.steps else ""})
        order = {"awaiting": 0, "running": 1, "queued": 2, "error": 3, "paused": 4, "idle": 5}
        rows.sort(key=lambda r: (order[r["status"]], -r["updated"]))
        acct = request.get("acct", "")
        me = {"name": acct, "role": self.role(acct), "root": display(Path(self.root_of(acct)).expanduser()) if fenced else ""}
        extra = {} if fenced else await asyncio.to_thread(self._ext_data, "sessions_extra", rows)   # 扩展可能要翻文件
        from ..app import router
        return web.json_response({"sessions": rows, "ext": extra, "me": me,
                                  "folders": [] if fenced else list(b.cfg.folders),
                                  "current": None if fenced else b.registry.current,
                                  "models": [m for m in model_options() if not fenced or m[0] in ("opus", "sonnet", "haiku", "fable")],
                                  "limits": self._limits(b.runner.limits) if not fenced else self._my_limits(acct),
                                  "limits_gpt": [] if fenced else self._limits(getattr(b.runner, "limits_gpt", {})),
                                  "recent": [] if fenced else self._recent_folders(), "last_folder": "" if fenced else self._last_folder(),
                                  "task_grouping": getattr(b.cfg, "task_grouping", False),
                                  "auto_where": router is not None,   # 有分派员才能“在哪做：自动”
                                  "has_gpt": any(getattr(x, "engine", "") == "codex" for x in b.registry.visible(owner))})

    def _recent_folders(self, limit: int = 6) -> list[dict]:
        """最近用过的工作文件夹（新对话菜单可用）；临时文件夹和家目录不算。这里不碰磁盘：受保护的文件夹一 stat 就可能卡住。"""
        b = self.bridge
        scratch = str(Path(b.cfg.scratch_root).expanduser())
        home = str(Path.home())
        seen, out = {str(Path(v).expanduser()) for v in b.cfg.folders.values()}, []   # 配了短名的文件夹已经在菜单里，不再重复列
        for s in sorted(b.registry.sessions.values(), key=lambda x: -x.updated):
            if s.owner != "admin" or s.role == "router":   # 受限账号的、总管的文件夹不算（总管的文件夹不是干活的地方）
                continue
            p = str(Path(s.cwd).expanduser())
            if p in seen or p == home or p.startswith(scratch):
                continue
            seen.add(p)
            out.append({"path": p, "name": Path(p).name or p, "show": display(Path(p))})
            if len(out) >= limit:
                break
        return out

    def _last_folder(self) -> str:
        """最近一个对话用的文件夹（有短名就给短名，没有给路径）；没有、或它用的是临时文件夹，就空。"""
        b = self.bridge
        scratch, home = str(Path(b.cfg.scratch_root).expanduser()), str(Path.home())
        for s in sorted(b.registry.sessions.values(), key=lambda x: -x.updated):
            if s.owner != "admin" or s.role == "router":   # 受限账号的、总管的文件夹不算（总管的文件夹不是干活的地方）
                continue
            p = str(Path(s.cwd).expanduser())
            if p == home or p.startswith(scratch):
                return ""
            return next((k for k, v in b.cfg.folders.items() if str(Path(v).expanduser()) == p), p)
        return ""

    def _my_limits(self, acct: str) -> list[dict]:
        """受限账号只看自己的额度：用了自己那份的百分之几、几点重置。"""
        q = self.quota(acct)
        if not q:
            return []
        return [{"name": "本周", "pct": round(q["pct"]), "reset": q["reset"], "reset_at": q["reset_at"]}]

    @staticmethod
    def _limits(d: dict) -> list[dict]:
        """每个窗口一格；eta = 按这速度何时用完（重置前用不完是 None），pace = 按这速度到重置时会用到几成（算不出是 None）。"""
        now, rows = time.time(), []
        for k, (u, r) in d.items():
            w = quota.window(k, (u, r), now) or {}
            rows.append({"name": LIMIT_CN.get(k, k), "pct": round(u * 100), "reset": r or "", "reset_at": usage.reset_epoch(r, now),
                         "eta": w.get("eta"), "pace": None if w.get("pace") is None else round(w["pace"] * 100), "level": w.get("level")})
        return rows

    async def _api_manage(self, request: web.Request) -> web.Response:
        """列表里长按一条对话：置顶 / 取消置顶、改名、归类、删除（收起）、找回。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        b = self.bridge
        s = b.registry.get(int(request.match_info["num"]))
        if not s:
            raise web.HTTPNotFound()
        data = await request.json()
        act, val = data.get("action"), str(data.get("value") or "").strip()
        if act == "pin":
            s.pinned = bool(data.get("value"))
        elif act == "rename":
            if not val:
                return web.json_response({"ok": False, "error": "名字不能空着"})
            for member in self._relay_chain(s):
                member.name, member.renamed = val[:40], True
        elif act == "task_group":
            if not 2 <= len(val) <= 24 or any(ord(c) < 32 for c in val):
                return web.json_response({"ok": False, "error": "任务分组名称需为2到24字"}, status=400)
            for member in self._relay_chain(s):
                member.task_group, member.task_group_manual = val, True
        elif act == "delete":
            if s.role == superagent.ROUTER:
                # 总管由 super_agent 功能管理（启动时 ensure 自动找回）：
                # 删了它工人的汇报链就断，直到下次重启才复活 —— 明确拒绝并说明。
                return web.json_response({"ok": False,
                                          "error": "总管删不掉：收起即可（重启远窗会自动置顶找回）"}, status=400)
            if s.num in b.runner.turns or b.runner.busy(s.num):
                return web.json_response({"ok": False, "error": "还在做，先停下再删"})
            await self.speech.remove_session(s.num)
            s.hidden, s.hidden_at, s.pinned = True, time.time(), False
            if b.registry.current == s.num:
                b.registry.current = None
        elif act == "autoresume":
            s.auto_resume = bool(data.get("value"))
            if s.auto_resume and s.limit_until > time.time():
                asyncio.create_task(b.quota.auto_resume(s.num, s.limit_until))
        elif act == "dismiss":   # 不想回答了：把“等你定 / 出错了”的状态撤掉，对话回到空闲
            if s.num in b.runner.turns or b.runner.busy(s.num):
                return web.json_response({"ok": False, "error": "还在做，没有可撤的状态"})
            s.awaiting = s.awaiting_kind = ""
            if s.status == "error":
                s.status = "idle"
            s.seen = time.time()
            self._threads.pop(s.num, None)
        elif act == "restore":
            s.hidden, s.hidden_at = False, 0.0
            s.updated = max(s.updated, time.time() - 60)   # 找回来的排在最近里
        else:
            raise web.HTTPBadRequest()
        b.registry.save()
        return web.json_response({"ok": True})

    @staticmethod
    def _list_dirs(base: Path) -> list[str] | None:
        """base 下看得见的子文件夹名（不读文件内容）；打不开返回 None。下载 / 桌面 / 文稿先用 readable 试，免得卡在隐私弹窗。"""
        if not base.is_dir() or not readable(base):
            return None

        def visible(e) -> bool:
            if e.name.startswith("."):
                return False
            try:
                return e.is_dir() and not (getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x2)   # Windows 隐藏属性
            except OSError:
                return False
        try:
            return sorted((e.name for e in base.iterdir() if visible(e)), key=str.lower)[:500]
        except OSError:
            return None

    async def _api_browse(self, request: web.Request) -> web.Response:
        """新对话“在哪做”的“浏览文件夹…”：一层层列子文件夹，默认从 ~/Projects 开始。"""
        raw = request.query.get("path", "").strip()
        if raw == "__drives__":   # “此电脑”层：所有盘符的列表（Windows）；文件系统里没有统一根，这层是虚拟的
            drives = [f"{d}\\" for d in os.listdrives()] if hasattr(os, "listdrives") else []
            return web.json_response({"raw": raw, "show": "此电脑", "parent": "",
                                      "dirs": drives, "absolute": True, "drive_level": True})
        home = Path.home()
        base = Path(raw).expanduser() if raw else (home / "Projects" if (home / "Projects").is_dir() else home)
        if not base.is_absolute():
            base = home / base
        base = base.resolve()
        dirs = await asyncio.to_thread(self._list_dirs, base)
        if dirs is None:
            return web.json_response({"error": "读不到这个文件夹"}, status=400)
        at_root = base == base.parent
        return web.json_response({"raw": str(base), "show": display(base),
                                  "parent": "" if at_root else str(base.parent), "dirs": dirs,
                                  "drive_root": at_root and bool(base.drive)})

    @staticmethod
    def _cc_list(d: Path | None, limit: int | None) -> list:
        from claude_agent_sdk import list_sessions
        return list_sessions(str(d) if d else None, limit, 0, False)

    async def _api_cc_sessions(self, request: web.Request) -> web.Response:
        """电脑端（终端 / IDE / 桌面端）开的 Claude Code 对话，接着聊用：读 ~/.claude/projects 的记录，不碰文件内容。
        不带 path 列所有文件夹最近的，规则同钉钉的“接着桌面”：远窗自己开的、临时文件夹和家目录里的不算；带 path 只列那个文件夹的。"""
        raw = request.query.get("path", "").strip()
        d = Path(raw).expanduser() if raw else None
        if d and (not d.is_absolute() or not d.is_dir()):
            return web.json_response({"error": "先选一个文件夹"}, status=400)
        try:
            infos = await asyncio.to_thread(self._cc_list, d, 30 if d else 150)
        except Exception as e:  # noqa: BLE001 - SDK 读记录失败就算了，不让网页 500
            return web.json_response({"error": f"读不到对话记录：{e}"}, status=400)
        sessions = self.bridge.registry.sessions.values() if self.bridge else []
        mine = {s.claude_id: s.num for s in sessions if s.claude_id}
        if not d:
            ours = {s.claude_id for s in sessions if s.claude_id and not s.from_desktop}
            cfg = getattr(self.bridge, "cfg", None)
            scratch = Path(cfg.scratch_root).expanduser() if cfg else None
            infos = [x for x in infos if x.session_id not in ours and x.cwd and Path(x.cwd) != Path.home()
                     and not (scratch and Path(x.cwd).is_relative_to(scratch))][:30]
        out = [{"id": x.session_id,
                "title": (x.custom_title or x.summary or x.first_prompt or "（没标题）").replace("\n", " ")[:60],
                "at": x.last_modified, "num": mine.get(x.session_id),
                "cwd": x.cwd or (str(d) if d else "")} for x in infos]
        return web.json_response({"sessions": out})

    async def _api_resume_cc(self, request: web.Request) -> web.Response:
        """把 Claude Code 已有的对话接进远窗：建一个带 claude_id 的对话，之后的说话都 resume 它。
        已经接过的（包括收起的）直接回到原来那个，不另开一个：两个对话续同一份记录会互相打架。"""
        if not self._same_origin(request):
            raise web.HTTPForbidden()
        b = self.bridge
        d = await request.json()
        path, sid = str(d.get("path", "")).strip(), str(d.get("session_id", "")).strip()
        name = str(d.get("name", "")).strip()[:16] or "接着聊"
        folder = Path(path).expanduser() if path else None
        if not folder or not folder.is_absolute() or not re.fullmatch(r"[0-9A-Za-z-]{8,64}", sid):
            return web.json_response({"error": "参数不完整"}, status=400)
        if s := next((v for v in b.registry.sessions.values() if v.claude_id == sid), None):
            if s.hidden:
                s.hidden, s.hidden_at = False, 0.0
                s.updated = max(s.updated, time.time() - 60)
                b.registry.save()
            return web.json_response({"num": s.num, "name": s.name, "existing": True})
        if not folder.is_dir():
            return web.json_response({"error": "这段对话的文件夹已经不在了"}, status=400)
        try:
            found = {x.session_id: x for x in await asyncio.to_thread(self._cc_list, folder, None)}
        except Exception:  # noqa: BLE001
            found = {}
        if sid not in found:   # 只接这个文件夹里真有的对话
            return web.json_response({"error": "这个文件夹里没有这段对话"}, status=400)
        # 几分钟内还有人写这段记录：电脑上那个窗口多半还开着。两边各自记着自己的上下文，接着用会分叉，先提醒一句
        idle = time.time() - (found[sid].last_modified or 0) / 1000
        if idle < DESK_FRESH and not d.get("force"):
            return web.json_response({"warn": True, "ago": max(0, int(idle))})
        s = b.registry.add(Session(num=b.registry.reserve_num(), name=name,
                                   cwd=str(folder), claude_id=sid, from_desktop=True))
        return web.json_response({"num": s.num, "name": s.name})

    async def _api_schedules(self, request: web.Request) -> web.Response:
        b = self.bridge
        if request.method == "GET":
            return web.json_response({"tasks": b.schedules.rows(), "timezone": str(schedules.TZ), "error": b.schedules.load_error})
        if not self._same_origin(request): raise web.HTTPForbidden()
        data = await request.json()
        try:
            async with b.schedules.lock:
                action = data.get("action", "save")
                if action == "save":
                    task = b.jobs.create(data)
                    return web.json_response({"ok": True, "task": task})
                b.schedules.action(str(data.get("id") or ""), action)
            return web.json_response({"ok": True})
        except (ValueError, TypeError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
