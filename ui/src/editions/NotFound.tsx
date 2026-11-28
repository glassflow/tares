import { Link } from "react-router-dom";

import { Button, EmptyState } from "../components/edition";

/** An address the console has no page for. */
export default function NotFound() {
  return (
    <EmptyState title="Page not found" data-testid="not-found"
                action={<Button variant="secondary" asChild><Link to="/projects">See the projects</Link></Button>}>
      This page does not exist. The address may be old, or mistyped.
    </EmptyState>
  );
}
