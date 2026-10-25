<script setup>
/**
 * DAG 可视化。
 *
 * 不用 vue-flow：这图的形状是固定的（planner → 并行 research → 串行 write），
 * 布局自己算三行就行，引入一个图布局库只为了画 20 个方块不值得。
 * 颜色只编码一件事——节点状态，这是用户在等待时唯一关心的信息。
 */
import { computed } from 'vue'

const props = defineProps({
  nodes: { type: Array, default: () => [] },
})

const ROWS = { planner: 0, research: 1, write: 2, review: 3 }
const COL_W = 168
const ROW_H = 92
const PAD_X = 60
const PAD_Y = 44

const box = computed(() => {
  const items = props.nodes.map((n) => {
    const m = /_ch(\d+)$/.exec(n.node_id)
    const col = m ? Number(m[1]) - 1 : 0
    const row = ROWS[n.type] ?? 0
    return {
      ...n,
      x: PAD_X + col * COL_W,
      y: PAD_Y + row * ROW_H,
      label: n.node_id.replace(/^(\w+)_/, '$1\n'),
    }
  })
  const maxX = Math.max(...items.map((i) => i.x), 0)
  const maxY = Math.max(...items.map((i) => i.y), 0)
  return { items, width: maxX + COL_W, height: maxY + 70 }
})

const byId = computed(() => Object.fromEntries(box.value.items.map((i) => [i.node_id, i])))

const edges = computed(() => {
  const out = []
  for (const n of props.nodes) {
    for (const dep of n.deps || []) {
      const from = byId.value[dep]
      const to = byId.value[n.node_id]
      if (!from || !to) continue
      out.push({
        key: `${dep}->${n.node_id}`,
        x1: from.x + 62,
        y1: from.y + 24,
        x2: to.x + 62,
        y2: to.y + 24,
      })
    }
  }
  return out
})
</script>

<template>
  <svg :width="box.width" :height="box.height" class="dag">
    <defs>
      <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
        <path d="M0,0 L0,6 L7,3 z" fill="#c3c8d0" />
      </marker>
    </defs>

    <line
      v-for="e in edges"
      :key="e.key"
      :x1="e.x1"
      :y1="e.y1"
      :x2="e.x2"
      :y2="e.y2"
      stroke="#c3c8d0"
      stroke-width="1.2"
      marker-end="url(#arrow)"
    />

    <g v-for="n in box.items" :key="n.node_id">
      <rect
        :x="n.x"
        :y="n.y"
        width="124"
        height="46"
        rx="8"
        :class="['node', n.status.toLowerCase()]"
      />
      <text :x="n.x + 62" :y="n.y + 20" class="nid">{{ n.node_id }}</text>
      <text :x="n.x + 62" :y="n.y + 36" class="nstatus">
        {{ n.status }}<tspan v-if="n.attempt > 1"> · a{{ n.attempt }}</tspan>
      </text>
    </g>
  </svg>
</template>

<style scoped>
.dag { overflow: visible; }
svg :deep(.node) {
  fill: #fff;
  stroke: #9ca3af;
  stroke-width: 1.2;
}
svg :deep(.node.done) { stroke: #16a34a; fill: #f0fdf4; }
svg :deep(.node.running) { stroke: #2563eb; fill: #eff6ff; }
svg :deep(.node.failed) { stroke: #dc2626; fill: #fef2f2; }
svg :deep(.node.blocked) { stroke: #d97706; fill: #fffbeb; }
svg :deep(.node.retrying) { stroke: #ea580c; fill: #fff7ed; }
.nid { text-anchor: middle; font-size: 11px; fill: #1f2329; }
.nstatus { text-anchor: middle; font-size: 10px; fill: #6b7280; }
</style>
