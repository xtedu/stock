import axios from 'axios'

const api = axios.create({ baseURL: '/', timeout: 15000 })
// 数据同步任务可能耗时较长，单独给一个长超时实例
const jobApi = axios.create({ baseURL: '/', timeout: 1800000 })

export const getStocks = (keyword = '', offset = 0, limit = 100) =>
  api.get('/api/stocks', { params: { keyword, offset, limit } }).then(r => r.data)
export const getPool = () => api.get('/api/pool').then(r => r.data)
export const getStockDetail = (code) => api.get(`/api/zones/${code}`).then(r => r.data)
export const getAlertLogs = () => api.get('/api/alerts/logs').then(r => r.data)
export const getAlertRules = () => api.get('/api/alerts/rules').then(r => r.data)
export const createAlertRule = (data) => api.post('/api/alerts/rules', data).then(r => r.data)
export const deleteAlertRule = (id) => api.delete(`/api/alerts/rules/${id}`).then(r => r.data)

// ===== 数据任务（手动触发） =====
export const syncQuotes = (years = 5) =>
  jobApi.post('/api/jobs/sync-quotes', null, { params: { years } }).then(r => r.data)
export const syncFundamentals = (years = 5) =>
  jobApi.post('/api/jobs/sync-fundamentals', null, { params: { years } }).then(r => r.data)
export const runScreen = () => jobApi.post('/api/jobs/screen').then(r => r.data)
export const calcZones = () => jobApi.post('/api/jobs/calc-zones').then(r => r.data)
export const runIntraday = () => jobApi.post('/api/jobs/intraday').then(r => r.data)
export const runMonitor = () => jobApi.post('/api/jobs/monitor').then(r => r.data)
