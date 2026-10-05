import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer, request as proxyRequest } from "node:http";
import { extname, join, normalize, relative, resolve } from "node:path";
import { spawn } from "node:child_process";


const root = resolve(import.meta.dirname, "..");
const clientRoot = join(root, "dist", "client");
const internalPort = Number.parseInt(
  process.env.WHISPER_INTERNAL_PORT || "3001",
  10,
);
const publicPort = Number.parseInt(
  process.env.WHISPER_PUBLIC_PORT || "3000",
  10,
);

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

const vinext = spawn(
  process.execPath,
  [
    join(root, "node_modules", "vinext", "dist", "cli.js"),
    "start",
    "-H",
    "127.0.0.1",
    "-p",
    String(internalPort),
  ],
  {
    cwd: root,
    stdio: "inherit",
    windowsHide: true,
  },
);

function safeStaticPath(urlPath) {
  let decoded;
  try {
    decoded = decodeURIComponent(urlPath.split("?")[0]);
  } catch {
    return null;
  }
  const candidate = resolve(clientRoot, `.${normalize(decoded)}`);
  const inside = relative(clientRoot, candidate);
  if (inside.startsWith("..") || inside.includes(":")) return null;
  return candidate;
}

const server = createServer((request, response) => {
  const staticPath = safeStaticPath(request.url || "/");
  if (
    staticPath &&
    staticPath !== clientRoot &&
    existsSync(staticPath) &&
    statSync(staticPath).isFile()
  ) {
    const stat = statSync(staticPath);
    response.writeHead(200, {
      "Content-Type":
        contentTypes[extname(staticPath).toLowerCase()] ||
        "application/octet-stream",
      "Content-Length": String(stat.size),
      // The application is updated in place, so caching an old bundle makes
      // the browser keep obsolete controls after a service restart.
      "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
      Pragma: "no-cache",
      Expires: "0",
    });
    createReadStream(staticPath).pipe(response);
    return;
  }

  const upstream = proxyRequest(
    {
      hostname: "127.0.0.1",
      port: internalPort,
      path: request.url,
      method: request.method,
      headers: request.headers,
    },
    (upstreamResponse) => {
      response.writeHead(
        upstreamResponse.statusCode || 502,
        upstreamResponse.headers,
      );
      upstreamResponse.pipe(response);
    },
  );
  upstream.on("error", () => {
    if (!response.headersSent) {
      response.writeHead(503, { "Content-Type": "text/plain; charset=utf-8" });
    }
    response.end("Интерфейс запускается. Обновите страницу через несколько секунд.");
  });
  request.pipe(upstream);
});

server.listen(publicPort, "127.0.0.1", () => {
  console.log(
    `Local Whisper interface: http://127.0.0.1:${publicPort} (internal ${internalPort})`,
  );
});

function shutdown() {
  server.close(() => process.exit(0));
  if (!vinext.killed) vinext.kill();
  setTimeout(() => process.exit(0), 1500).unref();
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
vinext.on("exit", (code) => {
  server.close(() => process.exit(code || 0));
});
