/** 后端接口封装。所有请求走 /api，由 vite 代理到 8000。 */

const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(BASE + path, options)
  const text = await res.text()
  let body = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = null
  }
  if (!res.ok) {
    const detail = body?.detail || body?.message || res.statusText
    throw new Error(`${res.status} ${detail}`)
  }
  return body
}

const post = (path, payload) =>
  request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })

export const api = {
  health: () => request('/health'),
  listTasks: (limit = 20) => request(`/tasks?limit=${limit}`),
  createTask: (payload) => post('/tasks', payload),
  runTask: (taskId) => post(`/tasks/${encodeURIComponent(taskId)}/run`),
  getTask: (taskId) => request(`/tasks/${encodeURIComponent(taskId)}`),
  exportTask: (taskId) => request(`/tasks/${encodeURIComponent(taskId)}/export`),
  trace: (taskId) => request(`/tasks/${encodeURIComponent(taskId)}/trace`),
  check: (taskId) => request(`/tasks/${encodeURIComponent(taskId)}/check`),
  cost: (taskId) => request(`/tasks/${encodeURIComponent(taskId)}/cost`),
  corpus: (docId) => request(`/corpus/${encodeURIComponent(docId)}`),
  listCorpus: () => request('/corpus'),
  deleteCorpus: (docId) => request(`/corpus/${encodeURIComponent(docId)}`, { method: 'DELETE' }),
  uploadCorpus: async (files) => {
    const form = new FormData()
    for (const f of files) form.append('files', f)
    return request('/corpus/upload', { method: 'POST', body: form })
  },

  /**
   * 订阅任务进度。返回关闭函数。
   * 事件名：task / node / done / error
   */
  stream(taskId, onEvent) {
    const url = `${BASE}/tasks/${encodeURIComponent(taskId)}/stream`
    const es = new EventSource(url)
    for (const name of ['task', 'node', 'done', 'error']) {
      es.addEventListener(name, (evt) => {
        let data = {}
        try {
          data = JSON.parse(evt.data)
        } catch {
          data = {}
        }
        onEvent({ type: name, data })
      })
    }
    return () => es.close()
  },
}
