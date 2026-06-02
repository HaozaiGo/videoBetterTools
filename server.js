import { createServer } from "node:http";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { basename, extname, join, normalize } from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { categories, estimateCredits, getTool, tools } from "./src/tool-config.js";

const root = fileURLToPath(new URL(".", import.meta.url));
const distDir = join(root, "dist");
const dataDir = join(root, "data");
const uploadDir = join(dataDir, "uploads");
const dbPath = join(dataDir, "db.json");
const port = Number(process.env.PORT || 4173);
const demoUserId = "demo-user";
let clientRoot = root;

const mimeTypes = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".mp4": "video/mp4",
  ".mov": "video/quicktime",
  ".webm": "video/webm",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
};

const defaultDb = {
  users: [{ id: demoUserId, email: "demo@modelplaza.local", name: "演示用户" }],
  wallets: [{ userId: demoUserId, credits: 180, frozenCredits: 0 }],
  walletLedger: [],
  assets: [],
  tasks: [],
  processedCallbacks: [],
};

async function ensureStorage() {
  await mkdir(uploadDir, { recursive: true });
  try {
    await stat(dbPath);
  } catch {
    await writeJson(dbPath, defaultDb);
  }
}

async function readDb() {
  await ensureStorage();
  return JSON.parse(await readFile(dbPath, "utf8"));
}

async function writeDb(db) {
  await writeJson(dbPath, db);
}

async function writeJson(path, value) {
  await writeFile(path, `${JSON.stringify(value, null, 2)}\n`);
}

function getWallet(db) {
  let wallet = db.wallets.find((item) => item.userId === demoUserId);
  if (!wallet) {
    wallet = { userId: demoUserId, credits: 0, frozenCredits: 0 };
    db.wallets.push(wallet);
  }
  return wallet;
}

function serialize(db) {
  const user = db.users.find((item) => item.id === demoUserId) || defaultDb.users[0];
  const wallet = getWallet(db);
  return {
    account: {
      id: user.id,
      name: user.name,
      email: user.email,
      credits: wallet.credits,
      frozenCredits: wallet.frozenCredits,
      availableCredits: wallet.credits - wallet.frozenCredits,
    },
    tools,
    categories,
    tasks: db.tasks
      .filter((task) => task.userId === demoUserId)
      .sort((a, b) => b.createdAt - a.createdAt),
    ledger: db.walletLedger
      .filter((entry) => entry.userId === demoUserId)
      .sort((a, b) => b.createdAt - a.createdAt),
  };
}

function addLedger(db, type, amount, title, taskId = null) {
  db.walletLedger.push({
    id: randomUUID(),
    userId: demoUserId,
    type,
    amount,
    title,
    taskId,
    createdAt: Date.now(),
  });
}

function sendJson(res, statusCode, payload) {
  res.writeHead(statusCode, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(payload));
}

function sendError(res, statusCode, message) {
  sendJson(res, statusCode, { error: message });
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks);
}

async function readJsonBody(req) {
  const body = await readBody(req);
  if (!body.length) return {};
  return JSON.parse(body.toString("utf8"));
}

function sanitizeFileName(filename) {
  const safe = basename(filename || "upload.bin").replace(/[^\w.-]+/g, "-");
  return safe || "upload.bin";
}

function parseMultipart(buffer, contentType) {
  const boundaryMatch = contentType.match(/boundary=(?:"([^"]+)"|([^;]+))/i);
  if (!boundaryMatch) throw new Error("missing multipart boundary");
  const boundary = Buffer.from(`--${boundaryMatch[1] || boundaryMatch[2]}`);
  const parts = [];
  let cursor = buffer.indexOf(boundary);

  while (cursor !== -1) {
    const next = buffer.indexOf(boundary, cursor + boundary.length);
    if (next === -1) break;
    let part = buffer.subarray(cursor + boundary.length + 2, next - 2);
    cursor = next;
    if (!part.length || part.equals(Buffer.from("--"))) continue;

    const headerEnd = part.indexOf("\r\n\r\n");
    if (headerEnd === -1) continue;
    const rawHeaders = part.subarray(0, headerEnd).toString("utf8");
    const content = part.subarray(headerEnd + 4);
    const name = rawHeaders.match(/name="([^"]+)"/)?.[1];
    const filename = rawHeaders.match(/filename="([^"]*)"/)?.[1];
    const contentTypeHeader = rawHeaders.match(/content-type:\s*([^\r\n]+)/i)?.[1] || "application/octet-stream";
    if (name) parts.push({ name, filename, contentType: contentTypeHeader, content });
  }

  return parts;
}

