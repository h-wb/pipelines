---
area: "[[Homelab]]"
categories:
  - "[[Resources]]"
reviewed: 2026-11-26
summary: How the server reads the timeline's tables, the JSON API it answers (screen, browser and conversations), how it serves images, and how the Day view, search and conversation pages behave.
topics:
  - "[[Warehouse]]"
  - "[[Screen activity]]"
  - "[[Browser history]]"
  - "[[Apple Messages]]"
created: 2026-11-24T23:30
---

# Timeline

The timeline (decision 040) lets you move through a day of screenshots and
browser visits and search what was on screen. When the analysis links a
`messages` input, it also reads Apple Messages: conversations to open and
search, and a lane of messages on the Day view. Its tables are built by the
[`timeline` package](analyses.md#the-timeline-package); the server only reads
them. It is the server's only access to the warehouse, and its code is the
viewer part, `src/viewer/` ([architecture](../architecture.md)): the reader
(`reader.py`, its sessions in `database.py`), the API (`timeline.py`), the Day
view and search (`timeline_pages.py`), the conversation pages
(`conversations.py`), and its own `templates/` and `static/` folders. It reads
nothing of the configuration itself: the server hands it a `ViewerSettings`
(`settings.py`) with the reader database and, as the configuration is when it
answers, the timeline analyses (their schema, time zone, linked inputs and
lanes) and each connection's media kinds. It names no source and no media kind:
its lanes are declared with the package ([a new source](#a-new-source)). Its pages extend the server's base template, and
its scripts and stylesheet are served under `/static/viewer/`. `serve` runs it
with the control plane; `view` runs it alone ([below](#running-it-alone)).

## Turning it on

It is off until the server's environment names a read-only connection and the
configuration has a `timeline` analysis:

- `MIROIR_TIMELINE_DATABASE_URL`, a `postgresql://` URL; or
- `MIROIR_TIMELINE_DATABASE__HOST`, `__USERNAME` and `__PASSWORD` (required) and
  `__PORT` and `__DATABASE` (default 5432 and the username). The URL wins.

The role should be able to select from the timeline's tables and nothing else,
with `statement_timeout = '5s'` and `default_transaction_read_only = on`. The
server sets both on every session it opens anyway, so one slow search cannot hold
a connection and a mistaken grant cannot lead to a write. Image lookups have a
1-second limit.

Each server process holds up to five connections as the reader: four for
queries, one of its own for image lookups, so images never wait behind searches.
During a rolling update two processes run, so the role's connection limit must
allow ten for both to run at full use.

The server starts without the warehouse; a database that is down only shows on
`GET /timeline` and `GET /api/timeline/health` (JSON: 200 when ready, 503 when not
configured, unreachable or writable).

Searches never put words in an address. They are sent with POST, on the page and
in the API; an address with `q` is refused (400), and the server's access log
drops every query string. Proxies in front log what they receive, which with POST
is no longer the words.

## Running it alone

`view` serves the viewer in a process of its own, beside `serve
--without-viewer`, which serves the control plane without it
([package split](../decisions/2026-11-25-package-split.md#runtime), step 10):

```bash
uv run --locked miroir --config miroir.json view --port 8766
# In a container: /app/.venv/bin/python -m src.cli --config miroir.json view --host 0.0.0.0
```

It needs:

- the configuration file, read only, when it answers: the `timeline` analyses,
  their inputs, each connection's media kinds and the `media` section. A missing
  file stops it (2); it never creates one;
- the reader database, `MIROIR_TIMELINE_DATABASE_URL` or
  `MIROIR_TIMELINE_DATABASE__*` as [above](#turning-it-on); without it `view`
  stops (2);
- `MIROIR_API_TOKEN`, the same as `serve`'s, so the `miroir_session` cookie and
  the bearer token open both;
- with a `media` section, `MIROIR_MEDIA_ACCESS_KEY_ID` and
  `MIROIR_MEDIA_SECRET_ACCESS_KEY`: the same names, a read-only key in the
  cluster.

It never opens the metadata store and needs neither `MIROIR_SECRET_KEY`,
Prefect nor the warehouse's writer. It answers `/timeline`, `/api/timeline`,
`/media` (the view route), `/static/viewer`, and `/login`, `/logout`,
`/healthz` and the shell's `/static` files; every admin path is 404. A login on
it lands on `/timeline` when it is reached directly; behind the gateway `/login`
is `serve`'s and lands on `/`. The gateway sends it `/timeline`, `/api/timeline`,
`/media` and `/static/viewer`, and everything else to `serve`
([architecture](../architecture.md#processes)). `serve --without-viewer` never
reads `MIROIR_TIMELINE_DATABASE*`, and its header still links the timeline, from
the configuration alone. `serve` with no option keeps running both.

## The API

The server reads the first `timeline` analysis in the configuration (the first
of a package that declares lanes), in the schema its build writes. A reader's
first query reads the tables' version in `contract`: a major this server does
not read answers 503 "built by another package version". Tables built before
`contract` existed (until the first build after the deploy that adds it) are
read as version 1.0. Others are named in a `warning` field of the health
check and of every answer. Every answer carries `analysis` and `timezone`. Dates
are local days in that time zone, `YYYY-MM-DD`; a day runs from its local
midnight to the next (where clocks go back at midnight, from the later of the
two). Times are ISO 8601 with their offset. Each route needs the login (cookie
or administrator bearer), reads through the read-only session, and bounds what
it returns. An unknown or repeated parameter is refused.

| Route | Parameters | Answer |
| --- | --- | --- |
| `GET /api/timeline/days` | `from`, `to`: days, inclusive, at most 400 apart | `{from, to, days: [{day, screen?: {events, frames}, browser?: {events}, messages?: {events}}]}`, only days with rows, in order: one key per lane shown, its source; `messages` only when the analysis links a `messages` input |
| `GET /api/timeline/screen/{day}` | none | A day's frames as columns, below |
| `GET /api/timeline/frames/{key}` | none (`key` is the frame's media key, slashes included) | One frame, below; 404 if unknown |
| `GET /api/timeline/events` | `from`, `to`: a day or a time with offset (`to` as a day includes it); `source`: lanes' sources, comma-separated: `screen`, `browser` and, when linked, `messages` (default: the lanes shown always, `screen,browser`); `limit` 1–500 (200); `after`: the previous page's `next` | `{events: [...], next}`, events that started in `[from, to)`, oldest first |
| `GET /api/timeline/conversations` | `limit` 1–200 (50); `after`: the previous page's `next` | `{conversations: [...], next}`, newest activity first, below |
| `GET /api/timeline/conversations/{id}` | none (`id` is a `conversation_id`) | One conversation, below; 404 if unknown |
| `GET /api/timeline/conversations/{id}/messages` | At most one of `before` and `after` (a cursor of a previous answer), `around` (a message's guid) and `at` (a day or a time with offset); `limit` 1–200 (100) | `{conversation_id, messages: [...], before, after}`, below |
| `POST /api/timeline/search` | A JSON body (`Content-Type: application/json`, else 415; at most 8 KB; each field once). `q`: 1–200 characters, web search syntax (`"a phrase"`, `-word`, `or`), with at least one word to look for in every part (`-word` alone, or `a or -b`, is 400); `kind`: a lane's source, `screen` (default), `browser` or `messages`; `app`: a bundle identifier (screen only); `conversation`: a `conversation_id` (messages only); `from`, `to`: days, optional; `limit` 1–50 (20); `before`: the previous page's `next`. A `GET` answers 400 "Search with POST" | Moments, visits or messages, newest first, below |

The screen index of a day, small enough for about 3,000 frames (about 110 KB,
15 KB compressed):

```json
{"day": "2026-11-20", "start": "2026-11-20T00:00:00+01:00",
 "end": "2026-11-21T00:00:00+01:00", "connection": "screen",
 "media_kind": "screenshots", "count": 2, "truncated": false,
 "apps": [{"bundle_id": "com.apple.Safari", "name": "Safari", "colour": "#2196f3"}],
 "frames": {"t": [0, 60], "app": [0, null], "key": ["2026/11/20/a.jpeg", "2026/11/20/b.jpeg"],
            "media": [1, 0]}}
```

`t` is seconds from `start`; `app` an index into `apps`, or null; `key` the media
key. `media` is what is known of the screenshot: `0` nothing yet (it may still
be queued or not derived; not the same as absent), `1` both thumbnails exist,
`2` stored without thumbnails, `3` withheld (never uploaded). At most 20,000
frames; `truncated` says more exist. This answer is
`Cache-Control: private, no-cache` with a weak ETag, so a browser revalidates it
(late frames can still change a past day) and gets 304 when unchanged.

A frame: `{key, captured_at, day, t, app: {bundle_id, name, colour} | null, text,
text_truncated, withheld, masked, media: {connection, kind, key, state, status}}`.
`text` is the masked text (at most 100,000 characters), null when withheld;
`withheld` the reason; `masked` how many replacements the collector made;
`state` one of `unknown`, `thumbnails`, `stored`, `withheld`; `status` the
worker's thumbnail status or null.

An event: `{source, event_id, started_at, ended_at, kind, title, body, group,
payload, media}`. For a screen session `kind` is `app_session`, `title` the app,
`group` its bundle identifier, `payload` `{bundle_id, frames, last_frame}`, and
`media` its first frame's (`state` `withheld` when that frame is). For a visit
`kind` is `visit`, `body` the address, `group` the site, `media` null. For a
message `kind` is `message`, `title` its text, `group` its `conversation_id`,
`payload` `{guid, conversation, display_name, sender, from_me, service,
attachments, reactions, edited, unsent, audio, reply_to}` (`display_name` the
conversation's, `sender` the sender's name or null for mine, `attachments` and
`reactions` counts), and `media` its first attachment's. Title and body are cut
at 2,000 characters.

A conversation in the list: `{conversation_id, display_name, is_group,
participant_count, last_message_at, message_count, preview, preview_from_me}`.
Only conversations with a message are listed; those whose messages are all
undated come last, with a null `last_message_at`. `message_count` includes
undated messages. `preview` is the first 200 characters of the last dated
message, null when it has no text; `preview_from_me` whether I sent it. One
conversation adds `title` (a group's own), `first_message_at`, `participants:
[{handle, display_name}]` (never me) and `undated`, how many of its messages have
no time. An id that is not 32 lower-case hex characters is 404 without a query.

A conversation's messages are its dated ones, oldest first, paged by `(sent_at,
guid)`: without a parameter the latest page; `before` or `after` the page before
or after a cursor; `around` a page with that message in the middle (nearer an
end, the page still fills; 404 if it is not a dated message of this
conversation); `at` the page starting at that time (a day: its local midnight),
empty when nothing is that late. The answer's `before` and `after` are the
neighbouring pages' cursors, null at either end. Undated messages are not paged:
they have no place on the axis, and the conversation counts them.

A message: `{guid, sent_at, day, from_me, sender: {handle, display_name} | null,
service, text, text_truncated, text_unreadable, edited, edited_at, unsent, audio,
reply_to, attachments: [{guid, kind, mime_type, bytes, transfer_name, media}],
reactions: [{kind, from_me, sender, part, at}]}`. `sender` is null for mine; its
`display_name` the address book's name, else the handle as the message writes it.
`text` is whole up to 100,000 characters (`text_truncated` beyond), as the
collector delivered it. A reaction's `sender` is the person's name, or their handle when the timeline has no one by that handle (someone no longer in the chat), null for mine. An
attachment's `media` is `{connection, kind: "attachments", key, state, status}`
as a frame's, `state` `unknown`, `thumbnails` or `stored` (an attachment is never
withheld), and null for a file with no media key. Names are read from `people`
and `conversations` at each answer, so a renamed contact shows at the next build.

A screen search answers `{kind: "screen", media_kind: "screenshots",
moment_seconds: 300, apps, moments, next}` (`media_kind`, the kind of the
moments' files, since step 11 of the package split). A moment is the matching frames of one app in one five-minute window
(windows start on multiples of five minutes): `{key, connection, at, until, day,
app, matches, snippet, media}`, `key` and `at` its first match, `until` its last.
A browser search answers `{kind: "browser", visits: [{event_id, at, title, url,
site, snippet}], next}`, matching titles and addresses: the search of every lane
without a plugin, over `events.search`, with `url` its events' `body` and `site`
their `group`. A message search answers `{kind: "messages",
messages: [{guid, conversation_id, display_name, sent_at, day, from_me, sender,
snippet}], next}`, dated messages only, matched through the text index of
`messages` whatever the filters. A `snippet` is a list of
strings alternating plain text and matches, plain text first: show it as text,
never as markup. `next` is null on the last page.

Errors are `{"error": "..."}`: 400 for a parameter, 404 for an unknown frame,
conversation or message, 503 when the timeline is not configured, not built yet
or unreachable (for the conversation routes and a message search, also when the
analysis has no `messages` input), 504 when a query reached the time limit. Every answer but the screen index is
`Cache-Control: private, no-store`. Answers over 1 KB are gzip-compressed for a
client that accepts it.

## A new source

A source joins the timeline without a change to the viewer: its Python, its
script and its templates draw lanes from declarations and `events` alone
(package split, step 11). It takes:

1. An input role in the timeline package (`src/analyses/catalog.py`), optional,
   for the connector that feeds it.
2. One model, `dbt/models/timeline/timeline_<source>.sql`, writing `events`
   rows with `source` its own name: the columns of
   [`events`](analyses.md#lanes-and-the-contract) and `deleted`,
   `previous_started_at`, `_input` and `_stamp`. Files are joined only through
   `media_ref`.
3. A `Lane(role, source, label, colour, plugin)` in `src/analyses/views.py`
   (`LANES`), after the others: they are the order `events` unites them in. Its
   `units` word its count ("walk", "walks"), `search_in` where its search looks.

The next build reads it (a normal build; a lane's role, source and media kind
are part of the build's receipt, so the analysis shows *stale* until then, but
its wording is not) and records it in `contract`. Where its role is linked, the Day view draws its lane (marks in one
colour, or by group), counts it in the summary and the calendar, lists its
events around the frame and gives search a kind of its own over its events'
titles and bodies (a model that writes an empty `search` is found by nothing).
The API takes its source in `/api/timeline/events?source=`, answers it in
`/api/timeline/days` and as a search `kind`. A plugin, a screen of its own, is
viewer code named by the lane; an unknown name fails the catalog test. A test
adds such a source to a copy of the dbt project and checks all of this
(`tests/test_timeline_lanes.py`).

Never take a lane out of the declaration while builds have its rows: each
build removes, from `events` and `days`, the rows of every listed lane whose
input was replaced or unlinked, and a lane no longer listed is no longer
cleaned.

## Images through the server

`GET /media/{connection}/{kind}/{key}` without parameters redirects to a short
signed link. With `size`:

- `size=360` or `size=1280` copies that thumbnail from the bucket; 404 if the
  worker made none (show a placeholder);
- `size=original` copies an original image (by its extension) of at most 25 MB
  and the kind's limit, and redirects to a signed link for any other or larger
  file, so the storage serves range requests.

Copied bytes carry `Cache-Control: private, max-age=31536000, immutable` and the
bucket's ETag, and answer `If-None-Match` with 304. The connection, kind and key
are checked as for an upload. The server only copies bytes; it never decodes an
image.

The route is the viewer's (`src/viewer/media.py`); it calls media's view side
(`src/media/links.py`) after its login and withheld checks. Before it serves a
file, at every size and for the plain redirect, it asks the analyses that link
the file's connection and whose package declares a `Withhold` for its kind
(`src/analyses/views.py`). The timeline declares one: a screenshot whose frame
in `screen_frames` has a `withheld` reason. A withheld frame's file answers 404
(it may have been stored before a newer rule withheld the frame); a key with no
frame yet is served. Only the analyses that can answer are asked (a
`reader_role` set and tables built); a frame withheld in any of them stays
hidden. If none can answer, the route refuses with the timeline's error (503 or
504) rather than serve the file. A key with a control character is 404 before
any lookup. Other kinds and connections are not affected. The file stays in the
bucket: it is hidden, not deleted
([decision 040](decisions.md#040-amendment--the-withheld-check-reads-declarations)).

## The pages

With a timeline configured, the header links **Timeline**, and **Conversations**
when the analysis links a `messages` input. Every page needs the login and is
`Cache-Control: private, no-store`. JavaScript fills the Day view's lanes and
scrubber from the API (`src/viewer/static/timeline-core.js`, tested with
`node --test`, and `timeline.js`) and pages a conversation as you scroll
(`conversations-core.js`, tested the same way, and `conversations.js`); the rest
of each page works without it. All four, and `conversations.css`, are served at
`/static/viewer/<name>`.

- `/timeline` opens the latest day with frames or visits (the lanes shown
  always) in the last 400 days, or today. Without a reader or a timeline analysis it says why (503), as every
  timeline page does; tables not built or the warehouse down say so too (503),
  a query past the time limit 504.
- `/timeline/{day}` is the Day view: a month calendar beside it, each day shaded
  by its frames plus visits, with links to the days before and after; the frame
  shown, with its time, app, state and masked text, selectable, and "Open the
  original" when its file is stored; a lane per source on the day's time axis
  (23 to 25 hours), Screen coloured by app and Browser with one mark per visit;
  the visits within fifteen minutes of the frame shown. The summary counts each
  lane (frames for the screen, events for the others), and so does the
  calendar's shade. A day with nothing recorded says so.
- With a `messages` input the Day view also counts the day's messages, shades
  the calendar by them too (a day of messages alone is not empty), draws a third
  lane, Messages, with one mark per message that opens its thread at that
  message, and lists the messages within fifteen minutes of the frame beside the
  visits, each with its conversation's name. Without the input it is as it was.
- `?frame=<key>` opens the day at that frame (a frame of another day opens on its
  own day). The address follows the scrubber.
- Scrubbing: drag the slider, or drag or tap a lane. Keys on the slider or a
  lane: arrows a frame, Shift and an arrow a minute, Page Up and Page Down an
  hour, Home and End the first and last frame. While moving it shows the
  360-pixel thumbnail of the frame under the cursor, at most ten a second; at
  rest (a quarter second) the 1280-pixel one, the frame's text, and the two
  thumbnails on each side preloaded.
- Instead of an image: "Image state not known yet" (no record of the file: it
  may well be stored), "No thumbnail could be made" (stored; the original may
  still open), "Withheld" (never uploaded, no text either), "The image could not
  be loaded" (a record whose file is gone).
- `/timeline/search` has a kind per lane shown. It searches screen text (moments, newest window first, apps in
  name order within one) or, with **Browser visits**, titles and addresses, or,
  with **Messages** (where linked), message text, newest first, each result with
  its conversation's name, the sender and the time, opening the thread at that
  message; with an app filter (screen only), a conversation filter (messages
  only, set by a thread's own search) and a date range. The form is posted, so the words
  are never in the address bar, the history or a log; `?q=` is refused (400) with
  an empty form. The form takes no files, at most 10 fields and 8 KB a part.
  Each moment opens the Day view at its first match. "Older results" posts the
  words, filters and next-page token again, in place with htmx or as a page.
- `/timeline/conversations` lists conversations, newest activity first: the
  name, a mark on a group, the last message's first 200 characters ("You:" when
  I sent it) and its time. More rows load as the end of the list comes into
  view (htmx; a "More conversations" link without it).
- `/timeline/conversations/{id}` is a thread, the list beside it. It opens on the
  latest messages; `?around=<guid>` opens with that message in the middle and
  marked, `?at=<day>` (or a time with its offset) at that day, from the date
  field. Older messages load as you scroll up, newer ones as you scroll down,
  80 at a time, and the page keeps your place when older ones arrive above (by
  hand: iOS Safari does no scroll anchoring); without JavaScript, "Older
  messages" and "Newer messages" are links (`?before=` and `?after=` cursors).
  Days are separated in the analysis's time zone; mine on the right, theirs on
  the left, in a group the sender's name on the first message of a run. A
  message shows when it was edited, "Unsent", "Audio message", "The text could
  not be read", and a reply quotes the first line of its message when that is on
  the page, else "Reply to an earlier message" opens the thread at it.
  Reactions are shown under the message, an emoji (or "emoji", "sticker") with
  who. The participants are named under the title; a thread's own search posts
  its words with the conversation set. The page title holds no name. An unknown
  conversation is 404; an `around` message that is not in it shows the latest
  messages and says so. Messages without a date are counted, not shown.
- An attachment shows in place, by what is known of its file: its 360-pixel
  thumbnail, opening the 1280-pixel one; a stored file without thumbnails
  (video, audio, documents, an image none could be made of) a tile with its
  name, type and size that opens the file from storage, so a video streams from
  there; "File state not known yet" with no record of it; "Not available" for a
  file that was never stored. A thumbnail box has a fixed size, so one that
  loads late moves nothing. An attachment shows its thumbnail or tile after the
  first timeline build that follows its derive run ([Media](media.md)). Beside
  a thumbnail, "Original" opens the stored file itself: the media route without
  `size`, as a stored file's tile does.
- `/timeline/conversations/{id}/photos`, linked as "Photos" from the thread's
  header, is a grid of the conversation's pictures: its attachments with
  thumbnails (images, and videos that have one), newest first, from the same
  messages the thread shows (deleted and undated ones never). Each tile is the
  360-pixel thumbnail opening the 1280-pixel one, with its time, "Original",
  and "In the thread", which opens the thread at its message (`?around=`).
  60 load at a time, by keyset on the message's time, its guid and the
  attachment's place in it: the next page loads as the end of the grid comes
  near, or from "More" (`?before=` a cursor) without JavaScript. A conversation
  with none says "No pictures in this conversation"; an unknown one is 404.
- An htmx request made after the login expired gets `HX-Redirect` to the login.
- Up to 760 pixels wide everything is one column: the image, the text, the
  lanes, the slider, the calendar; lanes and slider are taller for a finger. A
  thread is shown alone there, with a link back to the list.

Data is always shown as text: titles, snippets, screen text, message text, names
and file names are never read as markup, and only `http` and `https` addresses
become links (`rel="noreferrer"`), with no previews. A conversation is named in
an address by its id only, a message by its guid; a handle, a number or search
words never are.
The pages keep nothing in the browser beyond its HTTP cache: no service worker,
no IndexedDB, htmx's history off. Logging out sends
`Clear-Site-Data: "cache", "storage"` and empties the page's storages, and a page
restored from the back-forward cache after logging out reloads and asks for the
login.
