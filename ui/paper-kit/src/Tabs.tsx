/* vendored from glassflow/arche-design paper-kit-v2 b0f2a89c9b746ebbc4a4bd6968c128ccd414afb2: do not edit, run npm run paper:sync */
import * as React from "react";

import { cn } from "./lib/cn";
import { Tabs as BaseTabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs";

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
      <TabsList aria-label={label} className="overflow-x-auto [&>[data-tab-line]]:bg-brand">
        {items.map((t) => (
          <TabsTrigger
            key={t.value}
            value={t.value}
            disabled={t.disabled}
            className="data-[state=active]:border-b-brand data-[state=active]:hover:border-b-brand"
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
