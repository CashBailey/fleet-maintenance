export type Json = Record<string, unknown>;

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;

  constructor(message: string, status: number, code = "request_failed", details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export const AUTHORIZATION_FAILURE_EVENT = "fleetline:authorization-failure";

let csrfToken = "";
const inFlightMutations = new Map<string, Promise<unknown>>();
const ambiguousMutationKeys = new Map<string, string>();
const activeRequestControllers = new Set<AbortController>();

export function abortActiveRequests(): void {
  activeRequestControllers.forEach((controller) => controller.abort());
  activeRequestControllers.clear();
}

async function trackedFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const controller = new AbortController();
  const parentSignal = init.signal;
  const abort = () => controller.abort();
  if (parentSignal?.aborted) controller.abort();
  else parentSignal?.addEventListener("abort", abort, { once: true });
  activeRequestControllers.add(controller);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    activeRequestControllers.delete(controller);
    parentSignal?.removeEventListener("abort", abort);
  }
}

function notifyAuthorizationFailure(status: number, code = ""): void {
  const explicitlyRevoked = ["session_revoked", "account_disabled", "offline_access_revoked", "not_authenticated"].includes(code);
  if ((status === 401 || explicitlyRevoked) && typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(AUTHORIZATION_FAILURE_EVENT, { detail: { status } }));
  }
}

export async function sessionStatus(): Promise<boolean> {
  let response: Response;
  try {
    response = await trackedFetch("/api/v1/auth/csrf/", { credentials: "same-origin" });
  } catch {
    throw new ApiError("Network unavailable. Check your connection and try again.", 0, "network_error");
  }
  if (!response.ok) {
    notifyAuthorizationFailure(response.status);
    throw new ApiError("Unable to start a secure session", response.status);
  }
  const payload = (await response.json()) as { csrf_token?: string; authenticated?: boolean };
  csrfToken = payload.csrf_token ?? "";
  return payload.authenticated === true;
}

async function ensureCsrf(): Promise<string> {
  if (csrfToken) return csrfToken;
  await sessionStatus();
  return csrfToken;
}

function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  return `{${Object.entries(value as Record<string, unknown>).sort(([left], [right]) => left.localeCompare(right)).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(",")}}`;
}

function requestBodyFingerprint(body: BodyInit | null | undefined): string {
  if (!body) return "";
  if (typeof body === "string") return body;
  if (body instanceof URLSearchParams) return body.toString();
  if (body instanceof FormData) return [...body.entries()].map(([key, value]) => {
    if (typeof value === "string") return `${key}=${value}`;
    return `${key}=file:${value.name}:${value.type}:${value.size}:${value.lastModified}`;
  }).join("&");
  return Object.prototype.toString.call(body);
}

function mutationFingerprint(method: string, path: string, body: BodyInit | null | undefined, headers: Headers): string {
  const stableHeaders = [...headers.entries()]
    .filter(([name]) => !["x-csrftoken", "idempotency-key"].includes(name.toLowerCase()))
    .sort(([left], [right]) => left.localeCompare(right));
  return canonical({ method, path, body: requestBodyFingerprint(body), headers: stableHeaders });
}

export async function api<T = Json>(
  path: string,
  options: RequestInit & { json?: unknown; idempotencyKey?: string } = {},
): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  let body = options.body;

  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.json);
  }
  const mutation = !["GET", "HEAD", "OPTIONS"].includes(method);
  if (mutation) {
    headers.set("X-CSRFToken", await ensureCsrf());
  }

  const fingerprint = mutation && !options.idempotencyKey ? mutationFingerprint(method, path, body, headers) : "";
  const existing = fingerprint ? inFlightMutations.get(fingerprint) : undefined;
  if (existing) return existing as Promise<T>;
  const idempotencyKey = mutation ? options.idempotencyKey ?? ambiguousMutationKeys.get(fingerprint) ?? crypto.randomUUID() : "";
  if (mutation) headers.set("Idempotency-Key", idempotencyKey);

  const request = (async (): Promise<T> => {
    let response: Response;
    try {
      response = await trackedFetch(path, { ...options, method, body, headers, credentials: "same-origin" });
    } catch {
      if (fingerprint) ambiguousMutationKeys.set(fingerprint, idempotencyKey);
      throw new ApiError("Network unavailable. Check your connection and try again.", 0, "network_error");
    }

    if (fingerprint) ambiguousMutationKeys.delete(fingerprint);
    const contentType = response.headers.get("content-type") ?? "";
    const payload = contentType.includes("json") ? await response.json() : await response.text();
    if (!response.ok) {
      const problem = typeof payload === "object" && payload ? (payload as Json) : {};
      const nested = isRecord(problem.error) ? problem.error : problem;
      const message = String(nested.message ?? nested.detail ?? (typeof payload === "string" ? payload : "") ?? `Request failed (${response.status})`);
      const code = String(nested.code ?? "request_failed");
      notifyAuthorizationFailure(response.status, code);
      throw new ApiError(message || `Request failed (${response.status})`, response.status, code, nested.details);
    }
    if (path === "/api/v1/auth/login/" || path === "/api/v1/auth/logout/") {
      csrfToken = "";
      ambiguousMutationKeys.clear();
    }
    return payload as T;
  })();
  if (fingerprint) inFlightMutations.set(fingerprint, request);
  try { return await request; }
  finally {
    if (fingerprint && inFlightMutations.get(fingerprint) === request) inFlightMutations.delete(fingerprint);
  }
}

export async function uploadAttachment(file: Blob & { name?: string }, resourceType: string, resourceId: string, idempotencyKey: string, offlineGrant?: string, signal?: AbortSignal) {
  const form = new FormData();
  form.append("resource_type", resourceType);
  form.append("resource_id", resourceId);
  form.append("file", file, file.name ?? "attachment");
  const headers = offlineGrant ? { "X-Offline-Grant": offlineGrant } : undefined;
  return api<{ attachment: Json }>("/api/v1/attachments/", { method: "POST", body: form, headers, idempotencyKey, signal });
}

export function query(path: string, params: Record<string, string | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => value && search.set(key, value));
  return `${path}${search.size ? `?${search}` : ""}`;
}

export function records(payload: unknown, ...keys: string[]): Json[] {
  if (Array.isArray(payload)) return payload.filter(isRecord);
  if (!isRecord(payload)) return [];
  for (const key of keys) {
    const value = payload[key];
    if (Array.isArray(value)) return value.filter(isRecord);
  }
  for (const value of Object.values(payload)) {
    if (Array.isArray(value)) return value.filter(isRecord);
  }
  return [];
}

export function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function text(row: Json | undefined, ...keys: string[]): string {
  for (const key of keys) {
    const value = row?.[key];
    if (value !== undefined && value !== null && value !== "") {
      if (isRecord(value)) return text(value, "name", "number", "unit_number", "code", "id");
      return String(value);
    }
  }
  return "—";
}

export function identifier(row: Json): string {
  return text(row, "id", "pk", "uuid");
}

export function formatDate(value: unknown): string {
  if (!value) return "—";
  const date = new Date(String(value));
  return Number.isNaN(date.valueOf()) ? String(value) : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}
