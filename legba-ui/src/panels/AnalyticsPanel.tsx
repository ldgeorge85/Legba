import { useMemo } from 'react'
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  BarChart,
  Bar,
} from 'recharts'
import { useEventTimeseries, useEntityDistribution, useSourceHealth } from '@/api/hooks'

// ── Color constants ──

const CATEGORY_COLORS: Record<string, string> = {
  conflict: '#ef4444',
  political: '#8b5cf6',
  economic: '#f59e0b',
  technology: '#06b6d4',
  health: '#10b981',
  environment: '#22c55e',
  social: '#ec4899',
  disaster: '#f97316',
  other: '#6b7280',
}

const ENTITY_TYPE_COLORS: Record<string, string> = {
  person: '#3b82f6',
  organization: '#a855f7',
  location: '#22c55e',
  country: '#10b981',
  event: '#f59e0b',
  concept: '#06b6d4',
  weapon: '#ef4444',
  military_unit: '#f43f5e',
  infrastructure: '#64748b',
}

const CATEGORIES = ['conflict', 'political', 'economic', 'technology', 'health', 'environment', 'social', 'disaster', 'other'] as const

const SOURCE_STATUS_COLORS: Record<string, string> = {
  healthy: '#10b981',
  degraded: '#f59e0b',
  failing: '#ef4444',
}

// ── Dark-theme tooltip ──

function DarkTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-zinc-900 border border-zinc-700 rounded-lg px-3 py-2 shadow-xl text-xs">
      <p className="text-zinc-300 font-medium mb-1">{label}</p>
      {payload.map((entry: any, i: number) => (
        <div key={i} className="flex items-center gap-2">
          <span className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: entry.color }} />
          <span className="text-zinc-400">{entry.name}:</span>
          <span className="text-zinc-100 font-medium">{entry.value}</span>
        </div>
      ))}
    </div>
  )
}

// ── Chart card wrapper ──

function ChartCard({ title, children, className = '' }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={`bg-card border border-border rounded-lg p-4 ${className}`}>
      <h4 className="text-sm font-medium text-foreground mb-3">{title}</h4>
      {children}
    </div>
  )
}

// ── Chart 1: Event Volume (stacked area) ──

