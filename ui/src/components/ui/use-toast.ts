import * as React from "react";

import type { ToastActionElement, ToastProps } from "@/components/ui/toast";

const TOAST_LIMIT = 3;
const TOAST_REMOVE_DELAY = 4000;

type ToasterToast = ToastProps & {
  id: string;
  title?: React.ReactNode;
  description?: React.ReactNode;
  action?: ToastActionElement;
};

type State = { toasts: ToasterToast[] };
type Action =
  | { type: "ADD"; toast: ToasterToast }
  | { type: "UPDATE"; toast: Partial<ToasterToast> }
  | { type: "DISMISS"; toastId?: string }
  | { type: "REMOVE"; toastId?: string };

let count = 0;
function genId() {
  count = (count + 1) % Number.MAX_SAFE_INTEGER;
  return count.toString();
}

const timers = new Map<string, ReturnType<typeof setTimeout>>();

function scheduleRemove(toastId: string) {
  if (timers.has(toastId)) return;
  timers.set(
    toastId,
    setTimeout(() => {
      timers.delete(toastId);
      dispatch({ type: "REMOVE", toastId });
    }, TOAST_REMOVE_DELAY),
  );
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "ADD":
      return { toasts: [action.toast, ...state.toasts].slice(0, TOAST_LIMIT) };
    case "UPDATE":
      return { toasts: state.toasts.map((t) => (t.id === action.toast.id ? { ...t, ...action.toast } : t)) };
    case "DISMISS": {
      const { toastId } = action;
      if (toastId) scheduleRemove(toastId);
      else state.toasts.forEach((t) => scheduleRemove(t.id));
      return {
        toasts: state.toasts.map((t) => (t.id === toastId || toastId === undefined ? { ...t, open: false } : t)),
      };
    }
    case "REMOVE":
      return { toasts: action.toastId === undefined ? [] : state.toasts.filter((t) => t.id !== action.toastId) };
  }
}

const listeners: Array<(state: State) => void> = [];
let memoryState: State = { toasts: [] };

function dispatch(action: Action) {
  memoryState = reducer(memoryState, action);
  listeners.forEach((l) => l(memoryState));
}

type Toast = Omit<ToasterToast, "id">;

function toast(props: Toast) {
  const id = genId();
  const update = (p: Partial<ToasterToast>) => dispatch({ type: "UPDATE", toast: { ...p, id } });
  const dismiss = () => dispatch({ type: "DISMISS", toastId: id });
  dispatch({
    type: "ADD",
    toast: {
      ...props,
      id,
      open: true,
      onOpenChange: (open) => {
        if (!open) dismiss();
      },
    },
  });
  return { id, dismiss, update };
}

function useToast() {
  const [state, setState] = React.useState<State>(memoryState);
  React.useEffect(() => {
    listeners.push(setState);
    return () => {
      const i = listeners.indexOf(setState);
      if (i > -1) listeners.splice(i, 1);
    };
  }, []);
  return { ...state, toast, dismiss: (toastId?: string) => dispatch({ type: "DISMISS", toastId }) };
}

export { useToast, toast };
