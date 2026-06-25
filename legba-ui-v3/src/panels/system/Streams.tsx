import { makeStubPanel } from '@/panels/_DeferredStub'

export default makeStubPanel({
  title: 'NATS Stream Tail',
  spec: 'legba_ui_panels_v2.md §3.5 S7',
  description:
    'Live-tail an arbitrary NATS topic by subject pattern. Replaces old SSE EventStream — fully generalized.',
})
