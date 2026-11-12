<template>
  <div class="page">
    <van-nav-bar title="消息面" fixed :placeholder="true">
      <template #right>
        <van-button size="mini" plain type="primary" :loading="loading"
                    @click="refresh">刷新</van-button>
      </template>
    </van-nav-bar>

    <!-- 未来事件日历 -->
    <van-cell-group inset class="cal">
      <van-cell title="未来 10 个交易日" label="尚未发生的事件，用来提前布局或规避">
        <template #right-icon>
          <van-tag v-if="poolCalCount" type="danger" plain>池内 {{ poolCalCount }} 条</van-tag>
        </template>
      </van-cell>
      <div class="cal-body">
        <div v-for="g in calGroups" :key="g.date" class="cal-day">
          <div class="cal-date">{{ g.date.slice(5) }}<em>{{ weekday(g.date) }}</em></div>
          <div class="cal-items">
            <div v-for="(e, i) in g.items" :key="i" class="cal-row"
                 :class="{ hot: e.related_codes && e.related_codes.length }">
              <van-tag plain :type="kindType(e.kind)">{{ kindText(e.kind) }}</van-tag>
              <span class="cal-title">{{ e.title }}</span>
              <van-tag v-if="e.importance >= 3" type="danger" plain>高</van-tag>
              <span v-if="calExtra(e)" class="cal-extra">{{ calExtra(e) }}</span>
            </div>
          </div>
        </div>
        <van-empty v-if="!calGroups.length" image-size="60"
                   description="暂无日历数据，点刷新抓取" />
      </div>
    </van-cell-group>

    <!-- 日期 + 分类 -->
    <van-cell-group inset class="filter">
      <van-cell title="资讯日期" center>
        <template #value>
          <div class="chips">
            <span v-for="d in dates" :key="d.date" class="chip"
                  :class="{ on: d.date === day }" @click="pickDate(d.date)">
              {{ d.date.slice(5) }}<em>{{ d.count }}</em>
            </span>
            <span v-if="!dates.length" class="chip on">{{ day.slice(5) }}</span>
          </div>
        </template>
      </van-cell>
      <van-cell title="类型" center>
        <template #value>
          <van-radio-group v-model="fCat" direction="horizontal" @change="load">
            <van-radio name="">全部</van-radio>
            <van-radio name="policy">政策</van-radio>
            <van-radio name="macro">宏观</van-radio>
            <van-radio name="company">公告</van-radio>
            <van-radio name="general">快讯</van-radio>
          </van-radio-group>
        </template>
      </van-cell>
      <van-cell :title="`共 ${items.length} 条`"
                :label="briefTime ? `简报生成于 ${briefTime}` : ''" />
    </van-cell-group>

    <!-- AI 决策简报 -->
    <van-cell-group inset class="brief">
      <van-cell title="AI 研判：利好是否已兑现" />
      <div v-if="brief" class="brief-body">
        <div class="sentiment" :class="sentimentClass">
          <span class="st">{{ brief.sentiment || '中性' }}</span>
          <span class="ov">{{ brief.overall }}</span>
        </div>

        <div v-if="brief.ahead?.length" class="block">
          <div class="block-t">未来事件与预期差</div>
          <div v-for="(a, i) in brief.ahead" :key="i" class="ev-row">
            <span class="ev-date">{{ a.date?.slice(5) }}</span>
            <van-tag :type="dirType(a.impact)" plain>{{ a.impact }}</van-tag>
            <span class="ev-txt"><b>{{ a.event }}</b> · {{ a.note }}</span>
          </div>
        </div>

        <div v-if="brief.priced_in?.length" class="block">
          <div class="block-t warn">已兑现：别追</div>
          <div v-for="p in brief.priced_in" :key="p.code" class="row-item"
               @click="$router.push('/detail/' + p.code)">
            <van-tag type="danger" plain>兑现</van-tag>
            <span class="nm">{{ p.name }}</span>
            <span class="cd">{{ p.code }}</span>
            <span class="rs">{{ p.reason }}</span>
          </div>
        </div>

        <div v-if="brief.unpriced?.length" class="block">
          <div class="block-t good">未发酵：预期差在这里</div>
          <div v-for="p in brief.unpriced" :key="p.code" class="row-item"
               @click="$router.push('/detail/' + p.code)">
            <van-tag type="success" plain>未反应</van-tag>
            <span class="nm">{{ p.name }}</span>
            <span class="cd">{{ p.code }}</span>
            <span class="rs">{{ p.reason }}</span>
          </div>
        </div>

        <div v-if="brief.risks?.length" class="block risk">
          <div class="block-t">风险提示</div>
          <div v-for="(r, i) in brief.risks" :key="i" class="risk-row">· {{ r }}</div>
        </div>
        <div class="disclaim">以上为消息面定性研判，非投资建议</div>
      </div>
      <van-empty v-else :description="briefReason || '暂无简报，点击右上角刷新生成'" />
    </van-cell-group>

    <!-- 资讯流 -->
    <van-cell-group inset class="feed">
      <van-cell title="政策 / 快讯 / 公告"
                label="来源：财联社 · 东财 · 新浪 · 新闻联播 · 公告" />
      <div v-for="n in items" :key="n.id" class="news" @click="openUrl(n.url)">
        <div class="n-head">
          <van-tag plain :type="catType(n.category)">{{ catText(n.category) }}</van-tag>
          <van-tag v-if="digestState(n)" :type="digestType(n)" plain>
            {{ digestText(n) }}
          </van-tag>
          <span class="src">{{ sourceName(n.source) }}</span>
          <span class="time">{{ fmtTime(n.published_at) }}</span>
        </div>
        <div class="n-title">{{ n.title }}</div>
        <div v-if="n.summary" class="n-sum">{{ n.summary }}</div>
        <div v-if="n.related_codes?.length" class="n-rel">
          <van-tag v-for="c in n.related_codes" :key="c" type="danger" plain>
            池内 {{ c }}{{ digestHint(n, c) }}
          </van-tag>
        </div>
      </div>
      <van-empty v-if="!loading && !items.length"
                 description="当日暂无消息，点击右上角刷新抓取" />
    </van-cell-group>

    <div class="footer">数据仅作参考，不构成投资建议</div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  getNews, getNewsBrief, getNewsCalendar, getNewsDates, refreshNews
} from '../api'

