import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import { ErrorState, usePolling } from "../components/bits";
import { Stepper, errText, type FlowStep } from "../components/setup/common";
import { ConnectStep } from "../components/setup/connect";
import { GoalStep, type Who } from "../components/setup/goal";
import { PlanStep, baselineOf, type Baseline } from "../components/setup/plan";
import { PlanningView } from "../components/setup/planning";
import { TryStep } from "../components/setup/tryit";
import type { Plan, ProjectSetup as SetupData, SetupConnect } from "../types";

// Setting up a project, goal first (contract: setup-flow-contract.md, "Console"). It mirrors the
// running page: the person states a goal, sees the whole plan in plain words, adjusts it,
// confirms once, then does only what only they can do (connect data, their own agent, tools),
// optionally runs a practice spike, and lands on the Overview.
//   /projects/new           the Goal; "Plan it" makes a draft project and goes to its page
//   /projects/:id/setup     a draft: its planning as it happens, then the Plan, edits kept on the
//                           draft; once set up: Connect and Try it, resumed where it stopped
// Nothing of a draft runs until "Looks right, set it up"; it waits on the Projects list.
// The template gallery and the by-hand path stay one link away on the Goal step.

export default function ProjectSetup() {
  const { id } = useParams();
  return id ? <ResumeSetup key={id} id={id} /> : <NewSetup />;
}

function NewSetup() {
  const navigate = useNavigate();
  const handed = useLocation().state as { goal?: string } | null;
  const [goal, setGoal] = useState(handed?.goal ?? "");
  const [who, setWho] = useState<Who>("tares");
  return (
    <div className="su">
      <Stepper at="goal" />
      <GoalStep goal={goal} setGoal={setGoal} who={who} setWho={setWho}
                onSubmit={async (g, w) => {
                  const r = await api.createDraft({ goal: g, who: w });
                  navigate(`/projects/${encodeURIComponent(r.project.id)}/setup`);
                }} />
    </div>
  );
}

const SAVE_DELAY_MS = 800;

/** A draft's plan, edited in place and kept on the draft a moment after each change. */
function DraftPlan({ id, initial, onBack, onAdjust, onApplied }: {
  id: string; initial: Plan;
  onBack: () => void;
  onAdjust: (instruction: string, plan: Plan) => Promise<void>;
  onApplied: (r: Awaited<ReturnType<typeof api.applySetup>>) => void;
}) {
  const [plan, setPlanState] = useState<Plan>(initial);
  const [base, setBase] = useState<Baseline>(() => baselineOf(initial));
  const [saveErr, setSaveErr] = useState<string>();
  const timer = useRef<number>();
  const setPlan = (p: Plan) => {
    setPlanState(p);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      api.saveDraftPlan(id, p).then(() => setSaveErr(undefined))
        .catch((e) => setSaveErr(errText(e)));
    }, SAVE_DELAY_MS);
  };
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <>
      {saveErr && <div className="alert warn" role="status">Your last change is not saved on the draft yet: {saveErr}</div>}
      <PlanStep projectId={id} plan={plan} setPlan={setPlan} base={base} setBase={setBase}
                onBack={onBack} onAdjust={onAdjust}
                onApplied={(r) => { window.clearTimeout(timer.current); onApplied(r); }} />
    </>
  );
}

