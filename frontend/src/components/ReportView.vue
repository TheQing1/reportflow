<script setup>
/**
 * 正文渲染 + 可点击角标。
 *
 * 角标是这个界面存在的理由：点开就能看到原文。
 * 体检发现问题的角标标红——让读者一眼看出"这个出处对不上"。
 */
import MarkdownIt from 'markdown-it'
import { computed } from 'vue'

const props = defineProps({
  markdown: { type: String, default: '' },
  badDocs: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['cite'])

const md = new MarkdownIt({ html: false, linkify: false, breaks: false })

const html = computed(() => {
  const raw = md.render(props.markdown || '')
  return raw.replace(/\[\^([^\]\s]+)\]/g, (_m, id) => {
    const bad = props.badDocs[id] ? ' bad' : ''
    return `<span class="cite${bad}" data-doc="${id}">${id.replace(/^doc_/, '')}</span>`
  })
})

function onClick(event) {
  const el = event.target.closest?.('.cite')
  if (el) emit('cite', el.getAttribute('data-doc'))
}
</script>

<template>
  <div class="report" v-html="html" @click="onClick" />
</template>
