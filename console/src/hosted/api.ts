export const hostedMode = import.meta.env.VITE_HOSTED === "1";
const base = ((import.meta.env.VITE_API_BASE as string | undefined) ?? "").replace(/\/+$/, "");

export async function hostedRequest<T = any>(path: string, body?: unknown, method?: string): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    method: method ?? (body === undefined ? "GET" : "POST"),
    credentials: "include",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = response.status === 204 ? {} : await response.json();
  if (!response.ok) {
    if (response.status === 401) throw new Error("Please sign in to continue.");
    const code = typeof data.error === "string" ? data.error : data.error?.code;
    throw new Error((code ?? "Request failed").replaceAll("_", " "));
  }
  return data;
}

export type HostedConnection = {
  id: string; provider: string; state: string; version: number;
  metadata: Record<string, any>;
};
