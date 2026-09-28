import { api, ApiError, isRecord, Json, uploadAttachment } from "./api";

const DB_NAME = "fleetline-field-v1";
const DB_VERSION = 4;
const EVENT = "fleetline:sync";
const IDENTITY_CHANNEL = "fleetline:offline-identity-v1";
const MAX_OFFLINE_AGE_MS = 24 * 60 * 60 * 1000;
const MAX_SYNC_BATCH = 100;
const IDENTITY_MISMATCH_MESSAGE = "Saved work belongs to a different signed-in account. Sign in as the original user to recover it.";
const EXPIRED_MESSAGE = "Saved work has exceeded its offline access window and needs review.";
const MISSING_BLOB_MESSAGE = "A saved attachment is missing from this device. The change was not synchronized.";

export type SyncLabel = "Saved on this device" | "Waiting to sync" | "Synchronized" | "Sync conflict";

export interface OfflineIdentity {
  user_id: string;
  organization_id: string;
  expires_at: string;
  offline_grant: string;
}

type OwnedOfflineRecord = OfflineIdentity;

export interface OutboxItem extends OwnedOfflineRecord {
  operation_id: string;
  type: "defect.create" | "inspection.submit" | "work_note.create" | "task.complete" | "stock.issue" | "stock.return";
  payload: Json;
  created_at: string;
  status: "waiting" | "syncing" | "conflict" | "rejected";
  message?: string;
  code?: string;
  client_payload?: Json;
  server_payload?: Json;
  resolution_options?: string[];
  blob_ids: string[];
}

interface StoredBlob extends OwnedOfflineRecord {
  id: string;
  operation_id: string;
  name: string;
  type: string;
  blob: Blob;
}

interface CacheRecord<T> extends OwnedOfflineRecord {
  key: string;
  value: T;
  cached_at: string;
}

interface IdentityMessage {
  type: "identity" | "invalidated" | "deleted";
  identity?: OfflineIdentity;
}

let activeIdentity: OfflineIdentity | null = null;
let identityInvalidated = false;
const identityChannel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(IDENTITY_CHANNEL);

function parseExpiry(value: string): number {
  const parsed = new Date(value).valueOf();
  return Number.isFinite(parsed) ? parsed : 0;
}

function normalizeIdentity(identity: OfflineIdentity): OfflineIdentity {
  const maximum = Date.now() + MAX_OFFLINE_AGE_MS;
  return {
    user_id: String(identity.user_id),
    organization_id: String(identity.organization_id),
    expires_at: new Date(Math.min(parseExpiry(identity.expires_at), maximum)).toISOString(),
    offline_grant: String(identity.offline_grant),
  };
}

function sameIdentity(left: OfflineIdentity, right: OfflineIdentity): boolean {
  return left.user_id === right.user_id && left.organization_id === right.organization_id;
}

function ownedBy(record: Partial<OwnedOfflineRecord>, identity: OfflineIdentity): boolean {
  return record.user_id === identity.user_id && record.organization_id === identity.organization_id;
}

function identityIsUsable(identity: OfflineIdentity): boolean {
  return Boolean(identity.user_id && identity.organization_id && identity.offline_grant && parseExpiry(identity.expires_at) > Date.now());
}

function requireIdentity(): OfflineIdentity {
  if (!activeIdentity) throw new Error("Offline identity is unavailable. Connect and sign in again.");
  return activeIdentity;
}

async function clearStore(storeName: "cache" | "outbox" | "blobs"): Promise<void> {
  await transact(storeName, "readwrite", (store) => store.clear());
}

async function clearReadableCache(): Promise<void> {
  await clearStore("cache");
  if ("caches" in window) await Promise.all((await caches.keys()).map((key) => caches.delete(key)));
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = (event) => {
      const db = request.result;
      const transaction = request.transaction;
      if (!db.objectStoreNames.contains("cache")) db.createObjectStore("cache", { keyPath: "key" });
      if (!db.objectStoreNames.contains("outbox")) db.createObjectStore("outbox", { keyPath: "operation_id" });
      if (!db.objectStoreNames.contains("blobs")) db.createObjectStore("blobs", { keyPath: "id" });
      if (event.oldVersion > 0 && event.oldVersion < 4 && transaction) {
        transaction.objectStore("cache").clear();
        const quarantine = (storeName: "outbox" | "blobs") => {
          const cursorRequest = transaction.objectStore(storeName).openCursor();
          cursorRequest.onsuccess = () => {
            const cursor = cursorRequest.result;
            if (!cursor) return;
            const value = cursor.value as Record<string, unknown>;
            value.offline_grant = "";
            if (event.oldVersion < 3) {
              value.user_id = "";
              value.organization_id = "";
              value.expires_at = new Date(0).toISOString();
            }
            if (storeName === "outbox") {
              value.status = "conflict";
              value.code = "offline_access_expired";
              value.message = event.oldVersion < 3
                ? "Saved work predates account-bound offline storage and cannot be synchronized automatically."
                : "Sign in again with the original account to authorize this saved work.";
            }
            cursor.update(value);
            cursor.continue();
          };
        };
        quarantine("outbox");
        quarantine("blobs");
      }
    };
    request.onsuccess = () => {
      request.result.onversionchange = () => request.result.close();
      resolve(request.result);
    };
    request.onerror = () => reject(request.error);
  });
}

