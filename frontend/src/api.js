import axios from 'axios'

const api = axios.create({ baseURL: '/', timeout: 15000 })
// 数据同步任务可能耗时较长，单独给一个长超时实例
const jobApi = axios.create({ baseURL: '/', timeout: 1800000 })

export const getStocks = (keyword = '', offset = 0, limit = 100) =>
  api.get('/api/stocks', { params: { keyword, offset, limit } }).then(r => r.data)
export const getPool = (limit = 500) =>
  api.get('/api/pool', { params: { limit } }).then(r => r.data)
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
