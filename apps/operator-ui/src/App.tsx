import { CameraGrid } from "./components/CameraGrid";
import { ControlDock } from "./components/ControlDock";
import { EpisodePanel } from "./components/EpisodePanel";
import { OverlayHost } from "./components/OverlayHost";
import { StatusHeader } from "./components/StatusHeader";
import { TaskPanel } from "./components/TaskPanel";
import type { DeviceItem, OperatorAction, OperatorState, TaskItem } from "./domain/model";
import { useOperatorReal } from "./hooks/useOperatorReal";
import { useOperatorSimulation } from "./hooks/useOperatorSimulation";
import { devices as mockDevices, tasks } from "./mocks/fixtures";

interface OperatorRuntime {
  state: OperatorState;
  dispatch: React.Dispatch<OperatorAction>;
  selectedTask: TaskItem;
  elapsedSeconds: number;
  devices: DeviceItem[];
}

export default function App() {
  return import.meta.env.VITE_OPERATOR_MODE === "real" ? <RealApp /> : <SimulationApp />;
}

function RealApp() {
  return <OperatorView runtime={useOperatorReal()} />;
}

function SimulationApp() {
  const simulation = useOperatorSimulation();
  return <OperatorView runtime={{ ...simulation, devices: mockDevices }} />;
}

function OperatorView({ runtime }: { runtime: OperatorRuntime }) {
  const { state, dispatch, selectedTask, elapsedSeconds, devices } = runtime;
  const simulation = state.runtimeMode === "simulation";
  return (
    <main className="operator-shell">
      <aside className="left-rail">
        <TaskPanel
          interactive={simulation}
          tasks={tasks}
          selectedTaskId={state.selectedTaskId}
          onSelect={(taskId) => dispatch({ type: "task.select", taskId })}
        />
        <EpisodePanel episodes={state.episodes} />
      </aside>
      <section className="workspace">
        <StatusHeader
          activeEpisodeId={state.activeEpisodeId}
          connected={state.controlConnected}
          error={state.controlError}
          elapsedSeconds={elapsedSeconds}
          onOpenDemo={() => dispatch({ type: "window.open", window: "demo" })}
          runtimeMode={state.runtimeMode}
          status={state.status}
          task={selectedTask}
        />
        <CameraGrid online={state.cameraOnline} simulated={simulation} />
        <ControlDock dispatch={dispatch} state={state} />
      </section>
      <OverlayHost devices={devices} dispatch={dispatch} simulation={simulation} state={state} />
    </main>
  );
}
