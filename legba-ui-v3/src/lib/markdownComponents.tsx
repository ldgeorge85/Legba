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
    <h1 className="mb-3 mt-5 text-lg font-semibold text-ink-1 first:mt-0">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="mb-2 mt-5 text-base font-semibold text-ink-1 first:mt-0">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="mb-2 mt-4 text-sm font-semibold text-ink-1 first:mt-0">{children}</h3>
  ),
  p: ({ children }) => <p className="mb-3 leading-relaxed text-ink-1">{children}</p>,
  ul: ({ children }) => (
    <ul className="mb-3 list-disc space-y-1 pl-5 text-ink-1 marker:text-ink-3">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="mb-3 list-decimal space-y-1 pl-5 text-ink-1 marker:text-ink-3">{children}</ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-accent-info underline decoration-dotted hover:decoration-solid"
    >
      {children}
    </a>
  ),
  strong: ({ children }) => <strong className="font-semibold text-ink-1">{children}</strong>,
  em: ({ children }) => <em className="italic text-ink-2">{children}</em>,
  blockquote: ({ children }) => (
    <blockquote className="mb-3 border-l-2 border-line-strong pl-3 text-ink-2 italic">
      {children}
    </blockquote>
  ),
  code: ({ children }) => (
    <code className="rounded bg-surf-3 px-1 py-0.5 font-mono text-[0.85em] text-ink-1">
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <pre className="mb-3 overflow-auto rounded border border-line bg-surf-base p-3 text-xs text-ink-1">
      {children}
    </pre>
  ),
  hr: () => <hr className="my-4 border-line" />,
  table: ({ children }) => (
    <div className="mb-3 overflow-auto">
      <table className="w-full border-collapse text-sm text-ink-1">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-line bg-surf-2 px-2 py-1 text-left font-medium text-ink-1">
      {children}
    </th>
  ),
  td: ({ children }) => <td className="border border-line px-2 py-1 align-top">{children}</td>,
}
