<template>
  <div class="page">
    <van-nav-bar title="今日三只" left-arrow @click-left="goBack">
      <template #right>
        <div class="nav-right">
          <van-button size="mini" plain type="primary"
                      @click="$router.push('/picks/perf')">绩效</van-button>
          <van-button size="mini" plain type="primary" :loading="loading"
                      @click="load(true)">刷新</van-button>
        </div>
      </template>
    </van-nav-bar>

    <div class="intro">
      沪深 · 硬门槛后按「利润增速 70% + 营收增速 30%」分位排序 · 同一行业最多 2 只
    </div>

    <div v-if="ctx" class="regime" :class="ctx.market.regime">
      <div class="regime-head">
        <van-tag :type="regimeTag" plain>{{ ctx.market.label }}</van-tag>
        <span class="regime-meta">
          {{ ctx.date }} · 上涨 {{ ctx.market.adv_ratio }}% · 涨停 {{ ctx.market.limit_up }} ·
          成交额 {{ ctx.market.amount_yi }}亿 ({{ ctx.market.amount_ratio }}x)
        </span>
      </div>
      <div class="regime-note">{{ ctx.market.note }}</div>
      <div v-if="ctx.market.data_warning" class="regime-warn">
        {{ ctx.market.data_warning }}
      </div>
    </div>

    <van-loading v-if="loading && !items.length" class="loading" vertical>
      正在计算…
    </van-loading>

    <van-empty v-else-if="!items.length" description="暂无符合条件的标的" />

    <div v-for="item in items" :key="item.code" class="card"
         @click="$router.push(`/detail/${item.code}`)">
      <div class="card-head">
        <span class="rank">#{{ item.rank }}</span>
        <span class="name">{{ item.name }}</span>
        <span class="code">{{ item.code }}</span>
        <van-tag v-if="item.industry" plain type="primary" class="industry">
          {{ item.industry }}
        </van-tag>
      </div>

      <div class="price-row">
        <span class="price">{{ fmt(item.price) }}</span>
        <span :class="['chg', (item.change_pct || 0) >= 0 ? 'up' : 'down']">
          {{ signed(item.change_pct) }}%
        </span>
        <span class="score">得分 {{ item.score }}</span>
      </div>

      <div class="grid">
        <div class="cell">
          <div class="k">利润增速</div>
          <div class="v">{{ signed(item.profit_growth) }}%</div>
        </div>
        <div class="cell">
          <div class="k">营收增速</div>
          <div class="v">{{ signed(item.revenue_growth) }}%</div>
        </div>
        <div class="cell">
          <div class="k">近4期 ROE</div>
          <div class="v">{{ fmt(item.roe_avg) }}%</div>
        </div>
        <div class="cell">
          <div class="k">负债率</div>
          <div class="v">{{ fmt(item.debt_ratio) }}%</div>
        </div>
      </div>

      <div class="zone-row">
        <span>安全区间 {{ fmt(item.zone_low) }} ~ {{ fmt(item.zone_high) }}</span>
        <span :class="['zone', item.zone_status]">{{ zoneText(item.zone_status) }}</span>
      </div>

      <div v-if="indInfo(item.industry)" class="ind-row">
        行业20日 {{ signed(indInfo(item.industry).ret20) }}% ·
        <template v-if="indInfo(item.industry).flow !== null">
          资金占比 {{ fmt(indInfo(item.industry).flow) }}x ·
        </template>
        <template v-else>资金占比 数据不全 ·</template>
        今日涨停 {{ indInfo(item.industry).heat }} 家
      </div>

      <div class="tags">
        <van-tag v-for="t in item.tags" :key="t" :type="tagType(t)" plain>
          {{ t }}
        </van-tag>
      </div>
    </div>

    <div class="disclaimer">
      <template v-if="perf && perf.summary">
        前向跟踪自 {{ perf.summary.start }} 起，20 日非重叠样本
        {{ perf.summary.effective_periods }} 期：胜率 {{ pct(perf.summary.win_rate) }}，
        平均净超额 {{ signed(perf.summary.avg_excess_net) }}
        （已扣双边 {{ perf.cost_round_trip_pct }}% 成本）。
      </template>
      <template v-else>
        前向跟踪样本不足（需累计 20 个交易日才有 20 日收益），暂不披露胜率。
      </template>
      <span class="link" @click="$router.push('/picks/perf')">查看完整绩效 ›</span>
      页面内容不构成投资建议。
    </div>
  </div>
