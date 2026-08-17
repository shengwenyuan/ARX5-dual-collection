import type { OperatorAction, OperatorState } from "../domain/model";

interface ControlDockProps {
  state: OperatorState;
  dispatch: React.Dispatch<OperatorAction>;
}

export function ControlDock({ state, dispatch }: ControlDockProps) {
  const now = () => Date.now();
  const sessionActive = state.status !== "OFFLINE";
  const canRecord = state.status === "READY" || state.status === "RECORDING";
  const estimatedMinutes = Math.floor(state.diskFreeGb / 0.331 / 60);

  return (
    <section className="control-dock" aria-label="操作控制区">
      <div className="control-group utility-group">
        <span className="control-label">设备与准备</span>
        <div className="button-row">
          <button className="control-button" onClick={() => dispatch({ type: "window.open", window: "calibration" })} type="button">
            <span>CAL</span> Calibration
          </button>
          <button className="control-button" onClick={() => dispatch({ type: "window.open", window: "station" })} type="button">
            <span>CFG</span> Station 初始化
          </button>
          <button className="control-button" onClick={() => dispatch({ type: "window.open", window: "devices" })} type="button">
            <span>CHK</span> 设备检查
          </button>
        </div>
      </div>

      <div className="control-group primary-group">
        <span className="control-label">Episode 主操作</span>
        <div className="primary-actions">
          <button
            className="session-button"
            disabled={state.status !== "OFFLINE"}
            onClick={() => dispatch({ type: "session.start", now: now() })}
            type="button"
          >
            {sessionActive ? "SESSION 已启动" : "启动 SESSION"}
          </button>
          <button
            className={`record-button ${state.status === "RECORDING" ? "is-recording" : ""}`}
            disabled={!canRecord}
            onClick={() =>
              dispatch({
                type: state.status === "RECORDING" ? "episode.finish" : "episode.start",
                now: now(),
              })
            }
            type="button"
          >
            <span className="record-icon" aria-hidden="true" />
            <span>
              <strong>{state.status === "RECORDING" ? "完成本条" : "开始录制"}</strong>
              <small>{state.status === "RECORDING" ? "提交 success" : "GO_HOME 后启动"}</small>
            </span>
          </button>
          <button
            className="abort-button"
            disabled={state.status !== "RECORDING"}
            onClick={() => dispatch({ type: "episode.abort", now: now() })}
            type="button"
          >
            中止本条
          </button>
        </div>
      </div>

      <div className="control-group system-group">
        <span className="control-label">系统与辅助</span>
        <div className="storage-readout">
          <span>LOCAL STORAGE</span>
          <strong>{state.diskFreeGb.toFixed(0)} GB</strong>
          <small>预计可录制 {estimatedMinutes} 分钟</small>
          <i style={{ width: `${Math.min(100, state.diskFreeGb / 16)}%` }} />
        </div>
        <div className="system-actions">
          <button onClick={() => dispatch({ type: "window.open", window: "data-check" })} type="button">数据检查</button>
          <button onClick={() => dispatch({ type: "window.open", window: "logs" })} type="button">运行日志</button>
          <button
            className="exit-button"
            disabled={state.status !== "READY"}
            onClick={() => dispatch({ type: "session.exit", now: now() })}
            type="button"
          >
            退出 Session
          </button>
        </div>
      </div>
    </section>
  );
}
