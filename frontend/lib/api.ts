import type { SupabaseClient } from "@supabase/supabase-js";
import type { ApiEnvelope } from "./types";

export class ApiError extends Error {
  code: string;
  status: number;

  constructor(message: string, code = "request_failed", status = 0) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

const API_PREFIX = "/api/v1";

function messageFromPayload(payload: unknown, fallback: string): { message: string; code: string } {
  if (payload && typeof payload === "object") {
    const error = (payload as Record<string, unknown>).error;
    if (error && typeof error === "object") {
      const detail = error as Record<string, unknown>;
      return {
        message: typeof detail.message === "string" ? detail.message : fallback,
        code: typeof detail.code === "string" ? detail.code : "request_failed",
      };
    }
    const detail = (payload as Record<string, unknown>).detail;
    if (typeof detail === "string") return { message: detail, code: "request_failed" };
  }
  return { message: fallback, code: "request_failed" };
}

async function parseResponse(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try { return JSON.parse(text) as unknown; } catch { return text; }
}

export async function publicRequest<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    signal,
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  const payload = await parseResponse(response);
  if (!response.ok) {
    const detail = messageFromPayload(payload, `Request failed (${response.status}).`);
    throw new ApiError(detail.message, detail.code, response.status);
  }
  return payload as T;
}

export async function apiRequest<T>(
  client: SupabaseClient,
  path: string,
  options: { method?: string; body?: unknown; file?: File; signal?: AbortSignal; retry?: boolean } = {},
): Promise<T> {
  const makeRequest = async (accessToken: string | null) => {
    const headers = new Headers({ Accept: "application/json" });
    if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
    let body: BodyInit | undefined;
    if (options.file) {
      const form = new FormData();
      form.append("file", options.file, options.file.name);
      body = form;
    } else if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(options.body);
    }
    return fetch(`${API_PREFIX}${path}`, {
      method: options.method || "GET", headers, body, credentials: "same-origin",
      cache: "no-store", signal: options.signal,
    });
  };
  const { data } = await client.auth.getSession();
  let response = await makeRequest(data.session?.access_token || null);
  if (response.status === 401 && options.retry !== false) {
    const refreshed = await client.auth.refreshSession();
    if (refreshed.data.session?.access_token) response = await makeRequest(refreshed.data.session.access_token);
  }
  const payload = await parseResponse(response);
  if (!response.ok) {
    const detail = messageFromPayload(payload, `Request failed (${response.status}).`);
    throw new ApiError(detail.message, detail.code, response.status);
  }
  return payload as T;
}

export function unwrapData<T>(payload: ApiEnvelope<T> | T | null | undefined): T | null {
  if (!payload || typeof payload !== "object") return payload as T | null;
  if ("data" in payload && payload.data !== undefined) return payload.data as T;
  return payload as T;
}

export function unwrapItems<T>(payload: ApiEnvelope<T>): T[] {
  return Array.isArray(payload?.items) ? payload.items : [];
}

export function errorMessage(error: unknown, fallback = "Something went wrong."): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function idempotencyKey(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2);
  return `${prefix}-${suffix}`;
}
