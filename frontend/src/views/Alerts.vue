<template>
  <div class="page">
    <van-nav-bar title="提醒记录" />
    <van-cell v-for="log in logs" :key="log.id"
              :title="`${log.code} @ ${log.price}`"
              :label="`区间 ${log.zone_low}~${log.zone_high} · ${formatTime(log.notified_at)}`" />
    <van-empty v-if="!logs.length" description="暂无提醒" />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { getAlertLogs } from '../api'

const logs = ref([])
const formatTime = t => new Date(t).toLocaleString('zh-CN')
onMounted(async () => { logs.value = await getAlertLogs() })
</script>

<style scoped>
.page { padding-bottom: 60px; }
</style>
