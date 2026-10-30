import { useState } from "react";

import {
  Banner,
  Button,
  Checkbox,
  Dialog,
  EmptyState,
  Initials,
  Input,
  LineChart,
  ListRow,
  ListSection,
  Panel,
  ProgressBar,
  Select,
  Skeleton,
  SkeletonRows,
  Sparkline,
  Stat,
  StatusDot,
  StatusLabel,
  Switch,
  Tabs,
  Textarea,
  Toaster,
  useToast,
  type Tone,
} from "@/components/edition";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { applyTheme, currentTheme, type Theme } from "@/theme";

const TONES: Tone[] = ["working", "waiting", "ready", "done", "bad", "quiet"];

const SERVICES = [
  { value: "service-a", label: "service-a" },
  { value: "service-b", label: "service-b" },
  { value: "service-c", label: "service-c" },
];

const LONG =
  "service-a-eu-west-checkout-payment-gateway-retry-worker, a name long enough to wrap onto a second line instead of pushing the status off the screen";

const HOURS = Array.from({ length: 12 }, (_, i) => `2026-10-29T${String(i * 2).padStart(2, "0")}:00:00Z`);
const ERRORS = [2, 3, 2, 4, 3, 9, 14, 8, 5, 3, 2, 2];
const TRAFFIC = [8, 9, 8, 10, 9, 11, 12, 11, 10, 9, 9, 8];

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-10">
      <h2 className="m-0! mb-3! text-base! text-foreground">{title}</h2>
      <div className="grid gap-4">{children}</div>
    </section>
  );
}

