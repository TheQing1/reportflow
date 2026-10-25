<script setup>
/**
 * 成本归因。
 *
 * 这里刻意显示"回放命中次数"：在 LLM_MODE=auto 下大部分调用不花钱，
 * 只显示 ¥0.0000 会让人以为"这套系统免费"，其实是缓存命中。
 * 数字小可以，但口径不能骗人。
 */
const props = defineProps({
  cost: { type: Object, default: null },
})

const money = (v) => `¥${Number(v || 0).toFixed(4)}`
</script>

<template>
  <div v-if="!cost || !cost.calls" class="muted">还没有 LLM 调用记录。</div>
  <div v-else>
    <div class="row">
      <span>共 {{ cost.calls }} 次调用</span>
      <span class="ok">{{ money(cost.cost) }}</span>
      <span class="muted">{{ cost.prompt_tokens }} prompt / {{ cost.completion_tokens }} completion tok</span>
      <span class="muted">均延迟 {{ cost.latency_ms_avg }} ms</span>
    </div>
    <div class="row" style="margin-top: 4px">
      <span v-if="cost.degraded_calls" class="err">降级 {{ cost.degraded_calls }} 次</span>
      <span v-if="cost.failed_calls" class="err">失败 {{ cost.failed_calls }} 次</span>
      <span v-if="cost.cached_calls" class="muted">
        其中 {{ cost.cached_calls }} 次命中回放缓存（不计费）
      </span>
    </div>
    <table style="margin-top: 8px">
      <thead><tr><th>节点</th><th>调用</th><th>成本</th></tr></thead>
      <tbody>
        <tr v-for="n in cost.by_node" :key="n.node_id">
          <td>{{ n.node_id }}</td>
          <td>{{ n.calls }}</td>
          <td>{{ money(n.cost) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
