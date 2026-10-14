<template>
  <div class="page">
    <van-nav-bar title="我的" />
    <van-cell-group inset title="提醒规则">
      <van-cell v-for="r in rules" :key="r.id"
                :title="r.code || '全部股票池'"
                :label="`触发: ${TRIGGER_LABEL[r.trigger] || r.trigger} · 通道: ${(r.channels || []).join('/')}`">
        <template #right-icon>
          <van-icon name="delete-o" @click="remove(r.id)" />
        </template>
      </van-cell>
      <van-cell title="新增全池提醒" is-link @click="addDefault" />
    </van-cell-group>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { createAlertRule, deleteAlertRule, getAlertRules } from '../api'

const rules = ref([])
const TRIGGER_LABEL = { enter_zone: '低于区间下沿（低估区）' }
const load = async () => { rules.value = await getAlertRules() }
const addDefault = async () => {
  await createAlertRule({ code: null, trigger: 'enter_zone', channels: ['feishu'] })
  await load()
}
const remove = async (id) => { await deleteAlertRule(id); await load() }
onMounted(load)
</script>

<style scoped>
.page { padding-bottom: 60px; }
</style>
