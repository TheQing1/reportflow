<script setup>
/**
 * 主界面。
 *
 * 布局刻意只服务一个已验证的差异点：**每个角标都能点开看到原文**。
 * 执行视图和成本只是附属，不做成通用仪表盘——那种界面任何编排框架都能做，
 * 看不出本项目解决了什么。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from './api.js'
import DagView from './components/DagView.vue'
import ReportView from './components/ReportView.vue'
import CheckPanel from './components/CheckPanel.vue'
import CostPanel from './components/CostPanel.vue'
import CorpusPanel from './components/CorpusPanel.vue'

const health = ref(null)
const tasks = ref([])
const selectedId = ref('')
const task = ref(null)
const markdown = ref('')
const trace = ref(null)
const checkResult = ref(null)
const cost = ref(null)
const checking = ref(false)
const tab = ref('report')

const topic = ref('2026 年中国工商业储能行业')
const sections = ref(5)
const withReview = ref(false)
const busy = ref(false)
const error = ref('')

const drawer = ref(null) // { docId, text, quotes, loading, error }

let stopStream = null

const badDocs = computed(() => {
  const out = {}
  for (const i of checkResult.value?.issues || []) {
    if (i.verdict === 'unsupported') out[i.cited] = true
  }
  return out
})

const running = computed(() => task.value?.status === 'RUNNING' || task.value?.status === 'PENDING')

/**
 * 检索档位的人读标签。
 *
 * 显示「生效」档而不是「配置」档：hybrid 没配 embedding key 时会降级成 bm25，
 * 界面上如果照着配置显示 hybrid，会让人以为向量检索在跑，实际在跑关键词——
 * 排查「为什么召回没变好」时会白走一大圈。降级原因由后端一起给。
 */
const retrievalLabel = computed(() => {
  const h = health.value
  if (!h) return '-'
  const effective = h.retrieval_effective || h.retrieval_mode || 'bm25'
  const model = h.embedding_model ? ` + ${h.embedding_model}` : ''
  return `${effective}${model}`
})

async function refreshHealth() {
  try {
    health.value = await api.health()
  } catch (e) {
    health.value = null
  }
}

async function refreshTasks() {
  const data = await api.listTasks(30)
  tasks.value = data.tasks || []
}

async function select(id) {
  selectedId.value = id
  checkResult.value = null
  await Promise.all([loadTask(id), loadMarkdown(id), loadTrace(id)])
}

async function loadTask(id) {
  task.value = await api.getTask(id)
  try {
    cost.value = await api.cost(id)
  } catch {
    cost.value = null
  }
}

async function loadMarkdown(id) {
  try {
    markdown.value = (await api.exportTask(id)).markdown || ''
  } catch {
    markdown.value = ''
  }
}

async function loadTrace(id) {
  try {
    trace.value = await api.trace(id)
  } catch {
    trace.value = null
  }
}

function watch(id) {
  stopStream?.()
  stopStream = api.stream(id, async ({ type, data }) => {
    if (type === 'task') task.value = { ...(task.value || {}), ...data }
    if (type === 'node') {
      const nodes = [...(task.value?.nodes || [])]
      const idx = nodes.findIndex((n) => n.node_id === data.node_id)
      if (idx >= 0) nodes[idx] = { ...nodes[idx], ...data }
      else nodes.push(data)
      task.value = { ...(task.value || {}), nodes }
    }
    if (type === 'done') {
      await Promise.all([loadTask(id), loadMarkdown(id), loadTrace(id)])
      await refreshTasks()
    }
  })
}

