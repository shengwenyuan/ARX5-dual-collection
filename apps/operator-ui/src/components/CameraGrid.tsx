import type { CameraRole } from "../domain/model";

const cameras: Array<{ role: CameraRole; title: string; detail: string }> = [
  { role: "left", title: "左腕视角", detail: "camera_left · RGB" },
  { role: "overview", title: "桌面俯视", detail: "camera_overview · RGB" },
  { role: "right", title: "右腕视角", detail: "camera_right · RGB" },
];

interface CameraGridProps {
  online: Record<CameraRole, boolean>;
}

export function CameraGrid({ online }: CameraGridProps) {
  return (
    <section className="camera-grid" aria-label="三路相机预览">
      {cameras.map((camera, index) => (
        <article className={`camera-card camera-${camera.role}`} key={camera.role}>
          <header>
            <div>
              <span className="camera-order">CAM {String(index + 1).padStart(2, "0")}</span>
              <h2>{camera.title}</h2>
            </div>
            <span className={`signal-pill ${online[camera.role] ? "online" : "offline"}`}>
              <span aria-hidden="true" />
              {online[camera.role] ? "SIM 10 FPS" : "NO SIGNAL"}
            </span>
          </header>
          <div className={`camera-viewport ${online[camera.role] ? "" : "is-offline"}`}>
            <div className="test-grid" aria-hidden="true" />
            <div className="camera-reticle" aria-hidden="true"><span /></div>
            <div className="camera-watermark">
              <strong>{camera.role.toUpperCase()}</strong>
              <span>640 × 360 · MOCK RGB</span>
            </div>
          </div>
          <footer>
            <span>{camera.detail}</span>
            <span>预览与录制链路隔离</span>
          </footer>
        </article>
      ))}
    </section>
  );
}
