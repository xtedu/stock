<template>
  <div class="page">
    <van-nav-bar title="行情" />
    <van-search v-model="keyword" placeholder="搜索代码/名称" @search="reload" @clear="reload" />
    <van-list v-model:loading="loading" :finished="finished" finished-text="没有更多了"
              @load="loadMore">
      <van-cell v-for="s in items" :key="s.code"
                :title="`${s.name} ${s.code}`" is-link :to="`/detail/${s.code}`">
        <template #label>更新于 {{ s.trade_date }}</template>
        <template #value>{{ s.close ?? '-' }}</template>
      </van-cell>
    </van-list>
    <van-empty v-if="!loading && !items.length" description="暂无数据，等待行情同步" />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { getStocks } from '../api'

const keyword = ref('')
const items = ref([])
const loading = ref(false)
const finished = ref(false)
const total = ref(0)
const PAGE = 100

async function reload() {
  finished.value = false
  items.value = []
  total.value = 0
  await loadMore()
}

async function loadMore() {
  loading.value = true
  try {
    const data = await getStocks(keyword.value, items.value.length, PAGE)
    total.value = data.total
    items.value.push(...data.items)
    finished.value = items.value.length >= data.total || data.items.length < PAGE
  } finally {
    loading.value = false
  }
}

onMounted(loadMore)
</script>

<style scoped>
.page { padding-bottom: 60px; }
</style>
