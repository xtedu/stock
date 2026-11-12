<template>
  <div class="page">
    <van-nav-bar title="妖股筛选" fixed :placeholder="true">
      <template #right>
        <van-button size="mini" plain @click="$router.push('/yaogu/history')">历史</van-button>
        <van-button size="mini" plain type="primary" :loading="loading"
                    style="margin-left:6px" @click="fetchData(true)">刷新</van-button>
      </template>
    </van-nav-bar>

    <!-- 统计 -->
    <van-row gutter="8" class="stat-row">
      <van-col span="6">
        <van-statistic title="今日涨停" :value="stats.total_limit_today || 0" />
      </van-col>
      <van-col span="6">
        <van-statistic title="候选池" :value="candidates.length" />
      </van-col>
      <van-col span="6">
        <van-statistic title="梯队行业" :value="ladder.length" />
      </van-col>
      <van-col span="6">
        <van-statistic title="最强主线" :value="stats.top_theme || '-'" />
      </van-col>
    </van-row>

    <!-- 行业涨停统计 -->
    <van-cell-group inset>
      <van-cell title="行业涨停统计" :label="latestDate">
        <template #value>
          <van-tag v-if="isLive" type="danger" plain>盘中实时</van-tag>
          <van-tag v-else type="primary" plain>收盘数据</van-tag>
        </template>
      </van-cell>
      <div v-if="ladder.length" class="ind-stats">
        <div v-for="(g, i) in ladder" :key="g.industry" class="ind-stat-row"
             @click="toggleIndustry(g.industry)">
          <span class="rank" :class="'r' + (i + 1)">{{ i + 1 }}</span>
          <span class="ind">{{ g.industry }}</span>
          <span class="cnt">{{ g.count }}只</span>
          <span v-if="g.max_streak >= 2" class="mx">{{ g.max_streak }}板</span>
          <span class="reasons">{{ topReasons(g) || '-' }}</span>
        </div>
      </div>
      <van-empty v-else description="今日涨停为空" />
    </van-cell-group>

    <!-- 连板梯队：每个行业下的涨停明细 -->
    <van-cell-group inset style="margin-top:12px">
      <van-cell title="连板梯队（按行业）" label="点击行业展开涨停个股与原因" />
      <template v-if="ladder.length">
        <van-collapse v-model="activeLadder">
          <van-collapse-item v-for="g in ladder" :key="g.industry" :name="g.industry">
            <template #title>
              <span class="ind-title">{{ g.industry }}</span>
              <span class="ind-count">{{ g.count }}只涨停</span>
              <span v-if="g.max_streak >= 2" class="ind-max">最高{{ g.max_streak }}板</span>
            </template>
            <div class="ladder-list">
              <div v-for="s in g.stocks" :key="s.code" class="ladder-item"
                   @click="$router.push('/detail/' + s.code)">
                <span class="streak-tag">
                  <span v-if="s.streak >= 3" class="s-hot">{{ s.streak }}板</span>
                  <span v-else-if="s.streak >= 2" class="s-mid">{{ s.streak }}板</span>
                  <span v-else class="s-low">1板</span>
                </span>
                <div class="li-main">
                  <div class="li-top">
                    <span class="name">{{ s.name }}</span>
                    <span class="code">{{ s.code }}</span>
                    <span class="close">{{ s.close }}</span>
                    <span class="amount">{{ s.amount_yi }}亿</span>
                    <span v-if="s.is_open_board === false" class="tag-yizi">一字</span>
                  </div>
                  <div class="li-reason">
                    <span class="rz-label">涨停原因</span>
                    <span class="rz-text">{{ s.reason || '未获取到' }}</span>
                  </div>
                </div>
              </div>
            </div>
          </van-collapse-item>
        </van-collapse>
      </template>
      <van-empty v-else description="今日涨停为空" />
    </van-cell-group>

    <!-- 候选池六维打分 -->
    <van-cell-group inset style="margin-top:12px">
      <van-cell title="六维打分候选池（≥70分）" />
      <div v-if="candidates.length" class="tbl-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th>#</th>
              <th>股票</th>
              <th class="num">现价</th>
              <th class="num">额(亿)</th>
              <th class="num">连板</th>
              <th class="num">20日%</th>
              <th class="num">振幅%</th>
              <th class="num">年内涨停</th>
              <th class="num">行业涨停</th>
              <th class="num">总分</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(c, i) in candidates" :key="c.code" @click="$router.push('/detail/' + c.code)">
              <td>{{ i + 1 }}</td>
              <td>
                <div class="nm">{{ c.name }}</div>
                <div class="cd">{{ c.code }} · {{ c.industry }}</div>
              </td>
              <td class="num">{{ c.close }}</td>
              <td class="num">{{ c.amount_yi }}</td>
              <td class="num">
                <span class="streak-badge" :class="badgeClass(c.streak)">{{ c.streak }}</span>
              </td>
              <td class="num" :class="c.ret20_pct > 0 ? 'up' : 'down'">{{ c.ret20_pct }}</td>
              <td class="num">{{ c.amplitude_pct }}</td>
              <td class="num">{{ c.limit_count_1y }}</td>
              <td class="num">{{ c.industry_limit_count }}</td>
              <td class="num total">{{ c.score }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <van-empty v-else description="候选池为空" />
    </van-cell-group>

    <van-cell-group inset style="margin-top:12px">
      <van-cell title="六维打分明细（Top3）" />
      <div v-if="candidates.length" class="score-cards">
        <div v-for="(c, i) in candidates.slice(0, 3)" :key="c.code" class="score-card">
          <div class="sc-head">
            <span class="rank" :class="['r'+(i+1)]">#{{ i + 1 }}</span>
            <span class="name">{{ c.name }}</span>
            <span class="total">{{ c.score }}</span>
          </div>
          <div class="sc-grid">
            <div v-for="(v, k) in c.scores" :key="k" class="sc-item">
              <div class="sc-k">{{ scoreKeyName(k) }}</div>
              <div class="sc-bar">
                <div class="sc-bar-fill" :style="{ width: (v / scoreMax(k) * 100) + '%' }" :class="scoreColor(v, scoreMax(k))"></div>
                <span class="sc-v">{{ v }}</span>
              </div>
            </div>
          </div>
          <div class="sc-meta">
            <span>{{ c.code }} · {{ c.industry }}</span>
            <span>{{ c.close }}元 · {{ c.amount_yi }}亿</span>
            <span>连板 {{ c.streak }} · 20日 {{ c.ret20_pct }}%</span>
            <span>换手板: {{ c.is_open_board ? '是' : '否' }}</span>
          </div>
        </div>
      </div>
    </van-cell-group>

    <div class="footer">数据仅作妖股候选参考，不是投资建议</div>
  </div>
</template>

<script setup>
import { onActivated, onMounted, ref } from 'vue'
import { getYaogu } from '../api'

// 供 keep-alive include 匹配，从详情页返回时保留页面状态不重新请求
defineOptions({ name: 'Yaogu' })

const loading = ref(false)
const candidates = ref([])
const ladder = ref([])
const stats = ref({})
const latestDate = ref('')
const isLive = ref(false)
const activeLadder = ref([])

async function fetchData(forceRefresh = false) {
  loading.value = true
  try {
    const d = await getYaogu(50, forceRefresh)
    candidates.value = d.candidates || []
    // 后端已按涨停家数降序返回 [{industry, count, max_streak, stocks}]
    ladder.value = Array.isArray(d.ladder) ? d.ladder : []
    stats.value = d.stats || {}
    latestDate.value = d.latest_quote_date || d.batch_date || ''
    isLive.value = d.data_mode === 'intraday'
    // 默认展开涨停家数最多的前 2 个行业
    activeLadder.value = ladder.value.slice(0, 2).map(g => g.industry)
    lastFetch = Date.now()
  } finally {
    loading.value = false
  }
}

function toggleIndustry(name) {
  const i = activeLadder.value.indexOf(name)
  if (i >= 0) {
    activeLadder.value.splice(i, 1)
  } else {
    activeLadder.value.push(name)
  }
}

// 行业涨停原因聚合：把个股原因按 "+" 拆成题材词，取出现最多的前 3 个
function topReasons(g) {
  const cnt = {}
  ;(g.stocks || []).forEach(s => {
    if (!s.reason) return
    s.reason.split('+').map(t => t.trim()).filter(Boolean).slice(0, 2)
      .forEach(t => { cnt[t] = (cnt[t] || 0) + 1 })
  })
  return Object.entries(cnt)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([t]) => t)
    .join(' · ')
}

