"""远窗（远窗）：在手机或网页上给电脑里的 Claude Code / Codex 派活、收结果。

  anzhuo run      常驻运行（开机自启用这个）
  anzhuo check    核对配置、密码、Claude 登录，以及钉钉（如果开了）
  anzhuo passwd   设置网页登录的账号和密码
  anzhuo whoami   临时运行，打印给机器人发消息的人的钉钉 ID（用钉钉时才需要，首次配置用）
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import subprocess
import sys
from pathlib import Path

from .config import CONFIG_PATH, DATA_DIR, load_config, read_secret, read_web_hash, web_accounts


def _claude_logged_in() -> str:
    import claude_agent_sdk
    cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if sys.platform.startswith("win") else "claude")
    r = subprocess.run([str(cli), "auth", "status"], capture_output=True, text=True, timeout=30)
    try:
        j = json.loads(r.stdout)
        if j.get("loggedIn"):
            return f"已登录（{j.get('authMethod')}）"
        if sys.platform == "darwin":
            return "未登录：运行 scripts/claude_login.sh"
        if sys.platform.startswith("win"):
            return r"未登录：运行 .venv\Lib\site-packages\claude_agent_sdk\_bundled\claude.exe auth login（浏览器里点授权）"
        return f"未登录：运行 .venv/bin/{cli.name} auth login"
    except json.JSONDecodeError:
        return f"查不到：{r.stdout[:100]}{r.stderr[:100]}"


def check() -> int:
    ok = True
    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001
        print(f"配置：读不到 {CONFIG_PATH}：{e}")
        print("先复制 config.example.toml 到这个位置（见 README 的“安装”一节）。")
        return 1
    print(f"配置：{CONFIG_PATH}")
    print(f"文件夹短名：{', '.join(cfg.folders) or '（没配，网页里选“临时文件夹”或手输路径也能用）'}")
    for name, p in cfg.folders.items():
        if not Path(p).expanduser().is_dir():
            ok = False
            print(f"  短名 {name}：目录不存在 {p}")
    if cfg.web_base:
        if cfg.accounts:
            for name, account in web_accounts(cfg).items():
                if not account.get("hash"):
                    ok = False
                    print(f"网页账号 {name} 缺少密码：运行 scripts/set_account.py")
            print(f"网页：{cfg.web_base}，多账号配置")
        elif not cfg.web_user:
            ok = False
            print(f"网页：{cfg.web_base}（还没设登录账号密码：运行 anzhuo passwd）")
        elif not read_web_hash(cfg.web_user):
            ok = False
            print(f"网页：{cfg.web_base}（账号 {cfg.web_user} 的密码指纹没存：运行 anzhuo passwd）")
        else:
            print(f"网页：{cfg.web_base}，登录账号 {cfg.web_user}")
    else:
        print("网页：已关闭（web = false）")
    if cfg.dingtalk_enabled:
        from .dingtalk import DingTalk
        print(f"钉钉：已开启，允许的钉钉 ID {len(cfg.allowed_user_ids)} 个")
        try:
            secret = read_secret(cfg.client_id)
            print("  钥匙串：有钉钉密钥")
            asyncio.run(DingTalk(cfg.client_id, secret).check())
            print("  钉钉：能拿到调用凭证")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  钉钉：{e}")
    else:
        print("钉钉：没开（配置里没有 client_id，或写了 dingtalk = false），只用网页")
        if not cfg.web_base:
            ok = False
            print("  网页和钉钉都没开，远窗没有入口：至少开一个")
    print(f"Claude：{_claude_logged_in()}")
    print(f"数据目录：{DATA_DIR}")
    return 0 if ok else 1


def passwd() -> int:
    """设置网页登录的账号密码：密码只把 PBKDF2 指纹存进钥匙串（非 macOS 存 web_hash.json），账号写进配置的 web_user。"""
    from .web import hash_password
    if sys.stdin.isatty():
        user = input("网页登录账号：").strip()
        pw = getpass.getpass("密码：")
        if getpass.getpass("再输一遍：") != pw:
            print("两次不一样，没有保存。")
            return 1
    else:
        user, pw = (sys.stdin.readline().strip() for _ in range(2))
    if not user or not pw:
        print("账号和密码都不能空。")
        return 1
    from .config import write_web_hash
    write_web_hash(user, hash_password(pw))
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [ln for ln in (CONFIG_PATH.read_text().splitlines() if CONFIG_PATH.exists() else []) if not ln.startswith("web_user")]
    first_table = next((k for k, ln in enumerate(lines) if ln.startswith("[")), len(lines))
    lines.insert(first_table, f'web_user = "{user}"  # 网页登录账号；密码指纹在{"钥匙串" if sys.platform == "darwin" else "web_hash.json"}')
    CONFIG_PATH.write_text("\n".join(lines) + "\n")
    print("已设置：账号写进配置，密码指纹存好。远窗重启后生效。")
    return 0


def whoami() -> int:
    import dingtalk_stream
    cfg = load_config()
    if not cfg.dingtalk_enabled:
        print("配置里没有 client_id：没开钉钉，不需要这一步。")
        return 1
    secret = read_secret(cfg.client_id)

    class Handler(dingtalk_stream.CallbackHandler):
        async def process(self, callback):
            d = callback.data
            print(f"发消息的人：{d.get('senderNick')}　钉钉 ID（senderStaffId）：{d.get('senderStaffId')}　会话类型：{d.get('conversationType')}",
                  flush=True)
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    client = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential(cfg.client_id, secret))
    client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, Handler())
    print("已连上钉钉。现在在钉钉里给机器人发一句话，这里会打印你的钉钉 ID。按 Ctrl+C 结束。", flush=True)
    try:
        asyncio.run(client.start())
    except KeyboardInterrupt:
        pass
    return 0


def run() -> int:
    from .app import Bridge
    from .dingtalk import DingTalk, NoDingTalk
    cfg = load_config()
    if not cfg.dingtalk_enabled and not cfg.web_base:
        print("网页和钉钉都没开，远窗没有入口。把配置里的 web = false 去掉，或填上钉钉的 client_id。", file=sys.stderr)
        return 2
    if cfg.dingtalk_enabled and not cfg.allowed_user_ids:
        print("钉钉开着，但配置里还没有 allowed_user_ids，先运行 anzhuo whoami 拿到你的钉钉 ID。", file=sys.stderr)
        return 2
    if cfg.web_base and not any(a.get("hash") for a in web_accounts(cfg).values()):
        print("提示：网页还没设登录账号密码，现在谁也登录不了。运行 anzhuo passwd 设置。", file=sys.stderr)
    dt = DingTalk(cfg.client_id, read_secret(cfg.client_id)) if cfg.dingtalk_enabled else NoDingTalk()
    bridge = Bridge(cfg, dt)
    asyncio.run(bridge.run())
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="anzhuo", description="远窗（远窗）：在手机或网页上给电脑里的 Claude Code / Codex 派活")
    ap.add_argument("command", choices=["run", "check", "passwd", "whoami"])
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("dingtalk_stream").setLevel(logging.WARNING)
    sys.exit({"run": run, "check": check, "passwd": passwd, "whoami": whoami}[a.command]())
