import * as api from "./api.js";
import { $, $$, el, fmtTime, show } from "./dom.js";
import { accounts, initAccounts, onAccountsChange } from "./settings.js";
// ---------------------------------------------------------- hero: captions that light up word by word
(function karaoke() {
    const words = $$("#caption-hero .w");
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
        words[3]?.classList.add("on");
        return;
    }
    let i = -1;
    setInterval(() => {
        words.forEach((w) => w.classList.remove("on"));
        i = (i + 1) % (words.length + 2); // short pause at the end of each loop
        words[i]?.classList.add("on");
    }, 420);
})();
// ---------------------------------------------------------- form behaviour
const form = $("#job-form");
let mode = "link";
$$(".seg button").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode === "file" ? "file" : "link";
    $$(".seg button").forEach((x) => {
        x.classList.toggle("on", x === b);
        x.setAttribute("aria-pressed", String(x === b));
    });
    show($("#link-wrap"), mode === "link");
    show($("#file-wrap"), mode === "file");
    $("#url").required = mode === "link";
    $("#file").required = mode === "file";
}));
$("#file").addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    $("#file-name").textContent = f ? f.name : "Choose a video file from your computer";
});
$("#length").addEventListener("input", (e) => {
    $("#length-out").textContent = e.target.value;
});
$$("input[name=picker]").forEach((r) => r.addEventListener("change", () => {
    show($("#key-wrap"), form.elements.namedItem("picker").value !== "audio");
}));
// auto-upload boxes only work once the account is connected
onAccountsChange((s) => {
    const rules = [
        ["#auto-youtube", s.youtube.connected],
        ["#auto-instagram", s.instagram.connected],
    ];
    for (const [sel, ok] of rules) {
        const box = $(sel);
        box.disabled = !ok;
        if (!ok)
            box.checked = false;
        box.closest("label")?.classList.toggle("off", !ok);
    }
    show($("#yt-privacy-wrap"), s.youtube.connected);
    if (currentJob)
        renderResults(currentJob); // refresh the Post buttons
});
$("#auto-youtube").addEventListener("change", updateAutoWarning);
$("#auto-instagram").addEventListener("change", updateAutoWarning);
function updateAutoWarning() {
    const any = $("#auto-youtube").checked || $("#auto-instagram").checked;
    show($("#auto-warning"), any);
}
form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    if (mode === "link")
        fd.delete("file");
    else
        fd.delete("url");
    const targets = ["youtube", "instagram"].filter((t) => fd.get(`auto_${t}`));
    if (targets.length && !confirm(`When the clips are ready they will be posted to ${targets.join(" and ")} automatically. Continue?`))
        return;
    const go = $("#go");
    go.disabled = true;
    go.textContent = "Starting...";
    try {
        const job = await api.createJob(fd, (f) => {
            go.textContent = `Uploading ${Math.round(f * 100)}%`;
        });
        location.hash = `job=${job.id}`;
        openJob(job.id);
    }
    catch (err) {
        alert(err.message);
    }
    finally {
        go.disabled = false;
        go.textContent = "Make clips";
    }
});
// ---------------------------------------------------------- job view
let currentId = null;
let currentJob = null;
let timer;
let renderedFor = "";
function openJob(id) {
    currentId = id;
    currentJob = null;
    renderedFor = "";
    clearTimeout(timer);
    show($("#results"), false);
    show($("#job"), true);
    $("#job").scrollIntoView({ behavior: "smooth", block: "start" });
    void poll(id);
}
const isActive = (j) => j.state === "queued" || j.state === "running";
const uploadsBusy = (j) => (j.clips ?? []).some((c) => Object.values(c.uploads ?? {}).some((u) => u && (u.state === "queued" || u.state === "uploading")));
async function poll(id) {
    try {
        const job = await api.getJob(id);
        if (id !== currentId)
            return;
        currentJob = job;
        renderJob(job);
        if (isActive(job) || (job.state === "done" && uploadsBusy(job))) {
            timer = window.setTimeout(() => void poll(id), 1500);
        }
        if (!isActive(job))
            void loadHistory();
    }
    catch {
        if (id === currentId)
            timer = window.setTimeout(() => void poll(id), 3000);
    }
}
const STAGES = ["get", "find", "render"];
function renderJob(job) {
    $("#job-title").textContent = job.title;
    show($("#live-tag"), !!job.live);
    $("#step-get").textContent = job.live ? "Read the stream so far" : "Get the audio";
    const idx = job.state === "done" ? STAGES.length : Math.max(0, STAGES.indexOf(job.stage));
    $$("#steps li").forEach((li, i) => {
        li.classList.toggle("done", i < idx);
        li.classList.toggle("active", i === idx && job.state !== "error" && job.state !== "paused");
    });
    const pct = job.state === "done" ? 100 : job.percent ?? 0;
    $("#bar-fill").style.width = `${pct}%`;
    $(".bar").setAttribute("aria-valuenow", String(Math.round(pct)));
    $(".bar").classList.toggle("paused", job.state === "paused");
    $("#step-text").textContent =
        job.state === "queued" ? "Waiting for the current job to finish."
            : job.state === "error" ? "This job stopped."
                : job.state === "paused" ? job.step || "Paused."
                    : job.state === "done" ? `Done. ${job.clip_count || (job.clips ?? []).length} clips are ready.`
                        : job.step || "";
    // Pause / Continue / Retry / Delete
    const pause = $("#pause-btn");
    show(pause, job.state === "running" || job.state === "queued");
    pause.disabled = !!job.pausing;
    pause.textContent = job.pausing ? "Pausing..." : "Pause";
    show($("#continue-btn"), job.state === "paused");
    show($("#retry-btn"), job.state === "error");
    show($("#delete-btn"), job.state === "paused" || job.state === "error");
    const err = $("#error");
    show(err, job.state === "error");
    err.textContent = job.error ?? "";
    const log = $("#log");
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.textContent = (job.log ?? []).join("\n");
    if (atBottom)
        log.scrollTop = log.scrollHeight;
    if (job.state === "done")
        renderResults(job);
}
$("#pause-btn").addEventListener("click", async () => {
    if (!currentId)
        return;
    try {
        renderJob(await api.pauseJob(currentId));
        clearTimeout(timer);
        void poll(currentId);
    }
    catch (err) {
        alert(err.message);
    }
});
$("#continue-btn").addEventListener("click", async () => {
    if (!currentId)
        return;
    try {
        renderJob(await api.resumeJob(currentId));
        clearTimeout(timer);
        void poll(currentId);
    }
    catch (err) {
        alert(err.message);
    }
});
$("#retry-btn")?.addEventListener("click", async () => {
    if (!currentId)
        return;
    try {
        renderJob(await api.retryJob(currentId));
        clearTimeout(timer);
        void poll(currentId);
    }
    catch (err) {
        alert(err.message);
    }
});
$("#delete-btn").addEventListener("click", async () => {
    if (!currentId || !confirm("Delete this job and everything it saved on your computer?"))
        return;
    try {
        await api.deleteJob(currentId);
        currentId = null;
        currentJob = null;
        show($("#job"), false);
        show($("#results"), false);
        void loadHistory();
    }
    catch (err) {
        alert(err.message);
    }
});
// ---------------------------------------------------------- results
const LABEL = { youtube: "YouTube", instagram: "Instagram" };
function renderResults(job) {
    const clips = job.clips ?? [];
    show($("#results"), true);
    $("#zip").href = `/api/jobs/${job.id}/zip`;
    $("#tick-end").textContent = fmtTime(job.duration ?? 0);
    $("#timeline-label").textContent = job.live
        ? "Where the clips come from in the stream (the part that had been streamed when you started)"
        : "Where the clips come from in the original video";
    const key = `${job.id}:${clips.length}`;
    if (renderedFor !== key) {
        renderedFor = key;
        const dur = job.duration || 1;
        const track = $("#track");
        track.replaceChildren();
        clips.forEach((c, i) => track.append(el("a", {
            href: `#clip-${i + 1}`,
            text: String(i + 1),
            style: `left:${(c.start_s / dur) * 100}%;width:${((c.end_s - c.start_s) / dur) * 100}%`,
            title: `Clip ${i + 1}: ${c.from} to ${c.to}`,
        })));
        const grid = $("#grid");
        grid.replaceChildren();
        clips.forEach((c, i) => grid.append(clipCard(job, c, i)));
    }
    clips.forEach((c, i) => paintUploads(job, c, i));
}
function clipCard(job, c, i) {
    const src = `/media/${job.id}/${c.file}`;
    const tags = (c.hashtags ?? []).join(" ");
    const copyLabel = c.description || tags ? "Copy post text" : "Copy title";
    const copy = el("button", { class: "btn ghost small", type: "button", text: copyLabel });
    copy.addEventListener("click", async () => {
        const text = [c.title, c.description, tags].filter(Boolean).join("\n");
        try {
            await navigator.clipboard.writeText(text);
            copy.textContent = "Copied";
        }
        catch {
            copy.textContent = "Copy failed";
        }
        setTimeout(() => (copy.textContent = copyLabel), 1500);
    });
    const ratio = c.ratio ?? job.options.ratio;
    return el("article", { class: "clip", id: `clip-${i + 1}` }, el("video", { controls: true, preload: "metadata", playsinline: true, src: `${src}#t=0.5`, class: `r-${ratio.replace(":", "x")}` }), el("h3", { text: `${i + 1}. ${c.title}` }), el("p", { class: "range", text: `${c.from} to ${c.to} in the ${job.live ? "stream" : "original"}, ${Math.round(c.seconds)} seconds, ${ratio}` }), c.description ? el("p", { class: "desc", text: c.description }) : null, tags ? el("p", { class: "tags", text: tags }) : null, el("div", { class: "actions" }, el("a", { class: "btn small", href: src, download: c.file, text: "Download" }), copy), el("div", { class: "post", "data-i": String(i) }));
}
/** Rebuilds only the "Post to ..." row of one card, so a playing video is never interrupted. */
function paintUploads(job, c, i) {
    const box = document.querySelector(`#clip-${i + 1} .post`);
    if (!box)
        return;
    const acc = accounts();
    const privacy = job.options.yt_privacy ?? "private";
    const rows = ["youtube", "instagram"].map((t) => {
        const u = c.uploads?.[t];
        const connected = t === "youtube" ? acc?.youtube.connected : acc?.instagram.connected;
        if (u?.state === "done") {
            return el("p", { class: "up ok" }, `Posted to ${LABEL[t]}: `, el("a", { href: u.url ?? "#", target: "_blank", rel: "noopener", text: "open" }));
        }
        if (u?.state === "queued" || u?.state === "uploading") {
            return el("p", { class: "up busy", text: `${LABEL[t]}: ${u.step ?? "waiting"}...` });
        }
        const btn = el("button", { class: "btn ghost small", type: "button", text: u?.state === "error" ? `Retry ${LABEL[t]}` : `Post to ${LABEL[t]}` });
        if (!connected) {
            btn.disabled = true;
            btn.title = `Connect ${LABEL[t]} in the Accounts section first`;
        }
        btn.addEventListener("click", async () => {
            const where = t === "youtube" ? `YouTube as ${privacy}` : "Instagram as a public Reel";
            if (!confirm(`Post clip ${i + 1} to ${where}?`))
                return;
            try {
                currentJob = await api.uploadClip(job.id, i + 1, t, privacy, u?.state === "error");
                renderJob(currentJob);
                clearTimeout(timer);
                void poll(job.id);
            }
            catch (err) {
                alert(err.message);
            }
        });
        return el("div", { class: "up" }, btn, u?.state === "error" ? el("p", { class: "up bad", text: u.error ?? "Upload failed." }) : null);
    });
    box.replaceChildren(...rows);
}
// ---------------------------------------------------------- history
const BADGE = { done: "Done", error: "Failed", queued: "Waiting", paused: "Paused", running: "Working" };
async function loadHistory() {
    try {
        const items = await api.listJobs();
        const list = $("#history-list");
        list.replaceChildren();
        show($("#history-empty"), items.length === 0);
        for (const j of items) {
            const open = el("button", { class: "btn ghost small", type: "button", text: "Open" });
            open.addEventListener("click", () => {
                location.hash = `job=${j.id}`;
                openJob(j.id);
            });
            const del = el("button", { class: "btn ghost small", type: "button", text: "Delete" });
            del.disabled = j.state === "running";
            del.title = j.state === "running" ? "Pause it first" : "";
            del.addEventListener("click", async () => {
                if (!confirm("Delete this job and its clips from your computer?"))
                    return;
                try {
                    await api.deleteJob(j.id);
                }
                catch (err) {
                    alert(err.message);
                    return;
                }
                if (currentId === j.id) {
                    currentId = null;
                    show($("#job"), false);
                    show($("#results"), false);
                }
                void loadHistory();
            });
            const when = new Date(j.created * 1000).toLocaleString();
            list.append(el("li", {}, el("span", { class: `badge ${j.state}`, text: BADGE[j.state] ?? j.state }), j.live ? el("span", { class: "badge live", text: "Live" }) : null, el("span", { class: "h-title", text: j.title, title: j.title }), el("span", { class: "h-meta", text: `${j.clip_count} clips, ${when}` }), open, del));
        }
    }
    catch {
        /* server not reachable, leave the list as it is */
    }
}
// ---------------------------------------------------------- startup
api
    .health()
    .then((h) => {
    if (!h.ffmpeg) {
        const b = $("#banner");
        show(b, true);
        b.textContent = "ffmpeg is not installed, so clips cannot be made yet. Install it (see the README), then restart this app.";
    }
})
    .catch(() => undefined);
initAccounts();
void loadHistory();
const hashJob = /job=([a-f0-9]+)/.exec(location.hash);
if (hashJob?.[1])
    openJob(hashJob[1]);