async function transact<T>(storeName: string, mode: IDBTransactionMode, work: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, mode);
    const request = work(tx.objectStore(storeName));
    let result: T;
    request.onsuccess = () => { result = request.result; };
    request.onerror = () => reject(request.error);
    tx.oncomplete = () => { db.close(); resolve(result); };
    tx.onerror = () => { db.close(); reject(tx.error); };
    tx.onabort = () => { db.close(); reject(tx.error); };
  });
}

async function deleteRecord(storeName: "cache" | "outbox" | "blobs", key: IDBValidKey): Promise<void> {
  await transact(storeName, "readwrite", (store) => store.delete(key));
}

function cacheKeyIsAllowed(key: string): boolean {
  return key === "bootstrap" || key.startsWith("work-order:");
}

export function setOfflineIdentity(identity: OfflineIdentity): void {
  const normalized = normalizeIdentity(identity);
  if (!identityIsUsable(normalized)) throw new Error("Offline access has expired. Connect and sign in again.");
  activeIdentity = normalized;
  identityInvalidated = false;
  syncSuspended = false;
  identityChannel?.postMessage({ type: "identity", identity: normalized } satisfies IdentityMessage);
}

export function getOfflineIdentity(): OfflineIdentity | null {
  return activeIdentity;
}

export async function reauthorizeOfflineWork(identity: OfflineIdentity): Promise<void> {
  const normalized = normalizeIdentity(identity);
  if (!activeIdentity || !sameIdentity(activeIdentity, normalized) || !identityIsUsable(normalized)) {
    throw new Error("Saved work cannot be authorized for a different account.");
  }
  const db = await openDb();
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(["outbox", "blobs"], "readwrite");
    for (const storeName of ["outbox", "blobs"] as const) {
      const cursorRequest = tx.objectStore(storeName).openCursor();
      cursorRequest.onsuccess = () => {
        const cursor = cursorRequest.result;
        if (!cursor) return;
        const value = cursor.value as OutboxItem | StoredBlob;
        if (ownedBy(value, normalized)) {
          value.expires_at = normalized.expires_at;
          value.offline_grant = normalized.offline_grant;
          if (storeName === "outbox") {
            const outbox = value as OutboxItem;
            if (outbox.status === "conflict" && outbox.code === "offline_access_expired") {
              outbox.status = "waiting";
              outbox.message = undefined;
              outbox.code = undefined;
            }
          }
          cursor.update(value);
        }
        cursor.continue();
      };
    }
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
    tx.onabort = () => { db.close(); reject(tx.error); };
  });
  await refreshSyncLabel();
}

export async function cachePut(key: string, value: unknown): Promise<void> {
  if (!cacheKeyIsAllowed(key)) throw new Error(`Offline caching is not allowed for ${key}`);
  const identity = requireIdentity();
  if (!identityIsUsable(identity) || identityInvalidated) throw new Error("Offline access is no longer valid.");
  const record: CacheRecord<unknown> = {
    key,
    value,
    cached_at: new Date().toISOString(),
    ...identity,
  };
  await transact("cache", "readwrite", (store) => store.put(record));
}

export async function cacheGet<T>(key: string): Promise<T | undefined> {
  if (!cacheKeyIsAllowed(key)) return undefined;
  const result = await transact<CacheRecord<T> | undefined>("cache", "readonly", (store) => store.get(key));
  if (!result || !result.user_id || !result.organization_id || !result.offline_grant || parseExpiry(result.expires_at) <= Date.now()) {
    if (result) await deleteRecord("cache", key);
    return undefined;
  }
  if (!activeIdentity && key === "bootstrap") {
    activeIdentity = normalizeIdentity(result);
    identityInvalidated = false;
  }
  if (!activeIdentity || !ownedBy(result, activeIdentity) || identityInvalidated) return undefined;
  return result.value;
}

