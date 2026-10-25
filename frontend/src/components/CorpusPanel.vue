<script setup>
/**
 * 知识库管理。
 *
 * 关键点只有一个：**没有资料也要能跑**。
 * 库为空时后端会自动用示例语料兜底，这里把这件事说清楚——
 * 否则用户看到一堆文档却不知道它们是哪来的，更不敢删。
 */
import { ref, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({ active: { type: Boolean, default: false } })

const info = ref(null)
const loading = ref(false)
const error = ref('')
const picking = ref(false)
const fileInput = ref(null)

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    info.value = await api.listCorpus()
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    loading.value = false
  }
}

async function upload() {
  const files = fileInput.value?.files
  if (!files || !files.length) return
  picking.value = true
  error.value = ''
  try {
    const res = await api.uploadCorpus(files)
    if (res.failed && res.failed.length) {
      error.value = res.failed.map((f) => `${f.filename}: ${f.reason}`).join('；')
    }
    fileInput.value.value = ''
    await refresh()
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    picking.value = false
  }
}

async function remove(docId) {
  if (!confirm(`删除 ${docId}？已生成的任务仍会保留旧的引用。`)) return
  error.value = ''
  try {
    await api.deleteCorpus(docId)
    await refresh()
  } catch (e) {
    error.value = String(e.message || e)
  }
}

watch(
  () => props.active,
  (on) => {
    if (on && !info.value) refresh()
  },
)
refresh()
</script>

<template>
  <div>
    <div class="row" style="justify-content: space-between">
      <div class="muted">
        知识库：{{ info?.docs_dir || '加载中…' }}
        <span v-if="info"> · {{ info.docs.length }} 篇 / {{ info.chunks }} 个可检索片段</span>
      </div>
      <button :disabled="loading" @click="refresh">刷新</button>
    </div>

    <div v-if="error" class="err" style="margin-top: 6px">{{ error }}</div>

    <div class="row" style="margin-top: 10px">
      <input ref="fileInput" type="file" multiple accept=".md,.markdown,.txt" style="width: auto" />
      <button class="primary" :disabled="picking" @click="upload">
        {{ picking ? '上传中…' : '上传' }}
      </button>
      <span class="muted">只支持 .md / .markdown / .txt，单文件 ≤ 2MB</span>
    </div>

    <div class="muted" style="margin-top: 8px">
      库为空时后端会自动用 <code>sample-corpus/</code> 里的示例语料初始化（虚构数据，仅用于验证）。
      换成自己的资料：直接上传，或把文件放进上面的目录后点刷新。
    </div>

    <table style="margin-top: 12px">
      <thead><tr><th>文档 id</th><th>标题</th><th>字数</th><th>位置</th><th></th></tr></thead>
      <tbody>
        <tr v-for="d in info?.docs || []" :key="d.doc_id">
          <td>{{ d.doc_id }}</td>
          <td>{{ d.title }}</td>
          <td>{{ d.chars }}</td>
          <td class="muted">{{ d.path }}</td>
          <td><button @click="remove(d.doc_id)">删除</button></td>
        </tr>
      </tbody>
    </table>
    <div v-if="info && !info.docs.length" class="muted" style="margin-top: 8px">
      （知识库为空）
    </div>
  </div>
</template>
