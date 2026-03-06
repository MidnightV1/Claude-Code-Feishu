# -*- coding: utf-8 -*-
"""claude-code-feishu entry point."""

import asyncio
import signal
import sys
import os
import logging
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
from agent.orchestrator.pool import WorkerPool
from agent.orchestrator.engine import Orchestrator
from agent.jobs.briefing import BriefingPlugin


def setup_logging(config: dict):
    level = getattr(logging, config.get("level", "INFO").upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stdout)]

    log_file = config.get("file")
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)-20s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_config(cfg: dict):
    feishu = cfg.get("feishu", {})
    if not feishu.get("app_id") or not feishu.get("app_secret"):
        print("ERROR: feishu.app_id and feishu.app_secret are required in config.yaml")
        sys.exit(1)

    gemini = cfg.get("llm", {}).get("gemini-api", {})
    if not gemini.get("api_key"):
        print("WARNING: llm.gemini-api.api_key not set, Gemini API calls will fail")


def register_plugin(desc: dict, *, bot, scheduler):
    """Wire a plugin descriptor into bot commands and scheduler handlers."""
    for cmd in desc.get("commands", []):
        bot.register_command(cmd["prefix"], cmd["handler"], cmd.get("help"))
    for h in desc.get("handlers", []):
        scheduler.register_handler(h["name"], h["fn"])


async def main():
    # Load config
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)

    # Logging
    setup_logging(cfg.get("logging", {}))
    log = logging.getLogger("hub.main")
    log.info("claude-code-feishu starting...")

    # Validate
    validate_config(cfg)

    # Ensure data dir & write PID file
    os.makedirs("data", exist_ok=True)
    pid_path = os.path.join("data", "hub.pid")
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))

    # Initialize components
    llm_cfg = cfg.get("llm", {})

    claude = ClaudeCli(llm_cfg.get("claude-cli", {}))
    gemini_cli = GeminiCli(llm_cfg.get("gemini-cli", {}))
    gemini_api = GeminiAPI(llm_cfg.get("gemini-api", {}))

    router = LLMRouter(claude, gemini_cli, gemini_api, sessions_path="data/sessions.json")
    await router.load_sessions()

    dispatcher = Dispatcher(cfg.get("feishu", {}))
    await dispatcher.start()

    # Notification dispatcher (separate bot for alerts/tasks/briefing)
    notify_cfg = cfg.get("notify", {})
    if notify_cfg.get("app_id"):
        notifier = Dispatcher(notify_cfg)
        await notifier.start()
    else:
        notifier = dispatcher  # fallback: use main bot
        log.warning("No notify config, notifications will go through main bot")

    workspace_dir = os.path.expanduser(
        llm_cfg.get("claude-cli", {}).get("workspace_dir", ".")
    )

    scheduler = CronScheduler(cfg.get("scheduler", {}), router, notifier)
    await scheduler.start()

    hb_cfg = cfg.get("heartbeat", {})
    hb = HeartbeatMonitor(hb_cfg, router, dispatcher, workspace_dir,
                          notify_open_id=hb_cfg.get("notify_open_id", ""))
    await hb.start()

    default_llm_cfg = llm_cfg.get("default", {})
    default_llm = LLMConfig(
        provider=default_llm_cfg.get("provider", "claude-cli"),
        model=default_llm_cfg.get("model", "opus"),
    )

    file_store = FileStore(base_dir="data/files")

    feishu_cfg = cfg.get("feishu", {})
    from agent.platforms.feishu.api import FeishuAPI
    feishu_api = FeishuAPI(
        feishu_cfg.get("app_id", ""),
        feishu_cfg.get("app_secret", ""),
        feishu_cfg.get("domain", "https://open.feishu.cn"),
    )
    user_store = UserStore(path="data/users.json", feishu_api=feishu_api)
    await user_store.load()

    # Seed admin roles from config (idempotent)
    for admin_id in feishu_cfg.get("admin_open_ids", []):
        u = await user_store.get_or_create(admin_id)
        if not u.is_admin():
            await user_store.set_role(admin_id, "admin")

    # Orchestrator: Opus planning + Sonnet worker pool
    orch_cfg = cfg.get("orchestrator", {})
    worker_pool = WorkerPool(claude, max_concurrent=orch_cfg.get("max_concurrent", 3))
    orchestrator = Orchestrator(claude, worker_pool)

    bot = FeishuBot(
        feishu_cfg,
        router, scheduler, hb, dispatcher, default_llm,
        file_store=file_store,
        user_store=user_store,
        orchestrator=orchestrator,
    )

    # Plugins
    briefing_cfg = cfg.get("briefing", {})
    briefing = BriefingPlugin(
        notify_config=notify_cfg,
        default_domain=briefing_cfg.get("default_domain"),
    )
    register_plugin(briefing.descriptor(), bot=bot, scheduler=scheduler)

    await bot.start()

    log.info("All services started. Waiting for events...")

    # Notify: service started (timeout-protected)
    await asyncio.sleep(2)
    log.info("Sending startup notification...")
    try:
        ok = await asyncio.wait_for(
            notifier.send_to_delivery_target(
                f"✅ **claude-code-feishu 已启动** (pid={os.getpid()})"
            ),
            timeout=10,
        )
        if ok:
            log.info("Startup notification sent")
        else:
            log.warning("Startup notification send returned False")
    except asyncio.TimeoutError:
        log.warning("Startup notification timed out (10s)")
    except Exception as e:
        log.warning("Failed to send startup notification: %s", e)

    # Wait for shutdown signal
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # SIGUSR1 → hot-reload scheduler jobs
    try:
        def _on_sigusr1():
            log.info("SIGUSR1 received, reloading scheduler...")
            asyncio.create_task(scheduler.reload())
        loop.add_signal_handler(signal.SIGUSR1, _on_sigusr1)
    except (NotImplementedError, AttributeError):
        pass

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass

    # Graceful shutdown
    log.info("Shutting down...")
    try:
        await notifier.send_to_delivery_target("\u26a0\ufe0f **claude-code-feishu \u6b63\u5728\u5173\u95ed...**")
    except Exception:
        pass
    await bot.stop()
    await hb.stop()
    await scheduler.stop()
    await dispatcher.stop()
    if notifier is not dispatcher:
        await notifier.stop()
    await router.save_sessions()
    try:
        os.remove(pid_path)
    except OSError:
        pass
    log.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
