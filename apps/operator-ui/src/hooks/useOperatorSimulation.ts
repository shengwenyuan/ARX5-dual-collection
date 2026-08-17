import { useEffect, useMemo, useReducer, useState } from "react";

import { operatorReducer, scheduledDelay } from "../domain/reducer";
import { initialState, tasks } from "../mocks/fixtures";

export function useOperatorSimulation() {
  const [state, dispatch] = useReducer(operatorReducer, initialState);
  const [clock, setClock] = useState(Date.now());

  useEffect(() => {
    const delay = scheduledDelay(state.status);
    if (delay === null) return;
    const timeout = window.setTimeout(
      () => dispatch({ type: "status.advance", now: Date.now() }),
      delay,
    );
    return () => window.clearTimeout(timeout);
  }, [state.status]);

  useEffect(() => {
    if (state.status !== "RECORDING") return;
    setClock(Date.now());
    const interval = window.setInterval(() => setClock(Date.now()), 200);
    return () => window.clearInterval(interval);
  }, [state.status]);

  const selectedTask = useMemo(
    () => tasks.find((task) => task.id === state.selectedTaskId) ?? tasks[0],
    [state.selectedTaskId],
  );
  const elapsedSeconds = state.recordingStartedAt
    ? Math.max(0, (clock - state.recordingStartedAt) / 1000)
    : 0;

  return { state, dispatch, selectedTask, elapsedSeconds };
}
