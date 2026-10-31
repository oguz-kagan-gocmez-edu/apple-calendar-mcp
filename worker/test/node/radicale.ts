/** Vitest global setup: a throwaway Radicale CalDAV server, like tests/test_caldav_store.py. */
import { type ChildProcess, spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { TestProject } from "vitest/node";

declare module "vitest" {
  export interface ProvidedContext {
    radicaleUrl: string;
  }
}

function freePort(): Promise<number> {
  return new Promise((ok, fail) => {
    const srv = createServer();
    srv.once("error", fail);
    srv.listen(0, "127.0.0.1", () => {
      const port = (srv.address() as { port: number }).port;
      srv.close(() => ok(port));
    });
  });
}

let proc: ChildProcess | undefined;
let folder: string | undefined;

export async function setup(project: TestProject): Promise<void> {
  const port = await freePort();
  folder = mkdtempSync(join(tmpdir(), "radicale-"));
  const repo = resolve(import.meta.dirname, "../../..");
  proc = spawn(
    "uv",
    [
      "run", "--project", repo, "python", "-m", "radicale",
      "--server-hosts", `127.0.0.1:${port}`,
      "--storage-filesystem-folder", folder,
      "--auth-type", "none",
      "--rights-type", "owner_only",
      "--logging-level", "warning",
    ],
    { stdio: "ignore", env: { ...process.env, PYTHONWARNINGS: "ignore" } },
  );
  const url = `http://127.0.0.1:${port}/`;
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      await fetch(url);
      project.provide("radicaleUrl", url);
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 150));
    }
  }
  throw new Error("Radicale did not start (is `uv` installed and the Python dev deps synced?)");
}

export async function teardown(): Promise<void> {
  proc?.kill();
  if (folder) rmSync(folder, { recursive: true, force: true });
}