async function allOutboxItems(): Promise<OutboxItem[]> {
  return transact<OutboxItem[]>("outbox", "readonly", (store) => store.getAll());
}

export async function outboxItems(): Promise<OutboxItem[]> {
  const items = await allOutboxItems();
  const identity = activeIdentity;
  if (!identity) return items.filter((item) => !item.user_id || !item.organization_id);
  return items.filter((item) => ownedBy(item, identity));
}

async function putOutboxItem(item: OutboxItem): Promise<void> {
  await transact("outbox", "readwrite", (store) => store.put(item));
}

function emit(label: SyncLabel, count: number, message = "") {
  window.dispatchEvent(new CustomEvent(EVENT, { detail: { label, count, message } }));
}

export function onSync(listener: (detail: { label: SyncLabel; count: number; message: string }) => void): () => void {
  const handler = (event: Event) => listener((event as CustomEvent).detail);
  window.addEventListener(EVENT, handler);
  return () => window.removeEventListener(EVENT, handler);
}

export async function refreshSyncLabel(): Promise<void> {
  const items = await outboxItems();
  const conflict = items.find((item) => item.status === "conflict" || item.status === "rejected");
  const accessMessage = identityInvalidated
    ? IDENTITY_MISMATCH_MESSAGE
    : activeIdentity && !identityIsUsable(activeIdentity)
      ? EXPIRED_MESSAGE
      : "";
  emit(conflict || accessMessage ? "Sync conflict" : items.length ? "Waiting to sync" : "Synchronized", items.length, conflict?.message ?? accessMessage);
}

async function markOwnedWorkConflict(message: string): Promise<void> {
  if (!activeIdentity) return;
  const identity = activeIdentity;
  const items = await allOutboxItems();
  await Promise.all(items.filter((item) => ownedBy(item, identity) && (item.status === "waiting" || item.status === "syncing")).map(async (item) => {
    item.status = "conflict";
    item.message = message;
    await putOutboxItem(item);
  }));
  await refreshSyncLabel();
}

identityChannel?.addEventListener("message", (event: MessageEvent<IdentityMessage>) => {
  const message = event.data;
  if (message.type === "deleted") {
    identityInvalidated = true;
    syncSuspended = true;
    syncController?.abort();
    activeIdentity = null;
    void refreshSyncLabel();
    return;
  }
  if (!activeIdentity) return;
  if (message.type === "invalidated" || message.type === "identity" && message.identity && !sameIdentity(activeIdentity, message.identity)) {
    identityInvalidated = true;
    syncSuspended = true;
    syncController?.abort();
    void clearReadableCache().then(refreshSyncLabel);
  } else if (message.type === "identity" && message.identity && sameIdentity(activeIdentity, message.identity)) {
    activeIdentity = normalizeIdentity(message.identity);
    identityInvalidated = false;
    syncSuspended = false;
    void refreshSyncLabel();
  }
});

export async function queueOperation(type: OutboxItem["type"], payload: Json, files: File[] = []): Promise<string> {
  const identity = requireIdentity();
  const blockedMessage = identityInvalidated ? IDENTITY_MISMATCH_MESSAGE : !identityIsUsable(identity) ? EXPIRED_MESSAGE : "";
  const operationId = crypto.randomUUID();
  const blobIds: string[] = [];
  for (const file of files) {
    const id = crypto.randomUUID();
    blobIds.push(id);
    await transact("blobs", "readwrite", (store) => store.put({ id, operation_id: operationId, name: file.name, type: file.type, blob: file, ...identity } satisfies StoredBlob));
  }
  const item: OutboxItem = {
    operation_id: operationId,
    type,
    payload,
    created_at: new Date().toISOString(),
    status: blockedMessage ? "conflict" : "waiting",
    message: blockedMessage || undefined,
    code: blockedMessage ? (identityInvalidated ? "invalid_offline_grant" : "offline_access_expired") : undefined,
    blob_ids: blobIds,
    ...identity,
  };
  await putOutboxItem(item);
  emit(blockedMessage ? "Sync conflict" : "Saved on this device", (await outboxItems()).length, blockedMessage);
  if (!blockedMessage) window.setTimeout(() => void syncNow(), 0);
  return operationId;
}

