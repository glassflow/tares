import { useCallback, useRef, useState } from "react";

import { authHeader } from "../api";
import type { Proposal } from "./proposals";

// One streaming client for the in-app assistant, shared by the Ask chat and the AI-guided
// project builder (TR-243). It owns the raw fetch to POST /api/agent/chat, the SSE parse and the
// AbortController; what it does NOT own is the transcript. Each caller keeps its own message
// shape and folds the events it receives into it, so Ask's markdown turns and the builder's
// step panels can render the same stream differently.

export type WireMessage = { role: "user" | "assistant"; content: string };

export type StreamEvent =
  | { type: "text"; text: string }
  | { type: "tool"; id: string; name: string; input: unknown }
  | { type: "tool_done"; id: string; ms: number; ok: boolean; preview: string }
  | { type: "proposal"; proposal: Proposal }
  | { type: "error"; detail: string }
  | { type: "done" };

export type SendOptions = {
  mode?: "ask" | "build";
  step?: "sources" | "watch" | "agent";
};

/** A non-200 reply as one readable line. The daemon answers with JSON `{detail}`; anything
 *  else (an nginx 502 page, an empty body) is a proxy or network failure, and its HTML is
 *  not something to show the user. */
async function httpErrorDetail(res: Response): Promise<string> {
  const body = await res.text().catch(() => "");
  try {
    const parsed = JSON.parse(body);
    if (parsed && typeof parsed.detail === "string") return parsed.detail;
  } catch { /* not JSON */ }
  const status = `${res.status} ${res.statusText}`.trim();
  if (res.status >= 500 || !body.trim() || /<html/i.test(body)) {
    return `the server returned ${status}; try sending again`;
  }
  return body.slice(0, 300);
}

export function useAgentStream() {
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  /** Send one turn and feed every server event to `onEvent` as it arrives. Resolves when the
   *  stream ends, whether by `done`, an HTTP error (reported as an `error` event) or Stop (no
   *  event: an aborted run is the user's choice, not a failure to report). */
  const send = useCallback(async (messages: WireMessage[], onEvent: (e: StreamEvent) => void,
                                  opts: SendOptions = {}) => {
    if (abortRef.current) return;
    setStreaming(true);
    const ctl = new AbortController();
    abortRef.current = ctl;
    try {
      const body: Record<string, unknown> = { messages };
      if (opts.mode) body.mode = opts.mode;
      if (opts.step) body.step = opts.step;
      const res = await fetch("/api/agent/chat", {
        method: "POST",
        signal: ctl.signal,
        headers: { "content-type": "application/json", ...authHeader() },
        body: JSON.stringify(body),
      });
      if (!res.ok || !res.body) {
        onEvent({ type: "error", detail: await httpErrorDetail(res) });
        return;
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
          if (!chunk.startsWith("data: ")) continue;
          const e = JSON.parse(chunk.slice(6));
          if (e.type === "proposal") {
            onEvent({ type: "proposal", proposal: { id: e.id, kind: e.kind, ...e.payload } as Proposal });
          } else {
            onEvent(e as StreamEvent);
          }
        }
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        onEvent({ type: "error", detail: String((err as Error).message ?? err) });
      }
    } finally {
      abortRef.current = null;
      setStreaming(false);
    }
  }, []);

  const stop = useCallback(() => abortRef.current?.abort(), []);

  return { send, stop, streaming };
}
