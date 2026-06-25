import { makeStubPanel } from '@/panels/_DeferredStub'

export default makeStubPanel({
  title: 'Global Pulse',
  spec: 'legba_ui_panels_v2.md §3.3 D2',
  description:
    'Severity-weighted view of active situations across all targets — world map + list. Powered by the registered global_pulse_correlator analyst.',
})
