"""The conversation pages (decision 040, phase 2c, part 2): the list, a thread,
a conversation's pictures, and the fragments that page them.

Rendered on the server from the reader methods the JSON API uses; nothing here
writes SQL. `/static/viewer/conversations.js` loads further pages of a thread
(and of its pictures) as the reader scrolls and keeps their place; without it,
links page instead.
Every page is `Cache-Control: private, no-store`. A conversation is named in an
address only by its id; text, names and file names reach templates as values,
so Jinja escapes them, and only `http` and `https` addresses become links.

Kept apart from the Day view's routes: the thread plugin's pages, beside the
generic ones (package split).
"""

import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from src.kernel.errors import OperationError
from src.viewer.reader import (
    MAX_GUID,
    NO_ANALYSIS,
    NO_MESSAGES,
    NOT_CONFIGURED,
    TimelineReader,
    TimelineTimeout,
    TimelineUnavailable,
    read_cursor,
    timeline_source,
    valid_conversation_id,
)
from src.viewer.settings import ViewerSettings
from src.viewer.timeline import (
    NO_STORE,
    UNKNOWN_CONVERSATION,
    _cursor_time,
    _message_cursor,
    _plain,
    _time,
)

Page = Callable[..., HTMLResponse]
LIST_PAGE = 50
MESSAGE_PAGE = 80
PHOTO_PAGE = 60
# A reply's quote: the first line of the message it answers, this long at most.
QUOTE_CHARACTERS = 120
UNKNOWN_MESSAGE = "That message is not in this conversation."
# A tapback as an emoji, with its word for assistive technology; emoji and
# sticker reactions carry no emoji of their own (041), so they stay words.
REACTIONS = {
    "love": "❤️",
    "like": "👍",
    "dislike": "👎",
    "laugh": "😂",
    "emphasis": "‼️",
    "question": "❓",
}
# What an attachment shows where there is no image.
FILE_UNKNOWN = "File state not known yet"
FILE_MISSING = "Not available"
# Web addresses in message text; anything else stays text.
_LINK = re.compile(r"\bhttps?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING = ".,;:!?'\""


def thread_url(conversation_id: str, around: str | None = None) -> str:
    """A thread's address: its id, and a message's guid to open it at."""
    url = f"/timeline/conversations/{quote(conversation_id, safe='')}"
    return url + (f"?{urlencode({'around': around})}" if around else "")


def photos_url(conversation_id: str, before: str | None = None) -> str:
    """A conversation's pictures: its id, and the next page's cursor."""
    url = f"{thread_url(conversation_id)}/photos"
    return url + (f"?{urlencode({'before': before})}" if before else "")


def attachment_url(
    connection: str | None, kind: str, key: str, size: int | None
) -> str:
    """An attachment's file through the media route, from its row's connection
    and kind: a thumbnail with `size`, else the redirect to storage (so a video
    streams from there)."""
    url = (
        f"/media/{quote(connection or '', safe='')}/{quote(kind, safe='')}/"
        f"{quote(key, safe='/')}"
    )
    return url + (f"?size={size}" if size else "")


def linked(text: str) -> list[dict[str, Any]]:
    """Text as parts, `link` set on the web addresses in it."""
    parts: list[dict[str, Any]] = []
    position = 0
    for match in _LINK.finditer(text):
        address = match.group(0).rstrip(_TRAILING)
        # A closing bracket belongs to the address only if it opened one.
        while address.endswith(")") and address.count("(") < address.count(")"):
            address = address[:-1].rstrip(_TRAILING)
        if len(address) <= len("https://"):
            continue
        start = match.start()
        if start > position:
            parts.append({"text": text[position:start], "link": None})
        parts.append({"text": address, "link": address})
        position = start + len(address)
    if position < len(text):
        parts.append({"text": text[position:], "link": None})
    return parts


def file_size(count: Any) -> str | None:
    """A file's size for people: bytes, then KB, MB and GB (powers of 1000)."""
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None
    if count < 1000:
        return f"{count} bytes"
    value, unit = count / 1000, "KB"
    for larger in ("MB", "GB"):
        if value < 1000:
            break
        value, unit = value / 1000, larger
    return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"


def _moment(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _day_title(day: str) -> str:
    return date.fromisoformat(day).strftime("%A %d %B %Y")


def _when(iso: str | None, today: date) -> str:
    """A list row's time: the clock today, the day this year, else the date."""
    if not iso:
        return "No date"
    moment = _moment(iso)
    if moment.date() == today:
        return moment.strftime("%H:%M")
    if moment.year == today.year:
        return moment.strftime("%d %b")
    return moment.strftime("%d %b %Y")


def conversation_row(
    conversation: dict[str, Any], today: date, current: str | None
) -> dict[str, Any]:
    return {
        **conversation,
        "open": thread_url(conversation["conversation_id"]),
        "when": _when(conversation["last_message_at"], today),
        "current": conversation["conversation_id"] == current,
    }


def _attachment(item: dict[str, Any]) -> dict[str, Any]:
    media = item.get("media")
    view = {
        "name": item.get("transfer_name") or "Attachment",
        "type": item.get("mime_type") or item.get("kind") or "file",
        "size": file_size(item.get("bytes")),
        "image": None,
        "open": None,
        "original": None,
        "state": None,
    }
    if media is None:
        return {**view, "state": FILE_MISSING}
    key, kind = media["key"], media["kind"]
    if media["state"] == "thumbnails":
        return {
            **view,
            "image": attachment_url(media["connection"], kind, key, 360),
            "open": attachment_url(media["connection"], kind, key, 1280),
            # The stored file itself, as the route serves it without a size.
            "original": attachment_url(media["connection"], kind, key, None),
        }
    if media["state"] == "stored":
        return {**view, "open": attachment_url(media["connection"], kind, key, None)}
    return {**view, "state": FILE_UNKNOWN}


def _reactions(reactions: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Reactions grouped by kind, in the order first given, with who."""
    grouped: dict[str, list[str]] = {}
    for reaction in reactions:
        kind = reaction.get("kind") or "reaction"
        who = (
            "You" if reaction.get("from_me") else (reaction.get("sender") or "Someone")
        )
        names = grouped.setdefault(kind, [])
        if who not in names:
            names.append(who)
    return [
        {"kind": kind, "symbol": REACTIONS.get(kind, ""), "who": ", ".join(names)}
        for kind, names in grouped.items()
    ]


def _first_line(message: dict[str, Any]) -> str:
    text = (message.get("text") or "").strip()
    if text:
        line = text.splitlines()[0]
        cut = len(line) > QUOTE_CHARACTERS
        return line[:QUOTE_CHARACTERS].rstrip() + ("…" if cut else "")
    if message.get("unsent"):
        return "Unsent"
    if message.get("audio"):
        return "Audio message"
    if message.get("attachments"):
        return "Attachment"
    return "A message"


def _clock(iso: str) -> str:
    return _moment(iso).strftime("%H:%M")


def message_views(
    messages: list[dict[str, Any]],
    conversation: dict[str, Any],
    marked: str | None = None,
) -> list[dict[str, Any]]:
    """Messages as the thread shows them: a day separator before each new day,
    the sender's name on the first message of a run in a group."""
    on_page = {message["guid"]: message for message in messages}
    group = bool(conversation.get("is_group"))
    conversation_id = conversation["conversation_id"]
    views = []
    previous: dict[str, Any] | None = None
    for message in messages:
        sender = message["sender"]
        name = "You" if message["from_me"] else (sender or {}).get("display_name") or ""
        run = "me" if message["from_me"] else name
        separator = previous is None or previous["day"] != message["day"]
        reply = None
        if message["reply_to"]:
            quoted = on_page.get(message["reply_to"])
            reply = {
                "quote": _first_line(quoted) if quoted else None,
                "open": thread_url(conversation_id, message["reply_to"]),
            }
        edited = None
        if message["edited"]:
            edited = "Edited"
            if message["edited_at"]:
                at = _moment(message["edited_at"])
                same_day = at.date().isoformat() == message["day"]
                edited += " " + at.strftime("%H:%M" if same_day else "%d %b %H:%M")
        text = message["text"] or ""
        views.append(
            {
                "guid": message["guid"],
                "sent_at": message["sent_at"],
                "day": message["day"],
                "day_title": _day_title(message["day"]) if message["day"] else "",
                "separator": separator,
                "clock": _clock(message["sent_at"]),
                "from_me": message["from_me"],
                "sender": name,
                "run": run,
                "show_sender": group
                and not message["from_me"]
                and (separator or previous is None or previous["run"] != run),
                "service": message["service"],
                "parts": linked(text) if text and not message["unsent"] else [],
                "text_truncated": message["text_truncated"],
                "unreadable": message["text_unreadable"],
                "unsent": message["unsent"],
                "audio": message["audio"],
                "edited": edited,
                "reply": reply,
                "attachments": [_attachment(item) for item in message["attachments"]],
                "reactions": _reactions(message["reactions"]),
                "marked": message["guid"] == marked,
            }
        )
        previous = {"day": message["day"], "run": run}
    return views


def photo_views(
    photos: list[dict[str, Any]], conversation_id: str
) -> list[dict[str, Any]]:
    """A page of pictures as tiles: each with its message's time and a link to
    the thread at that message."""
    return [
        {
            **_attachment(photo["attachment"]),
            "message": photo["guid"],
            "sent_at": photo["sent_at"],
            "when": _moment(photo["sent_at"]).strftime("%d %b %Y %H:%M"),
            "thread": thread_url(conversation_id, photo["guid"]),
        }
        for photo in photos
    ]


def _photo_cursor(request: Request) -> tuple[datetime, str, int] | None:
    """Where a page of pictures starts: after `before`, a tile's place."""
    query = request.query_params
    if len(query.getlist("before")) > 1:
        raise OperationError("A parameter is given more than once")
    value = query.get("before")
    if value is None:
        return None
    at, guid, position = read_cursor(value, (str, str, int))
    if not guid or len(guid) > MAX_GUID or position < 1:
        raise OperationError("Invalid paging cursor")
    return _cursor_time(at), guid, position


def _position(request: Request) -> dict[str, Any]:
    """Where a thread opens: at most one of before, after (cursors), around (a
    message's guid) and at (a day or a time)."""
    query = request.query_params
    given = [name for name in ("before", "after", "around", "at") if name in query]
    if len(given) > 1:
        raise OperationError("Give at most one of before, after, around and at")
    if any(len(query.getlist(name)) > 1 for name in given):
        raise OperationError("A parameter is given more than once")
    if not given:
        return {}
    name = given[0]
    value = query[name]
    if name in ("before", "after"):
        return {name: _message_cursor(value)}
    if name == "around":
        around = _plain(value, "around")
        if not around or len(around) > MAX_GUID:
            raise OperationError("around must be a message's guid")
        return {"around": around}
    return {"at": _time(value, "at")}


def _list_cursor(value: str | None) -> tuple[datetime | None, str] | None:
    if value is None:
        return None
    at, conversation_id = read_cursor(value, (str, str))
    if not valid_conversation_id(conversation_id):
        raise OperationError("Invalid paging cursor")
    return (_cursor_time(at) if at else None, conversation_id)


def conversation_pages(settings: ViewerSettings, page: Page) -> APIRouter:
    database = settings.database
    router = APIRouter()

    def unavailable(request: Request, message: str, status: int) -> Response:
        state = {"configured": True, "ready": False, "message": message}
        if message in (NOT_CONFIGURED, NO_ANALYSIS, NO_MESSAGES):
            state["configured"] = False
        return _no_store(page(request, "timeline.html", timeline=state), status)

    def error(request: Request, message: str, status: int) -> Response:
        return _no_store(page(request, "error.html", message=message), status)

    def guarded(
        request: Request, render: Callable[[TimelineReader], Response]
    ) -> Response:
        if database is None:
            return unavailable(request, NOT_CONFIGURED, 503)
        source = timeline_source(settings.configuration())
        if source is None:
            return unavailable(request, NO_ANALYSIS, 503)
        if source.thread is None:
            return unavailable(request, NO_MESSAGES, 503)
        try:
            return render(TimelineReader(database, source))
        except TimelineTimeout as found:
            return unavailable(request, str(found), 504)
        except TimelineUnavailable as found:
            return unavailable(request, str(found), 503)
        except OperationError as found:
            return error(request, str(found), 400)

    def today(reader: TimelineReader) -> date:
        return datetime.now(ZoneInfo(reader.source.timezone)).date()

    def listing(
        reader: TimelineReader,
        after: tuple[datetime | None, str] | None,
        current: str | None,
    ) -> dict[str, Any]:
        found = reader.conversations(LIST_PAGE, after)
        day = today(reader)
        return {
            "conversations": [
                conversation_row(row, day, current) for row in found["conversations"]
            ],
            "more": f"/timeline/conversations?{urlencode({'after': found['next']})}"
            if found["next"]
            else None,
        }

    @router.get("/timeline/conversations", response_class=HTMLResponse)
    def conversation_list(request: Request) -> Response:
        """Newest activity first; `after` is the list's own cursor. htmx asks
        for the next rows alone."""

        def render(reader: TimelineReader) -> Response:
            after = _list_cursor(request.query_params.get("after"))
            context = {
                "source": reader.source.describe(),
                **listing(reader, after, None),
                "later_page": after is not None,
            }
            fragment = request.headers.get("hx-request") == "true"
            name = "_conversation_list.html" if fragment else "conversations.html"
            return _no_store(page(request, name, **context), 200)

        return guarded(request, render)

    def thread_page(
        reader: TimelineReader, conversation_id: str, position: dict[str, Any]
    ) -> dict[str, Any] | None:
        """A thread and one page of its messages; None if it is unknown."""
        # Not an id: unknown, before any query.
        if not valid_conversation_id(conversation_id):
            return None
        conversation = reader.conversation(conversation_id)
        if conversation is None:
            return None
        found = reader.conversation_messages(conversation_id, MESSAGE_PAGE, **position)
        missing = found is None
        if found is None:  # `around` a message that is not here: the latest page
            found = reader.conversation_messages(conversation_id, MESSAGE_PAGE)
            assert found is not None
            position = {}
        marked = position.get("around")
        return {
            "conversation": conversation,
            "messages": message_views(found["messages"], conversation, marked),
            "before": found["before"],
            "after": found["after"],
            "older": f"{thread_url(conversation_id)}?"
            + urlencode({"before": found["before"]})
            if found["before"]
            else None,
            "newer": f"{thread_url(conversation_id)}?"
            + urlencode({"after": found["after"]})
            if found["after"]
            else None,
            "thread": thread_url(conversation_id),
            "fragment": f"{thread_url(conversation_id)}/messages",
            "missing_message": missing,
            "opened": next(iter(position), "latest"),
        }

    @router.get(
        "/timeline/conversations/{conversation_id}", response_class=HTMLResponse
    )
    def thread(request: Request, conversation_id: str) -> Response:
        """A conversation: its latest page, or `around` a message, `at` a day or
        time, `before` or `after` a cursor (the links that page without
        JavaScript)."""

        def render(reader: TimelineReader) -> Response:
            position = _position(request)
            found = thread_page(reader, conversation_id, position)
            if found is None:
                return error(request, UNKNOWN_CONVERSATION, 404)
            conversation = found["conversation"]
            at = position.get("at")
            context = {
                "source": reader.source.describe(),
                **listing(reader, None, conversation_id),
                **found,
                "conversation_id": conversation_id,
                "conversation_name": conversation["display_name"],
                "people": [
                    person["display_name"] or person["handle"]
                    for person in conversation["participants"]
                ],
                "at_day": at.date().isoformat()
                if isinstance(at, datetime)
                else (at.isoformat() if at else ""),
                # The thread's kind of search, within this conversation.
                "search_kind": reader.source.thread.source
                if reader.source.thread
                else "",
                "photos": photos_url(conversation_id),
            }
            return _no_store(page(request, "conversation.html", **context), 200)

        return guarded(request, render)

    @router.get(
        "/timeline/conversations/{conversation_id}/messages",
        response_class=HTMLResponse,
    )
    def thread_messages(request: Request, conversation_id: str) -> Response:
        """One page of a thread's messages alone, for the page's script."""

        def render(reader: TimelineReader) -> Response:
            found = thread_page(reader, conversation_id, _position(request))
            if found is None or found["missing_message"]:
                message = UNKNOWN_MESSAGE if found is not None else UNKNOWN_CONVERSATION
                return error(request, message, 404)
            return _no_store(page(request, "_messages.html", **found), 200)

        return guarded(request, render)

    @router.get(
        "/timeline/conversations/{conversation_id}/photos",
        response_class=HTMLResponse,
    )
    def photos(request: Request, conversation_id: str) -> Response:
        """A conversation's pictures, newest first, `before` a cursor (the
        "More" link without JavaScript). htmx asks for the next tiles alone."""

        def render(reader: TimelineReader) -> Response:
            before = _photo_cursor(request)
            # Not an id: unknown, before any query.
            if not valid_conversation_id(conversation_id):
                return error(request, UNKNOWN_CONVERSATION, 404)
            fragment = request.headers.get("hx-request") == "true"
            conversation = None if fragment else reader.conversation(conversation_id)
            found = None
            if fragment or conversation is not None:
                found = reader.conversation_photos(conversation_id, PHOTO_PAGE, before)
            if found is None:
                return error(request, UNKNOWN_CONVERSATION, 404)
            context = {
                "photos": photo_views(found["photos"], conversation_id),
                "before": found["before"],
                # Not `more`: the list beside it has its own.
                "more_photos": photos_url(conversation_id, found["before"])
                if found["before"]
                else None,
                "later_page": before is not None,
            }
            if fragment:
                return _no_store(page(request, "_photos.html", **context), 200)
            assert conversation is not None
            context |= {
                "source": reader.source.describe(),
                **listing(reader, None, conversation_id),
                "conversation": conversation,
                "conversation_id": conversation_id,
                "conversation_name": conversation["display_name"],
                "thread": thread_url(conversation_id),
                "fragment": photos_url(conversation_id),
            }
            return _no_store(page(request, "conversation_photos.html", **context), 200)

        return guarded(request, render)

    return router


def _no_store(response: Response, status: int) -> Response:
    response.status_code = status
    response.headers["Cache-Control"] = NO_STORE
    return response
