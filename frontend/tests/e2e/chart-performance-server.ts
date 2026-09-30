import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

// Serves the identical preserved regular production build for every baseline run.
// This is local test infrastructure, not an application/deployment server.
const root = path.resolve(process.env.PM_BENCH_DIST ?? 'dist');
const types: Record<string, string> = { '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.html': 'text/html', '.png': 'image/png', '.json': 'application/json' };
createServer((request, response) => { void (async () => {
  const pathname = decodeURIComponent(new URL(request.url ?? '/', 'http://localhost').pathname);
  const file = path.resolve(root, `.${pathname}`);
  if (file !== root && !file.startsWith(`${root}${path.sep}`)) { response.writeHead(403).end(); return; }
  try {
    const data = await readFile(path.extname(file) ? file : path.join(root, 'index.html'));
    response.writeHead(200, { 'Content-Type': types[path.extname(file)] ?? 'text/html', 'Cache-Control': 'no-store' }).end(data);
  } catch { response.writeHead(404).end(); }
})(); }).listen(4181, '127.0.0.1');
