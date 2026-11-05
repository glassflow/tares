/** The crew's text puts a command in backticks; the page shows it in a monospace span. */
export function Detail({ text }: { text: string }) {
  const parts = text.split(/`([^`]+)`/);
  return (
    <>
      {parts.map((part, i) => (i % 2 === 1
        ? <code key={i} className="bg-muted px-1 font-mono text-[12.5px]" data-words-exempt>{part}</code>
        : part))}
    </>
  );
}

/** An item's lines, one under the other (the calls in a "calls to confirm" item). */
export function DetailLines({ lines }: { lines?: string[] }) {
  if (!lines || lines.length === 0) return null;
  return (
    <ul className="m-0! mt-1! grid list-none! gap-1 p-0! [&>li]:list-none!" data-testid="detail-lines">
      {lines.map((l, i) => (
        <li key={i} className="line-clamp-2 border-l-2 border-border pl-2.5 [overflow-wrap:anywhere]" title={l} data-user-text><Detail text={l} /></li>
      ))}
    </ul>
  );
}
