/**
 * markdownComponents — the ONE dark-theme react-markdown element map.
 *
 * The project does NOT enable @tailwindcss/typography on these surfaces (the
 * `prose` classes would be inert), so every markdown element is styled directly
 * here. Extracted to its own module (was defined on `WorldAssessment`) so the
 * reading kit — `CitedProse`, the Inspector, the Journal, the desk card — all
 * render prose IDENTICALLY off one source, with no import cycle back through a
 * heavy panel module.
 */
import type { Components } from 'react-markdown'

/** Dark-theme element map for a markdown body (replaces the absent `prose`). */
export const MD_COMPONENTS: Components = {
  h1: ({ children }) => (
    <h1 className="mb-3 mt-5 text-lg font-semibold text-slate-100 first:mt-0">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="mb-2 mt-5 text-base font-semibold text-slate-100 first:mt-0">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="mb-2 mt-4 text-sm font-semibold text-slate-200 first:mt-0">{children}</h3>
  ),
  p: ({ children }) => <p className="mb-3 leading-relaxed text-slate-300">{children}</p>,
  ul: ({ children }) => (
    <ul className="mb-3 list-disc space-y-1 pl-5 text-slate-300 marker:text-slate-600">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="mb-3 list-decimal space-y-1 pl-5 text-slate-300 marker:text-slate-600">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-accent-info underline decoration-dotted hover:text-blue-300"
    >
      {children}
    </a>
  ),
  strong: ({ children }) => <strong className="font-semibold text-slate-100">{children}</strong>,
  em: ({ children }) => <em className="italic text-slate-300">{children}</em>,
  blockquote: ({ children }) => (
    <blockquote className="mb-3 border-l-2 border-slate-700 pl-3 text-slate-400 italic">
      {children}
    </blockquote>
  ),
  code: ({ children }) => (
    <code className="rounded bg-surface-50 px-1 py-0.5 font-mono text-[0.85em] text-slate-200">
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <pre className="mb-3 overflow-auto rounded border border-slate-800 bg-surface-300 p-3 text-xs text-slate-200">
      {children}
    </pre>
  ),
  hr: () => <hr className="my-4 border-slate-800" />,
  table: ({ children }) => (
    <div className="mb-3 overflow-auto">
      <table className="w-full border-collapse text-sm text-slate-300">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-slate-800 bg-surface-100 px-2 py-1 text-left font-medium text-slate-200">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-slate-800 px-2 py-1 align-top">{children}</td>
  ),
}
