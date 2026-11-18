import * as React from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../../api";
import { Banner, Button, Collapse, Input, Panel, Select, Textarea } from "../../components/edition";
import { cn } from "../../lib/utils";
import type { Person } from "../../types";
import { useEdition } from "../edition";
import { addDecision } from "./BrainDecisions.api";
import { ADD_LINE, addErrorWords, FILE_TYPES, fileProblem, formProblem, MAX_TITLE, READ_ONLY_LINE, RECORD_WORDS } from "./brainDecisionsText";

// Record a decision (TR-1444), at /brain/decisions/new: a decision that lives nowhere else, pasted or as a
// Markdown, text or PDF file, with who decided. It reads the people for Decided by when it opens. Once the
// cell keeps it, the page goes back to Decisions with the new row, which shows at the top there.

const MUTED = "text-[13px] text-muted-foreground";

const readFile = (f: File): Promise<string> => new Promise((resolve, reject) => {
  const fr = new FileReader();
  fr.onload = () => { const s = String(fr.result ?? ""); resolve(s.slice(s.indexOf(",") + 1)); };
  fr.onerror = () => reject(fr.error ?? new Error("read"));
  fr.readAsDataURL(f);
});

function Head() {
  return (
    <div>
      <Link to="/brain/decisions" className={cn(MUTED, "no-underline hover:underline")} data-testid="brain-decision-new-back">Decisions</Link>
      <h1 className="m-0 mb-1.5 mt-1 text-[22px] md:text-[28px]">{RECORD_WORDS}</h1>
      <p className="m-0 max-w-[640px] text-[15px] text-muted-foreground">{ADD_LINE}</p>
    </div>
  );
}

export default function BrainDecisionNew() {
  const navigate = useNavigate();
  const { canWrite, personId, personName } = useEdition();
  const [people, setPeople] = React.useState<Person[] | null>(null);
  const [title, setTitle] = React.useState("");
  const [text, setText] = React.useState("");
  const [file, setFile] = React.useState<File | null>(null);
  const [who, setWho] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [problem, setProblem] = React.useState("");
  const fileInput = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    if (canWrite === false) return;
    api.people().then((r) => setPeople(r.people), () => { /* the picker keeps you alone */ });
  }, [canWrite]);

  // the viewer first, marked as you; then everyone else by name
  const options = React.useMemo(() => {
    const known = people ?? (personId && personName ? [{ id: personId, name: personName } as Person] : []);
    const list = [...known].sort((a, b) => (a.id === personId ? -1 : b.id === personId ? 1 : a.name.localeCompare(b.name)));
    return list.map((p) => ({ value: p.id, label: p.id === personId ? `${p.name} (you)` : p.name }));
  }, [people, personId, personName]);
  const chosen = who || (options.some((o) => o.value === personId) ? personId ?? "" : options[0]?.value ?? "");

  const pick = (f: File | null) => {
    setFile(f);
    setProblem(f ? fileProblem({ name: f.name, size: f.size }) : "");
    if (!f && fileInput.current) fileInput.current.value = "";
  };
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const p = formProblem({ title, text, file: file ? { name: file.name, size: file.size } : null });
    if (p) { setProblem(p); return; }
    setBusy(true); setProblem("");
    try {
      const body = file
        ? { title: title.trim(), file: { name: file.name, content_base64: await readFile(file) } }
        : { title: title.trim(), text };
      const row = await addDecision(chosen ? { ...body, decided_by: chosen } : body);
      navigate("/brain/decisions", { state: { added: row } });
    } catch (err) {
      setProblem(addErrorWords(err as { status?: number; message?: string }));
      setBusy(false);
    }
  };

  if (canWrite === false) {
    return (
      <div className="grid min-w-0 gap-5 md:gap-6" data-testid="brain-decision-new">
        <Head />
        <p className={cn("m-0", MUTED)} data-testid="brain-decision-add-read-only">{READ_ONLY_LINE}</p>
      </div>
    );
  }
  return (
    <div className="grid min-w-0 max-w-[880px] gap-5 md:gap-6" data-testid="brain-decision-new">
      <Head />
      <Panel data-testid="brain-decision-add">
        <form className="grid gap-3" onSubmit={submit} noValidate>
          <div className="grid gap-3 md:grid-cols-2">
            <Input label="Title" value={title} maxLength={MAX_TITLE} placeholder="What was decided" onChange={(e) => setTitle(e.target.value)} data-testid="brain-decision-add-title" />
            {options.length > 0 && (
              <Select label="Decided by" options={options} value={chosen} onValueChange={setWho} data-testid="brain-decision-add-who" />
            )}
          </div>
          <Textarea label="The decision and why" rows={8} value={text} disabled={file !== null}
                    hint={file ? "The file is added instead of pasted text." : undefined}
                    placeholder="Paste the text, or upload a Markdown or PDF file below" onChange={(e) => setText(e.target.value)} data-testid="brain-decision-add-text" />
          {/* the native file picker, opened by the Upload button; hidden so the button is the one control */}
          <div className="sr-only" aria-hidden="true">
            <Input ref={fileInput} label="File" type="file" accept={FILE_TYPES.join(",")} tabIndex={-1}
                   onChange={(e) => pick(e.target.files?.[0] ?? null)} data-testid="brain-decision-add-file" />
          </div>
          {file && (
            <div className="flex min-w-0 flex-wrap items-center gap-2 text-[13px]" data-testid="brain-decision-add-file-name">
              <span className="font-mono [overflow-wrap:anywhere]" data-user-text>{file.name}</span>
              <Button variant="quiet" className="h-7 px-2 text-[13px]" onClick={() => pick(null)} data-testid="brain-decision-add-file-clear">Remove file</Button>
            </div>
          )}
          <Collapse>{problem && <Banner tone="error" data-testid="brain-decision-add-error"><span data-user-text>{problem}</span></Banner>}</Collapse>
          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" busy={busy} data-testid="brain-decision-add-submit">Record decision</Button>
            <Button variant="secondary" disabled={busy} onClick={() => fileInput.current?.click()} data-testid="brain-decision-add-upload">
              {file ? "Choose another file" : "Upload a file"}
            </Button>
            <Button asChild variant="quiet"><Link to="/brain/decisions" data-testid="brain-decision-add-cancel">Cancel</Link></Button>
          </div>
        </form>
      </Panel>
    </div>
  );
}
