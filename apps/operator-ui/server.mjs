import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const port = Number(process.env.PORT ?? 4173);
const distRoot = fileURLToPath(new URL("./dist/", import.meta.url));
const mimeTypes = new Map([
  [".css", "text/css; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".svg", "image/svg+xml"],
]);

const server = createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url ?? "/", "http://localhost").pathname;
    const relativePath = pathname === "/" ? "index.html" : pathname.slice(1);
    const safePath = normalize(relativePath).replace(/^(\.\.[/\\])+/, "");
    const requestedPath = join(distRoot, safePath);
    const body = await readStaticFile(requestedPath, pathname);
    const extension = extname(body.path);
    response.writeHead(200, {
      "Cache-Control": extension === ".html" ? "no-store" : "public, max-age=31536000, immutable",
      "Content-Type": mimeTypes.get(extension) ?? "application/octet-stream",
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
    });
    response.end(body.content);
  } catch (error) {
    response.writeHead(500, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("Operator UI failed to serve this request.");
    console.error(error);
  }
});

server.listen(port, "0.0.0.0", () => {
  console.log(`ARX5 Operator UI listening on ${port}`);
});

async function readStaticFile(path, pathname) {
  try {
    return { content: await readFile(path), path };
  } catch (error) {
    if (error?.code !== "ENOENT" || pathname.includes(".")) throw error;
    const fallback = join(distRoot, "index.html");
    return { content: await readFile(fallback), path: fallback };
  }
}
