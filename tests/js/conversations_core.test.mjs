// Run by tests/test_timeline_js.py with Node's own test runner; no dependency.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  Pager,
  centredTop,
  initialScroll,
  keepingPlace,
  keptScrollTop,
  messageLine,
  nearEnd,
  seam,
  threadUrl,
  wanted,
} from "../../src/viewer/static/conversations-core.js";

const ID = "0123456789abcdef0123456789abcdef";

test("a thread is addressed by its id only, a message by its guid", () => {
  assert.equal(threadUrl(ID), `/timeline/conversations/${ID}`);
  assert.equal(threadUrl(ID, "a/b c&d"), `/timeline/conversations/${ID}?around=a%2Fb%20c%26d`);
  // A handle, a number or anything not an id makes no address.
  for (const bad of ["+15555550101", "sam@example.com", ID.toUpperCase(), `${ID}0`, "", null, 7]) {
    assert.equal(threadUrl(bad, "g1"), null, String(bad));
  }
});

test("a message event reads as one line", () => {
  assert.equal(messageLine("hello  there\nyou", { sender: "Robin" }), "Robin: hello there you");
  assert.equal(messageLine("done", { from_me: true, sender: null }), "You: done");
  assert.equal(messageLine("x".repeat(130), { sender: "Kai" }), `Kai: ${"x".repeat(120)}…`);
  assert.equal(messageLine(null, { sender: "Kai", attachments: 2 }), "Kai: 2 attachments");
  assert.equal(messageLine("", { sender: "Kai", attachments: 1 }), "Kai: 1 attachment");
  assert.equal(messageLine(null, { from_me: true, audio: true }), "You: Audio message");
  assert.equal(messageLine(null, { from_me: true, unsent: true }), "You: Unsent");
  assert.equal(messageLine(null, { sender: "Kai" }), "Kai");
  assert.equal(messageLine(null, null), "");
  // In a direct conversation the sender is its name: not said twice.
  assert.equal(messageLine("hi", { sender: "Alex", display_name: "Alex" }), "hi");
  assert.equal(messageLine("hi", { sender: "Robin", display_name: "Book Club" }), "Robin: hi");
});

test("a thread opens at its end, its mark or its start", () => {
  assert.equal(initialScroll("latest"), "bottom");
  assert.equal(initialScroll("before"), "bottom");
  assert.equal(initialScroll("around"), "marked");
  assert.equal(initialScroll("at"), "top");
  assert.equal(initialScroll("after"), "top");
  assert.equal(initialScroll(undefined), "bottom");
});

test("the reader's place is kept when older messages arrive above", () => {
  // 1,800 px of messages arrive above a reader 120 px from the top.
  assert.equal(keptScrollTop(120, 4000, 5800), 1920);
  assert.equal(keptScrollTop(0, 4000, 4000), 0);
  assert.equal(keptScrollTop(10, 4000, 3000), 0); // never above the top
  assert.equal(centredTop(1000, 100, 600), 750);
  assert.equal(centredTop(100, 100, 600), 0);
});

test("the place is kept by everything that changes above the reader", () => {
  // A box whose content is what sits above the reader plus what is below.
  const box = { above: 100, below: 3000, scrollTop: 60 };
  Object.defineProperty(box, "scrollHeight", { get: () => box.above + box.below });
  const before = box.above - box.scrollTop; // where the reader is, from the top
  keepingPlace(box, () => {
    box.above += 900; // the oldest page arrives...
    box.above -= 40; // ...and the "Older messages" link above it goes
  });
  assert.equal(box.scrollTop, 920);
  assert.equal(box.above - box.scrollTop, before);
});

test("pages are wanted near either end, while a cursor leads there", () => {
  const pager = new Pager("older-cursor", "newer-cursor");
  const box = (scrollTop) => ({ scrollTop, clientHeight: 600, scrollHeight: 10000 });
  assert.deepEqual(wanted(box(5000), pager), { older: false, newer: false });
  assert.deepEqual(wanted(box(300), pager), { older: true, newer: false });
  assert.deepEqual(wanted(box(9000), pager), { older: false, newer: true });
  assert.deepEqual(wanted({ scrollTop: 0, clientHeight: 600, scrollHeight: 600 }, pager), {
    older: true,
    newer: true,
  });
  // The latest page: no newer cursor, so only older messages load.
  const latest = new Pager("older-cursor", "");
  assert.deepEqual(wanted(box(9400), latest), { older: false, newer: false });
  assert.deepEqual(wanted(box(0), latest), { older: true, newer: false });
});

test("pictures load their next page near the bottom of the box", () => {
  const box = (scrollTop) => ({ scrollTop, clientHeight: 600, scrollHeight: 10000 });
  assert.equal(nearEnd(box(0)), false);
  assert.equal(nearEnd(box(8600)), true);
  assert.equal(nearEnd(box(9400)), true);
  // A grid shorter than its box is at its end.
  assert.equal(nearEnd({ scrollTop: 0, clientHeight: 600, scrollHeight: 300 }), true);
});

test("a pager loads one page at a time on each side, to the end", () => {
  const pager = new Pager("b1", null);
  assert.equal(pager.begin("after"), null); // no cursor: the end
  assert.equal(pager.begin("before"), "b1");
  assert.equal(pager.begin("before"), null); // one at a time
  assert.equal(pager.can("before"), false);
  pager.done("before", "b2");
  assert.equal(pager.begin("before"), "b2");
  pager.failed("before"); // kept: the next scroll tries again
  assert.equal(pager.begin("before"), "b2");
  pager.done("before", null);
  assert.equal(pager.can("before"), false);
  assert.equal(pager.begin("before"), null);
  // Both sides run on their own.
  const both = new Pager("b", "a");
  assert.equal(both.begin("before"), "b");
  assert.equal(both.begin("after"), "a");
  both.done("after", "");
  assert.equal(both.cursors.after, null);
});

test("where pages meet, a repeated day or sender is not shown twice", () => {
  const a = { day: "2026-01-10", run: "Robin" };
  assert.deepEqual(seam(a, { day: "2026-01-10", run: "Robin" }), { separator: true, sender: true });
  assert.deepEqual(seam(a, { day: "2026-01-10", run: "Kai" }), { separator: true, sender: false });
  // A new day shows its separator, and the name again under it.
  assert.deepEqual(seam(a, { day: "2026-01-11", run: "Robin" }), { separator: false, sender: false });
  assert.deepEqual(seam(null, a), { separator: false, sender: false });
});
