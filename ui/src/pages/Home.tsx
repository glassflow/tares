import { Navigate } from "react-router-dom";

import { useOwnProjects } from "../components/readiness";

// `/` has no page of its own: a cell with no project of the person's own opens Start (the list
// and the first project's goal), any other opens Projects. The cloud login handoff lands here,
// so whatever it carries in the address goes along. The numbers Overview used to show (model
// spend, storage) are in Settings.
export default function Home() {
  const { own } = useOwnProjects();
  if (!own) return <div className="dim">loading…</div>;
  return <Navigate to={(own.length ? "/projects" : "/start") + window.location.search} replace />;
}
