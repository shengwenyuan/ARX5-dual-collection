import type { DeviceItem, OperatorAction, OperatorState, RuntimeStatus } from "../domain/model";

interface OverlayHostProps {
  state: OperatorState;
  devices: DeviceItem[];
  dispatch: React.Dispatch<OperatorAction>;
  simulation?: boolean;
}

const demoStatuses: RuntimeStatus[] = [
  "OFFLINE",
  "STARTING",
  "READY",
  "HOMING",
  "RECORDING",
  "FINALIZING",
  "ABORTED",
  "ERROR",
  "SHUTTING_DOWN",
];

export function OverlayHost({ state, devices, dispatch, simulation = true }: OverlayHostProps) {
  const close = () => dispatch({ type: "window.close" });

  if (state.activeWindow === "logs" || state.activeWindow === "demo") {
    return (
      <div className="drawer-backdrop" role="presentation" onMouseDown={close}>
        <aside
          className="side-drawer"
          aria-label={state.activeWindow === "logs" ? "运行日志" : "模拟控制"}
          onMouseDown={(event) => event.stopPropagation()}
        >
          <DrawerHeader
            eyebrow={state.activeWindow === "logs" ? "RUNTIME" : "BETA 1"}
            title={state.activeWindow === "logs" ? "运行日志" : "Simulation Controls"}
            onClose={close}
          />
          {state.activeWindow === "logs" ? (
            <div className="log-console">
              {state.logs.map((line, index) => <code key={`${index}-${line}`}>{line}</code>)}
            </div>
          ) : (
            <DemoControls state={state} dispatch={dispatch} />
          )}
        </aside>
      </div>
    );
  }

  if (state.activeWindow === null) return null;

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={close}>
      <section
        className={`modal-window modal-${state.activeWindow}`}
        role="dialog"
        aria-modal="true"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <DrawerHeader
          eyebrow={simulation ? "SIMULATION" : "COLLECTOR"}
          title={modalTitle(state.activeWindow)}
          onClose={close}
        />
        {state.activeWindow === "station" && (simulation ? <StationTerminal /> : <PlaceholderPanel label="Station 初始化终端" detail="真实 PTY 接入按已确认顺序放在下一实现批次。" />)}
        {state.activeWindow === "calibration" && <PlaceholderPanel label="Calibration" />}
        {state.activeWindow === "dagger" && (
          <PlaceholderPanel
            label="DAgger 采集模式"
            detail="该模式将拥有独立的采集动作与状态契约。当前入口仅用于固定主界面位置，不启动 Session。"
          />
        )}
        {state.activeWindow === "data-check" && <PlaceholderPanel label="数据检查 / 清洗" />}
        {state.activeWindow === "devices" && (
          <DevicePanel devices={devices} healthy={state.devicesHealthy} simulation={simulation} />
        )}
      </section>
    </div>
  );
}

function DrawerHeader({ eyebrow, title, onClose }: { eyebrow: string; title: string; onClose: () => void }) {
  return (
    <header className="overlay-header">
      <div><span className="eyebrow">{eyebrow}</span><h2>{title}</h2></div>
      <button className="close-button" onClick={onClose} type="button" aria-label="关闭">×</button>
    </header>
  );
}

function StationTerminal() {
  return (
    <div className="terminal-panel">
      <div className="terminal-toolbar">
        <span className="terminal-dot red" /><span className="terminal-dot amber" /><span className="terminal-dot green" />
        <code>arx5-collect station configure</code>
      </div>
      <div className="terminal-body">
        <code><b>$</b> arx5-collect station configure</code>
        <code>Station ID [operator-station]: <strong>w4</strong></code>
        <code className="terminal-ok">ARX5: left/right role binding PASS</code>
        <code className="terminal-ok">D405: 3 × USB 3.2 detected</code>
        <code>Press pedal for SPACE / activate once <span className="terminal-caret" /></code>
        <p>beta1 终端仅验证窗口布局。不会启动 CLI，也不会读取设备。</p>
      </div>
      <footer className="modal-footer"><span>未来由 PTY 原样承载现有 CLI</span><button type="button" disabled>等待输入</button></footer>
    </div>
  );
}