function EventVolumeChart() {
  const { data: timeseries, isLoading } = useEventTimeseries(30)

  const chartData = useMemo(() => {
    if (!timeseries) return []
    return timeseries.map((d) => ({
      ...d,
      // Shorten date label: "2026-03-15" -> "Mar 15"
      label: new Date(d.date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
    }))
  }, [timeseries])

  if (isLoading) return <ChartCard title="Event Volume"><LoadingSkeleton height={240} /></ChartCard>
  if (!chartData.length) return <ChartCard title="Event Volume"><EmptyState /></ChartCard>

  return (
    <ChartCard title="Event Volume (30d)">
      <ResponsiveContainer width="100%" height={240}>
        <AreaChart data={chartData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <defs>
            {CATEGORIES.map((cat) => (
              <linearGradient key={cat} id={`grad-${cat}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={CATEGORY_COLORS[cat]} stopOpacity={0.4} />
                <stop offset="95%" stopColor={CATEGORY_COLORS[cat]} stopOpacity={0.05} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
          <XAxis
            dataKey="label"
            tick={{ fill: '#a1a1aa', fontSize: 10 }}
            axisLine={{ stroke: '#3f3f46' }}
            tickLine={false}
            interval="preserveStartEnd"
          />
          <YAxis
            tick={{ fill: '#a1a1aa', fontSize: 10 }}
            axisLine={{ stroke: '#3f3f46' }}
            tickLine={false}
            allowDecimals={false}
          />
          <Tooltip content={<DarkTooltip />} />
          {CATEGORIES.map((cat) => (
            <Area
              key={cat}
              type="monotone"
              dataKey={cat}
              stackId="events"
              stroke={CATEGORY_COLORS[cat]}
              fill={`url(#grad-${cat})`}
              strokeWidth={1.5}
              name={cat}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </ChartCard>
  )
}

// ── Chart 2: Category Distribution (donut) ──

function CategoryDistributionChart() {
  const { data: timeseries, isLoading } = useEventTimeseries(30)

  const pieData = useMemo(() => {
    if (!timeseries?.length) return []
    // Sum up all categories across the time range
    const totals: Record<string, number> = {}
    for (const cat of CATEGORIES) {
      const sum = timeseries.reduce((acc, d) => acc + ((d as any)[cat] ?? 0), 0)
      if (sum > 0) totals[cat] = sum
    }
    return Object.entries(totals)
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value)
  }, [timeseries])

  if (isLoading) return <ChartCard title="Category Distribution"><LoadingSkeleton height={240} /></ChartCard>
  if (!pieData.length) return <ChartCard title="Category Distribution"><EmptyState /></ChartCard>

  const total = pieData.reduce((s, d) => s + d.value, 0)

  return (
    <ChartCard title="Category Distribution">
      <div className="flex items-center gap-4">
        <ResponsiveContainer width="55%" height={240}>
          <PieChart>
            <Pie
              data={pieData}
              cx="50%"
              cy="50%"
              innerRadius={55}
              outerRadius={85}
              paddingAngle={2}
              dataKey="value"
              stroke="none"
            >
              {pieData.map((entry) => (
                <Cell key={entry.name} fill={CATEGORY_COLORS[entry.name] ?? '#6b7280'} />
              ))}
            </Pie>
            <Tooltip content={<DarkTooltip />} />
          </PieChart>
        </ResponsiveContainer>
        {/* Legend as a list with percentages */}
        <div className="flex-1 space-y-1.5 text-xs">
          {pieData.map((entry) => (
            <div key={entry.name} className="flex items-center gap-2">
              <span
                className="w-2.5 h-2.5 rounded-sm shrink-0"
                style={{ backgroundColor: CATEGORY_COLORS[entry.name] ?? '#6b7280' }}
              />
              <span className="text-zinc-300 capitalize flex-1">{entry.name}</span>
              <span className="text-zinc-500 tabular-nums">{entry.value}</span>
              <span className="text-zinc-600 tabular-nums w-10 text-right">
                {total > 0 ? `${Math.round((entry.value / total) * 100)}%` : '0%'}
              </span>
            </div>
          ))}
        </div>
      </div>
    </ChartCard>
  )
}

// ── Chart 3: Source Health (bar chart) ──

function SourceHealthChart() {
  const { data: sources, isLoading } = useSourceHealth()

  const chartData = useMemo(() => {
    if (!sources?.length) return []
    // Classify sources by health score into buckets
    let healthy = 0, degraded = 0, failing = 0
    for (const s of sources) {
      if (s.health >= 0.8) healthy++
      else if (s.health >= 0.5) degraded++
      else failing++
    }
    return [
      { status: 'Healthy', count: healthy, fill: SOURCE_STATUS_COLORS.healthy },
      { status: 'Degraded', count: degraded, fill: SOURCE_STATUS_COLORS.degraded },
      { status: 'Failing', count: failing, fill: SOURCE_STATUS_COLORS.failing },
    ]
  }, [sources])

  if (isLoading) return <ChartCard title="Source Health"><LoadingSkeleton height={240} /></ChartCard>
  if (!sources?.length) return <ChartCard title="Source Health"><EmptyState /></ChartCard>

  // Also prepare a top/bottom sources list
  const sorted = [...sources].sort((a, b) => a.health - b.health)
  const worstSources = sorted.slice(0, 5)

  return (
    <ChartCard title="Source Health">
      <div className="flex gap-4">
        <ResponsiveContainer width="50%" height={240}>
          <BarChart data={chartData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis
              dataKey="status"
              tick={{ fill: '#a1a1aa', fontSize: 11 }}
              axisLine={{ stroke: '#3f3f46' }}
              tickLine={false}
            />
            <YAxis
              tick={{ fill: '#a1a1aa', fontSize: 10 }}
              axisLine={{ stroke: '#3f3f46' }}
              tickLine={false}
              allowDecimals={false}
            />
            <Tooltip content={<DarkTooltip />} />
            <Bar dataKey="count" name="Sources" radius={[4, 4, 0, 0]} maxBarSize={48}>
              {chartData.map((entry, i) => (
                <Cell key={i} fill={entry.fill} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
        {/* Worst-performing sources list */}
        <div className="flex-1 overflow-hidden">
          <p className="text-[10px] text-zinc-500 uppercase tracking-wider mb-2">Lowest Health</p>
          <div className="space-y-1.5 text-xs">
            {worstSources.map((s) => (
              <div key={s.source_id} className="flex items-center gap-2">
                <span
                  className="w-2 h-2 rounded-full shrink-0"
                  style={{
                    backgroundColor:
                      s.health >= 0.8 ? SOURCE_STATUS_COLORS.healthy
                        : s.health >= 0.5 ? SOURCE_STATUS_COLORS.degraded
                          : SOURCE_STATUS_COLORS.failing,
                  }}
                />
                <span className="text-zinc-300 truncate flex-1" title={s.name}>{s.name}</span>
                <span className="text-zinc-500 tabular-nums shrink-0">
                  {Math.round(s.health * 100)}%
                </span>
              </div>
            ))}
          </div>
          <p className="text-[10px] text-zinc-600 mt-2">{sources.length} sources total</p>
        </div>
      </div>
    </ChartCard>
  )
}

// ── Chart 4: Entity Types (horizontal bar chart) ──

function EntityTypesChart() {
  const { data: entities, isLoading } = useEntityDistribution()

  const chartData = useMemo(() => {
    if (!entities?.length) return []
    return [...entities]
      .sort((a, b) => b.count - a.count)
      .slice(0, 12)
      .map((e) => ({
        type: e.type,
        count: e.count,
        fill: ENTITY_TYPE_COLORS[e.type] ?? '#6b7280',
      }))
  }, [entities])

  if (isLoading) return <ChartCard title="Entity Types"><LoadingSkeleton height={240} /></ChartCard>
  if (!chartData.length) return <ChartCard title="Entity Types"><EmptyState /></ChartCard>

  return (
    <ChartCard title="Entity Types">
      <ResponsiveContainer width="100%" height={Math.max(240, chartData.length * 28 + 20)}>
        <BarChart data={chartData} layout="vertical" margin={{ top: 4, right: 16, left: 4, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#27272a" horizontal={false} />
          <XAxis
            type="number"
            tick={{ fill: '#a1a1aa', fontSize: 10 }}
            axisLine={{ stroke: '#3f3f46' }}
            tickLine={false}
            allowDecimals={false}
          />
          <YAxis
            type="category"
            dataKey="type"
            tick={{ fill: '#a1a1aa', fontSize: 11 }}
            axisLine={{ stroke: '#3f3f46' }}
            tickLine={false}
            width={90}
          />
          <Tooltip content={<DarkTooltip />} />
          <Bar dataKey="count" name="Entities" radius={[0, 4, 4, 0]} maxBarSize={20}>
            {chartData.map((entry, i) => (
              <Cell key={i} fill={entry.fill} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartCard>
  )
}

// ── Shared small components ──

function LoadingSkeleton({ height }: { height: number }) {
  return <div className="animate-pulse bg-zinc-800/50 rounded" style={{ height }} />
}

function EmptyState() {
  return (
    <div className="flex items-center justify-center h-40 text-sm text-zinc-600">
      No data available
    </div>
  )
}

// ── Main Panel ──

export function AnalyticsPanel() {
  return (
    <div className="p-4 space-y-4 overflow-auto h-full">
      <h3 className="text-sm font-medium text-foreground">Analytics Dashboard</h3>
      <div className="grid grid-cols-2 gap-4">
        <EventVolumeChart />
        <CategoryDistributionChart />
        <SourceHealthChart />
        <EntityTypesChart />
      </div>
    </div>
  )
}
