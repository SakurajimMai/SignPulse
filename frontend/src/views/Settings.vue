<script setup lang="ts">
import GeneralSettings from '../components/settings/GeneralSettings.vue'
import ProxySettings from '../components/settings/ProxySettings.vue'
import TelegramApiSettings from '../components/settings/TelegramApiSettings.vue'
import AiSettings from '../components/settings/AiSettings.vue'
import BotNotifySettings from '../components/settings/BotNotifySettings.vue'
import SmtpSettings from '../components/settings/SmtpSettings.vue'
import DataManagementSettings from '../components/settings/DataManagementSettings.vue'
import AboutSettings from '../components/settings/AboutSettings.vue'
import { useSettingsPage } from '../composables/useSettingsPage'

const {
  t,
  settings,
  timezoneOptions,
  tgConfig,
  aiConfig,
  aiKeyDecryptFailed,
  loading,
  proxyLoading,
  proxyTestLoading,
  tgLoading,
  aiLoading,
  dataLoading,
  backupLoading,
  backupStatus,
  runtimeStatus,
  memoryStats,
  telegramBotRuntimeStatus,
  telegramBotRuntimeLoading,
  advancedLoading,
  botTestLoading,
  pageLoading,
  revealSecrets,
  isDirty,
  proxyDirty,
  dirtyLabels,
  appVersion,
  versionLoading,
  checkLoading,
  versionBanner,
  botLoading,
  smtpLoading,
  smtpTestLoading,
  keepaliveLoading,
  saveAllLoading,
  webdavTestLoading,
  webdavListLoading,
  remoteWebdavFiles,
  remoteWebdavMessage,
  webdavPasswordSet,
  botTokenSet,
  proxyPasswordSet,
  smtpPasswordSet,
  remoteDownloadName,
  saveSettings,
  saveProxySettings,
  runKeepaliveNow,
  saveBotSettings,
  saveSmtpSettings,
  saveAdvancedSettings,
  saveAllSettings,
  testBot,
  testProxy,
  testSmtp,
  saveTgConfig,
  resetTgConfig,
  saveAiConfig,
  testAi,
  handleExport,
  handleImportFile,
  handleBackupExport,
  handleWebdavTest,
  handleListRemoteBackups,
  handleDownloadRemoteBackup,
  handleCheckUpdate,
  refreshTelegramBotRuntimeStatus,
  toggleReveal,
} = useSettingsPage()
</script>

