/**
 * 系统设置 API：用户资料（密码/用户名/TOTP）、AI 配置、全局设置、
 * Telegram API 配置、设备保活。
 */
import { MEDIUM_TIMEOUT_MS, request, requestBlob } from "./core";

// ─── 用户设置 ───

export const changePassword = (token: string, oldPassword: string, newPassword: string) =>
  request<{ success: boolean; message: string }>("/user/password", {
    method: "PUT",
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  }, token);

export const getTOTPStatus = (token: string) =>
  request<{ enabled: boolean; secret?: string }>("/user/totp/status", {}, token);

export const setupTOTP = (token: string) =>
  request<{ enabled: boolean; secret: string }>("/user/totp/setup", {
    method: "POST",
  }, token);

export const fetchTOTPQRCode = async (token: string) => {
  const blob = await requestBlob("/user/totp/qrcode", {}, token);
  return window.URL.createObjectURL(blob);
};

export const enableTOTP = (token: string, totpCode: string) =>
  request<{ success: boolean; message: string }>("/user/totp/enable", {
    method: "POST",
    body: JSON.stringify({ totp_code: totpCode }),
  }, token);

export const disableTOTP = (token: string, totpCode: string) =>
  request<{ success: boolean; message: string }>("/user/totp/disable", {
    method: "POST",
    body: JSON.stringify({ totp_code: totpCode }),
  }, token);

export interface ChangeUsernameResponse {
  success: boolean;
  message: string;
  access_token?: string;
}

export const changeUsername = (token: string, newUsername: string, password: string) =>
  request<ChangeUsernameResponse>("/user/username", {
    method: "PUT",
    body: JSON.stringify({ new_username: newUsername, password: password }),
  }, token);

// ─── AI 配置 ───

export interface AIConfig {
  has_config: boolean;
  base_url?: string;
  model?: string;
  api_key?: string;
  api_key_masked?: string;
  /** 磁盘有配置但 APP_SECRET_KEY 不匹配，需重填 Key */
  api_key_decrypt_failed?: boolean;
}

export interface AITestResult {
  success: boolean;
  message: string;
  model_used?: string;
}

export const getAIConfig = (token: string, reveal = false) =>
  request<AIConfig>(`/config/ai${reveal ? "?reveal=1" : ""}`, {}, token);

export const saveAIConfig = (
  token: string,
  config: { api_key?: string; base_url?: string; model?: string }
) =>
  request<{ success: boolean; message: string }>("/config/ai", {
    method: "POST",
    body: JSON.stringify(config),
  }, token);

export const testAIConnection = (token: string) =>
  request<AITestResult>("/config/ai/test", {
    method: "POST",
  }, token);

// ─── 全局设置 ───

export interface GlobalSettings {
  sign_interval?: number | null;  // null 表示随机 1-120 秒
  log_retention_days?: number;    // 日志保留天数，默认 7
  data_dir?: string | null;
  /** 旧版代理 URL，仅用于兼容读取。 */
  global_proxy?: string | null;
  proxy_enabled?: boolean;
  proxy_scheme?: "http" | "socks5";
  proxy_host?: string | null;
  proxy_port?: number | null;
  proxy_username?: string | null;
  /** GET 不回传明文；写入时仅非空时更新。 */
  proxy_password?: string | null;
  proxy_password_set?: boolean;
  proxy_no_proxy?: string | null;
  /** 明确清除代理用户名和密码，区别于空密码保留。 */
  proxy_clear_credentials?: boolean;
  tg_global_concurrency?: number | null;
  device_keepalive_enabled?: boolean;
  device_keepalive_interval_days?: number;
  telegram_bot_notify_enabled?: boolean;
  telegram_bot_login_notify_enabled?: boolean;
  telegram_bot_task_failure_enabled?: boolean;
  telegram_bot_task_success_enabled?: boolean;
  telegram_bot_quiet_hours_enabled?: boolean;
  telegram_bot_quiet_hours_start?: string | null;
  telegram_bot_quiet_hours_end?: string | null;
  /** GET 不回传明文；写入时仅非空时更新 */
  telegram_bot_token?: string | null;
  telegram_bot_token_set?: boolean;
  telegram_bot_chat_id?: string | null;
  telegram_bot_message_thread_id?: number | null;
  telegram_bot_control_enabled?: boolean;
  telegram_bot_mini_app_url?: string | null;
  telegram_bot_allowed_user_ids?: number[];
  timezone?: string;
  sign_task_execution_timeout?: number | null;
  sign_task_account_cooldown?: number | null;
  sign_task_flow_retry_attempts?: number | null;
  sign_task_history_max_age_days?: number | null;
  ai_vision_timeout?: number | null;
  ai_vision_retry_attempts?: number | null;
  auto_backup_enabled?: boolean;
  auto_backup_interval_hours?: number | null;
  auto_backup_keep?: number | null;
  webdav_url?: string | null;
  webdav_username?: string | null;
  /** GET 永不返回明文；写入时仅在非空时更新 */
  webdav_password?: string | null;
  /** 服务端是否已保存 WebDAV 密码 */
  webdav_password_set?: boolean;
  webdav_remote_dir?: string | null;
  smtp_enabled?: boolean;
  smtp_host?: string | null;
  smtp_port?: number | null;
  smtp_username?: string | null;
  smtp_password?: string | null;
  smtp_password_set?: boolean;
  smtp_encryption?: string | null;
  smtp_from?: string | null;
  smtp_notify_email?: string | null;
}