async function handleUpload(req, res) {
  const contentType = req.headers["content-type"] || "";
  if (!contentType.includes("multipart/form-data")) {
    sendError(res, 415, "upload requires multipart/form-data");
    return;
  }

  const buffer = await readBody(req);
  const parts = parseMultipart(buffer, contentType);
  const filePart = parts.find((part) => part.filename);
  if (!filePart || !filePart.content.length) {
    sendError(res, 400, "missing file");
    return;
  }

  const fields = Object.fromEntries(
    parts
      .filter((part) => !part.filename)
      .map((part) => [part.name, part.content.toString("utf8")]),
  );
  const id = randomUUID();
  const originalName = sanitizeFileName(filePart.filename);
  const storageName = `${id}-${originalName}`;
  const storagePath = join(uploadDir, storageName);
  await writeFile(storagePath, filePart.content);

  const db = await readDb();
  const asset = {
    id,
    userId: demoUserId,
    kind: fields.kind || (filePart.contentType.startsWith("image/") ? "image" : "video"),
    originalName,
    mimeType: filePart.contentType,
    storageKey: storageName,
    url: `/uploads/${storageName}`,
    sizeBytes: filePart.content.length,
    durationSeconds: Number(fields.durationSeconds || 0),
    width: Number(fields.width || 0),
    height: Number(fields.height || 0),
    expiresAt: Date.now() + 7 * 24 * 60 * 60 * 1000,
    createdAt: Date.now(),
  };
  db.assets.push(asset);
  await writeDb(db);
  sendJson(res, 201, { asset });
}

async function createTask(req, res) {
  const body = await readJsonBody(req);
  const tool = getTool(body.toolSlug);
  if (!tool || tool.status !== "online") {
    sendError(res, 400, "tool is not available");
    return;
  }

  const db = await readDb();
  const wallet = getWallet(db);
  const inputAsset = db.assets.find((asset) => asset.id === body.inputAssetId && asset.userId === demoUserId);
  if (!inputAsset) {
    sendError(res, 400, "missing uploaded asset");
    return;
  }

  const params = body.params || {};
  const estimate = estimateCredits(tool, {
    ...params,
    duration: params.duration || inputAsset.durationSeconds || 30,
  });
  if (wallet.credits - wallet.frozenCredits < estimate) {
    sendError(res, 402, "insufficient credits");
    return;
  }

  const task = {
    id: randomUUID(),
    userId: demoUserId,
    toolSlug: tool.slug,
    inputAssetId: inputAsset.id,
    outputAssetId: null,
    status: "queued",
    params,
    estimatedCredits: estimate,
    frozenCredits: estimate,
    chargedCredits: 0,
    provider: tool.provider,
    providerJobId: `mock_${randomUUID()}`,
    errorCode: null,
    createdAt: Date.now(),
    completedAt: null,
    outputUrl: "",
  };

  wallet.frozenCredits += estimate;
  db.tasks.push(task);
  addLedger(db, "freeze", 0, `${tool.name} 冻结 ${estimate} 积分`, task.id);
  await writeDb(db);

  scheduleProviderSimulation(task.providerJobId);
  sendJson(res, 201, { task, state: serialize(db) });
}

function scheduleProviderSimulation(providerJobId) {
  setTimeout(() => {
    providerCallback({
      providerJobId,
      status: "processing",
      callbackId: `${providerJobId}:processing`,
    }).catch(console.error);
  }, 1200);

  setTimeout(() => {
    providerCallback({
      providerJobId,
      status: "succeeded",
      callbackId: `${providerJobId}:succeeded`,
      outputUrl: `/uploads/result-${providerJobId}.txt`,
    }).catch(console.error);
  }, 8000);
}

