import { createServer, request as requestHttp } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const defaultDistRoot = fileURLToPath(new URL("./dist/", import.meta.url));
const allowedControlRoutes = new Set([
  "GET /v1/snapshot",
  "GET /v1/logs",
  "POST /v1/devices/inspect",
  "POST /v1/session/start",
  "POST /v1/session/stop",
  "POST /v1/session/trigger",
]);
const mimeTypes = new Map([
  [".css", "text/css; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".svg", "image/svg+xml"],
]);

export function createOperatorServer({
  distRoot = defaultDistRoot,
  controlSocket = process.env.ARX5_CONTROL_SOCKET ?? "/run/arx5-control/api.sock",
} = {}) {
  return createServer(async (request, response) => {
    try {
      const url = new URL(request.url ?? "/", "http://localhost");
      if (url.pathname.startsWith("/api/")) {
        await proxyControlRequest(request, response, url, controlSocket);
        return;
      }
      const pathname = url.pathname;
      const relativePath = pathname === "/" ? "index.html" : pathname.slice(1);
      const safePath = normalize(relativePath).replace(/^(\.\.[/\\])+/, "");
      const requestedPath = join(distRoot, safePath);
      const body = await readStaticFile(requestedPath, pathname, distRoot);
      const extension = extname(body.path);
      response.writeHead(200, {
        "Cache-Control": extension === ".html" ? "no-store" : "public, max-age=31536000, immutable",
        "Content-Type": mimeTypes.get(extension) ?? "application/octet-stream",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
      });
      response.end(body.content);
    } catch (error) {
      writeJson(response, 500, { error: "Operator UI failed to serve this request." });
      console.error(error);
    }
  });
}

async function proxyControlRequest(request, response, url, controlSocket) {
  const upstreamPath = url.pathname.slice("/api".length);
  const routeKey = `${request.method} ${upstreamPath}`;
  if (!allowedControlRoutes.has(routeKey)) {
    writeJson(response, 404, { error: "unknown Operator API endpoint" });
    return;
  }
  const body = await readRequestBody(request);
  await new Promise((resolve) => {
    const upstream = requestHttp({
      socketPath: controlSocket,
      path: `${upstreamPath}${url.search}`,
      method: request.method,
      headers: {
        "Content-Type": "application/json",
        "Content-Length": body.length,
      },
    }, (upstreamResponse) => {
      const chunks = [];
      upstreamResponse.on("data", (chunk) => chunks.push(chunk));
      upstreamResponse.on("end", () => {
        response.writeHead(upstreamResponse.statusCode ?? 502, {
          "Cache-Control": "no-store",
          "Content-Type": "application/json; charset=utf-8",
          "X-Content-Type-Options": "nosniff",
        });
        response.end(Buffer.concat(chunks));
        resolve();
      });
    });
    upstream.on("error", (error) => {
      writeJson(response, 503, { error: `Collector Control unavailable: ${error.message}` });
      resolve();
    });
    upstream.end(body);
  });
}

async function readRequestBody(request) {
  const chunks = [];
  let total = 0;
  for await (const chunk of request) {
    total += chunk.length;
    if (total > 16_384) throw new Error("Operator API request is too large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

async function readStaticFile(path, pathname, distRoot) {
  try {
    return { content: await readFile(path), path };
  } catch (error) {
    if (error?.code !== "ENOENT" || pathname.includes(".")) throw error;
    const fallback = join(distRoot, "index.html");
    return { content: await readFile(fallback), path: fallback };
  }
}

function writeJson(response, status, payload) {
  const encoded = JSON.stringify(payload);
  response.writeHead(status, {
    "Cache-Control": "no-store",
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(encoded),
    "X-Content-Type-Options": "nosniff",
  });
  response.end(encoded);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const port = Number(process.env.PORT ?? 4173);
  createOperatorServer().listen(port, "0.0.0.0", () => {
    console.log(`ARX5 Operator UI listening on ${port}`);
  });
}
