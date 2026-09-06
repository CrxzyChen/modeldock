import { desktopBridge } from "./desktop";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export interface ApiOptions {
  method?: string;
  body?: unknown;
  idempotencyKey?: string;
}

export async function api<T>(
  path: string,
  options: ApiOptions = {},
): Promise<T> {
  const result = await desktopBridge().request<T>(path, {
    method: options.method ?? "GET",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    idempotencyKey: options.idempotencyKey,
  });
  if (!result.ok) {
    const data = result.data as { error?: { message?: string } } | null;
    throw new ApiError(
      data?.error?.message ?? `请求失败 (${result.status})`,
      result.status,
    );
  }
  return result.data;
}
