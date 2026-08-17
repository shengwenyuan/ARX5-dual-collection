import type { TaskItem } from "../domain/model";

interface TaskPanelProps {
  tasks: TaskItem[];
  selectedTaskId: string;
  onSelect: (taskId: string) => void;
}

export function TaskPanel({ tasks, selectedTaskId, onSelect }: TaskPanelProps) {
  return (
    <section className="side-section task-section" aria-labelledby="tasks-title">
      <header className="section-heading">
        <div>
          <span className="eyebrow">TASK QUEUE</span>
          <h2 id="tasks-title">待采集任务</h2>
        </div>
        <span className="count-badge">{tasks.length}</span>
      </header>
      <div className="task-list">
        {tasks.map((task, index) => (
          <button
            className={`task-card ${task.id === selectedTaskId ? "is-selected" : ""}`}
            key={task.id}
            onClick={() => onSelect(task.id)}
            type="button"
          >
            <span className={`task-index accent-${task.accent}`}>{String(index + 1).padStart(2, "0")}</span>
            <span className="task-copy">
              <strong>{task.title}</strong>
              <span>{task.description}</span>
            </span>
            <span className="task-progress">{task.progress}</span>
          </button>
        ))}
      </div>
      <div className="stub-note">
        <span className="stub-mark">占位</span>
        任务选择仅更新本页描述，不会下发真实配置
      </div>
    </section>
  );
}
