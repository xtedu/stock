<template>
  <div class="page" :class="{ 'has-bar': selectMode }">
    <van-nav-bar title="优质股池">
      <template #right>
        <template v-if="selectMode">
          <van-button size="mini" plain @click="exitSelect">取消</van-button>
        </template>
        <template v-else>
          <van-button size="mini" plain type="primary" class="nav-btn"
                      @click="enterSelect">对比</van-button>
          <van-button size="mini" plain class="nav-btn"
                      @click="$router.push('/compare')">历史</van-button>
        </template>
      </template>
    </van-nav-bar>

    <van-cell-group inset class="filter">
      <van-cell title="板块" center>
        <template #value>
          <van-radio-group v-model="fBoard" direction="horizontal" class="board-rg">
            <van-radio name="">全部</van-radio>
            <van-radio name="main">沪深主板</van-radio>
            <van-radio name="gem">创业板</van-radio>
            <van-radio name="star">科创板</van-radio>
            <van-radio name="bj">北交所</van-radio>
          </van-radio-group>
        </template>
      </van-cell>
      <van-cell title="入选来源" center>
        <template #value>
          <van-radio-group v-model="fSource" direction="horizontal">
            <van-radio name="">全部</van-radio>
            <van-radio name="intraday">盘中</van-radio>
            <van-radio name="official">收盘</van-radio>
          </van-radio-group>
        </template>
      </van-cell>
      <van-field v-model.number="fChangeMin" type="number" label="涨幅下限"
                 placeholder="不限" clearable>
        <template #button><span class="unit">%</span></template>
      </van-field>
      <van-field v-model.number="fChangeMax" type="number" label="涨幅上限"
                 placeholder="不限" clearable>
        <template #button><span class="unit">%</span></template>
      </van-field>
      <van-field v-model.number="fPriceMin" type="number" label="价格下限"
                 placeholder="不限" clearable />
      <van-field v-model.number="fPriceMax" type="number" label="价格上限"
                 placeholder="不限" clearable />
      <van-cell :title="`筛选结果：共 ${filtered.length} 只`"
                :label="list.length ? `行情日期 ${quoteDate || '-'}` : ''">
        <template #right-icon>
          <van-button size="mini" plain @click="resetAll">重置</van-button>
        </template>
      </van-cell>
    </van-cell-group>

    <div v-if="selectMode" class="tip">
      勾选 2-8 只股票后点「开始分析」，由大模型给出次日定性研判（上限 {{ maxSel }} 只）
    </div>

    <div v-for="item in filtered" :key="item.code" class="card"
         :class="{ sel: isSel(item.code) }" @click="onCard(item)">
      <div class="row main">
        <van-checkbox v-if="selectMode" class="pick"
                       :model-value="isSel(item.code)" icon-size="18"
                       @click.stop="toggle(item.code)" />
        <div class="grow">
          <span class="name">{{ item.name }}</span>
          <span class="code">{{ item.code }}</span>
        </div>
        <div class="rt">
          <span class="price">{{ fmt(item.price) }}</span>
          <span class="chg" :class="chgClass(item.change_pct)">
            {{ fmtPct(item.change_pct) }}
          </span>
        </div>
      </div>
      <div class="row sub">
        <van-tag :type="sourceType(item)" plain>{{ sourceText(item) }}</van-tag>
        <van-tag :type="levelType(item.score)" plain>
          {{ levelText(item.score) }} {{ item.score ?? '-' }}
        </van-tag>
        <span class="ohl">开 {{ fmt(item.open) }} · 收 {{ fmt(item.close) }}</span>
      </div>
      <div class="row badges">
        <van-tag type="success" plain>ROE {{ r(item, 'roe_avg') }}%</van-tag>
        <van-tag type="primary" plain>营收CAGR {{ r(item, 'rev_cagr_3y') }}%</van-tag>
        <van-tag type="warning" plain>PE分位 {{ pct(item) }}</van-tag>
      </div>
    </div>

    <van-empty v-if="!loading && !filtered.length"
               :description="list.length ? '无符合筛选条件的标的' : '暂无数据，等待基本面同步与筛选'" />

    <div v-if="selectMode" class="actionbar">
      <span class="count">已选 {{ selected.length }} 只</span>
      <van-button size="small" plain @click="clearSel">清空</van-button>
      <van-button size="small" type="primary" :loading="analyzing"
                  :disabled="!selected.length" @click="startAnalyze">
        开始分析
      </van-button>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { showToast } from 'vant'
import { analyzeCompare, getPool } from '../api'

const router = useRouter()
const maxSel = 8

const list = ref([])
const loading = ref(true)

const selectMode = ref(false)
const selected = ref([])
const analyzing = ref(false)

const fBoard = ref('')
const fSource = ref('')
const fChangeMin = ref('')
const fChangeMax = ref('')
const fPriceMin = ref('')
const fPriceMax = ref('')

const r = (item, key) => item.reason?.[key] ?? '-'
const pct = (item) => item.reason?.pe_percentile != null
  ? `${item.reason.pe_percentile}%` : '-'

