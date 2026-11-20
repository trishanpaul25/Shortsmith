import type { Clip, Health, Job, Privacy, Settings, Target } from "./types.js";

export class ApiError extends Error {}

async function parse<T>(r: Response): Promise<T> {
  let body: unknown = {};
  try {
    body = await r.json();
  } catch {
    /* empty body */
  }
  if (!r.ok) {
    const msg = (body as { error?: string }).error;
    throw new ApiError(msg ?? "Something went wrong. Try again.");
  }
  return body as T;
}

const send = (method: string, url: string, body?: unknown): Promise<Response> =>
  fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

export const health = (): Promise<Health> => fetch("/api/health").then((r) => parse<Health>(r));
export const listJobs = (): Promise<Job[]> => fetch("/api/jobs").then((r) => parse<Job[]>(r));
export const getJob = (id: string): Promise<Job> => fetch(`/api/jobs/${id}`).then((r) => parse<Job>(r));
export const pauseJob = (id: string): Promise<Job> => send("POST", `/api/jobs/${id}/pause`).then((r) => parse<Job>(r));
export const resumeJob = (id: string): Promise<Job> => send("POST", `/api/jobs/${id}/resume`).then((r) => parse<Job>(r));
export const retryJob = (id: string): Promise<Job> => send("POST", `/api/jobs/${id}/retry`).then((r) => parse<Job>(r));
export const deleteJob = (id: string): Promise<void> => send("DELETE", `/api/jobs/${id}`).then((r) => parse<void>(r));

export const uploadClip = (id: string, n: number, target: Target, privacy: Privacy, again = false): Promise<Job> =>
  send("POST", `/api/jobs/${id}/clips/${n}/upload`, { target, privacy, again }).then((r) => parse<Job>(r));

export const getSettings = (): Promise<Settings> => fetch("/api/settings").then((r) => parse<Settings>(r));
export const saveYoutube = (client_id: string, client_secret: string): Promise<unknown> =>
  send("POST", "/api/settings/youtube", { client_id, client_secret }).then((r) => parse(r));
export const saveInstagram = (token: string, user_id: string, host: string): Promise<unknown> =>
  send("POST", "/api/settings/instagram", { token, user_id, host }).then((r) => parse(r));
export const disconnect = (service: Target): Promise<void> =>
  send("DELETE", `/api/settings/${service}`).then((r) => parse<void>(r));

/** Create a job. Uses XMLHttpRequest so a big file upload can show its progress. */
export function createJob(form: FormData, onUpload: (fraction: number) => void): Promise<Job> {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", "/api/jobs");
    x.upload.onprogress = (e) => {
      if (e.lengthComputable) onUpload(e.loaded / e.total);
    };
    x.onload = () => {
      let body: { error?: string } & Partial<Job> = {};
      try {
        body = JSON.parse(x.responseText);
      } catch {
        /* ignore */
      }
      if (x.status < 300) resolve(body as Job);
      else reject(new ApiError(body.error ?? "Something went wrong. Try again."));
    };
    x.onerror = () => reject(new ApiError("Could not reach the app. Is it still running in your terminal?"));
    x.send(form);
  });
}

export type { Clip };
