import * as React from "react";

import { cn } from "@/lib/utils";
import { Tabs as BaseTabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export interface TabItem {
  value: string;
  label: string;
  content: React.ReactNode;
  disabled?: boolean;
}

export interface TabsProps extends Omit<React.ComponentProps<typeof BaseTabs>, "children"> {
  items: TabItem[];
  "aria-label"?: string;
}

export function Tabs({ items, className, defaultValue, value, "aria-label": label, ...props }: TabsProps) {
  return (
    <BaseTabs
      value={value}
      defaultValue={value === undefined ? (defaultValue ?? items[0]?.value) : undefined}
      className={cn("min-w-0", className)}
      {...props}
    >
      <TabsList aria-label={label} className="overflow-x-auto">
        {items.map((t) => (
          <TabsTrigger
            key={t.value}
            value={t.value}
            disabled={t.disabled}
            className="data-[state=active]:border-b-[color:var(--accent)]! data-[state=active]:hover:border-b-[color:var(--accent)]!"
          >
            {t.label}
          </TabsTrigger>
        ))}
      </TabsList>
      {items.map((t) => (
        <TabsContent key={t.value} value={t.value}>
          {t.content}
        </TabsContent>
      ))}
    </BaseTabs>
  );
}
