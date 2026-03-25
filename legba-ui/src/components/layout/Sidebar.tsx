import { useState, useRef, useEffect, useCallback } from 'react'
import {
  LayoutDashboard,
  Newspaper,
  Rss,
  Users,
  Globe,
  Target,
  Network,
  Map,
  Radio,
  MessageSquare,
  AlertTriangle,
  Eye,
  BarChart3,
  Activity,
  ChevronLeft,
  ChevronRight,
  Crosshair,
  FileText,
  BookMarked,
  Gauge,
  GitPullRequest,
  FlaskConical,
  ScrollText,
  Layers,
  Save,
  Trash2,
  ChevronDown,
  Settings,
  UserCog,
  LogOut,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useWorkspaceStore, type PanelType } from '@/stores/workspace'
import { GlobalSearch } from '@/components/GlobalSearch'
import {
  LAYOUT_PRESETS,
  loadCustomLayouts,
  saveCustomLayout,
  deleteCustomLayout,
} from '@/layouts/presets'

interface NavItem {
  type: PanelType
  label: string
  icon: React.ReactNode
  group: string
}

const navItems: NavItem[] = [
  // AWARENESS
  { type: 'dashboard', label: 'Dashboard', icon: <LayoutDashboard size={18} />, group: 'AWARENESS' },
  { type: 'situations', label: 'Situations', icon: <AlertTriangle size={18} />, group: 'AWARENESS' },
  { type: 'event-stream', label: 'Live Feed', icon: <Radio size={18} />, group: 'AWARENESS' },
  { type: 'map', label: 'Map', icon: <Map size={18} />, group: 'AWARENESS' },

  // INVESTIGATION
  { type: 'events', label: 'Events', icon: <Newspaper size={18} />, group: 'INVESTIGATION' },
  { type: 'entities', label: 'Entities', icon: <Users size={18} />, group: 'INVESTIGATION' },
  { type: 'graph', label: 'Graph', icon: <Network size={18} />, group: 'INVESTIGATION' },
  { type: 'timeline', label: 'Timeline', icon: <Activity size={18} />, group: 'INVESTIGATION' },
  { type: 'signals', label: 'Signals', icon: <Rss size={18} />, group: 'INVESTIGATION' },

  // ANALYSIS
  { type: 'hypotheses', label: 'Hypotheses', icon: <FlaskConical size={18} />, group: 'ANALYSIS' },
  { type: 'consult', label: 'Consult', icon: <MessageSquare size={18} />, group: 'ANALYSIS' },
  { type: 'facts', label: 'Facts', icon: <FileText size={18} />, group: 'ANALYSIS' },
  { type: 'analytics', label: 'Analytics', icon: <BarChart3 size={18} />, group: 'ANALYSIS' },

  // PRODUCTS
  { type: 'briefs', label: 'Briefs', icon: <ScrollText size={18} />, group: 'PRODUCTS' },
  { type: 'reports', label: 'Reports', icon: <BookMarked size={18} />, group: 'PRODUCTS' },
  { type: 'journal', label: 'Journal', icon: <FileText size={18} />, group: 'PRODUCTS' },
  { type: 'watchlist', label: 'Watchlist', icon: <Eye size={18} />, group: 'PRODUCTS' },

  // OPERATIONS
  { type: 'goals', label: 'Goals', icon: <Target size={18} />, group: 'OPERATIONS' },
  { type: 'sources', label: 'Sources', icon: <Globe size={18} />, group: 'OPERATIONS' },
  { type: 'cycle-monitor', label: 'Cycles', icon: <Crosshair size={18} />, group: 'OPERATIONS' },
  { type: 'scorecard', label: 'Scorecard', icon: <Gauge size={18} />, group: 'OPERATIONS' },
  { type: 'proposed-edges', label: 'Edge Queue', icon: <GitPullRequest size={18} />, group: 'OPERATIONS' },
  { type: 'config', label: 'Config', icon: <Settings size={18} />, group: 'OPERATIONS' },
  { type: 'users', label: 'Users', icon: <UserCog size={18} />, group: 'OPERATIONS' },
]

