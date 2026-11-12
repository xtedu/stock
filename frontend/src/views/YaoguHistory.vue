<template>
  <div class="page">
    <van-nav-bar title="妖股历史" fixed :placeholder="true" left-arrow @click-left="goBack">
      <template #right>
        <van-button size="mini" plain :loading="backfilling" @click="doBackfill">回填历史</van-button>
      </template>
    </van-nav-bar>

    <!-- 日期选择 -->
    <van-cell-group inset>
      <van-cell title="交易日" :label="dates.length ? `共 ${dates.length} 个交易日，点击切换` : '暂无快照数据'">
        <template #value>
          <van-button size="mini" plain type="primary" :loading="loadingDates" @click="loadDates">刷新</van-button>
        </template>
      </van-cell>
      <div v-if="dates.length" class="date-strip">
        <div v-for="d in dates" :key="d.trade_date" class="date-chip"
             :class="{ active: d.trade_date === activeDate }"
             @click="loadDetail(d.trade_date)">
          <div class="dc-date">{{ d.trade_date.slice(5) }}</div>
          <div class="dc-cnt">{{ d.count }}只</div>
        </div>
      </div>
      <van-empty v-else description="还没有历史快照，点右上角回填" />
    </van-cell-group>

    <!-- 当日明细 -->
    <template v-if="detail">
      <van-row gutter="8" class="stat-row">
        <van-col span="6">
          <van-statistic title="涨停" :value="detail.stats.total_limit || 0" />
        </van-col>
        <van-col span="6">
          <van-statistic title="行业数" :value="detail.stats.industries || 0" />
        </van-col>
        <van-col span="6">
          <van-statistic title="最高板" :value="detail.stats.max_streak || 0" />
        </van-col>
        <van-col span="6">
          <van-statistic title="最强主线" :value="detail.stats.top_theme || '-'" />
        </van-col>
      </van-row>

      <!-- 行业涨停统计 -->
      <van-cell-group inset>
        <van-cell title="行业涨停统计" :label="detail.trade_date" />
        <div class="ind-stats">
          <div v-for="(g, i) in detail.ladder" :key="g.industry" class="ind-stat-row"
               @click="toggleIndustry(g.industry)">
            <span class="rank" :class="'r' + (i + 1)">{{ i + 1 }}</span>
            <span class="ind">{{ g.industry }}</span>
            <span class="cnt">{{ g.count }}只</span>
            <span v-if="g.max_streak >= 2" class="mx">{{ g.max_streak }}板</span>
            <span class="reasons">{{ topReasons(g) || '-' }}</span>
          </div>
        </div>
      </van-cell-group>

      <!-- 连板梯队明细 -->
      <van-cell-group inset style="margin-top:12px">
        <van-cell title="涨停明细（按行业）" label="点击行业展开个股与涨停原因" />
        <van-collapse v-model="activeLadder">
          <van-collapse-item v-for="g in detail.ladder" :key="g.industry" :name="g.industry">
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
                  <div v-if="sealInfo(s)" class="li-seal">{{ sealInfo(s) }}</div>
                </div>
              </div>
            </div>
          </van-collapse-item>
        </van-collapse>
      </van-cell-group>
    </template>

    <div class="footer">历史快照来自每日收盘数据，仅作复盘参考，不是投资建议</div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { showToast } from 'vant'
import { getYaoguHistory, getYaoguHistoryDetail, runYaoguBackfill } from '../api'

defineOptions({ name: 'YaoguHistory' })

const dates = ref([])
const detail = ref(null)
const activeDate = ref('')
const activeLadder = ref([])
const loadingDates = ref(false)
const backfilling = ref(false)

async function loadDates() {
  loadingDates.value = true
  try {
    const d = await getYaoguHistory(200)
    dates.value = d.dates || []
    if (dates.value.length) {
      const first = dates.value[0].trade_date
      if (!activeDate.value) {
        await loadDetail(first)
      }
    }
  } finally {
    loadingDates.value = false
  }
}

async function loadDetail(day) {
  activeDate.value = day
  const d = await getYaoguHistoryDetail(day)
  detail.value = d
  activeLadder.value = (d.ladder || []).slice(0, 2).map(g => g.industry)
}

async function doBackfill() {
  backfilling.value = true
  try {
    await runYaoguBackfill(365)
    showToast({ message: '已提交回填任务，约需数分钟，稍后点刷新', duration: 2500 })
  } catch (e) {
    showToast('回填任务提交失败')
  } finally {
    backfilling.value = false
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

function sealInfo(s) {
  const parts = []
  if (s.first_limit_time) parts.push(`首封 ${s.first_limit_time.slice(0, 5)}`)
  if (s.break_count !== null && s.break_count !== undefined) parts.push(`炸板 ${s.break_count} 次`)
  if (s.seal_amount_yi !== null && s.seal_amount_yi !== undefined) parts.push(`封单 ${s.seal_amount_yi}亿`)
  if (s.turnover_rate !== null && s.turnover_rate !== undefined) parts.push(`换手 ${s.turnover_rate}%`)
  return parts.join(' · ')
}

function goBack() {
  if (window.history.length > 1) {
    window.history.back()
  } else {
    window.location.href = '/yaogu'
  }
}

onMounted(loadDates)
</script>

<style scoped>
.page { padding-bottom: 24px; }

.date-strip { display: flex; gap: 8px; overflow-x: auto; padding: 4px 12px 12px; }
.date-chip { flex: none; min-width: 58px; text-align: center; border: 1px solid #e5e7eb; border-radius: 8px; padding: 6px 4px; background: #fff; }
.date-chip.active { border-color: #c0392b; background: #fdecea; }
.date-chip .dc-date { font-size: 12.5px; font-weight: 600; color: #333; }
.date-chip .dc-cnt { font-size: 11px; color: #c0392b; margin-top: 2px; }

.stat-row { padding: 8px; }
.stat-row :deep(.van-statistic) { text-align: center; }
.stat-row :deep(.van-statistic__value) { font-size: 18px; font-weight: 700; color: #c0392b; }

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
.li-seal { margin-top: 2px; font-size: 11px; color: #a86400; }

.footer { text-align: center; font-size: 11px; color: #aaa; margin-top: 20px; }
</style>