async function createTask() {
  busy.value = true
  error.value = ''
  try {
    const res = await api.createTask({
      topic: topic.value.trim(),
      sections: Number(sections.value),
      review: withReview.value,
    })
    await refreshTasks()
    await select(res.task_id)
    await runTask(res.task_id)
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function runTask(id = selectedId.value) {
  if (!id) return
  error.value = ''
  try {
    await api.runTask(id)
    await loadTask(id)
    watch(id)
  } catch (e) {
    error.value = String(e.message || e)
  }
}

async function runCheck() {
  if (!selectedId.value) return
  checking.value = true
  tab.value = 'check'
  try {
    checkResult.value = await api.check(selectedId.value)
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    checking.value = false
  }
}

async function viewDoc(docId) {
  drawer.value = { docId, text: '', quotes: [], loading: true, error: '' }
  const quotes = []
  for (const ch of trace.value?.chapters || []) {
    for (const f of ch.facts || []) {
      if (f.source_id === docId) quotes.push(f.quote)
    }
  }
  try {
    const doc = await api.corpus(docId)
    drawer.value = { docId, text: doc.text, quotes, loading: false, error: '' }
  } catch (e) {
    drawer.value = { docId, text: '', quotes, loading: false, error: String(e.message || e) }
  }
}

/** 把原文里命中的引文高亮出来——用户点角标就是为了确认"这句真在那儿"。 */
const drawerHtml = computed(() => {
  if (!drawer.value?.text) return ''
  let out = escapeHtml(drawer.value.text)
  for (const q of drawer.value.quotes || []) {
    if (!q) continue
    const needle = escapeHtml(q.trim())
    if (needle.length > 8 && out.includes(needle)) {
      out = out.split(needle).join(`<mark>${needle}</mark>`)
    }
  }
  return out.replace(/\n/g, '<br/>')
})

function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c])
}

onMounted(async () => {
  await refreshHealth()
  await refreshTasks()
  if (tasks.value.length) await select(tasks.value[0].task_id)
  // 深度链接：?tab=trace|check|corpus 直达面板，?doc=<doc_id> 直达引用抽屉。
  // 把某条引用的原文链接发给同事核对时，对方打开就是高亮后的那篇，不用再点一遍。
  const params = new URLSearchParams(location.search)
  const wanted = params.get('tab')
  if (wanted) tab.value = wanted
  const doc = params.get('doc')
  if (doc) await viewDoc(doc)
})
onUnmounted(() => stopStream?.())
</script>

