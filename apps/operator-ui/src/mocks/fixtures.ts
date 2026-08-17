import type { DeviceItem, EpisodeItem, OperatorState, TaskItem } from "../domain/model";

export const tasks: TaskItem[] = [
  {
    id: "fold-shirt-01",
    title: "折叠浅色衬衫",
    description: "从桌面展开状态开始，双手配合完成一次平整折叠。保持衣物位于俯视相机中央。",
    progress: "0 / 30",
    accent: "blue",
  },
  {
    id: "stack-cups-02",
    title: "堆叠五只纸杯",
    description: "将散放的五只纸杯整理为单列堆叠，动作结束后双臂离开目标区域。",
    progress: "12 / 50",
    accent: "violet",
  },
  {
    id: "sort-blocks-03",
    title: "分类彩色积木",
    description: "按颜色将积木移动到左右托盘。当前仅作为任务列表与描述区的交互占位。",
    progress: "8 / 20",
    accent: "amber",
  },
];

export const initialEpisodes: EpisodeItem[] = [
  {
    id: "20260817T100502823084Z-4a33947b",
    startedAt: "10:05:02",
    durationSeconds: 46.13,
    outcome: "success",
    sizeGb: 15.27,
    warning: null,
    path: "/reports/w4/08-17/fold_shirt-01/20260817T100502823084Z-4a33947b",
  },
  {
    id: "20260817T100408699229Z-addbb8cc",
    startedAt: "10:04:08",
    durationSeconds: 27.54,
    outcome: "success",
    sizeGb: 9.1,
    warning: "overview 出现一次 66.7 ms 帧间隔",
    path: "/reports/w4/08-17/fold_shirt-01/20260817T100408699229Z-addbb8cc",
  },
  {
    id: "20260817T095912104282Z-8bf21ca1",
    startedAt: "09:59:12",
    durationSeconds: 18.72,
    outcome: "aborted",
    sizeGb: 6.19,
    warning: "操作员主动中止",
    path: "/reports/w4/08-17/fold_shirt-01/20260817T095912104282Z-8bf21ca1",
  },
];

export const devices: DeviceItem[] = [
  { id: "left-arm", label: "左臂", detail: "USB2CAN · can1", matched: true },
  { id: "right-arm", label: "右臂", detail: "USB2CAN · can3", matched: true },
  { id: "camera-left", label: "左相机", detail: "D405 · USB 3.2", matched: true },
  { id: "camera-overview", label: "俯视相机", detail: "D405 · USB 3.2", matched: true },
  { id: "camera-right", label: "右相机", detail: "D405 · USB 3.2", matched: true },
  { id: "trigger-activate", label: "开始踏板", detail: "hidraw · activate", matched: true },
  { id: "trigger-abort", label: "中止踏板", detail: "hidraw · abort", matched: true },
];

export const initialState: OperatorState = {
  status: "OFFLINE",
  selectedTaskId: tasks[0].id,
  episodes: initialEpisodes,
  activeWindow: null,
  recordingStartedAt: null,
  activeEpisodeId: null,
  pendingOutcome: null,
  cameraOnline: { left: true, overview: true, right: true },
  devicesHealthy: true,
  diskFreeGb: 1280,
  logs: [
    "[SIM] Operator UI beta1 已启动",
    "[SIM] 所有设备、相机与 Episode 数据均为视觉占位",
  ],
};