export const getGlobalSettings = (token: string, reveal = false) =>
  request<GlobalSettings>(
    `/config/settings${reveal ? "?reveal=1" : ""}`,
    {},
    token,
  );

export const saveGlobalSettings = (token: string, settings: GlobalSettings) =>
  request<{ success: boolean; message: string }>("/config/settings", {
    method: "POST",
    body: JSON.stringify(settings),
  }, token);

export interface ProxyTestResult {
  success: boolean;
  message: string;
  status_code?: number | null;
  latency_ms?: number | null;
}

/** 使用服务端已保存的代理配置检测 Telegram API 连通性。 */
export const testProxyConnection = (token: string) =>
  request<ProxyTestResult>("/config/proxy/test", {
    method: "POST",
  }, token);

export interface TelegramBotRuntimeStatus {
  state: string;
  configured: boolean;
  control_enabled: boolean;
  primary: boolean;
  running: boolean;
  webhook_conflict: boolean;
  polling_conflict: boolean;
  bot: {
    id: number | null;
    username: string;
    first_name: string;
  };
  last_error: string;
  last_update_id: number | null;
}

export const getTelegramBotRuntimeStatus = (token: string) =>
  request<TelegramBotRuntimeStatus>(
    "/config/settings/telegram-bot/runtime",
    {},
    token,
  );

export const testBotNotification = (token: string, message?: string) =>
  request<{ success: boolean; message: string }>("/config/bot/test", {
    method: "POST",
    body: JSON.stringify({ message: message || undefined }),
  }, token);

export interface DeviceKeepaliveRunResult {
  success: boolean;
  enabled: boolean;
  checked: number;
  kept_alive: number;
  skipped: number;
  failed: number;
  interval_days?: number | null;
  results: Array<{ account_name: string; status: string; message?: string }>;
}

export const runDeviceKeepalive = (token: string) =>
  request<DeviceKeepaliveRunResult>(
    "/config/settings/device-keepalive/run",
    { method: "POST" },
    token,
    // 多账号保活可能超过默认 30s
    MEDIUM_TIMEOUT_MS,
  );

// ─── Telegram API 配置 ───

export interface TelegramConfig {
  api_id: string;
  api_hash: string;
  is_custom: boolean;
  default_api_id: string;
  default_api_hash: string;
}

export const getTelegramConfig = (token: string) =>
  request<TelegramConfig>("/config/telegram", {}, token);

export const saveTelegramConfig = (
  token: string,
  config: { api_id: string; api_hash: string }
) =>
  request<{ success: boolean; message: string }>("/config/telegram", {
    method: "POST",
    body: JSON.stringify(config),
  }, token);

export const resetTelegramConfig = (token: string) =>
  request<{ success: boolean; message: string }>("/config/telegram", {
    method: "DELETE",
  }, token);
