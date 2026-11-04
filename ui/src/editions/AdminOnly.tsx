import { Banner } from "../components/edition";
import { useEdition } from "./edition";

/** What a key that can only read sees in place of Settings: the page is for admin keys, and the cell
 *  refuses the read for any other key, so nothing is loaded. A cell with no sign-in counts as admin. */
export function useSettingsRefused(sample: boolean): boolean {
  const { admin } = useEdition();
  return !sample && admin === false;
}

export function SettingsRefused({ testId }: { testId: string }) {
  return (
    <div className="grid gap-6" data-testid={testId}>
      <h1 className="m-0! text-[22px]! md:text-[28px]!">Settings</h1>
      <Banner tone="note" data-testid="settings-admin-only">
        This key can read but not change settings. Use a key that can write to open this page.
      </Banner>
    </div>
  );
}
