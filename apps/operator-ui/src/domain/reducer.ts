import type { EpisodeItem, OperatorAction, OperatorState, RuntimeStatus } from "./model";

const transitionDelay: Partial<Record<RuntimeStatus, number>> = {
  STARTING: 900,
  HOMING: 1200,
  FINALIZING: 800,
  ABORTED: 900,
  SHUTTING_DOWN: 800,
};

export function scheduledDelay(status: RuntimeStatus): number | null {
  return transitionDelay[status] ?? null;
}

export function operatorReducer(
  state: OperatorState,
  action: OperatorAction,
): OperatorState {
  switch (action.type) {
    case "task.select":
      return { ...state, selectedTaskId: action.taskId };
    case "window.open":
      return { ...state, activeWindow: action.window };
    case "window.close":
      return { ...state, activeWindow: null };
    case "session.start":
      if (state.status !== "OFFLINE") return state;
      return withLog({ ...state, status: "STARTING" }, action.now, "正在启动采集 Session");
    case "session.exit":
      if (state.status !== "READY") return state;
      return withLog({ ...state, status: "SHUTTING_DOWN" }, action.now, "正在退出采集 Session");
    case "episode.start":
      if (state.status !== "READY") return state;
      return withLog(
        {
          ...state,
          status: "HOMING",
          activeEpisodeId: mockEpisodeId(action.now),
          pendingOutcome: null,
        },
        action.now,
        "开始前归位与重力补偿",
      );
    case "episode.finish":
      if (state.status !== "RECORDING") return state;
      return withLog(
        { ...state, status: "FINALIZING", pendingOutcome: "success" },
        action.now,
        "正在关闭 MCAP 并提交 success",
      );
    case "episode.abort":
      if (state.status !== "RECORDING") return state;
      return withLog(
        { ...state, status: "ABORTED", pendingOutcome: "aborted" },
        action.now,
        "操作员中止当前 Episode",
      );
    case "status.advance":
      return advanceStatus(state, action.now);
    case "demo.status":
      return {
        ...state,
        status: action.status,
        recordingStartedAt: action.status === "RECORDING" ? action.now : null,
        activeEpisodeId:
          action.status === "RECORDING" ? mockEpisodeId(action.now) : null,
        pendingOutcome: null,
      };
    case "demo.camera":
      return {
        ...state,
        cameraOnline: {
          ...state.cameraOnline,
          [action.role]: !state.cameraOnline[action.role],
        },
      };
    case "demo.devices":
      return { ...state, devicesHealthy: !state.devicesHealthy };
    case "demo.disk":
      return { ...state, diskFreeGb: action.diskFreeGb };
  }
}

function advanceStatus(state: OperatorState, now: number): OperatorState {
  switch (state.status) {
    case "STARTING":
      return withLog({ ...state, status: "READY" }, now, "八路 Mock 数据已 READY");
    case "HOMING":
      return withLog(
        { ...state, status: "RECORDING", recordingStartedAt: now },
        now,
        "Recorder 已启动",
      );
    case "FINALIZING":
    case "ABORTED":
      return completeEpisode(state, now);
    case "SHUTTING_DOWN":
      return withLog(
        {
          ...state,
          status: "OFFLINE",
          recordingStartedAt: null,
          activeEpisodeId: null,
          pendingOutcome: null,
        },
        now,
        "采集 Session 已退出",
      );
    default:
      return state;
  }
}

function completeEpisode(state: OperatorState, now: number): OperatorState {
  if (!state.activeEpisodeId || !state.pendingOutcome) {
    return { ...state, status: "READY", recordingStartedAt: null };
  }
  const durationSeconds = Math.max(
    0,
    (now - (state.recordingStartedAt ?? now)) / 1000,
  );
  const episode: EpisodeItem = {
    id: state.activeEpisodeId,
    startedAt: new Date(state.recordingStartedAt ?? now).toLocaleTimeString("zh-CN", {
      hour12: false,
    }),
    durationSeconds,
    outcome: state.pendingOutcome,
    sizeGb: durationSeconds * 0.331,
    warning: state.pendingOutcome === "aborted" ? "操作员主动中止" : null,
    path: `/reports/mock/${state.activeEpisodeId}`,
  };
  return withLog(
    {
      ...state,
      status: "READY",
      recordingStartedAt: null,
      activeEpisodeId: null,
      pendingOutcome: null,
      episodes: [episode, ...state.episodes],
    },
    now,
    `Episode ${episode.outcome} 已加入列表`,
  );
}

function withLog(
  state: OperatorState,
  now: number,
  message: string,
): OperatorState {
  const timestamp = new Date(now).toLocaleTimeString("zh-CN", { hour12: false });
  return { ...state, logs: [...state.logs, `[${timestamp}] [SIM] ${message}`] };
}

function mockEpisodeId(now: number): string {
  const stamp = new Date(now).toISOString().replace(/[-:.]/g, "").replace("Z", "Z");
  return `${stamp}-mock`;
}
