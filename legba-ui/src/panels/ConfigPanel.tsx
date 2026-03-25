import { useState } from 'react'
import {
  useConfigKeys,
  useConfigValue,
  useConfigHistory,
  useUpdateConfig,
  useRollbackConfig,
} from '@/api/hooks'
import { Settings, Save, History, RotateCcw, Check, AlertCircle } from 'lucide-react'
import { cn } from '@/lib/utils'

export function ConfigPanel() {
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)
  const [editValue, setEditValue] = useState<string | null>(null)
  const [editNotes, setEditNotes] = useState('')
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')

  const { data: keysData, isLoading: keysLoading } = useConfigKeys()
  const { data: valueData, isLoading: valueLoading } = useConfigValue(selectedKey)
  const { data: historyData, isLoading: historyLoading } = useConfigHistory(
    showHistory ? selectedKey : null,
  )

  const updateMutation = useUpdateConfig()
  const rollbackMutation = useRollbackConfig()

  const keys = keysData?.keys ?? []

  // Find the active key metadata
  const activeKeyMeta = keys.find((k: any) => k.key === selectedKey)

  // When a key is selected or value data changes, initialize the editor
  const currentValue = valueData?.value ?? ''
  const isEditing = editValue !== null

  function handleSelectKey(key: string) {
    setSelectedKey(key)
    setEditValue(null)
    setEditNotes('')
    setShowHistory(false)
    setSaveStatus('idle')
  }

  function handleStartEdit() {
    setEditValue(currentValue)
    setEditNotes('')
    setSaveStatus('idle')
  }

  function handleSave() {
    if (!selectedKey || editValue === null) return
    setSaveStatus('saving')
    updateMutation.mutate(
      { key: selectedKey, value: editValue, notes: editNotes },
      {
        onSuccess: () => {
          setSaveStatus('saved')
          setEditValue(null)
          setEditNotes('')
          setTimeout(() => setSaveStatus('idle'), 2000)
        },
        onError: () => {
          setSaveStatus('error')
          setTimeout(() => setSaveStatus('idle'), 3000)
        },
      },
    )
  }

  function handleRollback(version: number) {
    if (!selectedKey) return
    if (!confirm(`Rollback "${selectedKey}" to version ${version}?`)) return
    rollbackMutation.mutate(
      { key: selectedKey, version },
      {
        onSuccess: () => {
          setEditValue(null)
          setShowHistory(false)
          setSaveStatus('idle')
        },
      },
    )
  }

  return (
    <div className="flex h-full">
      {/* Left sidebar: key list */}
      <div className="w-56 shrink-0 border-r border-border overflow-y-auto">
        <div className="sticky top-0 bg-card px-3 py-2 border-b border-border">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wider">
            <Settings size={12} />
            Config Keys
          </div>
        </div>
        {keysLoading && (
          <div className="px-3 py-4 text-xs text-muted-foreground">Loading...</div>
        )}
        {keys.map((k: any) => (
          <button
            key={k.key}
            onClick={() => handleSelectKey(k.key)}
            className={cn(
              'w-full text-left px-3 py-1.5 text-xs border-b border-border/50 transition-colors',
              selectedKey === k.key
                ? 'bg-secondary text-foreground'
                : 'text-muted-foreground hover:bg-secondary/50 hover:text-foreground',
            )}
          >
            <div className="font-mono truncate">{k.key}</div>
            <div className="text-[10px] text-muted-foreground mt-0.5">
              v{k.version} &middot; {k.updated_at ? new Date(k.updated_at).toLocaleDateString() : '?'}
            </div>
          </button>
        ))}
        {!keysLoading && keys.length === 0 && (
          <div className="px-3 py-4 text-xs text-muted-foreground">No config keys found</div>
        )}
      </div>

      {/* Main area */}
      <div className="flex-1 flex flex-col min-w-0">
        {!selectedKey ? (
          <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">
            Select a config key to view or edit
          </div>
        ) : (
          <>
            {/* Top bar */}
            <div className="flex items-center gap-2 px-3 py-2 border-b border-border shrink-0">
              <span className="font-mono text-sm font-semibold text-foreground truncate">
                {selectedKey}
              </span>
              {activeKeyMeta && (
                <span className="text-xs text-muted-foreground shrink-0">
                  v{activeKeyMeta.version}
                  {activeKeyMeta.updated_at && (
                    <> &middot; {new Date(activeKeyMeta.updated_at).toLocaleString()}</>
                  )}
                </span>
              )}
              <div className="flex-1" />

              {/* History toggle */}
              <button
                onClick={() => setShowHistory((v) => !v)}
                className={cn(
                  'flex items-center gap-1 px-2 py-1 rounded text-xs transition-colors',
                  showHistory
                    ? 'bg-secondary text-foreground'
                    : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                )}
              >
                <History size={12} />
                History
              </button>

              {/* Edit / Save buttons */}
              {!isEditing ? (
                <button
                  onClick={handleStartEdit}
                  className="flex items-center gap-1 px-2 py-1 rounded text-xs bg-primary text-primary-foreground hover:bg-primary/90"
                >
                  Edit
                </button>
              ) : (
                <>
                  <button
                    onClick={() => {
                      setEditValue(null)
                      setEditNotes('')
                      setSaveStatus('idle')
                    }}
                    className="px-2 py-1 rounded text-xs text-muted-foreground hover:bg-secondary"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleSave}
                    disabled={saveStatus === 'saving'}
                    className="flex items-center gap-1 px-2 py-1 rounded text-xs bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  >
                    <Save size={12} />
                    {saveStatus === 'saving' ? 'Saving...' : 'Save'}
                  </button>
                </>
              )}

              {/* Status indicator */}
              {saveStatus === 'saved' && (
                <span className="flex items-center gap-1 text-xs text-emerald-400">
                  <Check size={12} /> Saved
                </span>
              )}
              {saveStatus === 'error' && (
                <span className="flex items-center gap-1 text-xs text-destructive">
                  <AlertCircle size={12} /> Error
                </span>
              )}
            </div>

            {/* Notes input (visible when editing) */}
            {isEditing && (
              <div className="px-3 py-1.5 border-b border-border bg-secondary/30">
                <input
                  type="text"
                  value={editNotes}
                  onChange={(e) => setEditNotes(e.target.value)}
                  placeholder="Change notes (optional)..."
                  className="w-full bg-transparent text-xs text-foreground placeholder:text-muted-foreground outline-none"
                />
              </div>
            )}

            {/* Content area */}
            <div className="flex-1 flex min-h-0">
              {/* Text editor */}
              <div className="flex-1 min-w-0 overflow-hidden">
                {valueLoading ? (
                  <div className="p-4 text-sm text-muted-foreground">Loading...</div>
                ) : isEditing ? (
                  <textarea
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                    className="w-full h-full p-3 bg-background text-xs font-mono text-foreground resize-none outline-none"
                    spellCheck={false}
                  />
                ) : (
                  <pre className="w-full h-full p-3 overflow-auto text-xs font-mono text-foreground whitespace-pre-wrap break-words">
                    {currentValue || '(empty)'}
                  </pre>
                )}
              </div>

              {/* History sidebar */}
              {showHistory && (
                <div className="w-64 shrink-0 border-l border-border overflow-y-auto">
                  <div className="sticky top-0 bg-card px-3 py-2 border-b border-border">
                    <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                      Version History
                    </div>
                  </div>
                  {historyLoading && (
                    <div className="px-3 py-4 text-xs text-muted-foreground">Loading...</div>
                  )}
                  {(historyData?.versions ?? []).map((v: any) => (
                    <div
                      key={v.version}
                      className="px-3 py-2 border-b border-border/50 text-xs"
                    >
                      <div className="flex items-center gap-1.5">
                        <span
                          className={cn(
                            'font-semibold',
                            v.active ? 'text-emerald-400' : 'text-muted-foreground',
                          )}
                        >
                          v{v.version}
                        </span>
                        {v.active && (
                          <span className="px-1 py-0.5 rounded bg-emerald-500/20 text-emerald-400 text-[9px] uppercase">
                            Active
                          </span>
                        )}
                      </div>
                      <div className="text-muted-foreground mt-0.5">
                        {v.created_by ?? 'unknown'} &middot;{' '}
                        {v.created_at ? new Date(v.created_at).toLocaleString() : '?'}
                      </div>
                      {v.notes && (
                        <div className="text-muted-foreground mt-0.5 italic">{v.notes}</div>
                      )}
                      {v.preview && (
                        <div className="mt-1 font-mono text-[10px] text-muted-foreground/70 truncate">
                          {v.preview}
                        </div>
                      )}
                      {!v.active && (
                        <button
                          onClick={() => handleRollback(v.version)}
                          className="flex items-center gap-1 mt-1.5 text-[10px] text-amber-400 hover:text-amber-300"
                        >
                          <RotateCcw size={10} />
                          Rollback to this version
                        </button>
                      )}
                    </div>
                  ))}
                  {!historyLoading && (historyData?.versions ?? []).length === 0 && (
                    <div className="px-3 py-4 text-xs text-muted-foreground">No history</div>
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
