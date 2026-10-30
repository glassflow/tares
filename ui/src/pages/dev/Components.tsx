import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { Toaster } from "@/components/ui/toaster";
import { toast } from "@/components/ui/use-toast";
import { applyTheme, currentTheme, type Theme } from "@/theme";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-8">
      <h2 className="mb-3 text-base font-semibold text-foreground">{title}</h2>
      {children}
    </section>
  );
}

/** /dev/components: the component library on one page. Linked from no menu. */
export default function Components() {
  const [theme, setTheme] = useState<Theme>(currentTheme());
  const [pct, setPct] = useState(40);
  const [flag, setFlag] = useState(true);
  const [alerts, setAlerts] = useState(true);

  function pick(t: Theme) {
    applyTheme(t);
    setTheme(t);
  }

  return (
    <div className="mx-auto w-full max-w-3xl p-6 text-foreground" data-testid="dev-components">
      <div className="mb-6 flex items-center justify-between gap-4">
        <h1 className="m-0">Components</h1>
        <div className="flex gap-2" data-testid="theme-switch">
          <Button
            size="sm"
            variant={theme === "light" ? "default" : "outline"}
            onClick={() => pick("light")}
            data-testid="theme-light"
          >
            Light
          </Button>
          <Button
            size="sm"
            variant={theme === "dark" ? "default" : "outline"}
            onClick={() => pick("dark")}
            data-testid="theme-dark"
          >
            Dark
          </Button>
        </div>
      </div>

      <Section title="Button">
        <div className="flex flex-wrap items-center gap-2" data-testid="prim-button">
          <Button>Default</Button>
          <Button variant="secondary">Secondary</Button>
          <Button variant="outline">Outline</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="destructive">Destructive</Button>
          <Button variant="link">Link</Button>
          <Button size="sm">Small</Button>
          <Button size="lg">Large</Button>
          <Button disabled>Disabled</Button>
        </div>
      </Section>

      <Section title="Tabs">
        <Tabs defaultValue="logs" data-testid="prim-tabs">
          <TabsList>
            <TabsTrigger value="logs">Logs</TabsTrigger>
            <TabsTrigger value="metrics">Metrics</TabsTrigger>
            <TabsTrigger value="deploys">Deploys</TabsTrigger>
          </TabsList>
          <TabsContent value="logs">Recent log lines for service-a.</TabsContent>
          <TabsContent value="metrics">Request rate and latency for service-a.</TabsContent>
          <TabsContent value="deploys">Last deploys of service-a.</TabsContent>
        </Tabs>
      </Section>

      <Section title="Dialog">
        <Dialog>
          <DialogTrigger asChild>
            <Button variant="outline" data-testid="prim-dialog-open">
              Open dialog
            </Button>
          </DialogTrigger>
          <DialogContent data-testid="prim-dialog">
            <DialogHeader>
              <DialogTitle>Archive project-x</DialogTitle>
              <DialogDescription>The project stops running. You can restore it later.</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="destructive">Archive</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </Section>

      <Section title="Progress">
        <div className="grid gap-3">
          <Progress value={pct} data-testid="prim-progress" />
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={() => setPct((p) => Math.max(0, p - 20))}>
              Less
            </Button>
            <Button size="sm" variant="outline" onClick={() => setPct((p) => Math.min(100, p + 20))}>
              More
            </Button>
          </div>
        </div>
      </Section>

      <Section title="Dropdown menu">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" data-testid="prim-dropdown">
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
      </Section>

      <Section title="Skeleton">
        <div className="grid gap-2" data-testid="prim-skeleton">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      </Section>

      <Section title="Input and textarea">
        <div className="grid gap-3">
          <Input placeholder="Project name, for example project-x" data-testid="prim-input" />
          <Textarea placeholder="What should the agent do?" data-testid="prim-textarea" />
        </div>
      </Section>

      <Section title="Select">
        <div className="max-w-xs">
          <Select defaultValue="service-a">
            <SelectTrigger data-testid="prim-select">
              <SelectValue placeholder="Pick a service" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="service-a">service-a</SelectItem>
              <SelectItem value="service-b">service-b</SelectItem>
              <SelectItem value="service-c">service-c</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </Section>

      <Section title="Switch and checkbox">
        <div className="flex items-center gap-6">
          <label className="flex items-center gap-2">
            <Switch checked={flag} onCheckedChange={setFlag} data-testid="prim-switch" />
            Notify me
          </label>
          <label className="flex items-center gap-2">
            <Checkbox defaultChecked data-testid="prim-checkbox" />
            Include drafts
          </label>
        </div>
      </Section>

      <Section title="Toast">
        <Button
          variant="outline"
          data-testid="prim-toast-open"
          onClick={() => toast({ title: "Saved", description: "Settings for project-x are saved." })}
        >
          Show toast
        </Button>
      </Section>

      <Toaster />
    </div>
  );
}
