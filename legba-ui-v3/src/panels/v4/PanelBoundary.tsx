/**
 * Error boundary for the v4 visual panels. A render crash in one panel (e.g. a
 * cytoscape/deck.gl edge case) shows a graceful message + the error text IN the
 * panel instead of white-screening the whole workspace — and surfaces what
 * actually went wrong so it's fixable.
 */
import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}
interface State {
  error: Error | null
}

export class PanelBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  reset = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    if (error) {
      return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-2 bg-surface-300 p-6 text-center">
          <span className="text-sm text-rose-400">This panel hit an error.</span>
          <span className="max-w-md break-words text-xs text-slate-500">{error.message}</span>
          <button
            type="button"
            onClick={this.reset}
            className="text-xs text-slate-400 underline underline-offset-2 hover:text-slate-200"
          >
            retry
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
