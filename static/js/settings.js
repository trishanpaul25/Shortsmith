// The "Accounts" section: connect YouTube and Instagram so clips can be posted.
import * as api from "./api.js";
import { $, show } from "./dom.js";
let current = null;
const listeners = [];
export const accounts = () => current;
export function onAccountsChange(fn) {
    listeners.push(fn);
    if (current)
        fn(current);
}
function badge(node, on, textOn, textOff) {
    node.textContent = on ? textOn : textOff;
    node.classList.toggle("done", on);
}
function paint(s) {
    badge($("#yt-status"), s.youtube.connected, "Connected", s.youtube.configured ? "Saved, not connected" : "Not set up");
    badge($("#ig-status"), s.instagram.connected, "Connected", "Not connected");
    const connect = $("#yt-connect");
    connect.classList.toggle("disabled", !s.youtube.configured);
    connect.setAttribute("aria-disabled", String(!s.youtube.configured));
    show($("#yt-disconnect"), s.youtube.configured || s.youtube.connected);
    show($("#ig-disconnect"), s.instagram.connected);
    $("#yt-redirect").textContent = s.redirect_uri;
    $("#ig-host").value = s.instagram.host;
    if (s.instagram.user_id)
        $("#ig-user").placeholder = `Saved: ${s.instagram.user_id}`;
    listeners.forEach((fn) => fn(s));
}
export async function refreshAccounts() {
    try {
        current = await api.getSettings();
        paint(current);
    }
    catch {
        /* server not reachable: leave as is */
    }
}
function say(id, text, bad = false) {
    const p = $(id);
    p.textContent = text;
    p.classList.toggle("bad", bad);
}
export function initAccounts() {
    $("#yt-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const id = $("#yt-id");
        const secret = $("#yt-secret");
        try {
            await api.saveYoutube(id.value, secret.value);
            id.value = secret.value = "";
            say("#yt-msg", "Saved. Now press Connect to sign in with Google.");
            await refreshAccounts();
        }
        catch (err) {
            say("#yt-msg", err.message, true);
        }
    });
    $("#yt-connect").addEventListener("click", (e) => {
        if (!current?.youtube.configured) {
            e.preventDefault();
            say("#yt-msg", "Save your client ID and secret first.", true);
        }
    });
    $("#yt-disconnect").addEventListener("click", async () => {
        if (!confirm("Forget the saved YouTube login on this computer?"))
            return;
        await api.disconnect("youtube");
        say("#yt-msg", "Disconnected.");
        await refreshAccounts();
    });
    $("#ig-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const token = $("#ig-token");
        const user = $("#ig-user");
        try {
            await api.saveInstagram(token.value, user.value || current?.instagram.user_id || "", $("#ig-host").value);
            token.value = user.value = "";
            say("#ig-msg", "Saved. Instagram is connected.");
            await refreshAccounts();
        }
        catch (err) {
            say("#ig-msg", err.message, true);
        }
    });
    $("#ig-disconnect").addEventListener("click", async () => {
        if (!confirm("Forget the saved Instagram token on this computer?"))
            return;
        await api.disconnect("instagram");
        say("#ig-msg", "Disconnected.");
        await refreshAccounts();
    });
    // coming back from Google's sign-in page
    const q = new URLSearchParams(location.search);
    const yt = q.get("yt");
    if (yt) {
        say("#yt-msg", yt === "ok" ? "YouTube is connected." : q.get("msg") ?? "Could not connect YouTube.", yt !== "ok");
        history.replaceState(null, "", location.pathname + location.hash);
    }
    void refreshAccounts();
}
