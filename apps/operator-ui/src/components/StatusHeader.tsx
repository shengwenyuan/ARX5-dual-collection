import type { RuntimeStatus, TaskItem } from "../domain/model";

interface StatusHeaderProps {
  status: RuntimeStatus;
  task: TaskItem;
  elapsedSeconds: number;
  activeEpisodeId: string | null;
  onOpenDemo: () => void;
}

const statusCopy: Record<RuntimeStatus, string> = {
  OFFLINE: "采集系统已停止",
  STARTING: "正在启动采集系统",
  READY: "设备就绪，等待录制",
  HOMING: "双臂归位与重力补偿",
  RECORDING: "正在录制 Episode",
  FINALIZING: "正在安全落盘",
  ABORTED: "本条已中止",
  ERROR: "采集系统异常",
  SHUTTING_DOWN: "正在回收资源",
};

export function StatusHeader({
  status,
  task,
  elapsedSeconds,
  activeEpisodeId,
  onOpenDemo,
}: StatusHeaderProps) {
  return (
    <header className="status-header">
      <div className="task-context">
        <button className="simulation-flag" onClick={onOpenDemo} type="button">
          <span aria-hidden="true" /> SIMULATION
        </button>
        <div>
          <span className="eyebrow">CURRENT TASK</span>
          <h1>{task.title}</h1>
          <p>{task.description}</p>
        </div>
      </div>
      <div className="runtime-readout">
        <div className={`status-chip status-${status.toLowerCase()}`}>
          <span className="status-glyph" aria-hidden="true" />
          <span>
            <strong>{status}</strong>
            <small>{statusCopy[status]}</small>
          </span>
        </div>
        <div className="timer-block" aria-label="录制计时">
          <span>REC TIME</span>
          <strong>{formatDuration(elapsedSeconds)}</strong>
          <small>{activeEpisodeId ?? "NO ACTIVE EPISODE"}</small>
        </div>
      </div>
    </header>
  );
}

function formatDuration(seconds: number): string {
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}