// 供 keep-alive include 匹配
defineOptions({ name: 'News' })

const today = () => {
  const d = new Date()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${dd}`
}

const loading = ref(false)
const day = ref(today())
const dates = ref([])
const items = ref([])
const brief = ref(null)
const briefReason = ref('')
const briefTime = ref('')
const fCat = ref('')
const calendar = ref([])

const SOURCE_NAME = {
  cls: '财联社', em: '东财快讯', sina: '新浪快讯',
  cctv: '新闻联播', notice: '公司公告'
}
const sourceName = (s) => SOURCE_NAME[s] || s

const CAT_TEXT = { policy: '政策', macro: '宏观', company: '公告', general: '快讯' }
const catText = (c) => CAT_TEXT[c] || '快讯'
const catType = (c) => c === 'policy' ? 'danger'
  : c === 'macro' ? 'warning' : c === 'company' ? 'primary' : 'default'

const dirType = (d) => d === '利好' ? 'danger' : d === '利空' ? 'success' : 'default'
const sentimentClass = computed(() => {
  const s = brief.value?.sentiment
  return s === '偏多' ? 'bull' : s === '偏空' ? 'bear' : 'flat'
})

// ---- 日历 ----
const KIND_TEXT = { macro: '宏观', release: '解禁', forecast: '预告' }
const kindText = (k) => KIND_TEXT[k] || k
const kindType = (k) => k === 'release' ? 'danger' : k === 'forecast' ? 'warning' : 'primary'

const weekday = (d) => ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][
  new Date(d).getDay()]

const calExtra = (e) => {
  const d = e.detail || {}
  if (e.kind === 'macro') {
    const parts = []
    if (d.forecast != null) parts.push(`预期 ${d.forecast}`)
    if (d.previous != null) parts.push(`前值 ${d.previous}`)
    return parts.join(' / ')
  }
  if (e.kind === 'release') return `占比 ${d.ratio}%`
  if (e.kind === 'forecast') return `${d.type || ''} ${d.amplitude != null ? d.amplitude + '%' : ''}`
  return ''
}

const calGroups = computed(() => {
  const map = {}
  for (const e of calendar.value) (map[e.date] ||= []).push(e)
  return Object.entries(map)
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([date, list]) => ({ date, items: list }))
})

const poolCalCount = computed(
  () => calendar.value.filter(e => e.related_codes?.length).length)

// ---- 消化度 ----
const DIGEST_TEXT = { priced_in: '已兑现', fermenting: '发酵中', oversold: '错杀' }
const digestState = (n) => {
  const dg = n.digest || {}
  return Object.values(dg)[0]?.state || ''
}
const digestText = (n) => DIGEST_TEXT[digestState(n)] || ''
const digestType = (n) => {
  const s = digestState(n)
  return s === 'priced_in' ? 'danger' : s === 'oversold' ? 'success' : 'default'
}
const digestHint = (n, code) => {
  const d = (n.digest || {})[code]
  if (!d) return ''
  const z = d.ret5_z != null ? `≈${d.ret5_z}σ` : ''
  const mv = d.float_mv_yi != null ? ` · ${d.float_mv_yi}亿` : ''
  return ` 前5日${d.ret5 > 0 ? '+' : ''}${d.ret5}%${z}${mv} / 阈值${d.priced_th}%`
}

const fmtTime = (t) => {
  if (!t) return ''
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return ''
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')} ${hh}:${mm}`
}

const loadDates = async () => {
  const rows = await getNewsDates()
  dates.value = rows
  if (!rows.some(r => r.date === day.value) && rows.length) {
    day.value = rows[0].date
  }
}

