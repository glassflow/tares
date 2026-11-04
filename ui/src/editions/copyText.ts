/** Copy text to the clipboard. It rejects when the page has no clipboard (a cell served over plain
 *  http has none) or the browser refuses, so a caller's failure path covers both. */
export async function copyText(text: string): Promise<void> {
  if (!navigator.clipboard) throw new Error("no clipboard");
  await navigator.clipboard.writeText(text);
}
