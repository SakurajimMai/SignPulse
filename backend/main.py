from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# Monkeypatch sqlite3.connect to increase default timeout
_original_sqlite3_connect = sqlite3.connect


def _patched_sqlite3_connect(*args, **kwargs):
    # Force timeout to be at least 10 seconds, even if Pyrogram sets it to 1
    if "timeout" in kwargs:
        if kwargs["timeout"] < 10:
            kwargs["timeout"] = 10
    else:
        kwargs["timeout"] = 30
    return _original_sqlite3_connect(*args, **kwargs)


sqlite3.connect = _patched_sqlite3_connect

from backend.api import router as api_router  # noqa: E402
from backend.core.config import get_settings  # noqa: E402
from backend.core.database import (  # noqa: E402
    Base,
    get_engine,
    get_session_local,
    init_engine,
)
from backend.scheduler import (  # noqa: E402
    catch_up_missed_sign_tasks,
    init_scheduler,
    shutdown_scheduler,
    sync_jobs,
)
from backend.services.users import ensure_admin  # noqa: E402
from backend.utils.paths import ensure_data_dirs  # noqa: E402
from tg_signer.async_utils import create_logged_task  # noqa: E402


# Silence /health check logs
class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return (
            "/health" not in msg
            and "/healthz" not in msg
            and "/readyz" not in msg
        )


