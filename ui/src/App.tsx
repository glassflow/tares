import EditionShell from "./editions/Shell";
import { useWho } from "./editions/who";

/** The console: the factory's shell around the page. It waits for whoami, as the pages read who is
 *  signed in from their first render. */
export default function App() {
  const { loaded } = useWho();
  if (!loaded) return null;
  return <EditionShell />;
}