async function loadBlob(id: string): Promise<StoredBlob | undefined> {
  return transact<StoredBlob | undefined>("blobs", "readonly", (store) => store.get(id));
}

async function deleteBlob(id: string): Promise<void> {
  await deleteRecord("blobs", id);
}

export async function discardOperation(operationId: string): Promise<void> {
  const identity = requireIdentity();
  const item = await transact<OutboxItem | undefined>("outbox", "readonly", (store) => store.get(operationId));
  if (!item || !ownedBy(item, identity)) throw new Error("Saved work is not available to this account.");
  await deleteRecord("outbox", operationId);
  await Promise.all(item.blob_ids.map(deleteBlob));
  await refreshSyncLabel();
}

async function currentServerIdentity(signal: AbortSignal): Promise<Pick<OfflineIdentity, "user_id" | "organization_id">> {
  const response = await api<{ user?: Json }>("/api/v1/auth/me/", { signal });
  if (!isRecord(response.user)) throw new ApiError("The signed-in account could not be verified.", 401, "identity_unavailable");
  return {
    user_id: String(response.user.id ?? ""),
    organization_id: String(response.user.organization_id ?? ""),
  };
}

async function prepareOperation(item: OutboxItem, identity: OfflineIdentity, signal: AbortSignal): Promise<Json | null> {
  if (identityInvalidated || !ownedBy(item, identity) || item.offline_grant !== identity.offline_grant) {
    item.status = "conflict";
    item.message = IDENTITY_MISMATCH_MESSAGE;
    item.code = "invalid_offline_grant";
    await putOutboxItem(item);
    return null;
  }
  if (!identityIsUsable(identity) || parseExpiry(item.expires_at) <= Date.now()) {
    item.status = "conflict";
    item.message = EXPIRED_MESSAGE;
    item.code = "offline_access_expired";
    await putOutboxItem(item);
    return null;
  }
  const blobs: StoredBlob[] = [];
  for (const blobId of item.blob_ids) {
    const stored = await loadBlob(blobId);
    if (!stored || stored.operation_id !== item.operation_id || !ownedBy(stored, identity) || stored.offline_grant !== identity.offline_grant || parseExpiry(stored.expires_at) <= Date.now()) {
      item.status = "conflict";
      item.message = MISSING_BLOB_MESSAGE;
      item.code = "missing_attachment";
      await putOutboxItem(item);
      return null;
    }
    blobs.push(stored);
  }
  const attachmentIds: string[] = [];
  try {
    for (const stored of blobs) {
      if (signal.aborted) throw new DOMException("Synchronization was cancelled", "AbortError");
      const file = new File([stored.blob], stored.name, { type: stored.type });
      const uploaded = await uploadAttachment(
        file,
        item.type.split(".")[0],
        item.operation_id,
        stored.id,
        identity.offline_grant,
        signal,
      );
      const attachment = uploaded.attachment;
      if (!isRecord(attachment) || !attachment.id) throw new Error("Attachment upload was not acknowledged.");
      attachmentIds.push(String(attachment.id));
    }
  } catch {
    item.status = "waiting";
    await putOutboxItem(item);
    return null;
  }
  item.status = "syncing";
  item.message = undefined;
  await putOutboxItem(item);
  return { operation_id: item.operation_id, type: item.type, payload: { ...item.payload, attachment_ids: attachmentIds }, created_at: item.created_at };
}

let syncing: Promise<void> | null = null;
let syncController: AbortController | null = null;
let syncSuspended = false;

export function syncNow(): Promise<void> {
  if (syncSuspended) return refreshSyncLabel();
  if (syncing) return syncing;
  const controller = new AbortController();
  syncController = controller;
  syncing = performSync(controller.signal).finally(() => {
    if (syncController === controller) syncController = null;
    syncing = null;
  });
  return syncing;
}

export async function cancelSync(): Promise<void> {
  syncSuspended = true;
  syncController?.abort();
  try { await syncing; }
  catch { /* Cancellation keeps queued work for the next authorized session. */ }
}

export function resumeSync(): void {
  if (!identityInvalidated && activeIdentity && identityIsUsable(activeIdentity)) syncSuspended = false;
}