const scoreKeyName = (k) => ({
  price: '价格', float_mv: '流通市值', theme: '题材', chip: '筹码',
  turnover: '换手率', seal: '封单比', position: '位置板型'
}[k] || k)

const scoreMax = (k) => ({
  price: 15, float_mv: 20, theme: 15, chip: 10,
  turnover: 15, seal: 15, position: 10
}[k] || 15)

const scoreColor = (v, max) => {
  const pct = v / max
  if (pct >= 0.8) return 'c-red'
  if (pct >= 0.5) return 'c-yellow'
  return 'c-gray'
}

const badgeClass = (n) => n >= 3 ? 'b-hot' : n >= 2 ? 'b-mid' : 'b-low'

let lastFetch = 0
onMounted(() => fetchData())
// keep-alive 返回时若数据已过期（>2 分钟）则静默刷新，避免看到旧盘的空候选池
onActivated(() => {
  // 首次激活与 onMounted 同时触发，跳过以免重复请求
  if (lastFetch && Date.now() - lastFetch > 120000) fetchData()
})
</script>

<style scoped>
.page { padding-bottom: 24px; }
.stat-row { padding: 8px; }
.stat-row :deep(.van-statistic) { text-align: center; }
.stat-row :deep(.van-statistic__value) { font-size: 18px; font-weight: 700; color: #c0392b; }

/* 行业涨停统计 */
.ind-stats { padding: 2px 12px 8px; }
.ind-stat-row { display: flex; align-items: center; gap: 6px; padding: 7px 0; font-size: 13px; border-top: 1px solid #f2f3f5; }
.ind-stat-row:first-child { border-top: none; }
.ind-stat-row .rank { flex: none; width: 18px; height: 18px; line-height: 18px; text-align: center; border-radius: 50%; background: #dfe3e8; color: #fff; font-size: 11px; font-weight: 700; }
.ind-stat-row .rank.r1 { background: #c0392b; }
.ind-stat-row .rank.r2 { background: #e08b00; }
.ind-stat-row .rank.r3 { background: #7a869a; }
.ind-stat-row .ind { flex: none; max-width: 96px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.ind-stat-row .cnt { flex: none; color: #c0392b; font-weight: 700; }
.ind-stat-row .mx { flex: none; background: #fdecea; color: #c0392b; font-size: 11px; padding: 1px 5px; border-radius: 3px; }
.ind-stat-row .reasons { flex: 1; min-width: 0; color: #888; font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; }

/* 梯队标题 */
.ind-title { font-weight: 600; }
.ind-count { margin-left: 6px; color: #c0392b; font-weight: 700; font-size: 12px; }
.ind-max { margin-left: 6px; background: #fdecea; color: #c0392b; font-size: 11px; padding: 1px 5px; border-radius: 3px; }

.ladder-list { padding: 4px 0; }
.ladder-item { display: flex; align-items: flex-start; gap: 8px; padding: 7px 2px; font-size: 13px; border-top: 1px solid #f5f6f8; }
.ladder-item:first-child { border-top: none; }
.ladder-item .streak-tag { flex: none; min-width: 32px; text-align: center; padding-top: 1px; }
.streak-tag .s-hot { background: #c0392b; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
.streak-tag .s-mid { background: #e08b00; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
.streak-tag .s-low { background: #7a869a; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
.li-main { flex: 1; min-width: 0; }
.li-top { display: flex; align-items: center; gap: 6px; }
.li-top .name { font-weight: 600; }
.li-top .code { color: #888; font-size: 12px; }
.li-top .close { margin-left: auto; font-weight: 600; }
.li-top .amount { color: #888; font-size: 12px; }
.li-top .tag-yizi { background: #fdf1e0; color: #a86400; font-size: 11px; padding: 1px 5px; border-radius: 3px; }
.li-reason { margin-top: 3px; font-size: 11.5px; color: #666; line-height: 1.5; }
.li-reason .rz-label { display: inline-block; background: #f0f2f5; color: #7a869a; padding: 0 4px; border-radius: 2px; margin-right: 4px; font-size: 11px; }
.li-reason .rz-text { word-break: break-all; }

.tbl-wrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.tbl th { background: #f3f4f6; padding: 8px 6px; font-weight: 600; text-align: left; white-space: nowrap; position: sticky; top: 0; }
.tbl td { padding: 8px 6px; border-top: 1px solid #eee; white-space: nowrap; }
.tbl tr:hover { background: #fafbfc; }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.tbl .up { color: #c0392b; }
.tbl .down { color: #0a8043; }
.tbl .nm { font-weight: 600; }
.tbl .cd { color: #888; font-size: 11.5px; }
.tbl .total { font-weight: 800; color: #c0392b; font-size: 14px; }
.streak-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-weight: 700; font-size: 12px; }
.streak-badge.b-hot { background: #c0392b; color: #fff; }
.streak-badge.b-mid { background: #e08b00; color: #fff; }
.streak-badge.b-low { background: #7a869a; color: #fff; }

.score-cards { padding: 8px; }
.score-card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }
.sc-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.sc-head .rank { font-weight: 800; color: #fff; padding: 2px 8px; border-radius: 12px; font-size: 12px; }
.sc-head .r1 { background: #c0392b; } .sc-head .r2 { background: #e08b00; } .sc-head .r3 { background: #7a869a; }
.sc-head .name { font-weight: 700; font-size: 14px; }
.sc-head .total { margin-left: auto; font-size: 20px; font-weight: 800; color: #c0392b; }
.sc-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 14px; margin-bottom: 8px; }
.sc-item { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.sc-item .sc-k { color: #666; width: 64px; flex: none; }
.sc-bar { position: relative; flex: 1; height: 14px; background: #f0f2f5; border-radius: 3px; }
.sc-bar-fill { height: 100%; border-radius: 3px; min-width: 2px; }
.sc-bar-fill.c-red { background: #c0392b; }
.sc-bar-fill.c-yellow { background: #e08b00; }
.sc-bar-fill.c-gray { background: #b0b6c2; }
.sc-v { position: absolute; right: 4px; top: -1px; font-size: 11px; color: #333; font-weight: 600; }
.sc-meta { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 11.5px; color: #888; }

.footer { text-align: center; font-size: 11px; color: #aaa; margin-top: 20px; }
</style>
