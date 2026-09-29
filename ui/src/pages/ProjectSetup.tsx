import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import { ErrorState, usePolling } from "../components/bits";
import { Stepper, errText, type FlowStep } from "../components/setup/common";
import { ConnectStep } from "../components/setup/connect";
import { GoalStep, type Who } from "../components/setup/goal";
import { PlanStep, baselineOf, type Baseline } from "../components/setup/plan";
import { TryStep } from "../components/setup/tryit";
import type { Plan, SetupConnect } from "../types";

// Setting up a project, goal first (contract: setup-flow-contract.md, "Console"). It mirrors the
// running page: the person states a goal, sees the whole plan in plain words, adjusts it,
// confirms once, then does only what only they can do (connect data, their own agent, tools),
// optionally runs a practice spike, and lands on the Overview.
//   /projects/new           Goal and Plan; nothing exists until "Looks right, set it up"
//   /projects/:id/setup     Connect and Try it, resumed at the step the project stored
// The template gallery and the by-hand path stay one link away on the Goal step.

export default function ProjectSetup() {
  const { id } = useParams();
  return id ? <ResumeSetup key={id} id={id} /> : <NewSetup />;
}


function NewSetup() {
  const navigate = useNavigate();
  const handed = useLocation().state as { goal?: string } | null;
  const [step, setStep] = useState<"goal" | "plan">("goal");
  const [goal, setGoal] = useState(handed?.goal ?? "");
  const [who, setWho] = useState<Who>("tares");
  const [plan, setPlan] = useState<Plan>();
  const [base, setBase] = useState<Baseline>({});

  useEffect(() => { window.scrollTo(0, 0); }, [step]);

  return (
    <div className="su">
      <Stepper at={step} />
      {step === "goal" && (
        <GoalStep goal={goal} setGoal={setGoal} who={who} setWho={setWho}
                  onPlanned={(p) => { setPlan(p); setBase(baselineOf(p)); setWho(p.who); setStep("plan"); }} />
      )}
      {step === "plan" && plan && (
        <PlanStep plan={plan} setPlan={setPlan} base={base} setBase={setBase}
                  onBack={() => { setGoal(plan.goal || goal); setStep("goal"); }}
                  onApplied={({ project, connect }) => navigate(`/projects/${encodeURIComponent(project.id)}/setup`,
                    { replace: true, state: { connect, plan } })} />
      )}
    </div>
  );
}

function ResumeSetup({ id }: { id: string }) {
  const navigate = useNavigate();
  // apply's answer, handed over once: the key in it is shown on this visit only
  const loc = useLocation();
  const handed = loc.state as { connect?: SetupConnect; plan?: Plan } | null;
  // held in memory only, and taken out of the history entry, so a reload does not show the key again
  const [connect] = useState(handed?.connect);
  useEffect(() => {
    if (handed?.connect) navigate(loc.pathname, { replace: true, state: { plan: handed.plan } });
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  const [step, setStep] = useState<FlowStep>();
  const [moving, setMoving] = useState(false);
  const [err, setErr] = useState<string>();
  const { data, error, reload } = usePolling(() => api.projectSetup(id), step === "try" ? 10000 : 3000);
  const { data: project } = usePolling(() => api.project(id), 60000);

  useEffect(() => {
    if (!data || step) return;
    if (data.step === "done") { navigate(`/projects/${encodeURIComponent(id)}`, { replace: true }); return; }
    setStep(data.step);
  }, [data, step, id, navigate]);
  useEffect(() => { window.scrollTo(0, 0); }, [step]);

  const plan = data?.plan ?? handed?.plan;
  const move = async (next: "try" | "done") => {
    setMoving(true); setErr(undefined);
    try {
      await api.setSetupStep(id, next);
      if (next === "done") { navigate(`/projects/${encodeURIComponent(id)}`); return; }
      setStep(next);
    } catch (e) { setErr(errText(e)); }
    setMoving(false);
  };

  if (!data && error) {
    return (
      <div className="su">
        <ErrorState error={error} what="this project's setup" onRetry={reload} />
        <p className="help"><Link to={`/projects/${encodeURIComponent(id)}`}>Open the project</Link></p>
      </div>
    );
  }
  if (!plan || !step || step === "goal" || step === "plan") {
    return <div className="su"><p className="help">Loading…</p></div>;
  }

  return (
    <div className="su">
      <Stepper at={step} />
      {err && <div className="alert error" role="alert">{err}</div>}
      {step === "connect" && (
        <ConnectStep projectId={id} plan={plan} connect={connect ?? data?.connect} checks={data?.checks}
                     onContinue={() => move("try")} onRefresh={reload}
                     onLater={() => navigate(`/projects/${encodeURIComponent(id)}`)} />
      )}
      {step === "try" && (
        <TryStep projectId={id} plan={plan} ownCheck={data?.checks.own_agent}
                 onBackToConnect={() => setStep("connect")}
                 onFinish={() => move("done")} finishing={moving} />
      )}
    </div>
  );
}
