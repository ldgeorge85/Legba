import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function truncateUuid(uuid: string): string {
  return uuid.slice(0, 8)
}

export function formatConfidence(value: number): string {
  return `${Math.round(value * 100)}%`
}

export function categoryColor(category: string): string {
  const colors: Record<string, string> = {
    conflict: 'bg-red-500/20 text-red-400 border-red-500/30',
    political: 'bg-violet-500/20 text-violet-400 border-violet-500/30',
    economic: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
    technology: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
    health: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
    environment: 'bg-green-500/20 text-green-400 border-green-500/30',
    social: 'bg-pink-500/20 text-pink-400 border-pink-500/30',
    disaster: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
    other: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
  }
  return colors[category] ?? colors.other
}

export function entityTypeColor(type: string): string {
  const colors: Record<string, string> = {
    person: 'bg-blue-500/20 text-blue-400',
    organization: 'bg-purple-500/20 text-purple-400',
    location: 'bg-green-500/20 text-green-400',
    country: 'bg-emerald-500/20 text-emerald-400',
    event: 'bg-amber-500/20 text-amber-400',
    concept: 'bg-cyan-500/20 text-cyan-400',
    weapon: 'bg-red-500/20 text-red-400',
    military_unit: 'bg-rose-500/20 text-rose-400',
    infrastructure: 'bg-slate-500/20 text-slate-400',
  }
  return colors[type] ?? 'bg-gray-500/20 text-gray-400'
}
