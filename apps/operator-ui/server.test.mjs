import { createServer } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, test } from "vitest";

import { createOperatorServer } from "./server.mjs";

const servers = [];
const directories = [];

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => new Promise((resolve) => server.close(resolve))));
  await Promise.all(directories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

describe("Operator API proxy", () => {
  test("proxies only the versioned collector snapshot over a Unix socket", async () => {
    const directory = await mkdtemp(join(tmpdir(), "arx5-ui-proxy-"));
    directories.push(directory);
    const socketPath = join(directory, "control.sock");
    const collector = createServer((request, response) => {
      expect(request.url).toBe("/v1/snapshot");
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ schema_version: 1, status: "READY" }));
    });
    servers.push(collector);
    await listen(collector, socketPath);

    const operator = createOperatorServer({ controlSocket: socketPath });
    servers.push(operator);
    await listen(operator, 0, "127.0.0.1");
    const address = operator.address();
    const response = await fetch(`http://127.0.0.1:${address.port}/api/v1/snapshot`);

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ schema_version: 1, status: "READY" });
  });

  test("rejects arbitrary proxy paths", async () => {
    const operator = createOperatorServer({ controlSocket: "/tmp/not-used.sock" });
    servers.push(operator);
    await listen(operator, 0, "127.0.0.1");
    const address = operator.address();
    const response = await fetch(`http://127.0.0.1:${address.port}/api/v1/shell`);
    expect(response.status).toBe(404);
  });
});

function listen(server, ...args) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(...args, resolve);
  });
}