class AccessLogLevelFilter(logging.Filter):
    """将 uvicorn access log 的级别从 INFO 强制转换为 DEBUG"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.INFO:
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        return True


_SENSITIVE_LOG_PATTERNS = (
    (
        re.compile(r"(https?://api\.telegram\.org/bot)[^/\s\"']+", re.I),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?<![\w])\d{5,}:[A-Za-z0-9_-]{20,}"),
        "[TELEGRAM_BOT_TOKEN_REDACTED]",
    ),
    (
        re.compile(r"\b(https?|socks5)://([^\s/:@]+):([^\s/@]+)@", re.I),
        r"\1://\2:[REDACTED]@",
    ),
)


def redact_sensitive_log_text(value: object) -> str:
    text = str(value)
    for pattern, replacement in _SENSITIVE_LOG_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SensitiveDataFilter(logging.Filter):
    """Redact Telegram and proxy credentials before handlers format records."""

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        redacted = redact_sensitive_log_text(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        if record.exc_info:
            _exc_type, exc, traceback = record.exc_info
            safe_exception = redact_sensitive_log_text(exc)
            if safe_exception != str(exc):
                record.exc_info = (
                    RuntimeError,
                    RuntimeError(safe_exception),
                    traceback,
                )
                record.exc_text = None
        return True


# 配置后端日志等级，支持 LOG_LEVEL 环境变量
def _configure_backend_logging():
    """配置后端日志等级，从环境变量 LOG_LEVEL 读取，默认为 INFO

    日志等级说明：
    - DEBUG: 详细的调试信息，包括 uvicorn 访问日志（过滤健康检查端点）
    - INFO: 应用常规运行信息（默认）
    - WARNING: 警告信息
    - ERROR: 错误信息
    - CRITICAL: 严重错误

    访问日志处理：
    直接删除 uvicorn.access 的所有 handler，从根源禁用访问日志输出。
    DEBUG 模式下重新启用，但过滤健康检查端点以减少噪音。
    """
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    level_no = logging.getLevelName(log_level)

    # 验证日志等级有效性
    if not isinstance(level_no, int):
        logging.warning("Invalid LOG_LEVEL '%s', falling back to INFO", log_level)
        level_no = logging.INFO

    # 配置根日志器和主要模块日志器
    root = logging.getLogger()
    root.setLevel(level_no)
    # 确保 root logger 有 handler，否则 INFO 级别消息会被 lastResort 静默丢弃
    if not root.handlers:
        _handler = logging.StreamHandler()
        _handler.setLevel(level_no)
        _handler.setFormatter(logging.Formatter(
            "[%(levelname)s] [%(name)s] %(asctime)s %(filename)s %(lineno)s %(message)s"
        ))
        root.addHandler(_handler)
    for root_handler in root.handlers:
        if not any(
            isinstance(item, SensitiveDataFilter)
            for item in root_handler.filters
        ):
            root_handler.addFilter(SensitiveDataFilter())
    logging.getLogger("backend").setLevel(level_no)
    logging.getLogger("uvicorn").setLevel(level_no)
    for client_logger_name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(client_logger_name).setLevel(
            max(level_no, logging.WARNING)
        )

    # 暴力删除 uvicorn.access 的所有 handler，从根源禁用
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True

    # 只在 DEBUG 模式下重新启用访问日志
    if level_no <= logging.DEBUG:
        access_logger.disabled = False
        access_logger.setLevel(logging.DEBUG)
        # DEBUG 模式下过滤健康检查端点，减少日志噪音
        access_logger.addFilter(HealthCheckFilter())
        # 将 INFO 级别的访问日志强制转换为 DEBUG
        access_logger.addFilter(AccessLogLevelFilter())
        # 添加 stderr handler 输出访问日志（使用详细格式）
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        handler.addFilter(SensitiveDataFilter())
        handler.setFormatter(logging.Formatter(
            "[%(levelname)s] [%(name)s] %(asctime)s %(filename)s %(lineno)s %(message)s"
        ))
        access_logger.addHandler(handler)


# 注意：不在此处调用 _configure_backend_logging()
# 因为 uvicorn 启动后会重置 logging 配置，需要在 on_startup 事件中重新配置

settings = get_settings()

@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    await on_startup()
    try:
        yield
    finally:
        await on_shutdown()


def _app_version() -> str:
    """解析应用版本：优先 version_info，失败回退到包版本，最终回退占位符。

    仅捕获导入与属性访问相关的具体异常，避免吞掉编程错误；
    降级时记录 warning 日志便于排障。
    """
    try:
        from backend.utils.version_info import get_local_version_info

        return str(get_local_version_info().get("version") or "0.0.0")
    except (ImportError, AttributeError, ValueError, TypeError):
        logging.getLogger("backend.startup").debug(
            "version_info 解析失败，回退到 tg_signer.__version__",
            exc_info=True,
        )
        try:
            from tg_signer import __version__

            return str(__version__)
        except (ImportError, AttributeError):
            logging.getLogger("backend.startup").warning(
                "tg_signer.__version__ 也不可用，版本回退到 0.0.0",
                exc_info=True,
            )
            return "0.0.0"


app = FastAPI(
    title=settings.app_name,
    version=_app_version(),
    lifespan=lifespan,
)
app.state.ready = False


async def _sync_scheduler_on_startup() -> None:
    """Register current jobs, then enqueue eligible missed sign tasks once."""
    await sync_jobs()
    await catch_up_missed_sign_tasks()


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理器：仅捕获未处理的异常，不拦截 HTTPException"""
    # FastAPI 的 HTTPException（401/403/404 等）应由框架正常处理
    from fastapi.exceptions import HTTPException as FastAPIHTTPException
    if isinstance(exc, FastAPIHTTPException):
        raise exc

    logging.getLogger("backend.exception").error(
        "Unhandled exception on %s %s: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error"},
    )

app.add_middleware(GZipMiddleware, minimum_size=1000)


_MINI_APP_CACHE_REVISION = "20260902c"
_MINI_APP_CACHE_COOKIE = "mini_app_cache_revision"


