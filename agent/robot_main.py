# -*- coding: utf-8 -*-
"""Standalone bot process — runs a single standalone bot independently from main hub.

Usage:
    python -m agent.robot_main                  # first standalone bot (backwards compat)
    python -m agent.robot_main --bot robot      # specific bot by name
    python -m agent.robot_main --bot external   # another standalone bot
"""

import argparse
import asyncio
import signal
import sys
import os
import logging
import warnings

warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")
os.environ.setdefault("PYTHONWARNINGS", "ignore:::requests")

import yaml
from agent.infra.models import LLMConfig
from agent.llm.claude import ClaudeCli
from agent.llm.gemini_cli import GeminiCli
from agent.llm.gemini_api import GeminiAPI
from agent.llm.router import LLMRouter
from agent.platforms.feishu.dispatcher import Dispatcher
from agent.jobs.scheduler import CronScheduler
from agent.jobs.heartbeat import HeartbeatMonitor
from agent.platforms.feishu.bot import FeishuBot
from agent.infra.file_store import FileStore
from agent.infra.user_store import UserStore
from agent.infra.message_store import MessageStore


def setup_logging(config: dict, log_file: str):
    level = getattr(logging, config.get("level", "INFO").upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(log_file, encoding="utf-8")]
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)-20s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def find_bot_config(cfg: dict, bot_name: str | None) -> dict | None:
    """Find a standalone bot config by name, or the first standalone bot."""
    feishu = cfg.get("feishu", {})
    bots = feishu.get("bots", [])
    shared = {k: v for k, v in feishu.items() if k not in ("bots", "app_id", "app_secret")}

    for b in bots:
        if not b.get("standalone"):
            continue
        merged = {**shared, **b}
        if bot_name is None or merged.get("name") == bot_name:
            return merged
    return None


async def main():
    parser = argparse.ArgumentParser(description="Standalone bot process")
    parser.add_argument("--bot", type=str, default=None,
                        help="Bot name to run (default: first standalone bot)")
    parser.add_argument("--config", type=str, default="config.yaml",
                        help="Config file path")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    bot_cfg = find_bot_config(cfg, args.bot)
    if not bot_cfg:
        target = f"'{args.bot}'" if args.bot else "any standalone"
        print(f"ERROR: No {target} bot found in config.yaml")
        sys.exit(1)

    bot_name = bot_cfg.get("name", "standalone")
    os.makedirs("data", exist_ok=True)
    setup_logging(cfg.get("logging", {}), f"data/{bot_name}-bot.log")
    log = logging.getLogger(f"{bot_name}.main")
    log.info("%s-bot starting...", bot_name)
    log.info("Using bot '%s' (app_id=%s)", bot_name, bot_cfg.get("app_id", "")[:8])

    # LLM stack
    llm_cfg = cfg.get("llm", {})
    claude = ClaudeCli(llm_cfg.get("claude-cli", {}))
    gemini_cli = GeminiCli(llm_cfg.get("gemini-cli", {}))
    gemini_api = GeminiAPI(llm_cfg.get("gemini-api", {}))
    router = LLMRouter(claude, gemini_cli, gemini_api,
                       sessions_path=f"data/{bot_name}-sessions.json")
    await router.load_sessions()

    # Minimal scheduler (no jobs, just satisfies FeishuBot dependency)
    scheduler = CronScheduler({"enabled": False}, router, None)
    await scheduler.start()

    # Minimal heartbeat stub
    hb = HeartbeatMonitor({}, router, None, ".", notify_open_id="")

    # Per-bot default model
    bot_model = bot_cfg.get("default_model",
                            llm_cfg.get("default", {}).get("model", "sonnet"))
    bot_provider = bot_cfg.get("default_provider",
                               llm_cfg.get("default", {}).get("provider", "claude-cli"))

    # Per-bot workspace_dir
    bot_workspace = None
    if bot_cfg.get("workspace_dir"):
        bot_workspace = os.path.expanduser(bot_cfg["workspace_dir"])

    # Inject per-bot feishu credentials so Skills use the correct org's API
    bot_env = {
        "FEISHU_APP_ID": bot_cfg.get("app_id", ""),
        "FEISHU_APP_SECRET": bot_cfg.get("app_secret", ""),
    }
    if bot_cfg.get("domain"):
        bot_env["FEISHU_DOMAIN"] = bot_cfg["domain"]

    default_llm = LLMConfig(
        provider=bot_provider,
        model=bot_model,
        workspace_dir=bot_workspace,
        env=bot_env,
        setting_sources=bot_cfg.get("setting_sources"),
    )

    dispatcher = Dispatcher(bot_cfg)
    await dispatcher.start()

    file_store = FileStore(base_dir=f"data/{bot_name}-files")

    from agent.platforms.feishu.api import FeishuAPI
    feishu_api = FeishuAPI(
        bot_cfg.get("app_id", ""),
        bot_cfg.get("app_secret", ""),
        bot_cfg.get("domain", "https://open.feishu.cn"),
    )
    user_store = UserStore(path=f"data/{bot_name}-users.json", feishu_api=feishu_api)
    await user_store.load()

    # Seed admins
    for admin_id in bot_cfg.get("admin_open_ids", []):
        u = await user_store.get_or_create(admin_id)
        if not u.is_admin():
            await user_store.set_role(admin_id, "admin")

    message_store = MessageStore(f"data/{bot_name}-messages")

    bot = FeishuBot(
        bot_cfg, router, scheduler, hb, dispatcher, default_llm,
        file_store=file_store,
        user_store=user_store,
        message_store=message_store,
    )
    await bot.start()
    log.info("%s bot started (app_id=%s)", bot_name, bot_cfg.get("app_id", "")[:8])

    # PID file
    pid_path = f"data/{bot_name}-bot.pid"
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))

    # Wait for shutdown
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await stop.wait()

    log.info("Shutting down %s bot...", bot_name)
    await bot.stop()
    await dispatcher.stop()
    await scheduler.stop()
    await router.save_sessions()
    try:
        os.remove(pid_path)
    except OSError:
        pass
    log.info("%s bot stopped.", bot_name)
    # Force exit to skip asyncio executor shutdown timeout (300s).
    # SDK reconnect thread blocks forever; all meaningful cleanup is done above.
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