/** A draft project: its planning as it happens, then its plan; the goal can be changed. */
function DraftSetup({ id, data, reload, onApplied }: {
  id: string; data: SetupData; reload: () => void;
  onApplied: (r: Awaited<ReturnType<typeof api.applySetup>>) => void;
}) {
  const [editingGoal, setEditingGoal] = useState(!data.plan && data.planning?.state !== "running" && !data.planning);
  const [goal, setGoal] = useState(data.goal ?? "");
  const [who, setWho] = useState<Who>((data.who as Who) ?? "tares");
  const [dismissed, setDismissed] = useState<string>();   // a failed change set aside
  // each finished planning run hands the editor a fresh plan
  const [gen, setGen] = useState(0);
  const wasRunning = useRef(data.planning?.state === "running");
  const running = data.planning?.state === "running";
  useEffect(() => {
    if (wasRunning.current && !running) setGen((g) => g + 1);
    wasRunning.current = running;
  }, [running]);

  const replan = async (body: Parameters<typeof api.replanDraft>[1]) => {
    await api.replanDraft(id, body);
    setEditingGoal(false); setDismissed(undefined);
    reload();
  };

  if (editingGoal) {
    return (
      <>
        <Stepper at="goal" />
        <GoalStep goal={goal} setGoal={setGoal} who={who} setWho={setWho}
                  onSubmit={(g, w) => replan({ goal: g, who: w })}
                  onCancel={data.plan ? () => setEditingGoal(false) : undefined} />
      </>
    );
  }
  const failed = data.planning?.state === "failed";
  const failedAt = data.planning?.steps?.[0]?.at;
  if (running || (failed && (!data.plan || dismissed !== failedAt))) {
    return (
      <>
        <Stepper at="plan" />
        <PlanningView goal={data.plan?.goal || data.goal || ""} planning={data.planning}
                      changing={!!data.plan}
                      onRetry={() => replan({ goal: data.goal ?? goal, who })}
                      onEditGoal={() => setEditingGoal(true)}
                      onKeepPlan={data.plan ? () => setDismissed(failedAt) : undefined} />
      </>
    );
  }
  if (!data.plan) return <p className="help">Loading…</p>;
  return (
    <>
      <Stepper at="plan" />
      <DraftPlan key={gen} id={id} initial={data.plan}
                 onBack={() => { setGoal(data.plan?.goal || goal); setEditingGoal(true); }}
                 onAdjust={(instruction, plan) => replan({ instruction, plan })}
                 onApplied={onApplied} />
    </>
  );
}

function ResumeSetup({ id }: { id: string }) {
  const navigate = useNavigate();
  // apply's answer, handed over once: the key in it is shown on this visit only
  const loc = useLocation();
  const handed = loc.state as { connect?: SetupConnect; plan?: Plan } | null;
  // held in memory only, and taken out of the history entry, so a reload does not show the key again
  const [connect, setConnect] = useState(handed?.connect);
  const [appliedPlan, setAppliedPlan] = useState<Plan | undefined>(handed?.plan);
  useEffect(() => {
    if (handed?.connect) navigate(loc.pathname, { replace: true, state: { plan: handed.plan } });
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  const [step, setStep] = useState<FlowStep>();
  const [moving, setMoving] = useState(false);
  const [err, setErr] = useState<string>();
  const [planningFast, setPlanningFast] = useState(true);
  const { data, error, reload } = usePolling(() => api.projectSetup(id),
    step === "try" ? 10000 : planningFast ? 1500 : 3000);
  const draft = !!data?.draft && step === undefined;
  useEffect(() => { setPlanningFast(data?.planning?.state === "running"); }, [data?.planning?.state]);

  useEffect(() => {
    if (!data || step || data.draft) return;
    if (data.step === "done") { navigate(`/projects/${encodeURIComponent(id)}`, { replace: true }); return; }
    if (data.step !== "plan") setStep(data.step);
  }, [data, step, id, navigate]);
  useEffect(() => { window.scrollTo(0, 0); }, [step, draft]);

  const plan = appliedPlan ?? data?.plan ?? undefined;
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
  if (draft && data) {
    return (
      <div className="su">
        <DraftSetup id={id} data={data} reload={reload}
                    onApplied={(r) => { setConnect(r.connect); setAppliedPlan(r.plan); setStep("connect"); reload(); }} />
      </div>
    );
  }
  if (!plan || !step) {
    return <div className="su"><p className="help">Loading…</p></div>;
  }

  return (
    <div className="su">
      <Stepper at={step} />
      {err && <div className="alert error" role="alert">{err}</div>}
      {step === "connect" && (
        <ConnectStep projectId={id} plan={plan} connect={connect ?? data?.connect ?? undefined}
                     checks={data?.checks ?? undefined}
                     onContinue={() => move("try")} onRefresh={reload}
                     onLater={() => navigate(`/projects/${encodeURIComponent(id)}`)} />
      )}
      {step === "try" && (
        <TryStep projectId={id} plan={plan} ownCheck={data?.checks?.own_agent ?? null}
                 onBackToConnect={() => setStep("connect")}
                 onFinish={() => move("done")} finishing={moving} />
      )}
    </div>
  );
}
