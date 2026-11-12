<template>
  <div class="page">
    <van-nav-bar title="数据任务" />

    <van-cell-group inset>
      <van-cell title="说明"
        label="以下任务平时由定时任务自动执行，这里提供手动触发入口。同步行情/基本面耗时较长，点击后请耐心等待。" />
    </van-cell-group>

    <van-cell-group inset title="一键操作">
      <van-cell title="盘中一键" label="快照 → 资金流 → 筛选 → 区间 → 监控（约1-2分钟）">
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
      <van-cell title="补当日日线" label="收盘后一次快照写入全市场（秒级，抗限流）">
        <template #right-icon>
          <van-button size="small" type="primary" :loading="loading.dailyBars"
            @click="doJob('dailyBars')">执行</van-button>
        </template>
      </van-cell>
      <van-cell title="同步行情" label="逐只增量补漏；无历史代码会拉全历史（耗时较长）">
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
      <van-cell title="同步资金流" label="主力/超大单/大单净额与净占比（收盘官方口径）">
        <template #right-icon>
          <van-button size="small" :loading="loading.moneyflow"
            @click="doJob('moneyflow')">执行</van-button>
        </template>
      </van-cell>
    </van-cell-group>

    <van-cell-group inset title="选股评估">
      <van-cell title="同步基准指数" label="沪深300 / 中证500 / 中证1000 日线">
        <template #right-icon>
          <van-button size="small" :loading="loading.index"
            @click="doJob('index')">执行</van-button>
        </template>
      </van-cell>
      <van-cell title="选股快照落库" label="把当日 Top3 写入 picks，供绩效跟踪">
        <template #right-icon>
          <van-button size="small" :loading="loading.snapshot"
            @click="doJob('snapshot')">执行</van-button>
        </template>
      </van-cell>
      <van-cell title="历史快照回填" label="按历史交易日重算 Top3（约250日，耗时较长）">
        <template #right-icon>
          <van-button size="small" :loading="loading.backfill"
            @click="doJob('backfill')">执行</van-button>
        </template>
      </van-cell>
      <van-cell title="回填前向收益" label="计算 T+5/10/20/60 收益，已存在的不覆盖">
        <template #right-icon>
          <van-button size="small" :loading="loading.fwd"
            @click="doJob('fwd')">执行</van-button>
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
  syncQuotes, syncFundamentals, runScreen, calcZones, runIntraday, runMonitor,
  syncMoneyFlow, syncIndex, savePicksSnapshot, backfillPicks,
  updateForwardReturns, syncDailyBars
} from '../api'

const loading = reactive({
  quotes: false, fundamentals: false, screen: false,
  zones: false, intraday: false, monitor: false, moneyflow: false,
  index: false, snapshot: false, backfill: false, fwd: false,
  dailyBars: false
})
const result = ref('')

const runners = {
  dailyBars: { fn: syncDailyBars, loadingKey: 'dailyBars', name: '补当日日线' },
  quotes: { fn: syncQuotes, loadingKey: 'quotes', name: '同步行情' },
  fundamentals: { fn: syncFundamentals, loadingKey: 'fundamentals', name: '同步基本面' },
  screen: { fn: runScreen, loadingKey: 'screen', name: '选股筛选' },
  zones: { fn: calcZones, loadingKey: 'zones', name: '计算区间' },
  intraday: { fn: runIntraday, loadingKey: 'intraday', name: '盘中一键' },
  monitor: { fn: runMonitor, loadingKey: 'monitor', name: '监控提醒' },
  moneyflow: { fn: () => syncMoneyFlow(false), loadingKey: 'moneyflow', name: '同步资金流' },
  index: { fn: syncIndex, loadingKey: 'index', name: '同步基准指数' },
  snapshot: { fn: savePicksSnapshot, loadingKey: 'snapshot', name: '选股快照落库' },
  backfill: { fn: backfillPicks, loadingKey: 'backfill', name: '历史快照回填' },
  fwd: { fn: updateForwardReturns, loadingKey: 'fwd', name: '回填前向收益' }
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
  { time: '工作日 14:00', desc: '盘中链：快照→资金流→筛选→区间→监控，收盘前出结果' },
  { time: '工作日 15:40', desc: '收盘日线（一次快照写入全市场，秒级）' },
  { time: '工作日 15:45', desc: '逐只增量补漏（只修已有历史的缺口）' },
  { time: '工作日 15:50', desc: '基准指数同步（沪深300/中证500/中证1000）' },
  { time: '工作日 16:10', desc: '选股筛选' },
  { time: '工作日 16:15', desc: '安全区间计算' },
  { time: '工作日 16:20', desc: '资金流收盘官方数据（覆盖当日盘中记录）' },
  { time: '工作日 16:25', desc: '选股快照落库（Top3 写入 picks）' },
  { time: '工作日 16:30', desc: '回填前向收益（T+5/10/20/60）' },
  { time: '工作日 16:45', desc: '低估观察汇总（仅跌破下沿、未满足右侧确认）' },
  { time: '工作日 9:30-15:00 每30分钟', desc: '重点提醒（跌破下沿+MA20向上+相对基准超额）' },
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
