/* vendored from glassflow/arche-design paper-kit-v2 b0f2a89c9b746ebbc4a4bd6968c128ccd414afb2: do not edit, run npm run paper:sync */
import * as React from "react";

// The marks the primitives draw. The console draws them from its own icon set; the same 24x24 stroke
// paths are inlined here so the kit has no icon dependency. The markup is the console's, minus its
// `ico` class, which the className every caller passes replaced anyway.
function Icon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    />
  );
}

type IconProps = React.SVGProps<SVGSVGElement>;

export const Check = (p: IconProps) => (
  <Icon {...p}>
    <path d="M20 6 9 17l-5-5" />
  </Icon>
);

export const ChevronDown = (p: IconProps) => (
  <Icon {...p}>
    <path d="m6 9 6 6 6-6" />
  </Icon>
);

export const Close = (p: IconProps) => (
  <Icon {...p}>
    <path d="M18 6 6 18M6 6l12 12" />
  </Icon>
);
