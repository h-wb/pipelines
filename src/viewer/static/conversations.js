// The conversation pages (decision 040, phase 2c): a thread loads its older
// and newer pages as the reader scrolls, keeping their place, and opens at
// its latest message, a marked one or a day; a conversation's pictures load
// their next page as the end of the grid comes near. Pages are HTML the server
// rendered, with every value escaped; nothing from data is parsed here as
// markup. No storage: nothing is kept outside the page and the browser's HTTP
// cache, which logging out clears.

import {
  Pager,
  centredTop,
  initialScroll,
  keepingPlace,
  nearEnd,
  seam,
  wanted,
} from "./conversations-core.js";

// One page the server rendered alone, as htmx asks for it (an expired login
// answers 401 rather than the login page): the element `id` of it.
async function fragmentPage(url, id, what) {
  const response = await fetch(url, {
    credentials: "same-origin",
    headers: { "HX-Request": "true" },
  });
  if (response.status === 401) {
    location.assign("/login");
    throw new Error("You are logged out.");
  }
  if (!response.ok) throw new Error(`The ${what} could not be loaded (${response.status}).`);
  const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
  const page = parsed.getElementById(id);
  if (!page) throw new Error(`The ${what} could not be loaded.`);
  return page;
}

// A page's items without its empty placeholder, for this document.
function adopted(page) {
  return [...page.children]
    .filter((node) => !node.classList.contains("empty-page"))
    .map((node) => document.adoptNode(node));
}

class Thread {
  constructor(root) {
    this.root = root;
    this.fragment = root.dataset.fragment;
    this.box = document.getElementById("thread-scroll");
    this.list = document.getElementById("messages");
    this.status = document.getElementById("thread-status");
    this.links = { before: document.getElementById("older"), after: document.getElementById("newer") };
    this.pager = new Pager(this.list.dataset.before, this.list.dataset.after);
  }

  start() {
    const marked = document.getElementById("marked");
    const where = initialScroll(this.root.dataset.opened);
    if (where === "marked" && marked) {
      this.box.scrollTop = centredTop(marked.offsetTop, marked.offsetHeight, this.box.clientHeight);
    } else if (where === "bottom") {
      this.box.scrollTop = this.box.scrollHeight;
    } else {
      this.box.scrollTop = 0;
    }
    for (const [side, link] of Object.entries(this.links)) {
      // Still a way to load by hand, in place.
      link?.addEventListener("click", (event) => {
        event.preventDefault();
        this.load(side);
      });
    }
    this.box.addEventListener("scroll", () => this.check(), { passive: true });
    this.check();
  }

  check() {
    const want = wanted(this.box, this.pager);
    if (want.older) this.load("before");
    if (want.newer) this.load("after");
  }

  say(message) {
    this.status.textContent = message;
  }

  async load(side) {
    const cursor = this.pager.begin(side);
    if (cursor === null) return;
    const link = this.links[side];
    if (link) link.textContent = side === "before" ? "Loading older messages…" : "Loading newer messages…";
    let page;
    try {
      page = await this.#fetch(side, cursor);
    } catch (error) {
      this.pager.failed(side);
      if (link) link.textContent = side === "before" ? "Older messages" : "Newer messages";
      this.say(error.message);
      return;
    }
    // An empty page's placeholder goes once messages arrive; a fetched page
    // brings none of its own.
    const items = adopted(page);
    if (items.length) for (const empty of this.list.querySelectorAll(".empty-page")) empty.remove();
    this.pager.done(side, page.dataset[side]);
    if (side === "before") {
      const lower = this.list.querySelector(".message");
      // The link to older messages sits above too: hidden at the first
      // message, it is part of the height the place is kept by.
      keepingPlace(this.box, () => {
        this.list.prepend(...items);
        this.#join(this.#last(items), lower);
        this.#link(side);
      });
    } else {
      const upper = this.#last([...this.list.children]);
      this.list.append(...items);
      this.#join(upper, items.find((item) => item.classList.contains("message")));
      this.#link(side);
    }
    this.say("");
    this.check();
  }

