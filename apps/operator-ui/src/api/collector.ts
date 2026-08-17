import type { DeviceItem, EpisodeItem, OperatorState, RuntimeStatus } from "../domain/model";

interface CollectorDevice {
  id: string;
  kind: string;
  configured_serial: string | null;
  detected_serial: string | null;
  link: string | null;
  matched: boolean;
  detail: string;
}

interface CollectorEpisode {
  id: string;
  started_at: string;
  duration_s: number;
  outcome: "success" | "fail" | "aborted";
  size_bytes: number;
  warning: string | null;
  path: string;
}

interface CollectorLog {
  sequence: number;
  timestamp: string;
  source: string;
  message: string;
}

export interface CollectorSnapshot {
  schema_version: 1;
  status: RuntimeStatus;
  error: string | null;
  task: { id: string; description: string };
  recording_started_at: string | null;
  disk: { free_bytes: number };
  devices: CollectorDevice[];
  episodes: CollectorEpisode[];
  logs: CollectorLog[];
}

export const fetchSnapshot = () => requestJson<CollectorSnapshot>("/api/v1/snapshot", "GET");
export const inspectDevices = async () => { await requestJson("/api/v1/devices/inspect", "POST", {}); };
export const startSession = async () => { await requestJson("/api/v1/session/start", "POST", {}); };
export const stopSession = async () => { await requestJson("/api/v1/session/stop", "POST", {}); };
export const sendTrigger = async (event: "activate" | "abort") => {
  await requestJson("/api/v1/session/trigger", "POST", { event });
};

export function snapshotPatch(snapshot: CollectorSnapshot): Partial<OperatorState> {
  if (snapshot.schema_version !== 1) throw new Error("不支持的 Collector Control schema");
  return {
    status: runtimeStatus(snapshot.status),
    runtimeMode: "real",
    controlConnected: true,
    controlError: snapshot.error,
    authoritativeTask: {
      id: snapshot.task.id,
      title: snapshot.task.id,
      description: snapshot.task.description,
      progress: "FIXED",
      accent: "blue",
    },
    recordingStartedAt: snapshot.recording_started_at ? Date.parse(snapshot.recording_started_at) : null,
    episodes: snapshot.episodes.map(mapEpisode),
    diskFreeGb: snapshot.disk.free_bytes / 1024 ** 3,
    devicesHealthy: snapshot.devices.length === 7 && snapshot.devices.every((device) => device.matched),
    logs: snapshot.logs.map(formatLog),
  };
}

export function mapDevices(devices: CollectorSnapshot["devices"]): DeviceItem[] {
  const order = [
    "left_arm", "right_arm", "camera_left", "camera_overview", "camera_right",
    "trigger_activate", "trigger_abort",
  ];
  return [...devices]
    .sort((left, right) => order.indexOf(left.id) - order.indexOf(right.id))
    .map((device) => ({
      id: device.id,
      label: deviceLabel(device.id),
      detail: [device.detected_serial ?? device.configured_serial ?? "NO SERIAL", device.link, device.detail]
        .filter(Boolean)
        .join(" · "),
      matched: device.matched,
    }));
}

async function requestJson<T = unknown>(path: string, method: "GET" | "POST", body?: object): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({})) as { error?: string } & T;
  if (!response.ok) throw new Error(payload.error ?? `Collector API ${response.status}`);
  return payload;
}

function mapEpisode(episode: CollectorEpisode): EpisodeItem {
  const started = new Date(episode.started_at);
  return {
    id: episode.id,
    startedAt: Number.isNaN(started.valueOf())
      ? episode.started_at
      : started.toLocaleTimeString("zh-CN", { hour12: false }),
    durationSeconds: episode.duration_s,
    outcome: episode.outcome,
    sizeGb: episode.size_bytes / 1024 ** 3,
    warning: episode.warning,
    path: episode.path,
  };
}

function formatLog(log: CollectorLog): string {
  const timestamp = new Date(log.timestamp);
  const time = Number.isNaN(timestamp.valueOf())
    ? log.timestamp
    : timestamp.toLocaleTimeString("zh-CN", { hour12: false });
  return `[${time}] [${log.source}] ${log.message}`;
}

function runtimeStatus(value: string): RuntimeStatus {
  const statuses: RuntimeStatus[] = [
    "OFFLINE", "STARTING", "READY", "HOMING", "RECORDING", "FINALIZING",
    "ABORTED", "ERROR", "SHUTTING_DOWN",
  ];
  if (!statuses.includes(value as RuntimeStatus)) throw new Error(`未知采集状态：${value}`);
  return value as RuntimeStatus;
}

function deviceLabel(id: string): string {
  const labels: Record<string, string> = {
    left_arm: "左臂",
    right_arm: "右臂",
    camera_left: "左相机",
    camera_overview: "俯视相机",
    camera_right: "右相机",
    trigger_activate: "开始踏板",
    trigger_abort: "中止踏板",
  };
  return labels[id] ?? id;
}
