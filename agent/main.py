# -*- coding: utf-8 -*-
"""claude-code-feishu entry point."""

import asyncio
import signal
import sys
import os
import logging
import threading
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
from agent.jobs.explorer import ExplorerPlugin
from agent.jobs.planner import PlannerPlugin
from agent.jobs.worker import WorkerManager
from agent.jobs.loop_executor import LoopExecutor


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
    from agent.infra.error_tracker import ErrorTrackerHandler
    logging.getLogger().addHandler(ErrorTrackerHandler())
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
    bot_configs = [b for b in normalize_bot_configs(cfg) if not b.get("standalone")]
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
    Dispatcher.register_notifier(notifier)

    workspace_dir = os.path.expanduser(
        llm_cfg.get("claude-cli", {}).get("workspace_dir", ".")
    )

    # Per-bot dispatchers for cron notification routing
    all_bots = normalize_bot_configs(cfg)
    bot_dispatchers: dict[str, tuple[Dispatcher, str]] = {}
    for bcfg in all_bots:
        if bcfg.get("standalone") and bcfg.get("notify_open_id"):
            name = bcfg.get("name", "")
            d = Dispatcher(bcfg)
            await d.start()
            bot_dispatchers[name] = (d, bcfg["notify_open_id"])
            log.info("Bot dispatcher '%s' ready (notify_open_id=%s)",
                     name, bcfg["notify_open_id"][:12])

    scheduler = CronScheduler(cfg.get("scheduler", {}), router, notifier,
                              bot_dispatchers=bot_dispatchers)
    # NOTE: scheduler.start() is deferred until after all handlers are registered
    # to prevent _run_missed_jobs_bg() from hitting "Unknown handler" race condition

    hb_cfg = cfg.get("heartbeat", {})
    hb = HeartbeatMonitor(hb_cfg, router, primary_dispatcher, workspace_dir,
                          notify_open_id=hb_cfg.get("notify_open_id", ""))
    await hb.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # Sentinel: autonomous entropy control (scanners → orchestrator → heartbeat)
    sentinel_cfg = cfg.get("sentinel", {})
    if sentinel_cfg.get("enabled", True):
        from agent.jobs.sentinel.store import SentinelStore
        from agent.jobs.sentinel.orchestrator import SentinelOrchestrator
        from agent.jobs.sentinel.scanners.code_scanner import CodeScanner
        from agent.jobs.sentinel.scanners.doc_auditor import DocAuditor
        from agent.jobs.sentinel.scanners.health_pulse import HealthPulse

        sentinel_store = SentinelStore()
        scanners = [
            CodeScanner(),
            DocAuditor(),
            HealthPulse(),
        ]
        sentinel = SentinelOrchestrator(
            scanners=scanners,
            store=sentinel_store,
            dispatcher=notifier,
            user_dispatcher=primary_dispatcher,
            workspace_dir=workspace_dir,
            config={**sentinel_cfg, "maqs": cfg.get("maqs", {})},
            notify_open_id=sentinel_cfg.get("notify_open_id", "") or hb_cfg.get("notify_open_id", ""),
        )
        hb.set_sentinel(sentinel)
        log.info("Sentinel initialized with %d scanners: %s", len(scanners), ", ".join(s.name for s in scanners))

        async def _sentinel_handler(**kwargs):
            summary = await sentinel.run_cycle(trigger="cron")
            log.info("Sentinel cron cycle: %s", summary)
            return summary
        scheduler.register_handler("sentinel", _sentinel_handler)

    default_llm_cfg = llm_cfg.get("default", {})
    default_llm = LLMConfig(
        provider=default_llm_cfg.get("provider", "claude-cli"),
        model=default_llm_cfg.get("model", "opus"),
        effort=default_llm_cfg.get("effort"),
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
    loop_executor: "LoopExecutor | None" = None
    bots: list[FeishuBot] = []
    for bot_cfg in bot_configs:
        # Per-bot default model + env override
        bot_env = {
            "FEISHU_APP_ID": bot_cfg.get("app_id", ""),
            "FEISHU_APP_SECRET": bot_cfg.get("app_secret", ""),
        }
        if bot_cfg.get("domain"):
            bot_env["FEISHU_DOMAIN"] = bot_cfg["domain"]
        if bot_cfg.get("home_dir"):
            bot_env["HOME"] = os.path.expanduser(bot_cfg["home_dir"])
        # Per-bot workspace_dir: expand and resolve
        bot_workspace = None
        if bot_cfg.get("workspace_dir"):
            bot_workspace = os.path.expanduser(bot_cfg["workspace_dir"])

        bot_llm = default_llm
        if bot_cfg.get("default_model") or bot_env or bot_workspace:
            bot_llm = LLMConfig(
                provider=bot_cfg.get("default_provider", default_llm.provider),
                model=bot_cfg.get("default_model", default_llm.model),
                effort=bot_cfg.get("default_effort", default_llm.effort),
                env=bot_env,
                workspace_dir=bot_workspace,
                setting_sources=bot_cfg.get("setting_sources"),
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

            # Autonomous explorer
            explorer_cfg = cfg.get("explorer", {})
            # Fallback to heartbeat notify_open_id — same user receives both
            explorer_open_id = (explorer_cfg.get("notify_open_id", "")
                                or hb_cfg.get("notify_open_id", ""))
            explorer = ExplorerPlugin(
                router=router,
                dispatcher=notifier,
                budget=explorer_cfg.get("budget", 50),
                open_id=explorer_open_id,
                card_dispatcher=primary_dispatcher,
            )
            register_plugin(explorer.descriptor(), bot=bot, scheduler=scheduler)
            # Store reference for queue access from bot commands
            bot.explorer = explorer

            # Daily planner (strategic task generation)
            planner = PlannerPlugin(router=router, queue=explorer.queue)
            register_plugin(planner.descriptor(), bot=bot, scheduler=scheduler)

            # Error scan handler
            from agent.jobs.error_scan import scan_errors
            _error_cfg = cfg.get("error_scan", {})
            if _error_cfg.get("enabled", False):
                async def _error_scan_handler(**kwargs):
                    await scan_errors(router, notifier, _error_cfg)
                    return True
                scheduler.register_handler("error_scan", _error_scan_handler)

            # MAQS quality pipeline handler
            from agent.jobs.maqs import run_maqs_pipeline
            _maqs_cfg = cfg.get("maqs", {})
            if _maqs_cfg.get("enabled", False):
                async def _maqs_handler(**kwargs):
                    await run_maqs_pipeline(router, notifier, _maqs_cfg)
                    return True
                scheduler.register_handler("maqs_quality", _maqs_handler)

                # MADS composite pipeline handler (shares MAQS bitable config)
                from agent.jobs.mads.pipeline import run_mads_pipeline
                async def _mads_handler(**kwargs):
                    await run_mads_pipeline(router, notifier, _maqs_cfg)
                    return True
                scheduler.register_handler("mads_pipeline", _mads_handler)

            # MADS outcome tracker (daily fix survival / recurrence / test manipulation)
            from agent.jobs.mads_outcomes import run_mads_outcome_check
            async def _mads_outcome_handler(**kwargs):
                return await run_mads_outcome_check(notifier=notifier)
            scheduler.register_handler("mads_outcomes", _mads_outcome_handler)

            # Behavior signal digest (weekly aggregation + L1 notification)
            from agent.jobs.behavior_signals import run_behavior_signal_digest
            async def _behavior_signal_handler(**kwargs):
                return await run_behavior_signal_digest(notifier=notifier)
            scheduler.register_handler("behavior_signals", _behavior_signal_handler)

            # Response quality proxy metrics (weekly, depends on P0-α)
            from agent.jobs.response_quality import run_response_quality_check
            async def _response_quality_handler(**kwargs):
                return await run_response_quality_check()
            scheduler.register_handler("response_quality", _response_quality_handler)

        await bot.start()
        bots.append(bot)

        # Wire idle checker: primary bot → heartbeat explore layer
        if bot_cfg["name"] == primary_cfg["name"]:
            hb.set_idle_checker(bot.check_idle)
            # Hub 3.0: Loop Executor for async ticket processing
            if _maqs_cfg.get("enabled", False):
                worker_mgr = WorkerManager(router)
                loop_executor = LoopExecutor(worker_mgr, config=_maqs_cfg)
                bot.on_dev_signal = loop_executor.enqueue
                log.info("LoopExecutor wired to bot '%s' via on_dev_signal", bot.name)
        log.info("Bot '%s' started (app_id=%s)", bot.name, bot_cfg["app_id"][:8])

    # Start scheduler after all handlers are registered (prevents missed-job race)
    await scheduler.start()

    log.info("All services started (%d bot(s)). Waiting for events...", len(bots))

    # Notify: service started (timeout-protected)
    await asyncio.sleep(2)
    bot_names = ", ".join(b.name for b in bots)
    log.info("Sending startup notification...")
    try:
        ok = await asyncio.wait_for(
            notifier.send_to_delivery_target(
                f"{{{{card:header=服务已启动,color=green}}}}\n"
                f"**claude-code-feishu** (pid={os.getpid()}, bots: {bot_names})"
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

    # Shutdown watchdog: if cleanup hangs past 30s, force-exit instead of
    # becoming a zombie waiting for launchd SIGKILL. Uses a daemon thread so
    # it is independent of the event loop (which may itself be blocked by
    # synchronous calls like flush_all_sync or disk I/O).
    def _shutdown_watchdog(timeout_s: int = 30) -> None:
        import time
        time.sleep(timeout_s)
        log.error("Shutdown watchdog: cleanup exceeded %ds, forcing exit", timeout_s)
        os._exit(1)

    _wd = threading.Thread(target=_shutdown_watchdog, daemon=True, name="shutdown-watchdog")
    _wd.start()

    # Graceful shutdown
    log.info("Shutting down...")
    try:
        await asyncio.wait_for(
            notifier.send_to_delivery_target("{{card:header=服务关闭中,color=orange}}\n**claude-code-feishu 正在关闭...**"),
            timeout=5,
        )
    except Exception:
        pass
    # Stop scheduler first — running jobs use dispatchers, so scheduler must
    # finish before dispatchers are torn down (prevents "Dispatcher not started")
    await scheduler.stop()
    await hb.stop()
    stopped_dispatchers = set()
    for bot in bots:
        await bot.stop()
        d = bot.dispatcher
        if id(d) not in stopped_dispatchers:
            await d.stop()
            stopped_dispatchers.add(id(d))
    if id(notifier) not in stopped_dispatchers:
        await notifier.stop()
        stopped_dispatchers.add(id(notifier))
    await router.save_sessions()
    router.close()
    message_store.close()
    if loop_executor is not None:
        await loop_executor.shutdown()
    try:
        os.remove(pid_path)
    except OSError:
        pass
    log.info("Shutdown complete.")
    # Attempt clean Python shutdown first; os._exit(0) in __main__ guards
    # against SDK non-daemon reconnect threads that block sys.exit forever.
    sys.exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (SystemExit, KeyboardInterrupt):
        pass
    finally:
        # SDK non-daemon threads may survive asyncio shutdown; force-kill
        # the process after all meaningful cleanup in main() is done.
        os._exit(0)
