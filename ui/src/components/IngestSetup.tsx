import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { copyText } from "../editions/copyText";

// Setup hints shown next to a push source's ingest URL. The URL is the address; on a secured
// instance (`tares up --auth`) the producer ALSO needs an ingest API key in the Authorization
// header. At source creation we have the freshly minted key (`authKey`) and embed it in the snippet;
// on the detail page later we don't (it's show-once), so we show a placeholder + a pointer to it.
// Renders nothing when the instance is open (no auth) — the URL alone accepts events.
export default function IngestSetup({ connector, url, authKey }: {
  connector: string; url: string; authKey?: string;
}) {
  const [authRequired, setAuthRequired] = useState<boolean>();
  useEffect(() => { api.health().then((h) => setAuthRequired(h.auth_required)).catch(() => {}); }, []);

  const key = authKey ?? "<your ingest key>";

  // Alertmanager always shows its receiver YAML (the operator needs it regardless); the auth block
  // folds in only when the instance requires it.
  if (connector === "alertmanager") {
    const authBlock = authRequired
      ? `\n        http_config:\n          authorization:\n            type: Bearer\n            credentials: ${key}`
      : "";
    const yaml =
      `# alertmanager.yml\nreceivers:\n  - name: tares\n    webhook_configs:\n` +
      `      - url: ${url}\n        send_resolved: true${authBlock}\nroute:\n  receiver: tares`;
    return (
      <div className="ingest-setup">
        <p className="muted">
          Add this receiver to your <span className="mono">alertmanager.yml</span> and reload
          Alertmanager. Every alert it routes here becomes an event.
        </p>
        <div className="ingest-url">
          <code className="mono" style={{ whiteSpace: "pre-wrap" }}>{yaml}</code>
          <CopyBtn text={yaml} label="copy" />
        </div>
        {authRequired && <KeyNote authKey={authKey} />}
      </div>
    );
  }

  if (!authRequired) return null;   // open instance; the URL alone accepts events

  const snippet = connector === "otlp"
    ? [
        `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`,
        `OTEL_EXPORTER_OTLP_ENDPOINT=${new URL(url).origin}`,
        `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer ${key}`,
      ].join("\n")
    : [
        `curl -X POST ${url} \\`,
        `  -H 'Content-Type: application/json' \\`,
        `  -H 'Authorization: Bearer ${key}' \\`,
        `  -d '{"message": "hello from my producer"}'`,
      ].join("\n");

  return (
    <div className="ingest-setup">
      <KeyNote authKey={authKey} />
      <p className="muted" style={{ marginTop: 12 }}>
        {connector === "otlp"
          ? <>Exporter setup; Tares accepts OTLP/HTTP <strong>JSON</strong> (not protobuf); use a
              JSON-capable exporter, e.g. the OTel Collector's <span className="mono">otlphttp</span>{" "}
              exporter with <span className="mono">encoding: json</span>:</>
          : <>Test it right away:</>}
      </p>
      <div className="ingest-url">
        <code className="mono" style={{ whiteSpace: "pre-wrap" }}>{snippet}</code>
        <CopyBtn text={snippet} label="copy" />
      </div>
    </div>
  );
}

// The producer's auth requirement. With the freshly minted key in hand (creation), say "copy it
// now"; without it (detail page), point at the key that was minted for the source or Security.
function KeyNote({ authKey }: { authKey?: string }) {
  return authKey ? (
    <p className="muted">
      This instance requires auth; send this source's <strong>ingest key</strong> as{" "}
      <span className="mono">Authorization: Bearer …</span>. Copy it now; it's shown once and listed
      under <Link to="/settings">Settings</Link>.
    </p>
  ) : (
    <p className="muted">
      This instance requires auth; the producer needs this source's <strong>ingest key</strong> in{" "}
      <span className="mono">Authorization: Bearer …</span>. Use the key shown when you created the
      source, or mint one under <Link to="/settings">Settings</Link> (scope <span className="mono">ingest</span>).
    </p>
  );
}

function CopyBtn({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button onClick={() => {
      copyText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }, () => {});
    }}>{copied ? "copied" : label}</button>
  );
}
