// Grouped numbers, said one way across the console (TR-1261): "1,100", "12,408". The only place
// besides lib/time.ts that may call toLocaleString (the time selector in `npm run design-contract`).

/** "1,100": digits grouped the English way, whatever the viewer's locale. */
export const formatCount = (n: number) => n.toLocaleString("en-US");

/** "0.0025", "1,250.5": grouped, with at most `digits` decimals (a chart's gridline values). */
export const formatDecimal = (n: number, digits: number) => n.toLocaleString("en-US", { maximumFractionDigits: digits });
