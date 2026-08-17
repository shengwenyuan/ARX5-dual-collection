import { describe, expect, it } from "vitest";

import { initialState } from "../mocks/fixtures";
import type { OperatorState } from "./model";
import { operatorReducer, scheduledDelay } from "./reducer";

describe("operatorReducer", () => {
  it("runs the deterministic success path", () => {
    let state = operatorReducer(initialState, { type: "session.start", now: 1_000 });
    expect(state.status).toBe("STARTING");
    state = operatorReducer(state, { type: "status.advance", now: 2_000 });
    expect(state.status).toBe("READY");
    state = operatorReducer(state, { type: "episode.start", now: 3_000 });
    expect(state.status).toBe("HOMING");
    state = operatorReducer(state, { type: "status.advance", now: 4_000 });
    expect(state.status).toBe("RECORDING");
    state = operatorReducer(state, { type: "episode.finish", now: 9_000 });
    expect(state.status).toBe("FINALIZING");
    state = operatorReducer(state, { type: "status.advance", now: 10_000 });
    expect(state.status).toBe("READY");
    expect(state.episodes[0].outcome).toBe("success");
    expect(state.episodes[0].durationSeconds).toBe(6);
  });

  it("adds an aborted episode and rejects invalid transitions", () => {
    const unchanged = operatorReducer(initialState, { type: "episode.start", now: 0 });
    expect(unchanged).toBe(initialState);

    let state: OperatorState = { ...initialState, status: "READY" };
    state = operatorReducer(state, { type: "episode.start", now: 1_000 });
    state = operatorReducer(state, { type: "status.advance", now: 2_000 });
    state = operatorReducer(state, { type: "episode.abort", now: 3_000 });
    state = operatorReducer(state, { type: "status.advance", now: 4_000 });
    expect(state.status).toBe("READY");
    expect(state.episodes[0].outcome).toBe("aborted");
  });

  it("only schedules transitional states", () => {
    expect(scheduledDelay("STARTING")).toBe(900);
    expect(scheduledDelay("RECORDING")).toBeNull();
  });
});