<template>
  <header class="panel" style="margin-bottom: 14px">
    <div class="row" style="justify-content: space-between">
      <div>
        <h1>ReportFlow</h1>
        <div class="muted">多 Agent 研报流水线 · 每个角标都能点开看原文</div>
      </div>
      <div v-if="health" class="muted" style="text-align: right">
        <div>模式 {{ health.llm_mode }} · 判官 {{ health.judge_model }}</div>
        <div>
          检索 {{ retrievalLabel }}
          <span v-if="health.retrieval_degraded" class="warn">（已降级）</span>
          · 知识库 {{ health.corpus_chunks }} 片段
        </div>
        <div>review 循环 {{ health.review_in_pipeline ? '开' : '关' }}</div>
      </div>
    </div>
  </header>

  <div class="layout">
    <aside class="panel stack">
      <h2>新建任务</h2>
      <input v-model="topic" placeholder="研究主题" />
      <div class="row">
        <select v-model="sections" style="width: 90px">
          <option :value="1">1 章</option>
          <option :value="2">2 章</option>
          <option :value="3">3 章</option>
          <option :value="5">5 章</option>
        </select>
        <label class="muted row" style="gap: 4px; width: auto">
          <input v-model="withReview" type="checkbox" style="width: auto" /> 含审稿
        </label>
      </div>
      <button class="primary" :disabled="busy || !topic.trim()" @click="createTask">
        {{ busy ? '执行中…' : '创建并执行' }}
      </button>
      <div v-if="error" class="err">{{ error }}</div>

      <h2 style="margin-top: 14px">任务</h2>
      <ul class="tasks">
        <li
          v-for="t in tasks"
          :key="t.task_id"
          :class="{ active: t.task_id === selectedId }"
          @click="select(t.task_id)"
        >
          <div>{{ t.topic }}</div>
          <div class="row" style="justify-content: space-between">
            <span class="id">{{ t.task_id }} · {{ t.done }}/{{ t.total }}</span>
            <span :class="['badge', t.status.toLowerCase()]">{{ t.status }}</span>
          </div>
        </li>
      </ul>
    </aside>

    <section class="stack">
      <div v-if="!task" class="panel muted">左侧选一个任务，或新建一个。</div>
      <template v-else>
        <div class="panel">
          <div class="row" style="justify-content: space-between">
            <div>
              <h2 style="margin: 0">{{ task.topic }}</h2>
              <div class="muted">{{ task.task_id }} · {{ task.sections }} 章 · {{ task.status }}</div>
            </div>
            <div class="row">
              <button :disabled="running" @click="runTask()">{{ running ? '执行中…' : '重跑' }}</button>
              <button :disabled="checking" @click="runCheck">发布前体检</button>
            </div>
          </div>
          <div style="margin-top: 8px"><CostPanel :cost="cost" /></div>
          <div style="margin-top: 10px; overflow-x: auto">
            <DagView :nodes="task.nodes || []" />
          </div>
          <table style="margin-top: 10px">
            <thead>
              <tr><th>节点</th><th>类型</th><th>状态</th><th>尝试</th><th>耗时</th></tr>
            </thead>
            <tbody>
              <tr v-for="n in task.nodes" :key="n.node_id">
                <td>{{ n.node_id }}</td>
                <td>{{ n.type }}</td>
                <td><span :class="['badge', n.status.toLowerCase()]">{{ n.status }}</span></td>
                <td>{{ n.attempt }}</td>
                <td>{{ n.duration_ms }} ms</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="tabs">
          <button :class="{ on: tab === 'report' }" @click="tab = 'report'">正文</button>
          <button :class="{ on: tab === 'trace' }" @click="tab = 'trace'">引用溯源</button>
          <button :class="{ on: tab === 'check' }" @click="tab = 'check'">体检</button>
          <button :class="{ on: tab === 'corpus' }" @click="tab = 'corpus'">知识库</button>
        </div>

        <div class="panel">
          <ReportView v-if="tab === 'report'" :markdown="markdown" :bad-docs="badDocs" @cite="viewDoc" />
          <div v-else-if="tab === 'trace'">
            <div class="muted">每章真实用到的素材。点「看原文」核对这句话是不是真在那儿。</div>
            <div v-for="ch in trace?.chapters || []" :key="ch.chapter" style="margin-top: 12px">
              <h3>第 {{ ch.chapter }} 章<span v-if="ch.dropped_facts" class="muted"> · 丢弃 {{ ch.dropped_facts }} 条不可核验素材</span></h3>
              <div v-if="!ch.facts.length" class="muted">（无素材）</div>
              <table v-else>
                <thead><tr><th>来源</th><th>原文摘录</th><th></th></tr></thead>
                <tbody>
                  <tr v-for="(f, i) in ch.facts" :key="i">
                    <td>{{ f.source_id }}<span v-if="f.page" class="muted"> P.{{ f.page }}</span></td>
                    <td>{{ f.quote }}</td>
                    <td><button @click="viewDoc(f.source_id)">看原文</button></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
          <CheckPanel v-else-if="tab === 'check'" :report="checkResult" :loading="checking" @view="viewDoc" />
          <CorpusPanel v-else :active="tab === 'corpus'" />
        </div>
      </template>
    </section>
  </div>

  <div v-if="drawer" class="drawer">
    <div class="row" style="justify-content: space-between">
      <h2 style="margin: 0">{{ drawer.docId }}</h2>
      <button @click="drawer = null">关闭</button>
    </div>
    <div v-if="drawer.loading" class="muted" style="margin-top: 10px">加载原文…</div>
    <div v-else-if="drawer.error" class="err" style="margin-top: 10px">{{ drawer.error }}</div>
    <div v-else class="muted" style="margin-top: 10px" v-html="drawerHtml" />
  </div>
</template>
