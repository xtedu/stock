<template>
  <div class="page">
    <van-nav-bar title="数据任务" />

    <van-cell-group inset>
      <van-cell title="说明"
        label="以下任务平时由定时任务自动执行，这里提供手动触发入口。同步行情/基本面耗时较长，点击后请耐心等待。" />
    </van-cell-group>

    <van-cell-group inset title="一键操作">
      <van-cell title="盘中一键" label="快照 → 筛选 → 区间 → 监控（约1-2分钟）">
        <template #right-icon>
          <van-button size="small" type="primary" :loading="loading.intraday"
            @click="doJob('intraday')">执行</van-button>
        </template>
      </van-cell>
      <van-cell title="立即监控提醒" label="检查股池标的是否进入低估区并推送">
        <template #right-icon>
          <van-button size="small" type="success" :loading="loading.monitor"
            @click="doJob('monitor')">执行</van-button>
        </template>
      </van-cell>
    </van-cell-group>

    <van-cell-group inset title="分步操作">
      <van-cell title="同步行情" label="全市场日线（约15-20分钟）">
        <template #right-icon>
          <van-button size="small" :loading="loading.quotes"
            @click="doJob('quotes')">执行</van-button>
        </template>
      </van-cell>
      <van-cell title="同步基本面" label="财务+估值历史（数小时，建议周末）">
        <template #right-icon>
          <van-button size="small" :loading="loading.fundamentals"
            @click="doJob('fundamentals')">执行</van-button>
        </template>
      </van-cell>
      <van-cell title="运行选股筛选" label="按硬门槛+打分更新优质股池">
        <template #right-icon>
          <van-button size="small" :loading="loading.screen"
            @click="doJob('screen')">执行</van-button>
        </template>
      </van-cell>
      <van-cell title="计算安全区间" label="为股池标的计算MA250中枢区间">
        <template #right-icon>
          <van-button size="small" :loading="loading.zones"
            @click="doJob('zones')">执行</van-button>
        </template>
      </van-cell>
    </van-cell-group>

    <van-cell-group inset title="执行结果" v-if="result">
      <pre class="result">{{ result }}</pre>
    </van-cell-group>

    <van-cell-group inset title="定时任务时间表">
      <van-cell v-for="s in schedule" :key="s.time" :title="s.time" :label="s.desc" />
    </van-cell-group>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { showToast, showFailToast } from 'vant'
import {
  syncQuotes, syncFundamentals, runScreen, calcZones, runIntraday, runMonitor
} from '../api'

const loading = reactive({
  quotes: false, fundamentals: false, screen: false,
  zones: false, intraday: false, monitor: false
})
const result = ref('')

const runners = {
  quotes: { fn: syncQuotes, loadingKey: 'quotes', name: '同步行情' },
  fundamentals: { fn: syncFundamentals, loadingKey: 'fundamentals', name: '同步基本面' },
  screen: { fn: runScreen, loadingKey: 'screen', name: '选股筛选' },
  zones: { fn: calcZones, loadingKey: 'zones', name: '计算区间' },
  intraday: { fn: runIntraday, loadingKey: 'intraday', name: '盘中一键' },
  monitor: { fn: runMonitor, loadingKey: 'monitor', name: '监控提醒' }
}

const doJob = async (key) => {
  const r = runners[key]
  loading[r.loadingKey] = true
  result.value = `${r.name} 执行中...`
  try {
    const data = await r.fn()
    result.value = JSON.stringify(data, null, 2)
    showToast(`${r.name} 完成`)
  } catch (e) {
    const msg = e?.response?.data?.detail || e.message || '执行失败'
    result.value = `执行失败：${msg}`
    showFailToast(`${r.name} 失败`)
  } finally {
    loading[r.loadingKey] = false
  }
}

const schedule = [
  { time: '工作日 14:00', desc: '盘中链：快照→筛选→区间→监控，收盘前出结果' },
  { time: '工作日 15:40', desc: '收盘行情同步（全市场日线）' },
  { time: '工作日 16:10', desc: '选股筛选' },
  { time: '工作日 16:15', desc: '安全区间计算' },
  { time: '工作日 9:30-15:00 每30分钟', desc: '低估区监控提醒' },
  { time: '周六 09:00', desc: '全市场基本面同步（财务+估值）' }
]
</script>

<style scoped>
.page { padding-bottom: 60px; }
.result {
  margin: 12px;
  padding: 12px;
  background: #f7f8fa;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 300px;
  overflow: auto;
}
</style>
