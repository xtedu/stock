<template>
  <div class="page">
    <van-nav-bar :title="isHistory ? '对比历史' : '对比分析'"
                 left-arrow @click-left="onBack" />

    <!-- 历史列表 -->
    <template v-if="isHistory">
      <van-empty v-if="!loading && !history.length"
                 description="暂无对比记录，去股票池勾选股票发起对比" />
      <div v-for="h in history" :key="h.id" class="card link"
           @click="$router.push(`/compare/${h.id}`)">
        <div class="row main">
          <span class="name">{{ (h.codes || []).length }} 只 · {{ (h.codes || []).join(' ') }}</span>
          <van-icon name="arrow" color="#c8c9cc" />
        </div>
        <div class="row sub">
          <span class="gray">{{ fmtTime(h.created_at) }}</span>
        </div>
        <div v-if="h.ai_summary" class="overall">{{ h.ai_summary }}</div>
      </div>
    </template>

    <!-- 分析详情 -->
    <template v-else>
      <div v-if="loading" class="card">加载中…</div>
      <template v-else-if="rec">
        <div class="card">
          <div class="row main">
            <span class="name">分析时间</span>
            <span class="gray">{{ fmtTime(rec.created_at) }}</span>
          </div>
          <div class="overall-box">
            <div class="ov-title">大模型总体结论</div>
            <div class="ov-body">{{ rec.ai_summary || '大模型未返回结论（可能未配置 LLM_API_KEY）' }}</div>
            <div class="ov-note">以上为模型基于量化指标的定性判断，非精确概率预测，非投资建议。</div>
          </div>
        </div>

        <div class="card">
          <div class="sec-title">指标对比</div>
          <div class="table-wrap">
            <table class="cmp">
              <thead>
                <tr>
                  <th class="sticky">名称</th>
                  <th>现价</th>
                  <th>涨幅</th>
                  <th>区间位置</th>
                  <th>MA250乖离</th>
                  <th>得分</th>
                  <th>ROE</th>
                  <th>营收CAGR</th>
                  <th>净利CAGR</th>
                  <th>PE分位</th>
                  <th>主力净额(万)</th>
                  <th>主力占比</th>
                  <th>5日主力(万)</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="it in rec.items" :key="it.code">
                  <td class="sticky" @click="$router.push(`/detail/${it.code}`)">
                    <span class="n">{{ it.name }}</span>
                    <span class="c">{{ it.code }}</span>
                  </td>
                  <td>{{ fmt(it.price) }}</td>
                  <td :class="chgClass(it.change_pct)">{{ fmtPct(it.change_pct) }}</td>
                  <td>
                    <span class="zone" :class="it.zone_status">{{ zoneText(it.zone_status) }}</span>
                  </td>
                  <td>{{ fmtPct(it.ma250_dev) }}</td>
                  <td>{{ it.score ?? '-' }}</td>
                  <td>{{ num(it.roe_avg) }}</td>
                  <td>{{ num(it.rev_cagr_3y) }}</td>
                  <td>{{ num(it.profit_cagr_3y) }}</td>
                  <td>{{ it.pe_percentile != null ? it.pe_percentile + '%' : '-' }}</td>
                  <td :class="chgClass(it.main_net)">{{ wan(it.main_net) }}</td>
                  <td :class="chgClass(it.main_net)">{{ fmtPct(it.main_net_pct) }}</td>
                  <td :class="chgClass(it.main_net_5d)">{{ wan(it.main_net_5d) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="hint">区间：上方为安全区间上沿、下方为下沿（MA250 中枢 ±10%）</div>
          <div class="hint">资金流为东方财富按单笔成交金额推算口径（主力 = 超大单 + 大单净额），
            单位万元、当日累计值，非真实席位数据</div>
        </div>

        <div class="card">
          <div class="sec-title">AI 定性研判</div>
          <div v-for="it in rec.items" :key="it.code" class="ai-item"
               @click="$router.push(`/detail/${it.code}`)">
            <div class="row main">
              <span>
                <span class="n">{{ it.name }}</span>
                <span class="c">{{ it.code }}</span>
              </span>
              <span>
                <van-tag v-if="it.confidence" plain type="primary" class="mr">置信 {{ it.confidence }}</van-tag>
                <van-tag :type="viewType(it.view)">{{ it.view || '未判断' }}</van-tag>
              </span>
            </div>
            <div class="reason">{{ it.reason || '—' }}</div>
            <div class="row pts">
              <span>支撑 {{ fmt(it.support) }}</span>
              <span>压力 {{ fmt(it.resistance) }}</span>
              <span class="gray">现价 {{ fmt(it.price) }}</span>
            </div>
          </div>
        </div>
      </template>
      <van-empty v-else description="记录不存在" />
    </template>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getCompareDetail, getCompareHistory } from '../api'

const route = useRoute()
const router = useRouter()

const history = ref([])
const rec = ref(null)
const loading = ref(true)

const isHistory = computed(() => !route.params.id)

const fmt = (v) => (v == null ? '-' : Number(v).toFixed(2))
const fmtPct = (v) => (v == null ? '-' : `${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%`)
const num = (v) => (v == null ? '-' : `${Number(v).toFixed(1)}%`)
const wan = (v) => (v == null ? '-' : (v / 1e4).toFixed(0))
const chgClass = (v) => (v == null ? 'flat' : v > 0 ? 'up' : v < 0 ? 'down' : 'flat')

const ZONE = { above: '高于上沿', in: '区间内', below: '低估区' }
const zoneText = (s) => ZONE[s] || '-'
const viewType = (v) => (v === '看多' ? 'danger' : v === '看空' ? 'success' : 'default')

const fmtTime = (v) => {
  if (!v) return '-'
  const d = new Date(v)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}`
}

const load = async () => {
  loading.value = true
  rec.value = null
  try {
    if (isHistory.value) {
      history.value = await getCompareHistory(30)
    } else {
      rec.value = await getCompareDetail(route.params.id)
    }
  } finally {
    loading.value = false
  }
}

const onBack = () => {
  if (isHistory.value) router.push('/pool')
  else router.push('/compare')
}

watch(() => route.params.id, load, { immediate: true })
</script>

<style scoped>
.page { padding-bottom: 60px; }
.card {
  margin: 10px 12px;
  padding: 12px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
}
.card.link { cursor: pointer; }
.row { display: flex; align-items: center; }
.main { justify-content: space-between; }
.sub { margin-top: 6px; }
.name { font-weight: 600; }
.n { font-weight: 600; margin-right: 6px; }
.c { color: #999; font-size: 12px; }
.gray { color: #969799; font-size: 12px; }
.overall { margin-top: 8px; font-size: 13px; color: #323233; line-height: 1.5; }
.overall-box { margin-top: 10px; background: #f2f8ff; border-radius: 6px; padding: 10px; }
.ov-title { font-size: 13px; font-weight: 600; color: #1989fa; margin-bottom: 6px; }
.ov-body { font-size: 13px; line-height: 1.6; color: #323233; }
.ov-note { margin-top: 8px; font-size: 11px; color: #969799; }
.sec-title { font-size: 14px; font-weight: 600; margin-bottom: 10px; }
.table-wrap { overflow-x: auto; }
.cmp { border-collapse: collapse; font-size: 12px; white-space: nowrap; }
.cmp th, .cmp td { padding: 6px 8px; border-bottom: 1px solid #f2f3f5; text-align: right; }
.cmp th { color: #969799; font-weight: 500; }
.cmp td.sticky, .cmp th.sticky {
  position: sticky; left: 0; background: #fff; text-align: left; z-index: 1;
}
.zone { padding: 1px 4px; border-radius: 3px; font-size: 11px; }
.zone.above { color: #ed6a0c; background: #fffbe8; }
.zone.in { color: #07c160; background: #f0fdf5; }
.zone.below { color: #1989fa; background: #f2f8ff; }
.hint { margin-top: 8px; font-size: 11px; color: #969799; }
.up { color: #ee0a24; }
.down { color: #07c160; }
.flat { color: #969799; }
.ai-item { padding: 10px 0; border-bottom: 1px solid #f2f3f5; }
.ai-item:last-child { border-bottom: none; }
.reason { margin-top: 6px; font-size: 13px; color: #323233; line-height: 1.5; }
.pts { gap: 14px; margin-top: 6px; font-size: 12px; color: #646566; }
.mr { margin-right: 6px; }
</style>