function PlaceholderPanel({ label, detail }: { label: string; detail?: string }) {
  return (
    <div className="placeholder-panel">
      <span className="placeholder-symbol">◇</span>
      <h3>{label}</h3>
      <p>{detail ?? "该功能当前明确占位，不执行真实逻辑。"}</p>
      <span className="stub-mark">NOT IMPLEMENTED</span>
    </div>
  );
}

function DevicePanel({ devices, healthy, simulation }: { devices: DeviceItem[]; healthy: boolean; simulation: boolean }) {
  return (
    <div className="device-panel">
      <div className={`device-summary ${healthy ? "healthy" : "failed"}`}>
        <strong>{healthy ? "7 / 7 MATCHED" : "6 / 7 MATCHED"}</strong>
        <span>{healthy ? (simulation ? "所有 Mock 设备身份一致" : "所有设备身份一致") : "存在设备缺失或身份不匹配"}</span>
      </div>
      <div className="device-grid">
        {devices.map((device, index) => {
          const matched = healthy || index !== 3;
          return (
            <div className="device-row" key={device.id}>
              <span className={`outcome-dot ${matched ? "success" : "aborted"}`} />
              <span><strong>{device.label}</strong><small>{device.detail}</small></span>
              <b>{matched ? "MATCHED" : "FAILED"}</b>
            </div>
          );
        })}
      </div>
      <p className="mock-disclaimer">{simulation ? <>模拟结果，不会调用 <code>arx5-collect devices</code>。</> : <>结果来自 <code>arx5-collect devices</code>。</>}</p>
    </div>
  );
}

function DemoControls({ state, dispatch }: { state: OperatorState; dispatch: React.Dispatch<OperatorAction> }) {
  return (
    <div className="demo-controls">
      <section>
        <span className="control-label">权威状态</span>
        <div className="demo-status-grid">
          {demoStatuses.map((status) => (
            <button
              className={state.status === status ? "is-active" : ""}
              key={status}
              onClick={() => dispatch({ type: "demo.status", status, now: Date.now() })}
              type="button"
            >{status}</button>
          ))}
        </div>
      </section>
      <section>
        <span className="control-label">相机预览</span>
        <div className="demo-toggle-row">
          {(["left", "overview", "right"] as const).map((role) => (
            <button key={role} onClick={() => dispatch({ type: "demo.camera", role })} type="button">
              {role} · {state.cameraOnline[role] ? "ON" : "OFF"}
            </button>
          ))}
        </div>
      </section>
      <section>
        <span className="control-label">设备与存储</span>
        <button className="wide-demo-button" onClick={() => dispatch({ type: "demo.devices" })} type="button">
          七设备 · {state.devicesHealthy ? "MATCHED" : "FAILED"}
        </button>
        <label className="disk-slider">
          <span>磁盘 {state.diskFreeGb.toFixed(0)} GB</span>
          <input
            max="1600"
            min="20"
            onChange={(event) => dispatch({ type: "demo.disk", diskFreeGb: Number(event.target.value) })}
            type="range"
            value={state.diskFreeGb}
          />
        </label>
      </section>
      <p className="mock-disclaimer">此抽屉只控制浏览器内存中的假状态。</p>
    </div>
  );
}

function modalTitle(windowName: NonNullable<OperatorState["activeWindow"]>): string {
  const titles = {
    station: "Station 初始化终端",
    calibration: "Calibration",
    devices: "七设备身份检查",
    dagger: "DAgger 采集模式",
    "data-check": "数据检查",
    logs: "运行日志",
    demo: "Simulation Controls",
  };
  return titles[windowName];
}
