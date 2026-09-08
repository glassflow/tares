import { useState } from "react";

import IngestSetup from "./IngestSetup";
import type { Source } from "../types";

export function CopyableUrl({ url }: { url: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="ingest-url">
      <code className="mono">{url}</code>
      <button onClick={() => {
        navigator.clipboard?.writeText(url);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}>{copied ? "copied" : "copy"}</button>
    </div>
  );
}

// The full, copyable ingest URL of a push source, built from the browser's own origin so it is
// correct wherever the console is served. Shown on the source page and, folded into the row, on
// the project page: a push source is nothing until a producer posts to it, and the project page
// is where a builder-made source is first looked at. On a secured instance IngestSetup adds the
// auth-key guidance. OTLP uses the /v1/* endpoints (source chosen by header), no path key.
export default function IngestEndpoint({ source }: { source: Source }) {
  const origin = window.location.origin;
  if (source.connector === "otlp") {
    return (
      <div className="card ingest-card">
        <div className="k">OTLP endpoint</div>
        <CopyableUrl url={`${origin}/v1/logs`} />
        <p className="muted">Point an OTLP/HTTP exporter here (also <span className="mono">/v1/traces</span>, <span className="mono">/v1/metrics</span>).</p>
        <IngestSetup connector="otlp" url={`${origin}/v1/logs`} />
      </div>
    );
  }
  const url = `${origin}/ingest/${source.ingest_key || source.name}`;
  return (
    <div className="card ingest-card">
      <div className="k">ingest endpoint · POST</div>
      <CopyableUrl url={url} />
      <p className="muted">Point your producer (e.g. a Vercel log drain) at this URL.</p>
      <IngestSetup connector={source.connector} url={url} />
    </div>
  );
}
