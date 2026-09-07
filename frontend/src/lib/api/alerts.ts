import { request } from './core'

export type AlertGroup = 'manga' | 'games' | 'cloud' | 'system' | 'core' | string
export type AlertStatus = 'sent' | 'partial' | 'failed' | 'skipped' | 'ignored' | string
export type AlertSeverity = 'critical' | 'warning' | 'info'
export type AlertChannel = 'email' | 'telegram'

export interface AlertRule {
  id: string
  group: AlertGroup | string
  title?: string
  severity: AlertSeverity
  enabled: boolean
  email_enabled: boolean
  telegram_enabled: boolean
  cooldown_minutes: number
}

export interface AlertDelivery {
  channel: AlertChannel | string
  status: 'sent' | 'failed' | 'skipped' | string
  reason?: string
  error?: string
}

export interface AlertRecent {
  at?: string
  rule_id?: string
  title?: string
  detail?: string
  status?: AlertStatus
  reason?: string
  error?: string
  channel?: AlertChannel | string
  deliveries?: AlertDelivery[]
}

export type AlertRuleUpdate = Pick<
  AlertRule,
  'id' | 'enabled' | 'email_enabled' | 'telegram_enabled' | 'cooldown_minutes'
>

export interface AlertsPayload {
  rules: AlertRule[]
  recent: AlertRecent[]
  smtp_ready?: boolean
  bot_ready?: boolean
  smtp_enabled?: boolean
  quiet_hours?: boolean
}

export const getAlertRules = (token: string) =>
  request<AlertsPayload>('/alerts', {}, token)

export const saveAlertRules = (
  token: string,
  rules: AlertRuleUpdate[],
) =>
  request<AlertsPayload>(
    '/alerts',
    {
      method: 'PUT',
      body: JSON.stringify({ rules }),
    },
    token,
  )

export const testAlertMail = (token: string) =>
  request<{ success: boolean; message: string }>(
    '/alerts/test',
    { method: 'POST' },
    token,
  )
