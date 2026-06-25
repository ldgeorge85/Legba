/**
 * Workspace-level panel error boundary (resilience-observability W-1b §1).
 *
 * Wraps each lazy panel at the App Suspense host so a render crash in ONE
 * tile (a cytoscape/deck.gl edge case, a bad descriptor, an undefined field)
 * shows a graceful fallback tile IN that panel — it must NOT blank the whole
 * Dockview workspace.
 *
 * Unlike the v4-local `panels/v4/PanelBoundary`, this boundary also implements
 * `componentDidCatch` so the crash (plus the React component stack) is logged
 * to the console for triage — the observability half of the requirement.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  /** Human label for the crashed surface (panel id / kind) — aids triage. */
  label?: string
  children: ReactNode
}
interface State {
  error: Error | null
}

export class PanelErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Observability: surface the crash + React component stack for triage.
    // A single panel crash is isolated here; logging is the only side effect.
    // eslint-disable-next-line no-console
    console.error(
      `[panel-error] ${this.props.label ?? 'panel'} crashed:`,
      error,
      info.componentStack,
    )
  }

  reset = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    if (error) {
      return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-2 bg-surf-2 p-6 text-center text-ink-1">
          <span className="text-sm text-rose-400">
            This panel hit an error and was isolated.
          </span>
          {this.props.label && (
            <span className="font-mono text-xs text-ink-3">{this.props.label}</span>
          )}
          <span className="max-w-md break-words text-xs text-ink-2">{error.message}</span>
          <button
            type="button"
            onClick={this.reset}
            className="text-xs text-ink-2 underline underline-offset-2 hover:text-ink-1"
          >
            retry
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
