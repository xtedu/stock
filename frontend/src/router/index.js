import { createRouter, createWebHistory } from 'vue-router'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: () => import('../views/Stocks.vue') },
    { path: '/pool', component: () => import('../views/Pool.vue') },
    { path: '/compare', component: () => import('../views/Compare.vue') },
    { path: '/compare/:id', component: () => import('../views/Compare.vue') },
    { path: '/detail/:code', component: () => import('../views/Detail.vue') },
    { path: '/alerts', component: () => import('../views/Alerts.vue') },
    { path: '/jobs', component: () => import('../views/Jobs.vue') },
    { path: '/mine', component: () => import('../views/Mine.vue') }
  ]
})
