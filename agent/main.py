# -*- coding: utf-8 -*-
"""claude-code-feishu entry point."""

import asyncio
import signal
import sys
import os
import logging
import warnings
import yaml

# Suppress RequestsDependencyWarning globally (requests 2.32 vs urllib3 2.6 compat)
warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")
# Also suppress in subprocesses via env var
os.environ.setdefault("PYTHONWARNINGS", "ignore:::requests")

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
from agent.orchestrator.pool import WorkerPool
from agent.orchestrator.engine import Orchestrator
from agent.jobs.briefing import BriefingPlugin
from agent.jobs.arxiv import ArxivPlugin


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


def normalize_bot_configs(cfg: dict) -> list[dict]:
    """Extract bot configs from feishu section.

    Supports two formats:
    - Legacy: feishu: {app_id, app_secret, ...}  → single bot named "main"
    - Multi:  feishu: {bots: [{name, app_id, ...}, ...]}  → multiple bots

    Shared fields (domain, admin_open_ids) are inherited from feishu top-level
    unless overridden per bot.
    """
    feishu = cfg.get("feishu", {})
    bots = feishu.get("bots")
    if bots:
        # Multi-bot mode: inherit shared fields
        shared = {
            k: v for k, v in feishu.items()
            if k not in ("bots", "app_id", "app_secret")
        }
        result = []
        for bot_cfg in bots:
            merged = {**shared, **bot_cfg}
            if not merged.get("name"):
                print("ERROR: each bot in feishu.bots must have a 'name'")
                sys.exit(1)
            result.append(merged)
        return result
    # Legacy single-bot mode
    if feishu.get("app_id"):
        feishu.setdefault("name", "main")
        return [feishu]
    return []


