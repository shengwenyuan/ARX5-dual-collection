import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import {
  fetchSnapshot,
  inspectDevices,
  mapDevices,
  sendTrigger,
  snapshotPatch,
  startSession,
  stopSession,
  type CollectorSnapshot,
} from "../api/collector";
import type { OperatorAction } from "../domain/model";
import { operatorReducer } from "../domain/reducer";
import { initialState, tasks } from "../mocks/fixtures";

const realInitialState = {
  ...initialState,
  runtimeMode: "real" as const,
  controlConnected: false,
  controlError: null,
  authoritativeTask: null,
  episodes: [],
  diskFreeGb: 0,
  devicesHealthy: false,
  logs: [],
};

export function useOperatorReal() {
  const [state, localDispatch] = useReducer(operatorReducer, realInitialState);
  const [clock, setClock] = useState(Date.now());
  const [snapshot, setSnapshot] = useState<CollectorSnapshot | null>(null);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchSnapshot();
      if (!mounted.current) return;
      setSnapshot(next);
      localDispatch({ type: "real.sync", patch: snapshotPatch(next) });
    } catch (error) {
      if (!mounted.current) return;
      localDispatch({ type: "real.error", message: errorMessage(error) });
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const interval = window.setInterval(() => void refresh(), 500);
    return () => {
      mounted.current = false;
      window.clearInterval(interval);
    };
  }, [refresh]);

  useEffect(() => {
    if (state.status !== "RECORDING") return;
    setClock(Date.now());
    const interval = window.setInterval(() => setClock(Date.now()), 200);
    return () => window.clearInterval(interval);
  }, [state.status]);

  const dispatch = useCallback((action: OperatorAction) => {
    if (action.type === "task.select") return;
    if (action.type === "window.open" || action.type === "window.close") {
      localDispatch(action);
      if (action.type === "window.open" && action.window === "devices") {
        void runCommand(inspectDevices, refresh, localDispatch);
      }
      return;
    }
    let command: (() => Promise<void>) | null = null;
    if (action.type === "session.start") command = startSession;
    if (action.type === "session.exit") command = stopSession;
    if (action.type === "episode.start" || action.type === "episode.finish") {
      command = () => sendTrigger("activate");
    }
    if (action.type === "episode.abort") command = () => sendTrigger("abort");
    if (command) void runCommand(command, refresh, localDispatch);
  }, [refresh]);

  const selectedTask = state.authoritativeTask ?? tasks[0];
  const elapsedSeconds = state.recordingStartedAt
    ? Math.max(0, (clock - state.recordingStartedAt) / 1000)
    : 0;
  const devices = useMemo(() => mapDevices(snapshot?.devices ?? []), [snapshot]);

  return { state, dispatch, selectedTask, elapsedSeconds, devices };
}

async function runCommand(
  command: () => Promise<void>,
  refresh: () => Promise<void>,
  dispatch: React.Dispatch<OperatorAction>,
) {
  try {
    await command();
  } catch (error) {
    dispatch({ type: "real.error", message: errorMessage(error) });
  } finally {
    await refresh();
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
