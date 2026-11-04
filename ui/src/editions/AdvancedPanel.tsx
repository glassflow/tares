import { ListRow } from "../components/edition";

// Settings, Advanced: the building blocks of a workspace, for the factory and sre editions. They
// are today's pages, reached from here; nothing in the menu leads to them.
const BLOCKS: { to: string; label: string; text: string }[] = [
  { to: "/sources", label: "Connections", text: "The tools and services this workspace receives data from." },
  { to: "/explore", label: "Explore your data", text: "Look through the events and details Tares holds." },
  { to: "/resources", label: "All resources", text: "Every connection, rule and agent in this workspace, in one list." },
  { to: "/connect", label: "Connect", text: "Give an outside agent access to your data over MCP." },
  { to: "/reads", label: "Reads", text: "See what outside agents asked for, and when." },
];

export default function AdvancedPanel() {
  return (
    <div data-testid="advanced-panel" className="grid gap-4">
      <section className="min-w-0 border border-border bg-card">
        {BLOCKS.map((b) => (
          <ListRow key={b.to} to={b.to} data-testid={`advanced-${b.to.slice(1)}`}>
            <div className="min-w-0">
              <div className="font-semibold text-foreground">{b.label}</div>
              <div className="text-sm text-muted-foreground">{b.text}</div>
            </div>
          </ListRow>
        ))}
      </section>
      <p className="m-0 text-sm text-muted-foreground">Agents and rules live in each project, under Advanced setup.</p>
    </div>
  );
}