@app.middleware("http")
async def migrate_legacy_mini_app_cache(request: Request, call_next):
    """Remove the legacy root-scoped worker once from Telegram WebViews."""
    response = await call_next(request)
    if request.url.path != "/api/mini-app/auth":
        return response

    response.headers["Cache-Control"] = "no-store"
    if request.cookies.get(_MINI_APP_CACHE_COOKIE) == _MINI_APP_CACHE_REVISION:
        return response
    try:
        referer = urlsplit(request.headers.get("referer", ""))
        query = dict(parse_qsl(referer.query, keep_blank_values=True))
    except ValueError:
        return response
    if (
        referer.hostname != request.url.hostname
        or referer.path.rstrip("/") != "/mini-app"
        or query.get("rev") != _MINI_APP_CACHE_REVISION
    ):
        return response

    # This response always comes from the network (auth is POST). The storage
    # directive unregisters the old worker and clears its CacheStorage.
    response.headers["Clear-Site-Data"] = '"cache", "storage"'
    response.set_cookie(
        _MINI_APP_CACHE_COOKIE,
        _MINI_APP_CACHE_REVISION,
        max_age=365 * 24 * 60 * 60,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response



app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

# API 路由必须在静态文件挂载之前注册，并使用 /api 前缀
app.include_router(api_router, prefix="/api")


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz")
def health_checkz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def ready_check(response: Response) -> dict:
    """就绪探针：未完成启动时 503；就绪时附带调度锁与旧 API 写策略（便于运维自查）。"""
    if not getattr(app.state, "ready", False):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "starting"}

    payload: dict = {"status": "ready"}
    try:
        from backend.scheduler.instance_lock import has_scheduler_lock

        lock_held = has_scheduler_lock()
        payload["scheduler_lock_held"] = lock_held
        # 旧 /api/tasks 已移除，写能力恒为 false
        payload["legacy_tasks_writable"] = False
        payload["legacy_tasks_removed"] = True
        # 副本进程（未持锁）时显式提示：业务 cron 不在本实例执行
        payload["scheduler_role"] = "primary" if lock_held else "replica"
    except (ImportError, AttributeError) as exc:
        logging.getLogger("backend.readyz").debug("ready 附加信息失败: %s", exc)
    return payload


# 静态前端托管（Mode A: 单容器，FastAPI 提供静态文件）
# 挂载 Next.js 静态资源
web_dir = Path("/web")
next_static_dir = web_dir / "_next"
frontend_dev_url = os.getenv("FRONTEND_DEV_SERVER_URL", "http://127.0.0.1:3000")

if next_static_dir.exists():
    app.mount(
        "/_next",
        StaticFiles(directory=str(next_static_dir)),
        name="nextjs_static",
    )


def _resolve_web_file(full_path: str) -> Path | None:
    """安全解析文件路径，防止路径遍历攻击"""
    web_root = web_dir.resolve()
    candidate = (web_root / full_path).resolve()
    try:
        candidate.relative_to(web_root)
    except ValueError:
        return None
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


_NO_CACHE_WEB_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _web_file_response(path: Path) -> FileResponse:
    """Avoid stale app shells/workers while retaining hashed asset caching."""
    no_cache = path.suffix == ".html" or path.name in {
        "sw.js",
        "registerSW.js",
        "manifest.webmanifest",
    }
    return FileResponse(path, headers=_NO_CACHE_WEB_HEADERS if no_cache else None)


