import axios from 'axios'

const api = axios.create({ baseURL: '/', timeout: 15000 })
// 数据同步任务耗时很长（行情限速后 60+ 分钟），超时需与 nginx 的 7200s 匹配
const jobApi = axios.create({ baseURL: '/', timeout: 7200000 })

// GET 加时间戳：接口返回的是实时数据，避免浏览器/代理缓存旧结果
;[api, jobApi].forEach(inst => inst.interceptors.request.use(cfg => {
  if ((cfg.method || 'get').toLowerCase() === 'get') {
    cfg.params = { ...(cfg.params || {}), _t: Date.now() }
  }
  return cfg
}))

export const getStocks = (keyword = '', offset = 0, limit = 100) =>
  api.get('/api/stocks', { params: { keyword, offset, limit } }).then(r => r.data)
export const getPool = (limit = 500) =>
  api.get('/api/pool', { params: { limit } }).then(r => r.data)
// 今日三只（沪深）：利润/营收增速分位排序，服务端缓存 10 分钟
export const getPicks = (limit = 3, refresh = false) =>
  api.get('/api/picks', { params: { limit, refresh }, timeout: 60000 }).then(r => r.data)
export const getPickContext = () =>
  api.get('/api/picks/context', { timeout: 60000 }).then(r => r.data)
export const getStockDetail = (code) => api.get(`/api/zones/${code}`).then(r => r.data)
export const getAlertLogs = () => api.get('/api/alerts/logs').then(r => r.data)
export const getAlertRules = () => api.get('/api/alerts/rules').then(r => r.data)
export const createAlertRule = (data) => api.post('/api/alerts/rules', data).then(r => r.data)
export const deleteAlertRule = (id) => api.delete(`/api/alerts/rules/${id}`).then(r => r.data)

// ===== 股票池对比分析 =====
// 大模型分析耗时较长，用长超时实例
export const analyzeCompare = (codes) =>
  jobApi.post('/api/compare/analyze', { codes }).then(r => r.data)
export const getCompareHistory = (limit = 20) =>
  api.get('/api/compare/history', { params: { limit } }).then(r => r.data)
export const getCompareDetail = (id) => api.get(`/api/compare/${id}`).then(r => r.data)

// ===== 数据任务（手动触发） =====
export const syncQuotes = (years = 5) =>
  jobApi.post('/api/jobs/sync-quotes', null, { params: { years } }).then(r => r.data)
export const syncFundamentals = (years = 5) =>
  jobApi.post('/api/jobs/sync-fundamentals', null, { params: { years } }).then(r => r.data)
export const runScreen = () => jobApi.post('/api/jobs/screen').then(r => r.data)
export const calcZones = () => jobApi.post('/api/jobs/calc-zones').then(r => r.data)
export const runIntraday = () => jobApi.post('/api/jobs/intraday').then(r => r.data)
export const runMonitor = () => jobApi.post('/api/jobs/monitor').then(r => r.data)
// 资金流同步：intraday=true 取盘中累计值，false 取收盘官方数据
export const syncMoneyFlow = (intraday = false) =>
  jobApi.post('/api/jobs/money-flow', null, { params: { intraday } }).then(r => r.data)

// 妖股筛选（refresh=true 跳过服务端 120s 缓存）
export const getYaogu = (limit = 50, refresh = false) =>
  api.get('/api/yaogu', { params: { limit, refresh }, timeout: 120000 }).then(r => r.data)
export const getYaoguLadder = () =>
  api.get('/api/yaogu/ladder', { timeout: 120000 }).then(r => r.data)
export const refreshYaogu = () =>
  jobApi.post('/api/yaogu/refresh').then(r => r.data)
// 妖股每日快照历史
export const getYaoguHistory = (limit = 120) =>
  api.get('/api/yaogu/history', { params: { limit } }).then(r => r.data)
export const getYaoguHistoryDetail = (day) =>
  api.get(`/api/yaogu/history/${day}`).then(r => r.data)
export const runYaoguBackfill = (days = 365) =>
  jobApi.post('/api/jobs/yaogu-backfill', null, { params: { days } }).then(r => r.data)

// ===== 消息面（政策 / 快讯 / 公告 + AI 简报） =====
export const getNews = (day, category = '', source = '', keyword = '', limit = 500) =>
  api.get('/api/news', {
    params: { day, category: category || undefined, source: source || undefined,
              keyword: keyword || undefined, limit }
  }).then(r => r.data)
export const getNewsBrief = (day, refresh = false) =>
  api.get('/api/news/brief', { params: { day, refresh } }).then(r => r.data)
export const getNewsDates = () => api.get('/api/news/dates').then(r => r.data)
// 未来事件日历：宏观（含预期值）/ 限售解禁 / 业绩预告
export const getNewsCalendar = (days = 10) =>
  api.get('/api/news/calendar', { params: { days } }).then(r => r.data)
// 抓取 5 个数据源 + 重建简报，耗时较长，用长超时实例
export const refreshNews = (day) =>
  jobApi.post('/api/news/refresh', null, { params: { day } }).then(r => r.data)
