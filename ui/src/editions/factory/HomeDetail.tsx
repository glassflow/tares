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
