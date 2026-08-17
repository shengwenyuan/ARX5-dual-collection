import { CameraGrid } from "./components/CameraGrid";
import { ControlDock } from "./components/ControlDock";
import { EpisodePanel } from "./components/EpisodePanel";
import { OverlayHost } from "./components/OverlayHost";
import { StatusHeader } from "./components/StatusHeader";
import { TaskPanel } from "./components/TaskPanel";
import { useOperatorSimulation } from "./hooks/useOperatorSimulation";
import { devices, tasks } from "./mocks/fixtures";

export default function App() {
  const { state, dispatch, selectedTask, elapsedSeconds } = useOperatorSimulation();

  return (
    <main className="operator-shell">
      <aside className="left-rail">
        <TaskPanel
          tasks={tasks}
          selectedTaskId={state.selectedTaskId}
          onSelect={(taskId) => dispatch({ type: "task.select", taskId })}
        />
        <EpisodePanel episodes={state.episodes} />
      </aside>
      <section className="workspace">
        <StatusHeader
          activeEpisodeId={state.activeEpisodeId}
          elapsedSeconds={elapsedSeconds}
          onOpenDemo={() => dispatch({ type: "window.open", window: "demo" })}
          status={state.status}
          task={selectedTask}
        />
        <CameraGrid online={state.cameraOnline} />
        <ControlDock dispatch={dispatch} state={state} />
      </section>
      <OverlayHost devices={devices} dispatch={dispatch} state={state} />
    </main>
  );
}
