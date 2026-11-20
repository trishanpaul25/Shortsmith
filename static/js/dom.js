// Tiny helpers for building the page safely (titles can come from an AI or a stream,
// so text is never injected as HTML).
export function $(selector, root = document) {
    const node = root.querySelector(selector);
    if (!node)
        throw new Error(`The page is missing ${selector}`);
    return node;
}
export function $$(selector, root = document) {
    return [...root.querySelectorAll(selector)];
}
export function el(tag, props = {}, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
        if (typeof value === "function")
            node.addEventListener(key.replace(/^on/, ""), value);
        else if (key === "class")
            node.className = String(value);
        else if (key === "text")
            node.textContent = String(value);
        else if (value === true)
            node.setAttribute(key, "");
        else if (value !== false)
            node.setAttribute(key, value);
    }
    for (const kid of kids.flat()) {
        if (kid !== null && kid !== false && kid !== undefined)
            node.append(kid);
    }
    return node;
}
export function fmtTime(seconds) {
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const x = s % 60;
    return `${h ? `${h}:${String(m).padStart(2, "0")}` : m}:${String(x).padStart(2, "0")}`;
}
export function show(node, visible) {
    node.hidden = !visible;
}