def validate_config(cfg: dict):
    bot_configs = normalize_bot_configs(cfg)
    if not bot_configs:
        print("ERROR: feishu.app_id or feishu.bots is required in config.yaml")
        sys.exit(1)
    for bc in bot_configs:
        if not bc.get("app_id") or not bc.get("app_secret"):
            print(f"ERROR: bot '{bc.get('name')}' missing app_id or app_secret")
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

    # Clean up stale temp files from previous runs (media.py crash remnants)
    import glob as _glob
    for stale in _glob.glob(os.path.expanduser("~/tmp/feishu_*")):
        try:
            os.unlink(stale)
        except OSError:
            pass

    # Initialize components
    llm_cfg = cfg.get("llm", {})

    claude = ClaudeCli(llm_cfg.get("claude-cli", {}))
    gemini_cli = GeminiCli(llm_cfg.get("gemini-cli", {}))
    gemini_api = GeminiAPI(llm_cfg.get("gemini-api", {}))

    router = LLMRouter(claude, gemini_cli, gemini_api, sessions_path="data/sessions.json")
    await router.load_sessions()

    # Resolve bot configs once
    bot_configs = normalize_bot_configs(cfg)
    primary_cfg = bot_configs[0]

    # Primary bot dispatcher (used by heartbeat for DM sending)
    primary_dispatcher = Dispatcher(primary_cfg)
    await primary_dispatcher.start()

    # Notification dispatcher (separate bot for alerts/tasks/briefing)
    notify_cfg = cfg.get("notify", {})
    if notify_cfg.get("app_id"):
        notifier = Dispatcher(notify_cfg)
        await notifier.start()
    else:
        notifier = primary_dispatcher  # fallback: use main bot
        log.warning("No notify config, notifications will go through main bot")

    workspace_dir = os.path.expanduser(
        llm_cfg.get("claude-cli", {}).get("workspace_dir", ".")
    )

    scheduler = CronScheduler(cfg.get("scheduler", {}), router, notifier)
    await scheduler.start()

    hb_cfg = cfg.get("heartbeat", {})
    hb = HeartbeatMonitor(hb_cfg, router, primary_dispatcher, workspace_dir,
                          notify_open_id=hb_cfg.get("notify_open_id", ""))
    await hb.start()

    default_llm_cfg = llm_cfg.get("default", {})
    default_llm = LLMConfig(
        provider=default_llm_cfg.get("provider", "claude-cli"),
        model=default_llm_cfg.get("model", "opus"),
    )

    file_store = FileStore(base_dir="data/files")

    # Shared FeishuAPI uses primary bot's credentials (user store, admin seeding)
    from agent.platforms.feishu.api import FeishuAPI
    feishu_api = FeishuAPI(
        primary_cfg.get("app_id", ""),
        primary_cfg.get("app_secret", ""),
        primary_cfg.get("domain", "https://open.feishu.cn"),
    )
    user_store = UserStore(path="data/users.json", feishu_api=feishu_api)
    await user_store.load()

    # Seed admin roles from all bot configs (idempotent)
    all_admin_ids = set()
    for bc in bot_configs:
        all_admin_ids.update(bc.get("admin_open_ids", []))
    for admin_id in all_admin_ids:
        u = await user_store.get_or_create(admin_id)
        if not u.is_admin():
            await user_store.set_role(admin_id, "admin")

    # Orchestrator: Opus planning + Sonnet worker pool
    orch_cfg = cfg.get("orchestrator", {})
    worker_pool = WorkerPool(claude, max_concurrent=orch_cfg.get("max_concurrent", 3))
    orchestrator = Orchestrator(claude, worker_pool)

    # Message state machine — shared across all bots
    message_store = MessageStore("data")
    message_store.cleanup()

    # Create bot instances — one per feishu app
    bots: list[FeishuBot] = []
    for bot_cfg in bot_configs:
        # Per-bot default model + env override
        bot_env = {}
        if bot_cfg.get("home_dir"):
            bot_env["HOME"] = os.path.expanduser(bot_cfg["home_dir"])
        bot_llm = default_llm
        if bot_cfg.get("default_model") or bot_env:
            bot_llm = LLMConfig(
                provider=bot_cfg.get("default_provider", default_llm.provider),
                model=bot_cfg.get("default_model", default_llm.model),
                env=bot_env,
            )
        # Per-bot dispatcher: reuse primary for first bot, create new for others
        if bot_cfg["name"] == primary_cfg["name"]:
            bot_dispatcher = primary_dispatcher
        else:
            bot_dispatcher = Dispatcher(bot_cfg)
            await bot_dispatcher.start()

        bot = FeishuBot(
            bot_cfg,
            router, scheduler, hb, bot_dispatcher, bot_llm,
            file_store=file_store,
            user_store=user_store,
            orchestrator=orchestrator,
            message_store=message_store,
        )

        # Plugins: register on primary bot only
        if bot_cfg["name"] == primary_cfg["name"]:
            briefing_cfg = cfg.get("briefing", {})
            briefing = BriefingPlugin(
                notify_config=notify_cfg,
                default_domain=briefing_cfg.get("default_domain"),
            )
            register_plugin(briefing.descriptor(), bot=bot, scheduler=scheduler)

            arxiv_plugin = ArxivPlugin()
            register_plugin(arxiv_plugin.descriptor(), bot=bot, scheduler=scheduler)

            # Error scan handler
            from agent.jobs.error_scan import scan_errors
            _error_cfg = cfg.get("error_scan", {})
            if _error_cfg.get("enabled", False):
                async def _error_scan_handler(**kwargs):
                    await scan_errors(router, notifier, _error_cfg)
                scheduler.register_handler("error_scan", _error_scan_handler)

        await bot.start()
        bots.append(bot)
        log.info("Bot '%s' started (app_id=%s)", bot.name, bot_cfg["app_id"][:8])

    log.info("All services started (%d bot(s)). Waiting for events...", len(bots))

    # Notify: service started (timeout-protected)
    await asyncio.sleep(2)
    bot_names = ", ".join(b.name for b in bots)
    log.info("Sending startup notification...")
    try:
        ok = await asyncio.wait_for(
            notifier.send_to_delivery_target(
                f"✅ **claude-code-feishu 已启动** (pid={os.getpid()}, bots: {bot_names})"
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
    stopped_dispatchers = set()
    for bot in bots:
        await bot.stop()
        d = bot.dispatcher
        if id(d) not in stopped_dispatchers:
            await d.stop()
            stopped_dispatchers.add(id(d))
    await hb.stop()
    await scheduler.stop()
    if id(notifier) not in stopped_dispatchers:
        await notifier.stop()
        stopped_dispatchers.add(id(notifier))
    await router.save_sessions()
    try:
        os.remove(pid_path)
    except OSError:
        pass
    log.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