# Catch-all 路由：处理所有前端路由，返回 index.html
# 注意：FastAPI 的 /docs、/redoc、/openapi.json 在此路由之前已自动注册，
# 因此不会被此 catch-all 拦截。无需额外排除。


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """
    SPA fallback: 对于所有非 API 路由，返回 index.html
    这样刷新页面时不会 404
    """
    # API 路由版本不匹配或拼写错误时必须保持 JSON 404。否则生产镜像中的
    # index.html 会把未知 GET /api/* 伪装成 200，调用方随后暴露整段 HTML。
    if full_path == "api" or full_path.startswith("api/"):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    # 检查是否是静态文件请求（带路径遍历防护）
    file_path = _resolve_web_file(full_path)
    if file_path is not None:
        return _web_file_response(file_path)

    # 尝试添加 .html 后缀
    html_path = _resolve_web_file(f"{full_path}.html")
    if html_path is not None:
        return _web_file_response(html_path)

    # 否则返回 index.html（SPA 路由）
    index_path = web_dir / "index.html"
    if index_path.exists():
        return _web_file_response(index_path)

    # 如果 index.html 也不存在，开发模式下重定向到前端开发服务器，生产环境返回 404
    if os.getenv("FRONTEND_DEV_SERVER_URL"):
        normalized_path = full_path if full_path.startswith("/") else f"/{full_path}"
        if not normalized_path:
            normalized_path = "/"

        parsed_frontend = urlsplit(frontend_dev_url)
        redirect_target = urlunsplit(
            (
                parsed_frontend.scheme,
                parsed_frontend.netloc,
                normalized_path,
                "",
                "",
            )
        )
        return RedirectResponse(url=redirect_target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    return Response(content="Not Found", status_code=status.HTTP_404_NOT_FOUND)


async def on_startup() -> None:
    # 重新应用日志配置（uvicorn 启动后会重新配置 logging，覆盖之前的设置）
    _configure_backend_logging()

    # 版本标记
    _git_branch = os.getenv("GIT_BRANCH", "dev")
    _git_sha = os.getenv("GIT_SHA", "dev")[:7]
    logging.getLogger("backend.startup").info(
        "TG-SignPulse version=%s-%s", _git_branch, _git_sha
    )

    ensure_data_dirs(settings)
    # 清理过期头像缓存（7 天 TTL），避免长期运行累积陈旧文件
    try:
        from backend.services import avatar_cache

        avatar_root = settings.resolve_workdir() / "avatars"
        for sub_dir in (avatar_root, avatar_root / "chats"):
            avatar_cache.cleanup_avatar_cache(sub_dir)
    except Exception:
        logging.getLogger("backend.startup").debug(
            "清理过期头像缓存失败（可忽略）", exc_info=True
        )
    # 面板持久化的全局设置回灌环境变量：env_sync 仅在保存时执行，
    # 重启后若不回灌，tg_signer 等仍读 env 的路径会静默回退默认值
    try:
        from backend.services.config import get_config_service
        from backend.services.config_mixins import apply_global_settings_to_env
        from backend.services.proxy_runtime import apply_proxy_environment

        apply_global_settings_to_env(get_config_service().get_global_settings())
        apply_proxy_environment()
    except Exception:
        logging.getLogger("backend.startup").debug(
            "全局设置或系统代理回灌环境变量失败（可忽略）", exc_info=True
        )
    init_engine()
    Base.metadata.create_all(bind=get_engine())
    with get_session_local()() as db:
        ensure_admin(db)
    await init_scheduler(sync_on_startup=False)

    # Bot long polling shares the scheduler-primary ownership boundary so a
    # multi-replica deployment never consumes the same update stream twice.
    # Every replica starts the lightweight supervisor: secondaries remain in
    # standby and can take over if scheduler ownership changes later.
    try:
        from backend.services.telegram_bot import start_telegram_bot_runtime

        await start_telegram_bot_runtime()
    except Exception:
        logging.getLogger("backend.telegram_bot").exception(
            "Telegram Bot 控制服务未能启动，主面板继续运行"
        )

    # 漫画是 SignPulse 的独立子模块：目录数据库始终初始化，Telegram
    # 监听只有在 MANGA_ENABLED 或完整漫画环境配置开启时才启动。
    try:
        from backend.services.manga.runtime import get_manga_runtime

        await get_manga_runtime().initialize()
    except Exception:
        logging.getLogger("backend.manga").exception("漫画目录初始化失败，主面板继续启动")

    # Pre-export session strings from .session files to avoid SQLite locks during task execution
    _pre_export_session_strings()

    # 启动内存监控后台任务（阈值可通过 MEMORY_THRESHOLD_MB 覆盖）
    app.state.memory_monitor_task = create_logged_task(
        _memory_monitor_loop(),
        logger=logging.getLogger("backend.memory_monitor"),
        description="backend memory monitor loop",
    )

    async def _post_startup() -> None:
        try:
            await _sync_scheduler_on_startup()
            from backend.services.keyword_monitor import get_keyword_monitor_service

            await get_keyword_monitor_service().restart_from_tasks()
        except Exception as exc:
            # 顶层兜底：启动阶段任何未处理异常都不能让进程崩溃
            logging.getLogger("backend.startup").exception(
                "Delayed scheduler sync failed: %s", exc
            )
        finally:
            app.state.ready = True
            # 启动完成汇总：一次日志携带版本/监听地址/数据目录，便于部署排障
            logging.getLogger("backend.startup").info(
                "面板启动完成 version=%s host=%s port=%s data_dir=%s",
                _app_version(),
                settings.host,
                settings.port,
                settings.resolve_workdir(),
            )

    app.state.startup_task = create_logged_task(
        _post_startup(),
        logger=logging.getLogger("backend.startup"),
        description="backend delayed startup sync",
    )

    async def _start_manga_if_enabled() -> None:
        try:
            from backend.services.manga.runtime import get_manga_runtime

            await get_manga_runtime().start_if_enabled()
        except Exception:
            logging.getLogger("backend.manga").exception("漫画监听未能自动启动")

    app.state.manga_startup_task = create_logged_task(
        _start_manga_if_enabled(),
        logger=logging.getLogger("backend.manga"),
        description="manga worker startup",
    )

    async def _start_games_automation_if_enabled() -> None:
        try:
            from backend.scheduler.instance_lock import has_scheduler_lock
            from backend.services.games.automation import get_games_automation

            if not has_scheduler_lock():
                logging.getLogger("backend.games").info(
                    "当前实例不是 scheduler primary，跳过游戏自动发布监听"
                )
                return
            await get_games_automation().start_if_enabled()
        except Exception:
            logging.getLogger("backend.games").exception("游戏自动发布监听未能启动")
        try:
            from backend.scheduler.instance_lock import has_scheduler_lock
            from backend.services.games.keepalive import refresh_cloud_sessions

            if has_scheduler_lock():
                await refresh_cloud_sessions()
        except Exception:
            logging.getLogger("backend.games").exception("游戏网盘自动续期未能启动")

    app.state.games_startup_task = create_logged_task(
        _start_games_automation_if_enabled(),
        logger=logging.getLogger("backend.games"),
        description="games automation startup",
    )


def _pre_export_session_strings() -> None:
    """Export session strings from all .session files at startup to enable in-memory mode."""
    from backend.utils.tg_session import (
        get_session_mode,
        load_session_string_file,
    )

    session_dir = settings.resolve_session_dir()
    logger = logging.getLogger("backend.startup")

    # Clean up any stray "*" directories (legacy bug from update_task wildcard handling)
    try:
        signs_dir = settings.resolve_workdir() / "signs"
        wildcard_dir = signs_dir / "*"
        if wildcard_dir.exists() and wildcard_dir.is_dir():
            import shutil
            shutil.rmtree(wildcard_dir)
            logger.info("已清理遗留的 '*' 任务目录")
    except OSError as exc:
        logger.warning("清理通配符目录失败: %s", exc)

    # Only needed in file mode - string mode already has session strings
    if get_session_mode() == "string":
        return

    # Export for all accounts that have .session files
    exported = 0
    for session_file in session_dir.glob("*.session"):
        account_name = session_file.stem
        # load_session_string_file will auto-export if .session_string doesn't exist
        result = load_session_string_file(session_dir, account_name)
        if result:
            exported += 1

    if exported:
        logger.info("为内存任务执行预导出 %s 个会话串", exported)


async def _memory_monitor_loop() -> None:
    """周期性检查进程内存，超阈值时告警并可选触发 GC。"""
    import socket

    from backend.utils.memory_monitor import MemoryMonitor
    from tg_signer.utils import read_positive_float_env

    instance_name = (
        os.getenv("INSTANCE_ID", "").strip()
        or os.getenv("HOSTNAME", "").strip()
        or socket.gethostname()
    )
    instance_pid = os.getpid()
    instance_id = f"{instance_name}:{instance_pid}"

    def _send_memory_alert(record) -> None:
        from backend.services.alerts import schedule_alert

        schedule_alert(
            "system_memory_high",
            title=f"进程内存超限：{record.rss_mb:.1f} MB",
            detail=(
                f"{record.message}\n实例：{instance_name}\nPID：{instance_pid}"
            ),
            fingerprint=f"backend-process:{instance_id}",
        )

    threshold_mb = read_positive_float_env(
        "MEMORY_THRESHOLD_MB", 512.0, minimum=64.0
    )
    interval_s = read_positive_float_env(
        "MEMORY_CHECK_INTERVAL_S", 60.0, minimum=10.0
    )
    repeat_s = read_positive_float_env(
        "MEMORY_ALERT_REPEAT_INTERVAL_S", 300.0, minimum=10.0
    )
    monitor_interval = interval_s
    alert_repeat_interval = max(repeat_s, monitor_interval)
    monitor = MemoryMonitor(
        threshold_mb=threshold_mb,
        check_interval=monitor_interval,
        alert_repeat_interval=alert_repeat_interval,
        gc_enabled=True,
        alert_callback=_send_memory_alert,
    )
    app.state.memory_monitor = monitor
    logger = logging.getLogger("backend.memory_monitor")
    logger.info(
        "内存监控已启动 threshold=%.0fMB interval=%.0fs repeat=%.0fs",
        monitor.threshold_mb,
        monitor.check_interval,
        alert_repeat_interval,
    )
    try:
        while True:
            try:
                monitor.check()
            except Exception:
                # 内存检查可能因 psutil 系统调用瞬时失败；保留宽捕获确保监控循环不退出
                logger.exception("内存检查失败")
            await asyncio.sleep(monitor.check_interval)
    except asyncio.CancelledError:
        stats = monitor.get_stats()
        logger.info(
            "内存监控已停止 current_rss=%.1fMB alerts=%s",
            stats.get("current_rss_mb", 0),
            stats.get("alert_count", 0),
        )
        raise


async def on_shutdown() -> None:
    log = logging.getLogger("backend.shutdown")
    try:
        from backend.services.proxy_runtime import stop_proxy_runtime_reconcile

        await stop_proxy_runtime_reconcile()
    except Exception:
        log.exception("系统代理运行时协调器关闭失败")
    try:
        from backend.services.proxy_runtime import restore_initial_proxy_environment

        restore_initial_proxy_environment()
    except Exception:
        log.exception("恢复进程初始代理环境失败")
    startup_task = getattr(app.state, "startup_task", None)
    if startup_task is not None and not startup_task.done():
        startup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await startup_task

    memory_task = getattr(app.state, "memory_monitor_task", None)
    if memory_task is not None and not memory_task.done():
        memory_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await memory_task

    manga_startup_task = getattr(app.state, "manga_startup_task", None)
    if manga_startup_task is not None and not manga_startup_task.done():
        manga_startup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await manga_startup_task
    games_startup_task = getattr(app.state, "games_startup_task", None)
    if games_startup_task is not None and not games_startup_task.done():
        games_startup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await games_startup_task
    try:
        from backend.services.games.automation import get_games_automation

        await get_games_automation().stop(cancel_job=True)
    except Exception:
        log.exception("游戏自动发布 worker shutdown failed")
    try:
        from backend.services.manga.runtime import get_manga_runtime

        runtime = get_manga_runtime()
        await runtime.stop_ehentai()
        await runtime.stop(close_database=True)
    except Exception:
        log.exception("漫画 worker shutdown failed")

    # Long polling must stop while this process still owns the primary lock.
    try:
        from backend.services.telegram_bot import stop_telegram_bot_runtime

        await stop_telegram_bot_runtime()
    except Exception:
        log.exception("Telegram Bot control shutdown failed")

    shutdown_scheduler()
    try:
        from backend.services.keyword_monitor import get_keyword_monitor_service

        await get_keyword_monitor_service().stop()
    except Exception:
        # 顶层兜底：关闭阶段任何异常不能阻止进程退出
        log.exception("Keyword monitor shutdown failed")

    try:
        from backend.services.alerts import drain_scheduled_alerts

        await drain_scheduled_alerts(timeout=5.0)
    except Exception:
        log.exception("等待告警投递结束失败")

    # 释放调度文件锁，避免异常退出后锁文件残留导致下一进程误判 replica
    try:
        from backend.scheduler.instance_lock import release_scheduler_lock

        release_scheduler_lock()
    except Exception:
        log.exception("Scheduler lock release failed")