async function providerCallback(payload) {
  const db = await readDb();
  const callbackId = payload.callbackId || `${payload.providerJobId}:${payload.status}`;
  if (db.processedCallbacks.includes(callbackId)) return { duplicated: true, db };

  const task = db.tasks.find((item) => item.providerJobId === payload.providerJobId);
  if (!task) throw new Error("task not found for provider job");
  const tool = getTool(task.toolSlug);
  const wallet = getWallet(db);

  if (payload.status === "processing" && task.status === "queued") {
    task.status = "processing";
  }

  if (payload.status === "succeeded" && !["succeeded", "failed", "cancelled"].includes(task.status)) {
    const chargedCredits = Math.min(Number(payload.chargedCredits || task.estimatedCredits), task.frozenCredits);
    const outputAsset = {
      id: randomUUID(),
      userId: demoUserId,
      kind: "result",
      originalName: `${task.id}-result.txt`,
      mimeType: "text/plain",
      storageKey: `${task.id}-result.txt`,
      url: payload.outputUrl || `/uploads/${task.id}-result.txt`,
      sizeBytes: 0,
      durationSeconds: 0,
      width: 0,
      height: 0,
      expiresAt: Date.now() + 7 * 24 * 60 * 60 * 1000,
      createdAt: Date.now(),
    };
    await writeFile(join(uploadDir, outputAsset.storageKey), `任务 ${task.id} 已完成\n`);
    db.assets.push(outputAsset);
    task.status = "succeeded";
    task.outputAssetId = outputAsset.id;
    task.outputUrl = outputAsset.url;
    task.chargedCredits = chargedCredits;
    task.completedAt = Date.now();
    wallet.frozenCredits = Math.max(0, wallet.frozenCredits - task.frozenCredits);
    wallet.credits = Math.max(0, wallet.credits - chargedCredits);
    addLedger(db, "charge", -chargedCredits, `${tool?.name || task.toolSlug} 扣费完成`, task.id);
  }

  if (payload.status === "failed" && !["succeeded", "failed", "cancelled"].includes(task.status)) {
    task.status = "failed";
    task.errorCode = payload.errorCode || "PROVIDER_FAILED";
    task.completedAt = Date.now();
    wallet.frozenCredits = Math.max(0, wallet.frozenCredits - task.frozenCredits);
    addLedger(db, "refund", 0, `${tool?.name || task.toolSlug} 失败，释放 ${task.frozenCredits} 积分`, task.id);
  }

  db.processedCallbacks.push(callbackId);
  await writeDb(db);
  return { duplicated: false, db };
}

async function handleProviderCallback(req, res) {
  try {
    const result = await providerCallback(await readJsonBody(req));
    sendJson(res, 200, { duplicated: result.duplicated, state: serialize(result.db) });
  } catch (error) {
    sendError(res, 400, error.message);
  }
}

async function recharge(req, res) {
  const body = await readJsonBody(req);
  const credits = Math.max(1, Math.min(100000, Number(body.credits || 0)));
  const db = await readDb();
  const wallet = getWallet(db);
  wallet.credits += credits;
  addLedger(db, "recharge", credits, "模拟充值");
  await writeDb(db);
  sendJson(res, 200, { state: serialize(db) });
}

async function handleApi(req, res, pathname) {
  if (req.method === "GET" && pathname === "/api/bootstrap") {
    sendJson(res, 200, serialize(await readDb()));
    return true;
  }
  if (req.method === "POST" && pathname === "/api/assets") {
    await handleUpload(req, res);
    return true;
  }
  if (req.method === "POST" && pathname === "/api/tasks") {
    await createTask(req, res);
    return true;
  }
  if (req.method === "POST" && pathname === "/api/provider/callback") {
    await handleProviderCallback(req, res);
    return true;
  }
  if (req.method === "POST" && pathname === "/api/recharge") {
    await recharge(req, res);
    return true;
  }
  if (pathname.startsWith("/api/")) {
    sendError(res, 404, "api not found");
    return true;
  }
  return false;
}

function resolveStaticPath(url) {
  const pathname = decodeURIComponent(new URL(url, `http://localhost:${port}`).pathname);
  if (pathname.startsWith("/uploads/")) {
    const requested = pathname.replace(/^\/uploads\//, "");
    return join(uploadDir, normalize(requested).replace(/^(\.\.[/\\])+/, ""));
  }
  const requested = pathname === "/" ? "/index.html" : pathname;
  const normalized = normalize(requested).replace(/^(\.\.[/\\])+/, "");
  return join(clientRoot, normalized);
}

async function serveStatic(req, res) {
  try {
    const filePath = resolveStaticPath(req.url || "/");
    const content = await readFile(filePath);
    res.writeHead(200, { "Content-Type": mimeTypes[extname(filePath)] || "application/octet-stream" });
    res.end(content);
  } catch {
    const app = await readFile(join(clientRoot, "index.html"));
    res.writeHead(200, { "Content-Type": mimeTypes[".html"] });
    res.end(app);
  }
}

await ensureStorage();
try {
  await stat(join(distDir, "index.html"));
  clientRoot = distDir;
} catch {
  clientRoot = root;
}

createServer(async (req, res) => {
  try {
    const pathname = new URL(req.url || "/", `http://localhost:${port}`).pathname;
    if (await handleApi(req, res, pathname)) return;
    await serveStatic(req, res);
  } catch (error) {
    sendError(res, 500, error.message);
  }
}).listen(port, "0.0.0.0", () => {
  console.log(`Model Plaza is running at http://localhost:${port}`);
});