const fmt = (v) => (v == null ? '-' : Number(v).toFixed(2))
const fmtPct = (v) => (v == null ? '-' : `${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%`)
const chgClass = (v) => (v == null ? 'flat' : v > 0 ? 'up' : v < 0 ? 'down' : 'flat')

const sourceText = (it) => it.source === 'intraday' ? '盘中入选'
  : it.source === 'official' ? '收盘入选' : '来源未知'
const sourceType = (it) => (it.source === 'intraday' ? 'primary' : 'success')
const levelText = (s) => (s == null ? '未评级' : s >= 80 ? '优质' : s >= 60 ? '良好' : '一般')
const levelType = (s) => (s == null ? 'default' : s >= 80 ? 'danger'
  : s >= 60 ? 'warning' : 'default')

const isSel = (code) => selected.value.includes(code)

// 板块按代码前缀派生：60x=沪市主板 00x=深市主板(含原中小板) 300/301=创业板
// 688/689=科创板 4/8/92 开头=北交所
const boardOf = (code) => {
  if (/^(688|689)/.test(code)) return 'star'
  if (/^(300|301)/.test(code)) return 'gem'
  if (/^60/.test(code) || /^00/.test(code)) return 'main'
  if (/^([48]|92)/.test(code)) return 'bj'
  return 'other'
}
const toggle = (code) => {
  if (isSel(code)) {
    selected.value = selected.value.filter(c => c !== code)
    return
  }
  if (selected.value.length >= maxSel) {
    showToast(`最多选择 ${maxSel} 只`)
    return
  }
  selected.value = [...selected.value, code]
}

const enterSelect = () => { selectMode.value = true }
const exitSelect = () => { selectMode.value = false; selected.value = [] }
const clearSel = () => { selected.value = [] }
const onCard = (item) => {
  if (selectMode.value) toggle(item.code)
  else router.push(`/detail/${item.code}`)
}

const startAnalyze = async () => {
  if (selected.value.length < 2) {
    showToast('至少选择 2 只股票')
    return
  }
  analyzing.value = true
  try {
    const rec = await analyzeCompare(selected.value)
    selectMode.value = false
    selected.value = []
    router.push(`/compare/${rec.id}`)
  } catch (e) {
    showToast(e?.response?.data?.detail || '分析失败，请稍后重试')
  } finally {
    analyzing.value = false
  }
}

const num = (v) => (v === '' || v == null ? null : Number(v))
const inRange = (v, lo, hi) => {
  if (v == null) return lo == null && hi == null
  if (lo != null && v < lo) return false
  if (hi != null && v > hi) return false
  return true
}

const filtered = computed(() => list.value.filter(it => {
  if (fBoard.value && boardOf(it.code) !== fBoard.value) return false
  if (fSource.value && it.source !== fSource.value) return false
  if (!inRange(it.change_pct, num(fChangeMin.value), num(fChangeMax.value))) return false
  if (!inRange(it.price, num(fPriceMin.value), num(fPriceMax.value))) return false
  return true
}))

const quoteDate = computed(() => list.value.find(i => i.quote_date)?.quote_date || '')

const resetAll = () => {
  fBoard.value = ''
  fSource.value = ''
  fChangeMin.value = ''
  fChangeMax.value = ''
  fPriceMin.value = ''
  fPriceMax.value = ''
}

onMounted(async () => {
  try { list.value = await getPool() } finally { loading.value = false }
})
</script>

<style scoped>
.page { padding-bottom: 60px; }
.page.has-bar { padding-bottom: 120px; }
.filter { margin-top: 10px; }
.nav-btn { margin-left: 8px; }
.board-rg { max-width: 230px; flex-wrap: wrap; row-gap: 4px; }
.tip {
  margin: 10px 12px 0;
  padding: 8px 10px;
  font-size: 12px;
  color: #ed6a0c;
  background: #fffbe8;
  border-radius: 6px;
}
.card {
  margin: 10px 12px;
  padding: 12px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
  border: 1px solid transparent;
}
.card.sel { border-color: #1989fa; background: #f2f8ff; }
.row { display: flex; align-items: center; }
.main { justify-content: space-between; }
.sub { gap: 6px; margin-top: 8px; }
.badges { gap: 4px; margin-top: 8px; flex-wrap: wrap; }
.grow { flex: 1; }
.pick { margin-right: 10px; }
.name { font-weight: 600; margin-right: 6px; }
.code { color: #999; font-size: 12px; }
.rt { text-align: right; }
.price { font-size: 18px; font-weight: 700; }
.chg { font-size: 13px; margin-left: 6px; }
.up { color: #ee0a24; }
.down { color: #07c160; }
.flat { color: #969799; }
.ohl { margin-left: auto; color: #969799; font-size: 12px; }
.unit { color: #969799; }
.actionbar {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 50px;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: #fff;
  box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.08);
}
.actionbar .count { flex: 1; font-size: 14px; color: #323233; }
</style>
