from __future__ import annotations

from typing import Any


def _rule(
    rule_id: str,
    group: str,
    title: str,
    *,
    severity: str = "warning",
    cooldown_minutes: int = 30,
    email_enabled: bool = True,
    telegram_enabled: bool = True,
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "group": group,
        "title": title,
        "severity": severity,
        "default_enabled": True,
        "default_email_enabled": email_enabled,
        "default_telegram_enabled": telegram_enabled,
        "default_cooldown_minutes": cooldown_minutes,
    }


# Legacy aggregate rules are migrated in config.py and intentionally stay out
# of this list so the API cannot expose duplicate controls for one event.
ALERT_RULES: tuple[dict[str, Any], ...] = (
    _rule(
        "sign_task_rate_limited",
        "core",
        "签到任务触发频率限制",
        cooldown_minutes=60,
    ),
    _rule("sign_task_ai_fail", "core", "签到任务 AI 处理失败"),
    _rule(
        "sign_task_network_fail",
        "core",
        "签到任务网络或代理失败",
        cooldown_minutes=15,
    ),
    _rule("sign_task_timeout", "core", "签到任务执行超时"),
    _rule("sign_task_flow_fail", "core", "签到任务流程失败"),
    _rule(
        "account_login_invalid",
        "core",
        "Telegram 账号登录失效",
        severity="critical",
        cooldown_minutes=60,
    ),
    _rule(
        "manga_ingest_worker_fail",
        "manga",
        "漫画频道采集进程失败",
        severity="critical",
    ),
    _rule(
        "manga_imgbed_upload_fail",
        "manga",
        "漫画图床上传失败",
        cooldown_minutes=15,
    ),
    _rule(
        "manga_channel_publish_fail",
        "manga",
        "漫画出站频道发布失败",
        cooldown_minutes=15,
    ),
    _rule("manga_ehentai_worker_fail", "manga", "E-Hentai 监听进程失败"),
    _rule("manga_ehentai_gallery_fail", "manga", "E-Hentai 画廊采集失败"),
    _rule("manga_hmw_source_fail", "manga", "HMW 来源预处理失败"),
    _rule("manga_hmw_convert_fail", "manga", "HMW 图片转换失败"),
    _rule("manga_hmw_storage_fail", "manga", "HMW 存储上传或校验失败"),
    _rule("manga_site_fail", "manga", "漫画站点发布失败"),
    _rule(
        "games_source_pull_fail",
        "games",
        "游戏来源帖子拉取失败",
        cooldown_minutes=15,
    ),
    _rule("games_archive_fail", "games", "游戏解压、打包或伪装失败"),
    _rule(
        "games_site_publish_fail",
        "games",
        "游戏 WordPress 发布失败",
        cooldown_minutes=15,
    ),
    _rule(
        "games_cloud_fail",
        "games",
        "游戏网盘上传失败",
        cooldown_minutes=15,
    ),
    _rule(
        "cloud_auth_invalid",
        "cloud",
        "网盘登录凭据失效",
        severity="critical",
        cooldown_minutes=60,
    ),
    _rule(
        "cloud_keepalive_fail",
        "cloud",
        "网盘会话续期失败",
        cooldown_minutes=60,
    ),
    _rule(
        "system_backup_archive_fail",
        "system",
        "本地自动备份失败",
        severity="critical",
        cooldown_minutes=360,
    ),
    _rule(
        "system_backup_webdav_fail",
        "system",
        "WebDAV 备份上传失败",
        cooldown_minutes=360,
    ),
    _rule(
        "system_backup_retention_fail",
        "system",
        "备份保留策略清理失败",
        cooldown_minutes=360,
    ),
    _rule(
        "system_device_keepalive_fail",
        "system",
        "设备保活失败",
        cooldown_minutes=360,
    ),
    _rule(
        "system_memory_high",
        "system",
        "进程内存超限",
        severity="critical",
        cooldown_minutes=60,
    ),
    _rule(
        "system_scheduler_job_fail",
        "system",
        "系统调度任务失败",
        severity="critical",
        cooldown_minutes=60,
    ),
)


# A legacy setting fans out to all canonical event types it represented.
LEGACY_RULE_MIGRATIONS: dict[str, tuple[str, ...]] = {
    "sign_task_fail": (
        "sign_task_rate_limited",
        "sign_task_ai_fail",
        "sign_task_network_fail",
        "sign_task_timeout",
        "sign_task_flow_fail",
    ),
    "manga_telegram_fail": (
        "manga_ingest_worker_fail",
        "manga_imgbed_upload_fail",
        "manga_channel_publish_fail",
    ),
    "manga_ehentai_fail": (
        "manga_ehentai_worker_fail",
        "manga_ehentai_gallery_fail",
    ),
    "manga_hmw_fail": (
        "manga_hmw_source_fail",
        "manga_hmw_convert_fail",
        "manga_hmw_storage_fail",
    ),
    "games_publish_fail": (
        "games_source_pull_fail",
        "games_archive_fail",
        "games_site_publish_fail",
    ),
    "cloud_login_invalid": (
        "cloud_auth_invalid",
        "cloud_keepalive_fail",
    ),
    "system_backup_fail": (
        "system_backup_archive_fail",
        "system_backup_webdav_fail",
        "system_backup_retention_fail",
    ),
}

RULE_IDS = {item["id"] for item in ALERT_RULES}
LEGACY_RULE_IDS = set(LEGACY_RULE_MIGRATIONS)
ACCEPTED_RULE_IDS = RULE_IDS | LEGACY_RULE_IDS
RULE_TITLES = {item["id"]: str(item["title"]) for item in ALERT_RULES}
RULE_SEVERITIES = {item["id"]: str(item["severity"]) for item in ALERT_RULES}


def catalog() -> list[dict[str, Any]]:
    return [dict(item) for item in ALERT_RULES]


def rule_title(rule_id: str) -> str:
    return RULE_TITLES.get(rule_id, rule_id)


def rule_severity(rule_id: str) -> str:
    return RULE_SEVERITIES.get(rule_id, "warning")
