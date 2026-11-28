// The conversation pages' logic that needs no page: tested on its own with
// `node --test` (tests/js/). Nothing here touches the DOM or the network.

// A conversation's id, as the server writes it: nothing else names one in an
// address (decision 040, phase 2c).
const CONVERSATION_ID = /^[0-9a-f]{32}$/;

// The thread a message opens, or null for anything that is not an id.
export function threadUrl(conversationId, guid) {
  if (typeof conversationId !== "string" || !CONVERSATION_ID.test(conversationId)) return null;
  const base = `/timeline/conversations/${conversationId}`;
  return guid ? `${base}?around=${encodeURIComponent(guid)}` : base;
}

// A message event of the Day view as one line of text: who (unless it is the
// conversation's own name, as in a direct one), then the text cut to `most`
// characters, or what the message holds when it has none.
export function messageLine(title, payload, most = 120) {
  const sender = (payload && payload.sender) || "";
  const named = payload && sender === payload.display_name;
  const who = payload && payload.from_me ? "You" : named ? "" : sender;
  let text = typeof title === "string" ? title.replace(/\s+/g, " ").trim() : "";
  if (text.length > most) text = `${text.slice(0, most).trimEnd()}…`;
  if (!text && payload) {
    if (payload.unsent) text = "Unsent";
    else if (payload.audio) text = "Audio message";
    else if (payload.attachments > 0) {
      text = payload.attachments === 1 ? "1 attachment" : `${payload.attachments} attachments`;
    }
  }
  return who && text ? `${who}: ${text}` : who || text;
}

// Where a thread opens: at its end (the latest page, or the page before a
// cursor), at its marked message (around), or at its start (at a day, or the
// page after a cursor).
export function initialScroll(opened) {
  if (opened === "around") return "marked";
  if (opened === "at" || opened === "after") return "top";
  return "bottom";
}

// The scroll position that keeps the reader's place after content of
// `newHeight - oldHeight` pixels was put above it. Done by hand: iOS Safari
// has no scroll anchoring (`overflow-anchor`), and the page turns it off
// elsewhere so the two never add up.
export function keptScrollTop(oldTop, oldHeight, newHeight) {
  return Math.max(0, oldTop + (newHeight - oldHeight));
}

// Runs `change`, which puts content above what is read (and may change what
// sits above it, such as the link to older messages), then moves the box by
// the height that added, so the reader's place is kept. Anything that changes
// height below the reader must happen outside it.
export function keepingPlace(box, change) {
  const top = box.scrollTop;
  const height = box.scrollHeight;
  change();
  box.scrollTop = keptScrollTop(top, height, box.scrollHeight);
}

// The scroll position that puts an element in the middle of the box.
export function centredTop(offsetTop, height, clientHeight) {
  return Math.max(0, offsetTop - (clientHeight - height) / 2);
}

// Which pages to load now: older when the reader is within `margin` pixels of
// the top, newer within `margin` of the bottom, each only if a cursor leads
// there and no load of that side runs.
export function wanted(box, pager, margin = 800) {
  return {
    older: box.scrollTop <= margin && pager.can("before"),
    newer: nearEnd(box, margin) && pager.can("after"),
  };
}

// Whether the reader is within `margin` pixels of the box's bottom: where a
// conversation's pictures, newest first, load their next (older) page.
export function nearEnd(box, margin = 800) {
  return box.scrollHeight - box.scrollTop - box.clientHeight <= margin;
}

// A thread's two cursors: one load at a time on each side; a side whose
// cursor is null has reached its end. A failed load keeps its cursor, so the
// next scroll tries again.
export class Pager {
  constructor(before, after) {
    this.cursors = { before: before || null, after: after || null };
    this.loading = { before: false, after: false };
  }

  can(side) {
    return this.cursors[side] !== null && !this.loading[side];
  }

  // The cursor to load with, or null if this side cannot load now.
  begin(side) {
    if (!this.can(side)) return null;
    this.loading[side] = true;
    return this.cursors[side];
  }

  // A page arrived: its own cursor on the side it extends (null at the end).
  done(side, cursor) {
    this.loading[side] = false;
    this.cursors[side] = cursor || null;
  }

  failed(side) {
    this.loading[side] = false;
  }
}

// Where two pages meet, what the lower one repeats: its day separator when
// the message above is of the same day, and its sender's name when the same
// sender's run goes on across the join. `upper` and `lower` are {day, run}
// of the messages on either side, `upper` null at the thread's start.
export function seam(upper, lower) {
  if (!upper || !lower) return { separator: false, sender: false };
  const sameDay = upper.day === lower.day;
  return { separator: sameDay, sender: sameDay && upper.run === lower.run };
}
