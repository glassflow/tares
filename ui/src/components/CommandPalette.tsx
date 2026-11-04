import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import AskChat from "./AskChat";
import { Close } from "./icons";

// The ⌘K overlay: the Ask assistant, floating over whatever page you're on, so you can ask a
// question without navigating away and losing your place. Same engine as the /ask page.
export default function CommandPalette() {
  const [open, setOpen] = useState(false);
  // A question handed over by the edition shell's search field (a `tares:ask` window event); `seq`
  // makes AskChat start over for each new one. Cleared on close so ⌘K never sends it again.
  const [initial, setInitial] = useState("");
  const [seq, setSeq] = useState(0);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setInitial("");
        setOpen((o) => !o);
      } else if (e.key === "Escape") {
        setInitial("");
        setOpen(false);
      }
    };
    const onAsk = (e: Event) => {
      const q = String((e as CustomEvent).detail ?? "").trim();
      setInitial(q);
      if (q) setSeq((n) => n + 1);
      setOpen(true);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("tares:ask", onAsk);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("tares:ask", onAsk);
    };
  }, []);
  const close = () => { setInitial(""); setOpen(false); };

  if (!open) return null;
  return (
    <div className="cmdk-overlay" onClick={close}>
      <div className="cmdk" role="dialog" aria-label="Ask your data" onClick={(e) => e.stopPropagation()}>
        <div className="cmdk-head">
          <span className="cmdk-title">Ask your data</span>
          <div className="cmdk-actions">
            <Link to="/ask" className="cmdk-link" onClick={close}>Open in Ask ↗</Link>
            <button className="sheet-close" onClick={close} aria-label="Close"><Close /></button>
          </div>
        </div>
        <div className="cmdk-body">
          <AskChat key={seq} initial={initial} />
        </div>
      </div>
    </div>
  );
}
