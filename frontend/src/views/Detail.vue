<template>
  <div class="page">
    <van-nav-bar :title="detail.stock ? `${detail.stock.name} ${detail.stock.code}` : '个股'"
                 left-arrow @click-left="$router.back()" />
    <div ref="chartEl" style="width:100%;height:320px;"></div>

    <van-cell-group v-if="zone" inset title="当前状态">
      <van-cell title="最新价" :value="fmt(detail.price)" />
      <van-cell title="安全区间" :value="`${zone.zone_low} ~ ${zone.zone_high}`" />
      <van-cell title="判定结论">
        <template #value>
          <van-tag :type="statusType">
            {{ statusText }}
          </van-tag>
        </template>
      </van-cell>
      <van-cell title="计算日期" :value="zone.calc_date" />
    </van-cell-group>

    <van-cell-group v-if="zone" inset title="区间判定依据">
      <van-cell title="① 价值中枢（MA250）" :value="fmtNum(d.center_ma250)" />
      <van-cell title="② PE 5年分位" :value="pePctText" />
      <van-cell title="预警线（布林下轨）" :value="fmtNum(d.warn_line)" />
      <van-cell title="参考：估值分位带" :value="valuationText" />
      <van-cell title="参考：价格分位带" :value="pricePctText" />
      <van-cell title="最终采用" :value="methodText" />
      <van-cell title="数据来源" :value="d.source === 'intraday' ? '盘中快照' : '收盘数据'" />
      <van-cell title="说明" :label="methodNote" />
    </van-cell-group>

    <van-cell-group v-if="pool" inset :title="`入选优质股池依据（评分 ${pool.score}）`">
      <van-cell v-for="row in scoreRows" :key="row.label"
                :title="row.label" :value="row.value" />
    </van-cell-group>

    <van-empty v-if="!zone && !loading" description="该股暂无安全区间数据（未入选股池）" />
  </div>
</template>

<script setup>
import * as echarts from 'echarts'
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { getStockDetail } from '../api'

const route = useRoute()
const chartEl = ref(null)
const detail = ref({})
const loading = ref(true)

const zone = computed(() => detail.value.zone)
const pool = computed(() => detail.value.pool)
const d = computed(() => zone.value?.detail || {})
const r = computed(() => pool.value?.reason || {})
const sc = computed(() => r.value.scores || {})

const fmt = (v) => (v == null ? '-' : String(v))
const fmtNum = (v) => (v == null ? '无数据' : String(v))
const pct = (v) => (v == null ? '无数据' : `${v}%`)

const statusText = computed(() => {
  const p = detail.value.price
  if (p == null || !zone.value) return '价格缺失'
  if (p > zone.value.zone_high) return '高于区间上沿，等待回落'
  if (p < zone.value.zone_low) return '低于区间下沿（低估区，可关注）'
  return '已在安全区间内'
})

const statusType = computed(() => {
  const p = detail.value.price
  if (p == null || !zone.value) return 'default'
  if (p > zone.value.zone_high) return 'warning'
  if (p < zone.value.zone_low) return 'primary'
  return 'success'
})

const valuationText = computed(() => {
  const v = d.value.valuation
  return Array.isArray(v) ? `${v[0]} ~ ${v[1]}` : '未参与（该股估值历史不足）'
})
const pricePctText = computed(() => {
  const v = d.value.price_pct
  return Array.isArray(v) ? `${v[0]} ~ ${v[1]}` : '未参与'
})

const pePctText = computed(() => {
  const v = d.value.pe_percentile
  if (v == null) return '无数据'
  return `${Number(v).toFixed(1)}%${d.value.pe_squeeze ? '（偏贵，上沿已压缩至中枢）' : ''}`
})

const METHOD_LABEL = {
  center_anchor: '中枢锚定（MA250 中枢 ±10%）',
  fusion: '三法融合（历史口径）',
  price_pct_fallback: '价格分位带（历史口径）',
}
const methodText = computed(() => METHOD_LABEL[zone.value?.method] || zone.value?.method || '-')
const methodNote = computed(() => {
  if (zone.value?.method === 'center_anchor') {
    return '区间以 MA250 价值中枢为锚：下限 = 中枢×0.90，上限 = 中枢×1.10；' +
      '当前 PE 分位偏高时上沿压缩至中枢。现价高于上沿宜等待回落；' +
      '低于下沿说明比参考带更便宜，可结合预警线（布林下轨）分批关注。'
  }
  const used = (d.value.used || []).join(' + ')
  return `参与计算：${used || '-'}。区间为“值得关注的低吸参考带”，` +
    `现价高于上沿时应等待回落，而非直接买入。`
})

const scoreRows = computed(() => {
  const w = { roe: 35, rev: 15, profit: 15, cash: 15, pe_pct: 20 }
  return [
    { label: `ROE 均值（权重${w.roe}%）`, value: `${pct(r.value.roe_avg)}｜得分 ${sc.value.roe ?? '-'}` },
    { label: `营收 3年CAGR（权重${w.rev}%）`, value: `${pct(r.value.rev_cagr_3y)}｜得分 ${sc.value.rev ?? '-'}` },
    { label: `利润 3年CAGR（权重${w.profit}%）`, value: `${pct(r.value.profit_cagr_3y)}｜得分 ${sc.value.profit ?? '-'}` },
    { label: `现金流质量（权重${w.cash}%）`, value: `${pct(r.value.cash_quality)}｜得分 ${sc.value.cash ?? '-'}` },
    { label: `PE 分位（权重${w.pe_pct}%）`, value: `${r.value.pe_percentile != null ? r.value.pe_percentile + '%' : '无数据（按中性50计）'}｜得分 ${sc.value.pe_pct ?? '-'}` },
    { label: '资产负债率（门槛 ≤60%）', value: pct(r.value.debt_ratio) },
    { label: '最新报告期', value: r.value.report_date || '-' },
    { label: '数据来源', value: r.value.source === 'intraday' ? '盘中快照' : '收盘数据' },
  ]
})

onMounted(async () => {
  try {
    detail.value = await getStockDetail(route.params.code)
  } finally {
    loading.value = false
  }
  const chart = echarts.init(chartEl.value)
  const quotes = detail.value.quotes || []
  const dates = quotes.map(q => q.trade_date)
  const prices = quotes.map(q => q.close)
  const series = [{ type: 'line', data: prices, showSymbol: false, name: '收盘价' }]
  const z = detail.value.zone
  if (z && z.zone_low != null && z.zone_high != null) {
    series[0].markArea = {
      itemStyle: { color: 'rgba(7,193,96,0.12)' },
      data: [[{ yAxis: z.zone_low }, { yAxis: z.zone_high }]]
    }
  }
  chart.setOption({
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: dates },
    yAxis: { scale: true },
    dataZoom: [{ type: 'inside' }],
    series
  })
})
</script>

<style scoped>
.page { padding-bottom: 60px; }
</style>
