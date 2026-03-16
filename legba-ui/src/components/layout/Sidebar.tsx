import {
  LayoutDashboard,
  Newspaper,
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
  BookOpen,
  ChevronLeft,
  ChevronRight,
  Crosshair,
  FileText,
  BookMarked,
  Gauge,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useWorkspaceStore, type PanelType } from '@/stores/workspace'
import { GlobalSearch } from '@/components/GlobalSearch'

interface NavItem {
  type: PanelType
  label: string
  icon: React.ReactNode
  group: string
}

const navItems: NavItem[] = [
  { type: 'dashboard', label: 'Dashboard', icon: <LayoutDashboard size={18} />, group: 'Overview' },
  { type: 'events', label: 'Events', icon: <Newspaper size={18} />, group: 'Intelligence' },
  { type: 'entities', label: 'Entities', icon: <Users size={18} />, group: 'Intelligence' },
  { type: 'sources', label: 'Sources', icon: <Globe size={18} />, group: 'Intelligence' },
  { type: 'goals', label: 'Goals', icon: <Target size={18} />, group: 'Intelligence' },
  { type: 'facts', label: 'Facts', icon: <FileText size={18} />, group: 'Intelligence' },
  { type: 'graph', label: 'Graph', icon: <Network size={18} />, group: 'Visualization' },
  { type: 'map', label: 'Map', icon: <Map size={18} />, group: 'Visualization' },
  { type: 'timeline', label: 'Timeline', icon: <Activity size={18} />, group: 'Visualization' },
  { type: 'event-stream', label: 'Live Feed', icon: <Radio size={18} />, group: 'Real-Time' },
  { type: 'consult', label: 'Consult', icon: <MessageSquare size={18} />, group: 'Real-Time' },
  { type: 'situations', label: 'Situations', icon: <AlertTriangle size={18} />, group: 'Tracking' },
  { type: 'watchlist', label: 'Watchlist', icon: <Eye size={18} />, group: 'Tracking' },
  { type: 'analytics', label: 'Analytics', icon: <BarChart3 size={18} />, group: 'System' },
  { type: 'cycle-monitor', label: 'Cycles', icon: <Crosshair size={18} />, group: 'System' },
  { type: 'journal', label: 'Journal', icon: <BookOpen size={18} />, group: 'System' },
  { type: 'reports', label: 'Reports', icon: <BookMarked size={18} />, group: 'System' },
  { type: 'scorecard', label: 'Scorecard', icon: <Gauge size={18} />, group: 'System' },
]

export function Sidebar() {
  const { sidebarCollapsed, toggleSidebar, openPanel } = useWorkspaceStore()

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
    </div>
  )
}
