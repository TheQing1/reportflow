<script setup>
/**
 * 体检结果。
 *
 * 只做一件事：把"哪句话的引用对不上、应该挂哪篇"说清楚。
 * 不提供自动改稿——改不改由人决定，这也是它不进写作循环的原因。
 */
const props = defineProps({
  report: { type: Object, default: null },
  loading: { type: Boolean, default: false },
})
const emit = defineEmits(['view'])

const verdictLabel = { supported: '支持', weak: '部分支持', unsupported: '不支持' }
</script>

<template>
  <div>
    <div v-if="loading" class="muted">体检中（每份报告一次判官调用）…</div>
    <div v-else-if="!report" class="muted">点「发布前体检」核查角标与原文是否一致。</div>
    <div v-else-if="report.error" class="err">未完成：{{ report.error }}</div>
    <div v-else>
      <div class="row">
        <span>核查 {{ report.total }} 条引用断言：</span>
        <span class="ok">支持 {{ report.supported }}</span>
        <span class="muted">部分支持 {{ report.weak }}</span>
        <span :class="report.unsupported ? 'err' : 'muted'">不支持 {{ report.unsupported }}</span>
        <span v-if="report.skipped" class="muted">判官漏判 {{ report.skipped }}</span>
      </div>
      <div v-if="report.clean" class="ok" style="margin-top: 8px">
        没有发现引用对不上的地方。
      </div>
      <ul v-else class="issues">
        <li v-for="(issue, i) in report.issues" :key="i">
          <span :class="['badge', issue.verdict === 'unsupported' ? 'failed' : 'blocked']">
            {{ verdictLabel[issue.verdict] || issue.verdict }}
          </span>
          <strong>第 {{ issue.chapter }} 章 · 角标 {{ issue.cited }}</strong>
          <div class="sentence">{{ issue.sentence }}</div>
          <div v-if="issue.reason" class="muted">原因：{{ issue.reason }}</div>
          <div v-if="issue.likely_docs && issue.likely_docs.length" class="row" style="margin-top: 4px">
            <span class="muted">其他含这些数字的文档：</span>
            <button v-for="d in issue.likely_docs" :key="d" @click="emit('view', d)">{{ d }}</button>
          </div>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
ul.issues { list-style: none; margin: 12px 0 0; padding: 0; }
ul.issues li {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 8px;
}
.sentence { margin: 4px 0; font-size: 13px; }
</style>
