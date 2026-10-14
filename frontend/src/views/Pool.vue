<template>
  <div class="page">
    <van-nav-bar title="优质股池" />
    <van-cell v-for="item in list" :key="item.code" is-link :to="`/detail/${item.code}`">
      <template #title>
        <span class="name">{{ item.name }}</span>
        <span class="code">{{ item.code }}</span>
      </template>
      <template #label>
        <div class="badges">
          <van-tag type="success" plain>ROE {{ r(item, 'roe_avg') }}%</van-tag>
          <van-tag type="primary" plain>营收CAGR {{ r(item, 'rev_cagr_3y') }}%</van-tag>
          <van-tag type="warning" plain>PE分位 {{ pct(item) }}</van-tag>
        </div>
      </template>
      <template #value>
        <span class="score">{{ item.score ?? '-' }}</span>
      </template>
    </van-cell>
    <van-empty v-if="!loading && !list.length"
               description="暂无数据，等待基本面同步与筛选" />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { getPool } from '../api'

const list = ref([])
const loading = ref(true)
const r = (item, key) => item.reason?.[key] ?? '-'
const pct = (item) => item.reason?.pe_percentile != null
  ? `${item.reason.pe_percentile}%` : '-'
onMounted(async () => {
  try { list.value = await getPool() } finally { loading.value = false }
})
</script>

<style scoped>
.page { padding-bottom: 60px; }
.name { font-weight: 600; margin-right: 6px; }
.code { color: #999; font-size: 12px; }
.badges { display: flex; gap: 4px; margin-top: 4px; flex-wrap: wrap; }
.score { font-size: 18px; font-weight: 700; color: #ee0a24; }
</style>