</template>

<script setup>
import { computed, onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getPickContext, getPickPerformance, getPicks } from '../api'

const router = useRouter()
const items = ref([])
const ctx = ref(null)
const perf = ref(null)
const loading = ref(false)
const loadedAt = ref(0)

const fmt = (v) => (v === null || v === undefined ? '—' : Number(v).toFixed(2))
const pct = (v) => (v === null || v === undefined ? '—' : `${Number(v).toFixed(2)}%`)
const signed = (v) => {
  if (v === null || v === undefined) return '—'
  return `${v > 0 ? '+' : ''}${Number(v).toFixed(2)}`
}
const zoneText = (s) => ({
  above: '高于上沿',
  below: '低于下沿',
  in: '区间内'
}[s] || '—')
const tagType = (t) => {
  if (t.includes('追高')) return 'danger'
  if (t.includes('领涨') || t.includes('涨停')) return 'warning'
  if (t.includes('MA20 向上')) return 'success'
  return 'primary'
}
const regimeTag = computed(() => ({
  offensive: 'danger',
  defensive: 'success',
  neutral: 'primary'
}[ctx.value?.market?.regime] || 'primary'))
const indInfo = (name) => (name && ctx.value?.industries?.[name]) || null

async function load(refresh = false) {
  if (loading.value) return
  loading.value = true
  try {
    const [p, c, pf] = await Promise.all([
      getPicks(3, refresh),
      getPickContext().catch(() => null),
      getPickPerformance('000852', 20).catch(() => null)
    ])
    items.value = p
    if (c) ctx.value = c
    if (pf) perf.value = pf
    loadedAt.value = Date.now()
  } finally {
    loading.value = false
  }
}

function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/pool')
}

onMounted(load)
onActivated(() => {
  if (Date.now() - loadedAt.value > 10 * 60 * 1000) load()
})
</script>

<style scoped>
.page {
  min-height: 100vh;
  background: #f5f6f8;
  padding-bottom: 24px;
}
.intro {
  padding: 10px 14px;
  font-size: 12px;
  color: #969799;
  line-height: 1.6;
}
.regime {
  margin: 0 12px 4px;
  padding: 10px 12px;
  background: #fff;
  border-radius: 10px;
  border-left: 3px solid #1989fa;
}
.regime.offensive {
  border-left-color: #ee0a24;
}
.regime.defensive {
  border-left-color: #07c160;
}
.regime-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.regime-meta {
  font-size: 11px;
  color: #969799;
}
.regime-note {
  margin-top: 6px;
  font-size: 12px;
  color: #646566;
  line-height: 1.5;
}
.regime-warn {
  margin-top: 6px;
  font-size: 11px;
  color: #ee0a24;
  line-height: 1.5;
}
.ind-row {
  margin-top: 6px;
  font-size: 11px;
  color: #969799;
}
.loading {
  padding: 60px 0;
}
.card {
  margin: 10px 12px;
  padding: 14px;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.04);
}
.card-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.rank {
  font-size: 13px;
  font-weight: 600;
  color: #ee0a24;
}
.name {
  font-size: 17px;
  font-weight: 600;
  color: #323233;
}
.code {
  font-size: 12px;
  color: #969799;
}
.industry {
  margin-left: auto;
}
.price-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 8px;
}
.price {
  font-size: 24px;
  font-weight: 600;
  color: #323233;
}
.chg {
  font-size: 14px;
}
.up {
  color: #ee0a24;
}
.down {
  color: #07c160;
}
.score {
  margin-left: auto;
  font-size: 12px;
  color: #969799;
}
.grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
  margin-top: 10px;
}
.cell {
  background: #f7f8fa;
  border-radius: 8px;
  padding: 6px 8px;
}
.k {
  font-size: 11px;
  color: #969799;
}
.v {
  margin-top: 2px;
  font-size: 13px;
  font-weight: 600;
  color: #323233;
}
.zone-row {
  display: flex;
  justify-content: space-between;
  margin-top: 10px;
  font-size: 12px;
  color: #646566;
}
.zone.below {
  color: #07c160;
}
.zone.above {
  color: #ee0a24;
}
.zone.in {
  color: #1989fa;
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}
.nav-right {
  display: flex;
  gap: 6px;
}
.disclaimer {
  margin: 16px 14px 0;
  font-size: 11px;
  line-height: 1.6;
  color: #969799;
}
.link {
  color: #1989fa;
  margin: 0 4px;
}
</style>
