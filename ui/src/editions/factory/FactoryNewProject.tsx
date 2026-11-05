import * as React from "react";
import { Link } from "react-router-dom";

import { Button, Input, ListSection, Toaster, useToast } from "../../components/edition";
import { cn } from "../../lib/utils";
import { copyText } from "../copyText";

// New project on a factory cell (TR-1014). A software factory project starts as a spec, written with
// Claude Code in the repo it builds on: `tares factory spec "<idea>"` (claude-plugin/bin/factory
// cmd_spec). Claude asks the questions, writes the spec, milestones and tickets into a project here,
// and ends by asking whether to hand it to the crew. This page says that and gives the one command;
// it reads nothing from the cell. The generic template gallery and setup are not for this edition.

const MUTED = "text-[13px] text-muted-foreground";

/** The command for an idea: the idea in double quotes, its own quotes and backslashes escaped. */
export function specCommand(idea: string): string {
  const t = idea.replace(/\s+/g, " ").trim();
  const said = t ? t.replace(/(["\\$`])/g, "\\$1") : "what you want to build";
  return `tares factory spec "${said}"`;
}

const NEXT = [
  "Claude asks you about the project, one question at a time.",
  "It writes the spec, the milestones and the tickets into a new project here.",
  "The project shows on Projects while you plan it.",
  "At the end Claude asks whether to hand it to the crew, who build the tickets.",
];

export default function FactoryNewProject() {
  const [idea, setIdea] = React.useState("");
  const { toast } = useToast();
  const command = specCommand(idea);
  const copy = () => copyText(command).then(
    () => toast("Copied. Paste it in a terminal in the repo the project builds on."),
    () => toast("Could not copy. Select the command and copy it yourself."),
  );
  return (
    <div className="grid gap-5 md:gap-6" data-testid="factory-new-project">
      <div>
        <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">New project</h1>
        <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground">
          A project starts as a spec you write with Claude Code, in the repo it builds on.
        </p>
      </div>
      <ListSection title="Start it in Claude Code" data-testid="new-project-start">
        <div className="grid gap-4 px-4 py-4">
          <Input label="What do you want to build? (optional)" value={idea} onChange={(e) => setIdea(e.target.value)}
                 placeholder="A settings page for the billing team" data-testid="new-project-idea" />
          <div className="grid gap-1.5">
            <div className="text-[13px] font-semibold">In a terminal, in the repo the project builds on</div>
            <div className="flex min-w-0 flex-col gap-2 md:flex-row md:items-center">
              <code className="min-w-0 flex-1 border border-border bg-[var(--input-bg)] px-3 py-2 font-mono text-[13px] [overflow-wrap:anywhere]"
                    data-testid="new-project-command">{command}</code>
              <Button type="button" onClick={copy} data-testid="new-project-copy">Copy the command</Button>
            </div>
            <p className={cn("m-0!", MUTED)}>For a project across several repos, use the folder that holds them.</p>
          </div>
        </div>
      </ListSection>
      <ListSection title="What happens next" data-testid="new-project-next">
        <ol className="m-0! grid list-decimal gap-1.5 py-4 pl-9! pr-4 text-[14px]">
          {NEXT.map((s) => <li key={s}>{s}</li>)}
        </ol>
      </ListSection>
      <p className={cn("m-0", MUTED)} data-testid="new-project-first-time">
        First time here? The Get started list on <Link to="/" className="text-foreground! underline! decoration-border! underline-offset-[3px]">Home</Link> signs
        you in from Claude Code and starts the crew.
      </p>
      <Toaster />
    </div>
  );
}
