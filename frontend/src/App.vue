<template>
  <router-view />
  <van-tabbar route>
    <van-tabbar-item to="/" icon="apps-o">行情</van-tabbar-item>
    <van-tabbar-item to="/pool" icon="chart-trending-o">股票池</van-tabbar-item>
    <van-tabbar-item to="/alerts" icon="bell">提醒</van-tabbar-item>
    <van-tabbar-item to="/jobs" icon="todo-list-o">任务</van-tabbar-item>
    <van-tabbar-item to="/mine" icon="setting-o">我的</van-tabbar-item>
  </van-tabbar>
</template>
