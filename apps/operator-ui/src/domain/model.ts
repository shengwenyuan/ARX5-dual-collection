export type RuntimeStatus =
  | "OFFLINE"
  | "STARTING"
  | "READY"
  | "HOMING"
  | "RECORDING"
  | "FINALIZING"
  | "ABORTED"
  | "ERROR"
  | "SHUTTING_DOWN";

export type WindowName =
  | "station"
  | "calibration"
  | "devices"
  | "data-check"
  | "logs"
  | "demo"
  | null;

export interface TaskItem {
  id: string;
  title: string;
  description: string;
  progress: string;
  accent: "blue" | "violet" | "amber";
}

export interface EpisodeItem {
  id: string;
  startedAt: string;
  durationSeconds: number;
  outcome: "success" | "aborted";
  sizeGb: number;
  warning: string | null;
  path: string;
}

export interface DeviceItem {
  id: string;
  label: string;
  detail: string;
  matched: boolean;
}

export type CameraRole = "left" | "overview" | "right";

export interface OperatorState {
  status: RuntimeStatus;
  selectedTaskId: string;
  episodes: EpisodeItem[];
  activeWindow: WindowName;
  recordingStartedAt: number | null;
  activeEpisodeId: string | null;
  pendingOutcome: "success" | "aborted" | null;
  cameraOnline: Record<CameraRole, boolean>;
  devicesHealthy: boolean;
  diskFreeGb: number;
  logs: string[];
}

export type OperatorAction =
  | { type: "task.select"; taskId: string }
  | { type: "window.open"; window: Exclude<WindowName, null> }
  | { type: "window.close" }
  | { type: "session.start"; now: number }
  | { type: "session.exit"; now: number }
  | { type: "episode.start"; now: number }
  | { type: "episode.finish"; now: number }
  | { type: "episode.abort"; now: number }
  | { type: "status.advance"; now: number }
  | { type: "demo.status"; status: RuntimeStatus; now: number }
  | { type: "demo.camera"; role: CameraRole }
  | { type: "demo.devices" }
  | { type: "demo.disk"; diskFreeGb: number };