export function Sidebar() {
  const { sidebarCollapsed, toggleSidebar, openPanel, applyPreset, saveCurrentLayout } =
    useWorkspaceStore()
  const [layoutMenuOpen, setLayoutMenuOpen] = useState(false)
  const [customLayouts, setCustomLayouts] = useState<Record<string, string>>({})
  const menuRef = useRef<HTMLDivElement>(null)

  // Load custom layouts on mount and when menu opens
  useEffect(() => {
    if (layoutMenuOpen) {
      setCustomLayouts(loadCustomLayouts())
    }
  }, [layoutMenuOpen])

  // Close menu on outside click
  useEffect(() => {
    if (!layoutMenuOpen) return
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setLayoutMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [layoutMenuOpen])

  const handleSaveLayout = useCallback(() => {
    const name = prompt('Layout name:')
    if (!name?.trim()) return
    const layoutJSON = saveCurrentLayout()
    if (layoutJSON) {
      saveCustomLayout(name.trim(), layoutJSON)
      setCustomLayouts(loadCustomLayouts())
    }
    setLayoutMenuOpen(false)
  }, [saveCurrentLayout])

  const handleDeleteCustom = useCallback((name: string) => {
    deleteCustomLayout(name)
    setCustomLayouts(loadCustomLayouts())
  }, [])

  const groups = navItems.reduce<Record<string, NavItem[]>>((acc, item) => {
    ;(acc[item.group] ??= []).push(item)
    return acc
  }, {})

  return (
    <div
      className={cn(
        'flex flex-col h-full bg-card border-r border-border transition-all duration-200',
        sidebarCollapsed ? 'w-12' : 'w-48',
      )}
    >
      {/* Logo */}
      <div className="flex items-center gap-2 px-3 h-10 border-b border-border shrink-0">
        {!sidebarCollapsed && (
          <span className="text-sm font-semibold tracking-wide text-primary">LEGBA</span>
        )}
        <button
          onClick={toggleSidebar}
          className="ml-auto p-1 rounded hover:bg-secondary text-muted-foreground"
        >
          {sidebarCollapsed ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
        </button>
      </div>

      {/* Layout Preset Selector */}
      {!sidebarCollapsed && (
        <div className="relative px-2 py-1.5 border-b border-border" ref={menuRef}>
          <button
            onClick={() => setLayoutMenuOpen((v) => !v)}
            className={cn(
              'flex items-center gap-1.5 w-full px-2 py-1 rounded text-xs',
              'text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors',
            )}
          >
            <Layers size={14} />
            <span>Layouts</span>
            <ChevronDown size={12} className="ml-auto" />
          </button>
          {layoutMenuOpen && (
            <div className="absolute left-2 right-2 top-full mt-1 z-50 rounded border border-border bg-popover shadow-lg">
              {/* Preset layouts */}
              <div className="px-2 py-1">
                <p className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground mb-1">
                  Presets
                </p>
                {LAYOUT_PRESETS.map((preset) => (
                  <button
                    key={preset.name}
                    onClick={() => {
                      applyPreset(preset.panels)
                      setLayoutMenuOpen(false)
                    }}
                    className="flex items-center gap-1.5 w-full px-1.5 py-1 rounded text-xs text-muted-foreground hover:bg-secondary hover:text-foreground"
                  >
                    {preset.label}
                  </button>
                ))}
              </div>

              {/* Custom layouts */}
              {Object.keys(customLayouts).length > 0 && (
                <div className="px-2 py-1 border-t border-border">
                  <p className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground mb-1">
                    Custom
                  </p>
                  {Object.entries(customLayouts).map(([name, layoutJSON]) => (
                    <div
                      key={name}
                      className="flex items-center gap-1 w-full rounded text-xs text-muted-foreground hover:bg-secondary group"
                    >
                      <button
                        onClick={() => {
                          useWorkspaceStore.getState().restoreCustomLayout(layoutJSON)
                          setLayoutMenuOpen(false)
                        }}
                        className="flex-1 text-left px-1.5 py-1 hover:text-foreground"
                      >
                        {name}
                      </button>
                      <button
                        onClick={() => handleDeleteCustom(name)}
                        className="p-0.5 opacity-0 group-hover:opacity-100 hover:text-destructive transition-opacity"
                        title="Delete layout"
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {/* Save current */}
              <div className="px-2 py-1.5 border-t border-border">
                <button
                  onClick={handleSaveLayout}
                  className="flex items-center gap-1.5 w-full px-1.5 py-1 rounded text-xs text-muted-foreground hover:bg-secondary hover:text-foreground"
                >
                  <Save size={12} />
                  Save Layout...
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Collapsed layout button */}
      {sidebarCollapsed && (
        <div className="px-2 py-1.5 border-b border-border">
          <button
            onClick={() => {
              toggleSidebar()
              setTimeout(() => setLayoutMenuOpen(true), 250)
            }}
            className="flex items-center justify-center w-full p-1 rounded text-muted-foreground hover:bg-secondary"
            title="Layouts"
          >
            <Layers size={16} />
          </button>
        </div>
      )}

      {/* Search */}
      <GlobalSearch collapsed={sidebarCollapsed} />

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-2">
        {Object.entries(groups).map(([group, items]) => (
          <div key={group} className="mb-2">
            {!sidebarCollapsed && (
              <p className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                {group}
              </p>
            )}
            {items.map((item) => (
              <button
                key={item.type}
                onClick={() => openPanel(item.type)}
                className={cn(
                  'flex items-center gap-2 w-full px-3 py-1.5 text-sm text-muted-foreground',
                  'hover:bg-secondary hover:text-foreground transition-colors',
                  sidebarCollapsed && 'justify-center',
                )}
                title={sidebarCollapsed ? item.label : undefined}
              >
                {item.icon}
                {!sidebarCollapsed && <span>{item.label}</span>}
              </button>
            ))}
          </div>
        ))}
      </nav>

      {/* Logout */}
      <div className="shrink-0 border-t border-border px-3 py-2">
        <button
          onClick={() => {
            fetch('/api/v2/auth/logout', { method: 'POST' }).finally(() => {
              window.location.reload()
            })
          }}
          className={cn(
            'flex items-center gap-2 w-full px-1 py-1 rounded text-xs text-muted-foreground',
            'hover:bg-secondary hover:text-foreground transition-colors',
            sidebarCollapsed && 'justify-center',
          )}
          title={sidebarCollapsed ? 'Log out' : undefined}
        >
          <LogOut size={14} />
          {!sidebarCollapsed && <span>Log out</span>}
        </button>
      </div>
    </div>
  )
}
