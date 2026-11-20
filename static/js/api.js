export class ApiError extends Error {
}
async function parse(r) {
    let body = {};
    try {
        body = await r.json();
    }
    catch {
        /* empty body */
    }
    if (!r.ok) {
        const msg = body.error;
        throw new ApiError(msg ?? "Something went wrong. Try again.");
    }
    return body;
}
const send = (method, url, body) => fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
});
export const health = () => fetch("/api/health").then((r) => parse(r));
export const listJobs = () => fetch("/api/jobs").then((r) => parse(r));
export const getJob = (id) => fetch(`/api/jobs/${id}`).then((r) => parse(r));
export const pauseJob = (id) => send("POST", `/api/jobs/${id}/pause`).then((r) => parse(r));
export const resumeJob = (id) => send("POST", `/api/jobs/${id}/resume`).then((r) => parse(r));
export const retryJob = (id) => send("POST", `/api/jobs/${id}/retry`).then((r) => parse(r));
export const deleteJob = (id) => send("DELETE", `/api/jobs/${id}`).then((r) => parse(r));
export const uploadClip = (id, n, target, privacy, again = false) => send("POST", `/api/jobs/${id}/clips/${n}/upload`, { target, privacy, again }).then((r) => parse(r));
export const getSettings = () => fetch("/api/settings").then((r) => parse(r));
export const saveYoutube = (client_id, client_secret) => send("POST", "/api/settings/youtube", { client_id, client_secret }).then((r) => parse(r));
export const saveInstagram = (token, user_id, host) => send("POST", "/api/settings/instagram", { token, user_id, host }).then((r) => parse(r));
export const disconnect = (service) => send("DELETE", `/api/settings/${service}`).then((r) => parse(r));
/** Create a job. Uses XMLHttpRequest so a big file upload can show its progress. */
export function createJob(form, onUpload) {
    return new Promise((resolve, reject) => {
        const x = new XMLHttpRequest();
        x.open("POST", "/api/jobs");
        x.upload.onprogress = (e) => {
            if (e.lengthComputable)
                onUpload(e.loaded / e.total);
        };
        x.onload = () => {
            let body = {};
            try {
                body = JSON.parse(x.responseText);
            }
            catch {
                /* ignore */
            }
            if (x.status < 300)
                resolve(body);
            else
                reject(new ApiError(body.error ?? "Something went wrong. Try again."));
        };
        x.onerror = () => reject(new ApiError("Could not reach the app. Is it still running in your terminal?"));
        x.send(form);
    });
}