  #fetch(side, cursor) {
    const query = new URLSearchParams({ [side]: cursor });
    return fragmentPage(`${this.fragment}?${query}`, "messages", "messages");
  }

  #last(items) {
    return items.filter((item) => item.classList.contains("message")).at(-1) || null;
  }

  // Where two pages meet, the lower one's day separator and sender's name are
  // hidden if they only repeat what is just above.
  #join(upper, lower) {
    if (!upper || !lower) return;
    const repeats = seam(
      { day: upper.dataset.day, run: upper.dataset.run },
      { day: lower.dataset.day, run: lower.dataset.run },
    );
    const separator = lower.previousElementSibling;
    if (repeats.separator && separator?.classList.contains("day-separator")) separator.hidden = true;
    const sender = lower.querySelector(".sender");
    if (repeats.sender && sender) sender.hidden = true;
  }

  #link(side) {
    const link = this.links[side];
    if (!link) return;
    const cursor = this.pager.cursors[side];
    link.parentElement.hidden = cursor === null;
    if (cursor !== null) {
      link.href = `${location.pathname}?${new URLSearchParams({ [side]: cursor })}`;
      link.textContent = side === "before" ? "Older messages" : "Newer messages";
    }
  }
}

// A conversation's pictures, newest first: the next page loads as the end of
// the grid comes near, or from the "More" link, in place.
class Photos {
  constructor(root) {
    this.fragment = root.dataset.fragment;
    this.box = document.getElementById("photos-scroll");
    this.grid = document.getElementById("photos");
    this.status = document.getElementById("thread-status");
    this.link = document.getElementById("more");
    this.pager = new Pager(this.grid.dataset.before, null);
  }

  start() {
    this.link?.addEventListener("click", (event) => {
      event.preventDefault();
      this.load();
    });
    this.box.addEventListener("scroll", () => this.check(), { passive: true });
    this.check();
  }

  check() {
    if (nearEnd(this.box) && this.pager.can("before")) this.load();
  }

  async load() {
    const cursor = this.pager.begin("before");
    if (cursor === null) return;
    if (this.link) this.link.textContent = "Loading more pictures…";
    let page;
    try {
      const query = new URLSearchParams({ before: cursor });
      page = await fragmentPage(`${this.fragment}?${query}`, "photos", "pictures");
    } catch (error) {
      this.pager.failed("before");
      if (this.link) this.link.textContent = "More";
      this.status.textContent = error.message;
      return;
    }
    this.grid.append(...adopted(page));
    this.pager.done("before", page.dataset.before);
    const next = this.pager.cursors.before;
    if (this.link) {
      this.link.parentElement.hidden = next === null;
      if (next !== null) {
        this.link.href = `${location.pathname}?${new URLSearchParams({ before: next })}`;
        this.link.textContent = "More";
      }
    }
    this.status.textContent = "";
    this.check();
  }
}

// The list beside a thread shows where it is, also in rows loaded later.
function markCurrent() {
  const thread = document.getElementById("thread");
  const pane = document.querySelector(".conversation-pane");
  if (!thread || !pane) return;
  for (const row of pane.querySelectorAll("a[data-conversation]")) {
    if (row.dataset.conversation === thread.dataset.conversation) row.setAttribute("aria-current", "page");
  }
}

const thread = document.getElementById("thread");
if (thread && document.getElementById("messages")) new Thread(thread).start();
if (thread && document.getElementById("photos")) new Photos(thread).start();
markCurrent();
const current = document.querySelector(".conversation-pane [aria-current]");
const pane = document.querySelector(".conversation-pane");
if (current && pane && pane.scrollHeight > pane.clientHeight) {
  pane.scrollTop = centredTop(current.offsetTop, current.offsetHeight, pane.clientHeight);
}
document.body.addEventListener("htmx:afterSwap", markCurrent);
// A thumbnail whose file is gone shows a tile, never a broken image: those
// that failed before this script ran, and those that fail later.
function brokenImage(image) {
  const link = image.closest(".attachment-image");
  if (!link) return;
  const tile = document.createElement("span");
  tile.className = "file-tile file-absent";
  const name = document.createElement("span");
  name.className = "file-name";
  name.textContent = image.alt;
  const about = document.createElement("span");
  about.className = "file-about";
  about.textContent = "The image could not be loaded";
  tile.append(name, about);
  link.replaceWith(tile);
}
document.addEventListener(
  "error",
  (event) => {
    if (event.target instanceof HTMLImageElement) brokenImage(event.target);
  },
  true,
);
for (const image of document.querySelectorAll(".attachment-image img")) {
  if (image.complete && image.naturalWidth === 0) brokenImage(image);
}
// A page brought back from the back-forward cache after logging out shows
// nothing it kept: it loads again, and the server asks for the login.
window.addEventListener("pageshow", (event) => {
  if (event.persisted) location.reload();
});