async function performSync(signal: AbortSignal): Promise<void> {
  const identity = activeIdentity;
  const pending = (await outboxItems())
    .filter((item) => item.status === "waiting" || item.status === "syncing")
    .sort((left, right) => left.created_at.localeCompare(right.created_at) || left.operation_id.localeCompare(right.operation_id));
  if (signal.aborted || !identity || identityInvalidated || !identityIsUsable(identity) || !navigator.onLine || pending.length === 0) {
    await refreshSyncLabel();
    return;
  }
  emit("Waiting to sync", pending.length);
  for (let offset = 0; offset < pending.length; offset += MAX_SYNC_BATCH) {
    const batch = pending.slice(offset, offset + MAX_SYNC_BATCH);
    let serverIdentity: Pick<OfflineIdentity, "user_id" | "organization_id">;
    try {
      serverIdentity = await currentServerIdentity(signal);
    } catch {
      break;
    }
    if (!sameIdentity(identity, { ...serverIdentity, expires_at: identity.expires_at, offline_grant: identity.offline_grant })) {
      identityInvalidated = true;
      await markOwnedWorkConflict(IDENTITY_MISMATCH_MESSAGE);
      break;
    }
    const prepared: { item: OutboxItem; operation: Json }[] = [];
    for (const item of batch) {
      if (signal.aborted) break;
      const operation = await prepareOperation(item, identity, signal);
      if (operation) prepared.push({ item, operation });
    }
    if (signal.aborted) break;
    if (!prepared.length) continue;
    try {
      const response = await api<{ results?: Json[] }>("/api/v1/offline/sync/", {
        method: "POST",
        json: { operations: prepared.map(({ operation }) => operation) },
        headers: { "X-Offline-Grant": identity.offline_grant },
        signal,
      });
      const results = new Map((response.results ?? []).map((result) => [String(result.operation_id ?? ""), result]));
      for (const { item } of prepared) {
        const result = results.get(item.operation_id);
        if (!result) {
          item.status = "waiting";
          item.message = "The server did not acknowledge this saved change. It will be retried.";
          await putOutboxItem(item);
        } else if (result.status === "synced") {
          await deleteRecord("outbox", item.operation_id);
          await Promise.all(item.blob_ids.map(deleteBlob));
        } else {
          item.status = result.status === "conflict" ? "conflict" : "rejected";
          item.message = String(result.message ?? "This change needs your attention");
          item.code = result.code ? String(result.code) : undefined;
          item.client_payload = isRecord(result.client)
            ? result.client
            : { type: item.type, payload: item.payload, created_at: item.created_at };
          item.server_payload = isRecord(result.server) ? result.server : {};
          item.resolution_options = Array.isArray(result.resolution_options)
            ? result.resolution_options.map(String)
            : ["review_server", "discard_local"];
          await putOutboxItem(item);
        }
      }
    } catch {
      for (const { item } of prepared) {
        item.status = "waiting";
        await putOutboxItem(item);
      }
      break;
    }
  }
  await refreshSyncLabel();
}

export async function invalidateOfflineAccess(): Promise<void> {
  identityInvalidated = true;
  syncSuspended = true;
  syncController?.abort();
  identityChannel?.postMessage({ type: "invalidated" } satisfies IdentityMessage);
  await clearReadableCache();
  await refreshSyncLabel();
}

async function deleteOwnedOfflineWork(identity: OfflineIdentity): Promise<void> {
  const db = await openDb();
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(["outbox", "blobs"], "readwrite");
    for (const storeName of ["outbox", "blobs"] as const) {
      const cursorRequest = tx.objectStore(storeName).openCursor();
      cursorRequest.onsuccess = () => {
        const cursor = cursorRequest.result;
        if (!cursor) return;
        if (ownedBy(cursor.value as Partial<OwnedOfflineRecord>, identity)) cursor.delete();
        cursor.continue();
      };
    }
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
    tx.onabort = () => { db.close(); reject(tx.error); };
  });
}

export async function clearOfflineData(): Promise<void> {
  const identity = activeIdentity;
  identityInvalidated = true;
  syncSuspended = true;
  syncController?.abort();
  await clearReadableCache();
  if (identity) await deleteOwnedOfflineWork(identity);
  activeIdentity = null;
  identityChannel?.postMessage({ type: "deleted" } satisfies IdentityMessage);
}

export function startForegroundSync(): () => void {
  const run = () => void syncNow();
  window.addEventListener("online", run);
  window.addEventListener("focus", run);
  document.addEventListener("visibilitychange", run);
  void refreshSyncLabel();
  void syncNow();
  return () => {
    window.removeEventListener("online", run);
    window.removeEventListener("focus", run);
    document.removeEventListener("visibilitychange", run);
  };
}
