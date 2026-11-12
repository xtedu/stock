import { createRouter, createWebHistory } from 'vue-router'

export default createRouter({
  history: createWebHistory(),
  // 后退时恢复原滚动位置，避免从详情页返回后跳回顶部
  scrollBehavior(to, from, savedPosition) {
    return savedPosition || { top: 0 }
  },
  routes: [
    { path: '/', component: () => import('../views/Stocks.vue') },
    { path: '/pool', component: () => import('../views/Pool.vue') },
    { path: '/picks', component: () => import('../views/Picks.vue') },
    { path: '/picks/perf', component: () => import('../views/PicksPerf.vue') },
    { path: '/yaogu', component: () => import('../views/Yaogu.vue') },
    { path: '/yaogu/history', component: () => import('../views/YaoguHistory.vue') },
    { path: '/news', component: () => import('../views/News.vue') },
    { path: '/compare', component: () => import('../views/Compare.vue') },
    { path: '/compare/:id', component: () => import('../views/Compare.vue') },
    { path: '/detail/:code', component: () => import('../views/Detail.vue') },
    { path: '/alerts', component: () => import('../views/Alerts.vue') },
    { path: '/jobs', component: () => import('../views/Jobs.vue') },
    { path: '/mine', component: () => import('../views/Mine.vue') }
  ]
})
