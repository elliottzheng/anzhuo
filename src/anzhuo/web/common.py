"""网页各模块共用的常量和小工具：登录 Cookie、路径前缀、密码指纹、型号显示名、过程页模板。"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import re
import secrets


from .. import engines
from ..engines import MODELS
from ..report import model_name

log = logging.getLogger(__name__)

COOKIE = "anzhuo_sid"
SESSION_DAYS = 30
LOCK_IP = (5, 15 * 60)  # 同一来源：错几次、锁多久
LOCK_ALL = (30, 60 * 60)  # 所有来源合计：一小时内错几次就全体锁一小时


def hash_password(password: str, salt: str | None = None, iters: int = 300_000) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iters).hex()
    return f"pbkdf2${iters}${salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$")
        return hmac.compare_digest(hash_password(password, salt, int(iters)).split("$")[3], digest)
    except ValueError:
        return False

DESK_FRESH = 300  # 电脑端对话的记录在这么多秒内被写过，就当电脑上可能还开着
PREFIX = "/_x"  # 远窗自己的页面都在这下面，其余路径留给扩展的兜底（没有就 404）
PUBLIC = (f"{PREFIX}/login", f"{PREFIX}/icon.png", f"{PREFIX}/icon-512.png", f"{PREFIX}/manifest.json", f"{PREFIX}/sw.js",
          f"{PREFIX}/health")
# 受限账号（role = user）能用的页面和接口；别的（历史记录、扩展、定时、浏览文件夹、接电脑端对话、管理页）一律不给
USER_ROUTES = {f"{PREFIX}/", PREFIX, "/", f"{PREFIX}/api/push/key", f"{PREFIX}/api/push", f"{PREFIX}/api/logout",
               f"{PREFIX}/api/devices", f"{PREFIX}/api/devices/logout", f"{PREFIX}/api/sessions", f"{PREFIX}/api/events",
               f"{PREFIX}/api/uimg/{{num}}/{{uuid}}/{{k}}", f"{PREFIX}/api/thread/{{num}}", f"{PREFIX}/api/manage/{{num}}",
               f"{PREFIX}/api/speech/{{num}}", f"{PREFIX}/api/speech/release", f"{PREFIX}/api/audio/{{key}}",
               f"{PREFIX}/audio/{{key}}.mp3", f"{PREFIX}/api/search", f"{PREFIX}/api/send", f"{PREFIX}/api/settings/{{num}}",
               f"{PREFIX}/api/compact/{{num}}", f"{PREFIX}/api/relay/{{num}}", f"{PREFIX}/api/confirm/{{num}}", f"{PREFIX}/api/stop/{{num}}",
               f"{PREFIX}/api/rewind/{{num}}", f"{PREFIX}/api/fork/{{num}}", f"{PREFIX}/api/unqueue/{{num}}",
               f"{PREFIX}/api/presence", f"{PREFIX}/t/{{tid}}", f"{PREFIX}/t/{{tid}}/img/{{i}}", f"{PREFIX}/t/{{tid}}/file/{{i}}",
               "/dl/{tid}/{i}", "/dl/{tid}/{i}/pages", "/dl/{tid}/{i}/p/{name}"}

def display_model(model_id: str) -> str:
    """统一网页名称，保留未知型号；兼容早先保存的 GPT（实际型号）。"""
    wrapped = re.fullmatch(r"GPT[（(](.*)[）)]", model_id or "")
    mid = wrapped[1] if wrapped else model_id or ""
    if mid in ("Codex", "GPT", "gpt"):
        return "GPT"  # 没有型号证据时不编版本；网页上两家统一叫 Claude / GPT（Codex 是干活的工具，不是模型）
    if mid.startswith("claude-"):
        label = model_name(mid)
        return "Claude " + label if label != mid else mid
    if mid.lower().startswith("gpt-"):
        return "GPT-" + mid[4:].replace("-", " ").title()
    if mid.lower().startswith("kimi-"):
        return "Kimi " + mid[5:].replace("-", " ").title()
    return mid


def model_options() -> list[list[str]]:
    from ..codex import configured_model
    out = [[key, display_model(MODELS[key])] for key in ("opus", "sonnet", "haiku", "fable")]
    for e in engines.known():
        if e.pick_model != "own" or not (e is engines.CODEX or e.available()):
            continue
        if e is engines.CODEX:   # Codex：型号那一栏写引擎键，用它自己配置的默认型号
            out.append([e.key, (display_model(configured_model()) or e.label)])
            continue
        out.append([e.key, e.label])   # ACP 助手：选它 = 用它自己配置的默认型号
        out += [[m, display_model(m)] for m in (e.models()[0] or [])]   # 探到的具体型号也能选
    return out

def _md(text: str) -> str:
    """回复里的 Markdown 粗略转成 HTML：转义后只处理加粗、行内代码和链接，其余保留换行。"""
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{title}</title>
<meta name="theme-color" content="#d5dee5" media="(prefers-color-scheme: light)"><meta name="theme-color" content="#2e3a45" media="(prefers-color-scheme: dark)">
<style>
:root{{--sky:linear-gradient(158deg,#d4e0ea 0%,#e1eaf1 38%,#ecf2f6 72%,#f3f6f9 100%);--ink:#25313e;--mute:#566474;--faint:#8592a0;--line:rgba(24,44,64,.065);
  --acc:#2c4a74;--acc-hi:#4b78b4;--acc-glow:rgba(66,108,170,.55);--accsoft:rgba(44,74,116,.08);--red:#b0443a;
  --glass:rgba(255,255,255,.60);--glass-hi:rgba(255,255,255,.72);--edge:rgba(255,255,255,.92);--hi:rgba(255,255,255,.95);--glow:rgba(58,98,156,.40);
  --shadow:10px 18px 48px -12px rgba(92,124,168,.22),3px 6px 14px -6px rgba(92,124,168,.10);--blur:saturate(1.15) blur(24px);
  --ff:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif;--fd:-apple-system,BlinkMacSystemFont,"SF Pro Display","PingFang SC",sans-serif}}
@media (prefers-color-scheme:dark){{:root{{--sky:linear-gradient(176deg,#151b22 0%,#11171d 50%,#0d1217 100%);--ink:#e6ebf0;--mute:#a3aeb9;--faint:#76828d;--line:rgba(255,255,255,.075);
  --acc:#9dbbe4;--acc-hi:#8db4ee;--acc-glow:rgba(122,166,234,.55);--accsoft:rgba(141,180,238,.11);--red:#ee8b7c;
  --glass:rgba(255,255,255,.045);--glass-hi:rgba(255,255,255,.08);--edge:rgba(255,255,255,.085);--hi:rgba(255,255,255,.10);--glow:rgba(96,140,210,.22);
  --shadow:10px 18px 48px -12px rgba(0,0,0,.5),3px 6px 14px -6px rgba(0,0,0,.3)}}}}
html{{background:var(--sky) fixed;min-height:100%}}
body{{margin:0;color:var(--ink);font:400 16px/1.75 var(--ff);letter-spacing:.01em;-webkit-font-smoothing:antialiased}}
main{{max-width:760px;margin:0 auto;padding:20px 16px 48px}}
h1{{font:300 30px/1.3 var(--fd);letter-spacing:.06em;margin:18px 0 6px}} h2{{font-size:11.5px;font-weight:500;letter-spacing:.14em;margin:28px 0 8px;color:var(--faint)}}
b,strong,th{{font-weight:500}} .meta{{color:var(--mute);font-size:14px}} a{{color:var(--acc)}}
.card{{background:var(--glass);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);border-radius:16px;padding:14px 16px;margin:10px 0;
  box-shadow:inset 0 0 0 1px var(--edge)}}
.say{{white-space:pre-wrap}} code{{font:400 13px ui-monospace,"SF Mono",Menlo,monospace;background:var(--glass);box-shadow:inset 0 0 0 1px var(--edge);padding:1px 6px;border-radius:6px}}
pre{{white-space:pre-wrap;word-break:break-all;font:400 13px/1.55 ui-monospace,"SF Mono",Menlo,monospace;margin:6px 0 0}}
.step{{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid var(--line)}} .step:last-child{{border:0}}
.t{{color:var(--faint);font:400 12px ui-monospace,"SF Mono",Menlo,monospace;min-width:58px;padding-top:4px;font-variant-numeric:tabular-nums}}
details summary{{cursor:pointer;color:var(--mute)}} img{{max-width:100%;border-radius:12px;box-shadow:inset 0 0 0 1px var(--edge);margin:6px 0}}
.btn{{display:inline-block;color:var(--acc);text-decoration:none;padding:9px 18px;border-radius:999px;margin:4px 6px 4px 0;font-weight:500;background:var(--accsoft);box-shadow:inset 0 0 0 1px rgba(44,74,116,.14)}}
ul{{padding-left:20px;margin:4px 0}} li::marker{{color:var(--faint)}}
.card > a{{text-decoration:none;font-size:17px;color:var(--ink)}} details li{{margin:4px 0}} details li a{{color:var(--ink);text-decoration:none;font-size:15px}}
.bk{{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:4px;padding:calc(10px + env(safe-area-inset-top)) 14px 10px;
  background:rgba(236,241,244,.5);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);border-bottom:1px solid var(--line);color:var(--acc);font-size:16px;text-decoration:none}}
@media (prefers-color-scheme:dark){{.bk{{background:rgba(14,20,27,.6)}}}}
.mk{{display:block;overflow:hidden}} .mk>svg{{display:block;width:100%;height:100%}} .mk>svg.d{{display:none}}
@media (prefers-color-scheme:dark){{.mk>svg.l{{display:none}} .mk>svg.d{{display:block}}}}
::selection{{background:rgba(75,120,180,.22)}}
/* 第五版：扁平磨砂（对照参考图） */
:root{{--sky:linear-gradient(200deg,#d5dee5 0%,#e3e9ed 34%,#edf1f3 66%,#f5f7f7 100%);--ink:#34434f;--mute:#67757f;--faint:#909ca6;
  --glass:rgba(255,255,255,.52);--glass-hi:rgba(255,255,255,.66);--knob-in:rgba(60,84,104,.07);--shadow:none}}
@media (prefers-color-scheme:dark){{:root{{--sky:linear-gradient(200deg,#2e3a45 0%,#222c35 40%,#182027 100%);--ink:#e2e8ed;--mute:#9eabb6;--faint:#6f7c87;
  --glass:rgba(255,255,255,.055);--glass-hi:rgba(255,255,255,.085);--knob-in:rgba(255,255,255,.06)}}}}
.card{{box-shadow:none}} .bk{{border-bottom:0;background:transparent;-webkit-backdrop-filter:none;backdrop-filter:none}}
.btn{{background:rgba(44,74,116,.12);box-shadow:none}}
</style></head><body>
<a class="bk" href="/_x/" onclick="if(history.length>1){{history.back();return false}}">‹ 返回</a>
<main>{body}</main></body></html>"""


_RELAY_MARK = "用户这次说：\n\n"


def _said_has(recorded: str, sent: str) -> bool:
    """记录里的这句是不是你发的那句。接力对话的第一句，记录里是“交接说明 + 用户这次说：+ 原话”。"""
    head = sent.strip()[:40]
    if recorded.startswith(head):
        return True
    return recorded.startswith("（这是接力的新对话") and _RELAY_MARK in recorded and recorded.split(_RELAY_MARK, 1)[1].startswith(head)
