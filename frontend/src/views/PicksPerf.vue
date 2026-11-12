<template>
  <div class="page">
    <van-nav-bar title="策略绩效" left-arrow @click-left="goBack">
      <template #right>
        <van-button size="mini" plain type="primary" :loading="loading"
                    @click="load">刷新</van-button>
      </template>
    </van-nav-bar>

    <div class="filters">
      <div class="row">
        <van-button v-for="b in benchmarks" :key="b.code" size="mini"
                    :type="b.code === bm ? 'primary' : 'default'"
                    @click="setBenchmark(b.code)">{{ b.name }}</van-button>
      </div>
      <div class="row">
        <van-button v-for="w in windows" :key="w" size="mini"
                    :type="w === win ? 'primary' : 'default'"
                    @click="setWindow(w)">{{ w }}日</van-button>
      </div>
    </div>

    <van-loading v-if="loading && !data" class="loading" vertical>
      正在统计…
    </van-loading>
    <van-empty v-else-if="!summary" :description="emptyText" />

    <template v-else>
      <div class="cards">
        <div class="c">
          <div class="k">有效样本</div>
          <div class="v">{{ summary.effective_periods }} 期</div>
        </div>
        <div class="c">
          <div class="k">胜率</div>
          <div class="v">{{ pct(summary.win_rate) }}</div>
        </div>
        <div class="c">
          <div class="k">平均净超额</div>
          <div class="v" :class="signCls(summary.avg_excess_net)">
            {{ signed(summary.avg_excess_net) }}
          </div>
        </div>
        <div class="c">
          <div class="k">盈亏比</div>
          <div class="v">{{ summary.payoff ?? '—' }}</div>
        </div>
        <div class="c">
          <div class="k">最大回撤</div>
          <div class="v down">{{ pct(summary.max_drawdown) }}</div>
        </div>
        <div class="c">
          <div class="k">累计净超额</div>
          <div class="v" :class="signCls(summary.cum_excess_net)">
            {{ signed(summary.cum_excess_net) }}
          </div>
        </div>
      </div>

      <div class="meta">
        {{ summary.start }} ~ {{ summary.end }} · 持有 {{ data.window }} 日 ·
        基准 {{ data.benchmark_name }} · 已扣双边 {{ data.cost_round_trip_pct }}% 成本
      </div>

      <div ref="chartEl" class="chart"></div>

      <div class="note">
        口径：每 {{ data.window }} 个交易日取 1 期非重叠样本，避免重叠窗口虚高 t 值；
        成本按每期双边 {{ data.cost_round_trip_pct }}% 扣减（佣金 0.025%×2 + 印花税 0.05% + 滑点 0.03%×2）。
        t={{ summary.t_stat ?? '—' }}。历史回填样本仍含幸存者偏差，仅供横向比较。
        页面内容不构成投资建议。
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { getPickPerformance } from '../api'

const router = useRouter()
const benchmarks = [
  { code: '000852', name: '中证1000' },
  { code: '000905', name: '中证500' },
  { code: '000300', name: '沪深300' }
]
const windows = [5, 10, 20, 60]
const bm = ref('000852')
const win = ref(20)
const data = ref(null)
const loading = ref(false)
const chartEl = ref(null)
let chart = null

const summary = computed(() => data.value?.summary || null)
const emptyText = computed(() => data.value?.reason === 'benchmark data missing'
  ? '基准指数数据缺失，请先同步指数行情'
  : '前向样本不足：需要至少 20 个交易日后的收益才能统计')

const pct = (v) => (v === null || v === undefined ? '—' : `${Number(v).toFixed(2)}%`)
const signed = (v) => (v === null || v === undefined ? '—'
  : `${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%`)
const signCls = v => ({ up: v > 0, down: v < 0 })

function render() {
  if (!chartEl.value || !data.value?.curve?.length) return
  if (!chart) chart = echarts.init(chartEl.value)
  const c = data.value.curve
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['策略', data.value.benchmark_name, '超额'], top: 0 },
    grid: { left: 48, right: 12, top: 36, bottom: 28 },
    xAxis: { type: 'category', data: c.map(p => p.date), axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', axisLabel: { formatter: '{value}%' } },
    series: [
      { name: '策略', type: 'line', showSymbol: false, data: c.map(p => p.strategy) },
      { name: data.value.benchmark_name, type: 'line', showSymbol: false,
        data: c.map(p => p.bench), lineStyle: { type: 'dashed' } },
      { name: '超额', type: 'bar', data: c.map(p => p.excess),
        itemStyle: { color: 'rgba(25,137,250,0.45)' } }
    ]
  }, true)
}

async function load() {
  if (loading.value) return
  loading.value = true
  try {
    data.value = await getPickPerformance(bm.value, win.value)
    await nextTick()
    render()
  } finally {
    loading.value = false
  }
}

function setBenchmark(code) {
  if (bm.value === code) return
  bm.value = code
  load()
}

function setWindow(w) {
  if (win.value === w) return
  win.value = w
  load()
}

function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/picks')
}

onMounted(load)
onBeforeUnmount(() => chart?.dispose())
</script>

<style scoped>
.page {
  min-height: 100vh;
  background: #f5f6f8;
  padding-bottom: 24px;
}
.filters {
  padding: 8px 12px;
  background: #fff;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.row {
  display: flex;
  gap: 8px;
}
.loading {
  padding: 60px 0;
}
.cards {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin: 10px 12px;
}
.c {
  background: #fff;
  border-radius: 10px;
  padding: 10px;
}
.k {
  font-size: 11px;
  color: #969799;
}
.v {
  margin-top: 4px;
  font-size: 15px;
  font-weight: 600;
  color: #323233;
}
.up {
  color: #ee0a24;
}
.down {
  color: #07c160;
}
.meta {
  margin: 0 12px;
  font-size: 11px;
  color: #969799;
  line-height: 1.6;
}
.chart {
  height: 300px;
  margin: 10px 12px;
  background: #fff;
  border-radius: 12px;
}
.note {
  margin: 0 14px;
  font-size: 11px;
  line-height: 1.6;
  color: #969799;
}
</style>