<template>
  <div class="max-w-7xl pb-10">
    <div
      v-if="isDirty && !pageLoading"
      class="sticky top-0 z-20 mb-4 flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs border border-amber-200 dark:border-amber-800/50 bg-amber-50 dark:bg-amber-500/10 text-amber-800 dark:text-amber-200 shadow-sm"
      role="status"
    >
      <div class="min-w-0">
        <div>{{ t('settings.unsavedBanner') }}</div>
        <div v-if="dirtyLabels.length" class="mt-0.5 text-[10px] opacity-90">
          {{ t('settings.dirtySections') }}: {{ dirtyLabels.join(' · ') }}
        </div>
      </div>
      <button
        type="button"
        class="ui-btn-primary !px-3 !py-1.5 !text-xs shrink-0"
        :disabled="saveAllLoading || loading || proxyLoading || botLoading || smtpLoading || advancedLoading || tgLoading || aiLoading"
        @click="saveAllSettings"
      >
        {{ saveAllLoading ? t('settings.saving') : t('settings.saveAll') }}
      </button>
    </div>
    <div v-if="pageLoading" class="grid grid-cols-1 lg:grid-cols-2 gap-6" aria-busy="true">
      <div v-for="i in 4" :key="i" class="ui-card p-6 space-y-4">
        <div class="ui-skeleton h-5 w-32" />
        <div class="ui-skeleton h-3 w-48" />
        <div class="ui-skeleton h-10 w-full" />
        <div class="ui-skeleton h-10 w-full" />
        <div class="ui-skeleton h-10 w-2/3" />
      </div>
    </div>
    <div v-else class="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">

      <!-- 左列必须是同一条 flex 栈：2×2 grid 会被更高的右列撑开行高，
           Telegram API 与数据管理之间就会空出一整块。 -->
      <div class="flex flex-col gap-6">
        <GeneralSettings
          v-model="settings"
          :timezone-options="timezoneOptions"
          :loading="loading"
          :keepalive-loading="keepaliveLoading"
          @save="saveSettings"
          @run-keepalive="runKeepaliveNow"
        />
        <ProxySettings
          v-model="settings"
          :password-set="proxyPasswordSet"
          :reveal="{ proxyPassword: revealSecrets.proxyPassword }"
          :loading="proxyLoading"
          :test-loading="proxyTestLoading"
          :test-disabled="proxyDirty"
          @save="saveProxySettings"
          @test="testProxy"
          @toggle-reveal="toggleReveal"
        />
        <TelegramApiSettings
          v-model="tgConfig"
          :reveal="{ tgApiId: revealSecrets.tgApiId, tgApiHash: revealSecrets.tgApiHash }"
          :loading="tgLoading"
          @save="saveTgConfig"
          @reset="resetTgConfig"
          @toggle-reveal="toggleReveal"
        />
        <DataManagementSettings
          v-model="settings"
          :webdav-password-set="webdavPasswordSet"
          :reveal="{ webdavPassword: revealSecrets.webdavPassword }"
          :backup-status="backupStatus"
          :remote-files="remoteWebdavFiles"
          :remote-message="remoteWebdavMessage"
          :remote-download-name="remoteDownloadName"
          :data-loading="dataLoading"
          :backup-loading="backupLoading"
          :webdav-test-loading="webdavTestLoading"
          :webdav-list-loading="webdavListLoading"
          :advanced-loading="advancedLoading"
          @export-json="handleExport"
          @import-json="handleImportFile"
          @backup-export="handleBackupExport"
          @webdav-test="handleWebdavTest"
          @webdav-list="handleListRemoteBackups"
          @webdav-download="handleDownloadRemoteBackup"
          @save-advanced="saveAdvancedSettings"
          @toggle-reveal="toggleReveal"
        />
      </div>

      <div class="flex flex-col gap-6">
        <AiSettings
          v-model:ai-model-value="aiConfig"
          v-model:settings-model-value="settings"
          :reveal="{ aiKey: revealSecrets.aiKey }"
          :ai-loading="aiLoading"
          :key-decrypt-failed="aiKeyDecryptFailed"
          @save-ai="saveAiConfig"
          @test-ai="testAi"
          @toggle-reveal="toggleReveal"
        />
        <BotNotifySettings
          v-model="settings"
          :bot-token-set="botTokenSet"
          :reveal="{ botToken: revealSecrets.botToken }"
          :bot-loading="botLoading"
          :bot-test-loading="botTestLoading"
          :runtime-status="telegramBotRuntimeStatus"
          :runtime-loading="telegramBotRuntimeLoading"
          @save="saveBotSettings"
          @test="testBot"
          @refresh-status="refreshTelegramBotRuntimeStatus()"
          @toggle-reveal="toggleReveal"
        />
        <SmtpSettings
          v-model="settings"
          :smtp-password-set="smtpPasswordSet"
          :reveal="{ smtpPassword: revealSecrets.smtpPassword }"
          :smtp-loading="smtpLoading"
          :smtp-test-loading="smtpTestLoading"
          @save="saveSmtpSettings"
          @test="testSmtp"
          @toggle-reveal="toggleReveal"
        />
        <AboutSettings
          :app-version="appVersion"
          :runtime-status="runtimeStatus"
          :memory-stats="memoryStats"
          :version-banner="versionBanner"
          :version-loading="versionLoading"
          :check-loading="checkLoading"
          @check-update="handleCheckUpdate(true)"
        />
      </div>

    </div>
  </div>
</template>