function Row({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-wrap items-center gap-2">{children}</div>;
}

function Caption({ children }: { children: React.ReactNode }) {
  return <div className="font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">{children}</div>;
}

function Item({ name, tone, children }: { name: string; tone: Tone; children: React.ReactNode }) {
  return (
    <ListRow>
      <StatusDot tone={tone} label={tone} />
      <div className="min-w-0 flex-1 basis-64">
        <div className="font-medium">{name}</div>
        <div className="text-xs text-muted-foreground">{children}</div>
      </div>
      <StatusLabel tone={tone}>{tone}</StatusLabel>
    </ListRow>
  );
}

/** /dev/components: the component library on one page. Linked from no menu. */
export default function Components() {
  const [theme, setTheme] = useState<Theme>(currentTheme());
  const [pct, setPct] = useState(40);
  const [alerts, setAlerts] = useState(true);
  const { toast } = useToast();

  function pick(t: Theme) {
    applyTheme(t);
    setTheme(t);
  }

  return (
    <div className="mx-auto w-full max-w-3xl p-6 text-foreground" data-testid="dev-components">
      <div className="mb-8 flex items-center justify-between gap-4">
        <h1 className="m-0!">Components</h1>
        <div className="flex gap-2" data-testid="theme-switch">
          <Button size="md" variant={theme === "light" ? "primary" : "secondary"} onClick={() => pick("light")} data-testid="theme-light">
            Light
          </Button>
          <Button size="md" variant={theme === "dark" ? "primary" : "secondary"} onClick={() => pick("dark")} data-testid="theme-dark">
            Dark
          </Button>
        </div>
      </div>

      <Section title="Button">
        <Row>
          <Button data-testid="btn-primary">Primary</Button>
          <Button variant="secondary" data-testid="btn-secondary">
            Secondary
          </Button>
          <Button variant="quiet" data-testid="btn-quiet">
            Quiet
          </Button>
        </Row>
        <Row>
          <Button size="lg">Large primary</Button>
          <Button size="lg" variant="secondary">
            Large secondary
          </Button>
          <Button size="lg" variant="quiet">
            Large quiet
          </Button>
        </Row>
        <Row>
          <Button disabled>Disabled</Button>
          <Button variant="secondary" disabled>
            Disabled
          </Button>
          <Button variant="quiet" disabled>
            Disabled
          </Button>
          <Button busy>Saving</Button>
          <Button variant="secondary" busy>
            Saving
          </Button>
        </Row>
        <Row>
          <Button asChild>
            <a href="#links">Link as button</a>
          </Button>
          <Button asChild variant="secondary">
            <a href="#links">Link as secondary</a>
          </Button>
          <Button asChild variant="quiet">
            <a href="#links">Link as quiet</a>
          </Button>
          <Button asChild variant="secondary" disabled>
            <a href="#links">Disabled link</a>
          </Button>
        </Row>
      </Section>

      <Section title="List section and rows">
        <ListSection title="Services" aside={<a href="#all">View all</a>} data-testid="list-section">
          <Item name="service-a" tone="working">
            Checking error rates, 17 seconds ago
          </Item>
          <Item name="service-b" tone="waiting">
            Waiting on Kim for 6 days
          </Item>
          <Item name="service-c" tone="bad">
            Error rate above the limit for 8 minutes
          </Item>
          <ListRow to="/dev/components">
            <Initials name="Alex Kim" />
            <div className="min-w-0 flex-1 basis-64">Router link row: opens this page again</div>
          </ListRow>
          <ListRow as="a" href="#row-link">
            <div className="min-w-0 flex-1 basis-64">Plain link row</div>
          </ListRow>
          <Item name={LONG} tone="ready">
            Long text wraps inside the row
          </Item>
        </ListSection>

        <ListSection title="Many rows" aside="12 of 12">
          {Array.from({ length: 12 }, (_, i) => (
            <Item key={i} name={`project-${i + 1}`} tone={TONES[i % TONES.length]}>
              Updated {i + 1} hours ago
            </Item>
          ))}
        </ListSection>

        <ListSection title="Empty">
          <EmptyState title="No services yet">Connect your monitoring to see services here.</EmptyState>
        </ListSection>

        <ListSection title="Loading">
          <SkeletonRows count={3} />
        </ListSection>
      </Section>

      <Section title="Panel">
        <Panel title="Settings" action={<Button variant="secondary">Edit</Button>} data-testid="panel">
          Alerts for project-x go to #oncall.
        </Panel>
        <Panel>A panel with no title or action.</Panel>
        <Panel title={LONG} action={<Button variant="quiet">Hide</Button>}>
          {LONG}
        </Panel>
      </Section>

      <Section title="Tabs">
        <Tabs
          aria-label="service-a details"
          data-testid="tabs"
          items={[
            { value: "logs", label: "Logs", content: "Recent log lines for service-a." },
            { value: "metrics", label: "Metrics", content: "Request rate and latency for service-a." },
            { value: "deploys", label: "Deploys", content: "Last deploys of service-a." },
            { value: "off", label: "Disabled", content: "Not reachable.", disabled: true },
          ]}
        />
        <Tabs
          aria-label="Many tabs"
          items={Array.from({ length: 10 }, (_, i) => ({
            value: `t${i}`,
            label: `Environment ${i + 1}`,
            content: `Content of environment ${i + 1}.`,
          }))}
        />
      </Section>

      <Section title="Dialog">
        <Row>
          <Dialog
            trigger={<Button variant="secondary" data-testid="dialog-open">Open dialog</Button>}
            title="Archive project-x"
            description="The project stops running. You can restore it later."
            data-testid="dialog"
            footer={
              <>
                <Button>Archive</Button>
              </>
            }
          />
          <Dialog
            trigger={<Button variant="secondary">Dialog with a form</Button>}
            title="Rename project-x"
            description="The new name shows everywhere the project is listed."
            footer={<Button>Save</Button>}
          >
            <Input label="Name" hint="Letters, numbers and dashes." defaultValue="project-x" />
          </Dialog>
          <Dialog
            trigger={<Button variant="secondary">Long dialog</Button>}
            title="Notes for service-a"
            description="Scrolls inside the dialog when it is taller than the screen."
          >
            {Array.from({ length: 12 }, (_, i) => (
              <p key={i} className="m-0 text-sm">
                Note {i + 1}: {LONG}
              </p>
            ))}
          </Dialog>
        </Row>
      </Section>

      <Section title="Empty state">
        <Panel>
          <EmptyState title="Nothing here yet" action={<Button>Add a connection</Button>} data-testid="empty-state">
            Connect GitHub to see pull requests.
          </EmptyState>
        </Panel>
        <Panel>
          <EmptyState title="No incidents">service-a has had none in the last 30 days.</EmptyState>
        </Panel>
      </Section>

      <Section title="Stat">
        <div className="grid grid-cols-2 gap-6 sm:grid-cols-4">
          <Stat label="Open PRs" value="12" note="3 waiting on review" data-testid="stat" />
          <Stat label="Error rate" value="0.4%" />
          <Stat label="A label that is rather long for one cell" value="1,284,903" note="Counted since Monday, a long note that wraps" />
          <Stat label="Sessions" value="0" />
        </div>
      </Section>

      <Section title="Progress bar">
        <ProgressBar value={pct} aria-label="Tickets done" data-testid="progress" />
        <Row>
          <Button size="md" variant="secondary" onClick={() => setPct((p) => Math.max(0, p - 20))}>
            Less
          </Button>
          <Button size="md" variant="secondary" onClick={() => setPct((p) => Math.min(100, p + 20))}>
            More
          </Button>
        </Row>
        <Caption>Empty, with blocked, full</Caption>
        <ProgressBar value={0} aria-label="Nothing done" />
        <ProgressBar value={45} blocked={20} aria-label="Done and blocked" />
        <ProgressBar value={100} aria-label="All done" />
      </Section>

      <Section title="Initials">
        <Row>
          <Initials name="Alex Kim" data-testid="initials" />
          <Initials name="alex.kim@example.dev" />
          <Initials name="Kim" />
          <Initials name="Alexandra Maria Konstantinopoulos" />
        </Row>
      </Section>

      <Section title="Status label and dot">
        {TONES.map((t) => (
          <Row key={t}>
            <StatusDot tone={t} label={t} />
            <StatusLabel tone={t} data-testid={t === "working" ? "status-label" : undefined}>
              {t}
            </StatusLabel>
          </Row>
        ))}
      </Section>

      <Section title="Skeleton">
        <div className="grid gap-2" data-testid="skeleton">
          <Skeleton width="33%" />
          <Skeleton />
          <Skeleton width="66%" />
          <Skeleton width={96} height={36} />
        </div>
        <Panel>
          <SkeletonRows count={2} />
        </Panel>
      </Section>

      <Section title="Form controls">
        <Input label="Project name" hint="Letters, numbers and dashes." placeholder="project-x" data-testid="input" />
        <Input label="Webhook address" hint="Where alerts are sent." error="That address does not look right." defaultValue="not an address" />
        <Input label="Locked" hint="Set by your admin." value="service-a" disabled readOnly />
        <Textarea label="Instructions" hint="What should the agent do?" placeholder="Watch service-a and tell #oncall." data-testid="textarea" />
        <Textarea label="Notes" error="Keep it under 500 characters." defaultValue={LONG} />
        <Select label="Service" hint="The service to watch." options={SERVICES} defaultValue="service-a" data-testid="select" />
        <Select label="Owner" options={SERVICES} placeholder="Pick a service" error="Pick one to continue." />
        <Select label="Locked service" options={SERVICES} defaultValue="service-b" disabled />
        <Switch label="Notify me" hint="One message a day, at most." defaultChecked data-testid="switch" />
        <Switch label="Weekly summary" error="Connect Slack first." />
        <Switch label="Paused" disabled />
        <Checkbox label="Include drafts" hint="Drafts show up in the list." defaultChecked data-testid="checkbox" />
        <Checkbox label="Some repos" checked="indeterminate" onCheckedChange={() => {}} />
        <Checkbox label="I understand the project stops" error="Tick this to continue." />
        <Checkbox label="Not available" disabled />
      </Section>

      <Section title="Banner">
        <Banner data-testid="banner">Your changes are saved for project-x.</Banner>
        <Banner tone="warn" action={<Button variant="secondary">Reconnect</Button>}>
          GitHub needs to be reconnected.
        </Banner>
        <Banner tone="error" action={<Button variant="secondary">Try again</Button>}>
          Could not refresh service-a.
        </Banner>
        <Banner tone="warn">{LONG}</Banner>
      </Section>

      <Section title="Toast">
        <Row>
          <Button variant="secondary" data-testid="toast-open" onClick={() => toast("Settings for project-x are saved.")}>
            Show toast
          </Button>
          <Button variant="secondary" onClick={() => toast("Copied")}>
            Short toast
          </Button>
        </Row>
      </Section>

      <Section title="Sparkline">
        <Row>
          <div className="w-40 text-[color:var(--err)]">
            <Sparkline points={ERRORS} marks={[5]} aria-label="Errors for service-a, with one deploy" data-testid="sparkline" />
          </div>
          <div className="w-40">
            <Sparkline points={TRAFFIC} aria-label="Requests for service-a" />
          </div>
          <div className="w-40">
            <Sparkline points={[4, 4, 4, 4]} aria-label="A flat line for service-b" />
          </div>
          <div className="w-40">
            <Sparkline points={[]} aria-label="No data for service-c" />
          </div>
        </Row>
      </Section>

      <Section title="Line chart">
        <Panel>
          <LineChart
            aria-label="Errors and requests for service-a, with two deploys"
            data-testid="line-chart"
            series={[
              { label: "Errors", points: HOURS.map((t, i) => [t, ERRORS[i]]) },
              { label: "Requests per minute", points: HOURS.map((t, i) => [t, TRAFFIC[i]]) },
            ]}
            marks={[
              { at: HOURS[4], label: "Deploy of service-a" },
              { at: HOURS[8], label: "Deploy of service-b" },
            ]}
          />
        </Panel>
        <Panel>
          <LineChart aria-label="Errors for service-a, one series" series={[{ label: "Errors", points: HOURS.map((t, i) => [t, ERRORS[i]]) }]} height={100} />
        </Panel>
        <Panel>
          <LineChart aria-label="Errors for service-b, empty" series={[{ label: "Errors", points: [] }]} data-testid="line-chart-empty" />
        </Panel>
      </Section>

      <Section title="Dropdown menu">
        <div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="secondary" data-testid="prim-dropdown">
                Actions
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuLabel>service-a</DropdownMenuLabel>
              <DropdownMenuItem>Open</DropdownMenuItem>
              <DropdownMenuItem>Rename</DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuCheckboxItem checked={alerts} onCheckedChange={setAlerts}>
                Alerts on
              </DropdownMenuCheckboxItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </Section>

      <Toaster />
    </div>
  );
}