const loadCalendar = async () => {
  try {
    const d = await getNewsCalendar(10)
    calendar.value = d.events || []
  } catch {
    calendar.value = []
  }
}

const load = async () => {
  loading.value = true
  try {
    const [n, b] = await Promise.all([
      getNews(day.value, fCat.value),
      getNewsBrief(day.value)
    ])
    items.value = n.items || []
    brief.value = b.payload || null
    briefReason.value = b.reason || ''
    briefTime.value = b.created_at ? fmtTime(b.created_at) : ''
  } finally {
    loading.value = false
  }
}

const pickDate = (d) => {
  day.value = d
  load()
}

const refresh = async () => {
  loading.value = true
  try {
    await refreshNews(day.value)
    await loadDates()
    await loadCalendar()
    await load()
  } finally {
    loading.value = false
  }
}

const openUrl = (url) => {
  if (url) window.open(url, '_blank', 'noopener')
}

onMounted(async () => {
  await Promise.all([loadDates(), loadCalendar()])
  await load()
})
</script>

<style scoped>
.page { padding-bottom: 24px; }

/* 日历 */
.cal { margin-top: 8px; }
.cal-body { padding: 2px 14px 12px; }
.cal-day { display: flex; gap: 10px; padding: 8px 0;
           border-top: 1px solid #f2f3f5; }
.cal-day:first-child { border-top: none; }
.cal-date { width: 54px; flex: none; font-size: 13px; font-weight: 700;
            color: #333; }
.cal-date em { display: block; font-style: normal; font-size: 11px;
               color: #999; font-weight: 400; }
.cal-items { flex: 1; min-width: 0; }
.cal-row { display: flex; align-items: baseline; gap: 6px; padding: 3px 0;
           font-size: 12.5px; line-height: 1.5; }
.cal-row .cal-title { color: #333; flex: 1; }
.cal-row.hot .cal-title { color: #c0392b; font-weight: 600; }
.cal-extra { font-size: 11.5px; color: #888; flex: none; }

/* 筛选 */
.filter { margin-top: 12px; }
.chips { display: flex; gap: 6px; overflow-x: auto; max-width: 70vw; }
.chip { flex: none; padding: 2px 8px; border-radius: 10px; background: #f2f3f5;
        color: #555; font-size: 12px; cursor: pointer; }
.chip.on { background: #c0392b; color: #fff; }
.chip em { font-style: normal; opacity: .7; margin-left: 3px; font-size: 11px; }

/* 简报 */
.brief { margin-top: 12px; }
.brief-body { padding: 4px 14px 12px; }
.sentiment { display: flex; align-items: center; gap: 8px; padding: 8px 10px;
             border-radius: 8px; background: #f6f7f9; }
.sentiment .st { font-weight: 800; font-size: 15px; padding: 2px 8px;
                 border-radius: 6px; color: #fff; background: #7a869a; flex: none; }
.sentiment.bull .st { background: #c0392b; }
.sentiment.bear .st { background: #0a8043; }
.sentiment .ov { font-size: 13px; line-height: 1.5; color: #333; }

.block { margin-top: 12px; }
.block-t { font-size: 12px; color: #888; margin-bottom: 6px; font-weight: 600; }
.block-t.warn { color: #c0392b; }
.block-t.good { color: #0a8043; }

.ev-row { display: flex; align-items: baseline; gap: 6px; padding: 5px 0;
          font-size: 12.5px; border-top: 1px solid #f2f3f5; }
.ev-date { color: #999; flex: none; font-size: 12px; }
.ev-txt { color: #555; line-height: 1.5; flex: 1; }
.ev-txt b { color: #222; }

.row-item { display: flex; align-items: baseline; gap: 6px; padding: 5px 0;
            font-size: 13px; border-top: 1px solid #f2f3f5; }
.row-item .nm { font-weight: 700; }
.row-item .cd { color: #999; font-size: 12px; }
.row-item .rs { color: #666; font-size: 12px; line-height: 1.4; flex: 1; }

.risk-row { font-size: 12.5px; color: #a86400; line-height: 1.7; }
.disclaim { margin-top: 10px; font-size: 11px; color: #aaa; }

/* 资讯流 */
.feed { margin-top: 12px; }
.news { padding: 10px 14px; border-top: 1px solid #f0f1f3; }
.news:first-of-type { border-top: none; }
.n-head { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.n-head .src { font-size: 11.5px; color: #888; }
.n-head .time { font-size: 11.5px; color: #aaa; margin-left: auto; }
.n-title { font-size: 14px; font-weight: 600; line-height: 1.5; color: #222; }
.n-sum { font-size: 12.5px; color: #777; line-height: 1.55; margin-top: 3px;
         display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
         overflow: hidden; }
.n-rel { margin-top: 5px; display: flex; gap: 5px; flex-wrap: wrap; }
.footer { text-align: center; font-size: 11px; color: #aaa; margin-top: 20px; }
</style>
