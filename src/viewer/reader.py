"""Read access to the warehouse for the timeline (decision 040).

The server reads the warehouse only here, and only to read, through the
read-only, time-limited sessions of `src.viewer.database`. Which analysis it
reads, and its schema, come from the viewer's settings (`src.viewer.settings`),
built by the composition root: nothing here reads the configuration.
"""

import base64
import json
import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from src.analyses.views import SCRUBBER, THREAD, Lane
from src.kernel.errors import OperationError, Timeout, Unavailable
from src.viewer.database import (
    NOT_CONFIGURED,
    STATEMENT_TIMEOUT_MS,
    UNAVAILABLE,
    TimelineDatabase,
)
from src.viewer.settings import TimelineAnalysis, ViewerConfiguration

log = logging.getLogger(__name__)


def status(
    database: TimelineDatabase | None,
    configuration: ViewerConfiguration | None = None,
) -> dict[str, Any]:
    """What the timeline routes report: not configured, unreachable, or ready.

    With `configuration`, one without a timeline analysis is not configured
    either, and the analysis shown is named.
    """
    if database is None:
        return {"configured": False, "ready": False, "message": NOT_CONFIGURED}
    source = timeline_source(configuration) if configuration is not None else None
    if configuration is not None and source is None:
        return {"configured": False, "ready": False, "message": NO_ANALYSIS}
    described = source.describe() if source else {}
    try:
        checked = database.health()
    except OperationError as error:
        return {
            "configured": True,
            "ready": False,
            "message": str(error),
            **described,
        }
    ready = checked["read_only"]
    return {
        **described,
        "configured": True,
        "ready": ready,
        "message": "The timeline database is reachable and read-only."
        if ready
        else "The timeline database session is not read-only.",
        **checked,
    }


# ---- Which tables -------------------------------------------------------------

# The majors of the tables' version (the package's `contract`) this viewer
# reads; a minor change only adds, so any minor of these is read.
CONTRACT_MAJORS = (1,)
# The version of tables built before the package wrote `contract`: the same
# tables as 1.0 (tests/test_timeline_identity.py).
UNRECORDED_VERSION = "1.0"
NO_ANALYSIS = (
    "The timeline is not configured: no analysis of the timeline package is set up."
)
NOT_BUILT = "The timeline has not been built yet, or its reader role cannot read it."
OTHER_VERSION = (
    "The timeline was built by another package version than this viewer reads;"
    " deploy the matching viewer, or rebuild the timeline."
)
NO_MESSAGES = (
    "The timeline reads no Apple Messages: its analysis has no messages input."
)
TIMED_OUT = (
    f"The timeline query took longer than {STATEMENT_TIMEOUT_MS // 1000} seconds; "
    "narrow it (a shorter range, more words) and try again."
)
SCHEMA_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")


class TimelineUnavailable(Unavailable):
    """The timeline cannot answer now: not configured, not built, unreachable."""


class TimelineTimeout(Timeout):
    """A query reached the session's time limit."""


@dataclass(frozen=True)
class TimelineSource:
    """The analysis whose tables the server reads, and how to read them."""

    analysis_id: str
    schema: str
    timezone: str
    # Further timeline analyses, not shown: only the first one is.
    ignored: tuple[str, ...] = ()
    # Linked input role -> connection id.
    inputs: Mapping[str, str] = field(default_factory=dict)
    # Its package's lanes (src.analyses.views), linked or not, in order.
    lanes: tuple[Lane, ...] = ()

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def shown(self) -> tuple[Lane, ...]:
        """The lanes of days, events and search: those whose role is linked,
        and those shown always."""
        return tuple(
            lane for lane in self.lanes if lane.always or lane.role in self.inputs
        )

    @property
    def sources(self) -> tuple[str, ...]:
        """The sources of days and events, a lane's where it is shown."""
        return tuple(lane.source for lane in self.shown)

    @property
    def default_sources(self) -> tuple[str, ...]:
        """What is read where no source is asked for: the lanes shown always."""
        return tuple(lane.source for lane in self.lanes if lane.always)

    def lane(self, source: str) -> Lane | None:
        return next((lane for lane in self.lanes if lane.source == source), None)

    def plugin(self, name: str) -> Lane | None:
        """The shown lane whose plugin is `name`, if any."""
        return next((lane for lane in self.shown if lane.plugin == name), None)

    @property
    def thread(self) -> Lane | None:
        """The lane of the conversations, where its role is linked."""
        return self.plugin(THREAD)

    def media_kind(self, plugin: str) -> str:
        """The kind of the files a plugin's rows point at, as its lane declares."""
        lane = next((lane for lane in self.lanes if lane.plugin == plugin), None)
        if lane is None or lane.media_kind is None:
            raise TimelineUnavailable(f"No lane of the timeline has the {plugin}")
        return lane.media_kind

    def table(self, name: str) -> str:
        return f'"{self.schema}".{name}'

    def describe(self) -> dict[str, Any]:
        described: dict[str, Any] = {
            "analysis": self.analysis_id,
            "timezone": self.timezone,
        }
        if self.ignored:
            described["warning"] = (
                f"{len(self.ignored) + 1} timeline analyses are configured; "
                f"only the first, {self.analysis_id}, is shown."
            )
        return described


def timeline_source(configuration: ViewerConfiguration) -> TimelineSource | None:
    """The first configured timeline analysis, in configuration order, or None.

    Its schema is the one the build derives (the composition root names it from
    the build's own plan), so no setting names it.
    """
    found = configuration.analyses
    if not found:
        return None
    return _source(found[0], tuple(a.id for a in found[1:]))


def _source(analysis: TimelineAnalysis, ignored: tuple[str, ...]) -> TimelineSource:
    if not SCHEMA_NAME.fullmatch(analysis.schema):  # from validated IDs; kept honest
        raise TimelineUnavailable("The timeline analysis has an unexpected schema")
    return TimelineSource(
        analysis.id,
        analysis.schema,
        analysis.timezone,
        ignored,
        dict(analysis.inputs),
        analysis.lanes,
    )


# ---- Reading --------------------------------------------------------------------

# Bounds on what one answer holds.
MAX_DAYS_RANGE = 400
MAX_DAY_FRAMES = 20_000
MAX_TEXT = 100_000
MAX_EVENTS = 500
MAX_EVENT_TEXT = 2_000
MAX_SEARCH = 50
MAX_QUERY = 200
MOMENT_SECONDS = 300
# Private-use characters mark a snippet's matches; they are removed from the
# text first, so none in it can be taken for a mark.
_START, _STOP = "", ""
_HEADLINE = (
    f"StartSel={_START}, StopSel={_STOP}, MaxWords=30, MinWords=12, "
    'MaxFragments=2, FragmentDelimiter=" … "'
)
# A frame's screenshot, as the screen index codes it.
MEDIA_UNKNOWN, MEDIA_THUMBNAILS, MEDIA_NO_THUMBNAILS, MEDIA_WITHHELD = 0, 1, 2, 3
_MEDIA_STATES = {
    MEDIA_UNKNOWN: "unknown",
    MEDIA_THUMBNAILS: "thumbnails",
    MEDIA_NO_THUMBNAILS: "stored",
    MEDIA_WITHHELD: "withheld",
}
MAX_CONVERSATIONS = 200
MAX_MESSAGES = 200
MAX_PHOTOS = 200
PREVIEW_CHARACTERS = 200
# A conversation's opaque id (timeline_conversation_id): checked before any query.
_CONVERSATION_ID = re.compile(r"[0-9a-f]{32}")
MAX_GUID = 255
# Text search covers the first characters of a text, as the models index it
# (timeline_search_vector in dbt/macros/timeline.sql, the same number).
SEARCH_CHARACTERS = 100_000


def search_vector(column: str) -> str:
    """The indexed search expression over a text column; queries must use it
    as it is for the index to answer."""
    return f"to_tsvector('simple', left({column}, {SEARCH_CHARACTERS}))"


def media_code(
    withheld: str | None, status: str | None, w360: bool | None, w1280: bool | None
) -> int:
    """What is known of a frame's screenshot. No record is unknown, not absent: the
    file may still be queued or not derived yet (decision 040, phase 1b)."""
    if withheld:
        return MEDIA_WITHHELD  # never uploaded, whatever a record says
    if status is None:
        return MEDIA_UNKNOWN
    return MEDIA_THUMBNAILS if w360 and w1280 else MEDIA_NO_THUMBNAILS


def snippet(marked: str | None) -> list[str]:
    """A headline as alternating plain and matched parts, plain first, so a page
    can show it as text without parsing markup."""
    parts = [""]
    for char in marked or "":
        # An odd count of parts means the last one is plain.
        if (char == _START and len(parts) % 2) or (
            char == _STOP and not len(parts) % 2
        ):
            parts.append("")
        elif char not in (_START, _STOP):
            parts[-1] += char
    while len(parts) > 1 and parts[-1] == "":
        parts.pop()
    return parts


def _iso(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).isoformat()


def _app(row: Any) -> dict[str, str]:
    return {"bundle_id": row[0], "name": row[1], "colour": row[2]}


class _Apps:
    """A per-answer list of apps, referred to by index."""

    def __init__(self) -> None:
        self.index: dict[str, int] = {}

    def __call__(self, bundle_id: str | None) -> int | None:
        if bundle_id is None:
            return None
        return self.index.setdefault(bundle_id, len(self.index))

    def described(self, reader: "TimelineReader", db: Any) -> list[dict[str, str]]:
        if not self.index:
            return []
        rows = db.execute(
            sa.text(
                f"SELECT bundle_id, name, colour FROM {reader.source.table('apps')}"
                " WHERE bundle_id = ANY(:ids)"
            ),
            {"ids": list(self.index)},
        ).all()
        known = {row[0]: _app(row) for row in rows}
        # An app the last build has not named yet keeps its identifier.
        return [
            known.get(
                bundle, {"bundle_id": bundle, "name": bundle, "colour": "#9e9e9e"}
            )
            for bundle in self.index
        ]


def _cursor(values: list[Any]) -> str:
    raw = json.dumps(values, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_cursor(value: str, shape: tuple[type, ...]) -> list[Any]:
    """An opaque paging cursor back into its values; OperationError if it is not one
    this server wrote."""
    try:
        if len(value) > 1024:
            raise ValueError
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        values = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise OperationError("Invalid paging cursor") from None
    if (
        not isinstance(values, list)
        or len(values) != len(shape)
        or not all(
            isinstance(v, kind) and not isinstance(v, bool)
            for v, kind in zip(values, shape, strict=True)
        )
    ):
        raise OperationError("Invalid paging cursor")
    if any(isinstance(v, str) and "\x00" in v for v in values):
        raise OperationError("Invalid paging cursor")
    return values


def _day_bounds_sql(day: str) -> tuple[str, str]:
    """A local day's start and end in the analysis's timezone. Where clocks go back
    at midnight, Postgres reads the repeated midnight as the later one, so the
    hour before it belongs to the previous day. Selecting by these bounds keeps
    the time index usable; `_local_day_sql` and the models (timeline_local_day)
    assign a time to a day by the same rule."""
    return (
        f"(CAST(:{day} AS date)::timestamp AT TIME ZONE :tz)",
        f"((CAST(:{day} AS date) + 1)::timestamp AT TIME ZONE :tz)",
    )


def _local_day_sql(column: str) -> str:
    """The local day whose bounds (`_day_bounds_sql`) hold a time."""
    date_at = f"CAST({column} AT TIME ZONE :tz AS date)"
    return (
        f"(CASE WHEN {column} < ({date_at}::timestamp AT TIME ZONE :tz)"
        f" THEN {date_at} - 1 ELSE {date_at} END)"
    )


@contextmanager
def session(database: TimelineDatabase, lookup: bool = False) -> Iterator[Any]:
    """One read-only, time-limited session; `lookup` takes the connection kept
    for single-row lookups, so they never wait behind searches. Errors become
    TimelineTimeout or TimelineUnavailable."""
    engine = database.lookup_engine if lookup else database.engine
    try:
        with engine.connect() as db:
            yield db
    except DBAPIError as error:
        code = getattr(error.orig, "pgcode", None)
        if code == "57014":  # query_canceled: the statement time limit
            raise TimelineTimeout(TIMED_OUT) from None
        log.warning("Timeline query failed: %s %s", type(error).__name__, code)
        if code in ("42P01", "3F000", "42501", "42703"):
            # No table, schema or column yet, or no privilege on them.
            raise TimelineUnavailable(NOT_BUILT) from None
        raise TimelineUnavailable(UNAVAILABLE) from None
    except SQLAlchemyError as error:
        log.warning("Timeline query failed: %s", type(error).__name__)
        raise TimelineUnavailable(UNAVAILABLE) from None


class TimelineReader:
    """The timeline's queries, each through one read-only, time-limited session.

    The first session of a reader checks the tables' version (`contract`):
    tables of a major this viewer does not read answer 503. Tables built before
    `contract` existed (until the first build after the deploy that added it)
    are read as version 1.0: they are the same tables, row for row.
    """

    def __init__(self, database: TimelineDatabase, source: TimelineSource) -> None:
        self.database = database
        self.source = source
        self.checked = False

    def _session(self, lookup: bool = False) -> AbstractContextManager[Any]:
        return self._checked_session(lookup)

    @contextmanager
    def _checked_session(self, lookup: bool) -> Iterator[Any]:
        with session(self.database, lookup) as db:
            if not self.checked:
                contract = self.source.table("contract")
                built = db.execute(
                    sa.text("SELECT to_regclass(:contract) IS NOT NULL"),
                    {"contract": contract},
                ).scalar_one()
                if built:
                    found = db.execute(sa.text(f"SELECT version FROM {contract}")).all()
                    versions = [row[0] for row in found]
                else:
                    versions = [UNRECORDED_VERSION]
                if len(versions) != 1 or _major(versions[0]) not in CONTRACT_MAJORS:
                    raise TimelineUnavailable(OTHER_VERSION)
                self.checked = True
            yield db

    def days(
        self, start: date, end: date, sources: tuple[str, ...] | None = None
    ) -> dict[str, Any]:
        """Counts per local day and source, from `start` to `end` inclusive. The
        API and the Day view ask for every source of the analysis
        (`TimelineSource.sources`); by default, the lanes shown always, so the
        home page never opens a day of another lane alone."""
        if sources is None:
            sources = self.source.default_sources
        if end < start or (end - start).days >= MAX_DAYS_RANGE:
            raise OperationError(
                f"The range must run forward and span at most {MAX_DAYS_RANGE} days"
            )
        with self._session() as db:
            rows = db.execute(
                sa.text(
                    f"SELECT day, source, events, frames FROM {self.source.table('days')}"
                    " WHERE day >= :start AND day <= :end AND timezone = :tz"
                    " AND source = ANY(:sources) ORDER BY day, source"
                ),
                # Only the sources asked for: the package counts messages even
                # where no page shows them.
                {
                    "start": start,
                    "end": end,
                    "tz": self.source.timezone,
                    "sources": list(sources),
                },
            ).all()
        days: dict[str, dict[str, Any]] = {}
        for day, source, events, frames in rows:
            counts: dict[str, int] = {"events": int(events)}
            if frames is not None:
                counts["frames"] = int(frames)
            days.setdefault(day.isoformat(), {})[source] = counts
        return {
            **self.source.describe(),
            "from": start.isoformat(),
            "to": end.isoformat(),
            "days": [{"day": day, **sources} for day, sources in days.items()],
        }

    def screen_day(self, day: date) -> dict[str, Any]:
        """One local day's frames in time order, as columns for a small answer."""
        lo, hi = _day_bounds_sql("day")
        frames = self.source.table("screen_frames")
        apps = _Apps()
        with self._session() as db:
            bounds = db.execute(
                sa.text(f"SELECT {lo}, {hi}"),
                {"day": day, "tz": self.source.timezone},
            ).one()
            rows = db.execute(
                sa.text(
                    "SELECT CAST(EXTRACT(EPOCH FROM captured_at - :lo) AS integer),"
                    " bundle_id, image_path, withheld, media_status, thumbnail_w360,"
                    f" thumbnail_w1280, connection FROM {frames}"
                    " WHERE captured_at >= :lo AND captured_at < :hi"
                    " ORDER BY captured_at, image_path LIMIT :limit"
                ),
                {"lo": bounds[0], "hi": bounds[1], "limit": MAX_DAY_FRAMES + 1},
            ).all()
            truncated = len(rows) > MAX_DAY_FRAMES
            rows = rows[:MAX_DAY_FRAMES]
            columns: dict[str, list[Any]] = {"t": [], "app": [], "key": [], "media": []}
            for t, bundle, key, withheld, status, w360, w1280, _ in rows:
                columns["t"].append(t)
                columns["app"].append(apps(bundle))
                columns["key"].append(key)
                columns["media"].append(media_code(withheld, status, w360, w1280))
            described = apps.described(self, db)
        zone = self.source.zone
        return {
            **self.source.describe(),
            "day": day.isoformat(),
            "start": _iso(bounds[0], zone),
            "end": _iso(bounds[1], zone),
            "connection": rows[0][7] if rows else None,
            "media_kind": self.source.media_kind(SCRUBBER),
            "count": len(rows),
            "truncated": truncated,
            "apps": described,
            "frames": columns,
        }

    def frame(self, key: str) -> dict[str, Any] | None:
        """One frame: time, app, text, what was masked or withheld, its media."""
        tz = self.source.timezone
        with self._session() as db:
            row = db.execute(
                sa.text(
                    "SELECT f.image_path, f.captured_at, f.bundle_id, a.name, a.colour,"
                    " left(f.text, :max_text), length(f.text) > :max_text, f.withheld,"
                    " f.masked, f.connection, f.media_status, f.thumbnail_w360,"
                    " f.thumbnail_w1280,"
                    f" {_local_day_sql('f.captured_at')}"
                    f" FROM {self.source.table('screen_frames')} f"
                    f" LEFT JOIN {self.source.table('apps')} a"
                    " ON a.bundle_id = f.bundle_id"
                    " WHERE f.image_path = :key"
                ),
                {"key": key, "tz": tz, "max_text": MAX_TEXT},
            ).one_or_none()
            if row is None:
                return None
            lo, _ = _day_bounds_sql("day")
            start = db.execute(sa.text(f"SELECT {lo}"), {"day": row[13], "tz": tz})
            day_start = start.scalar_one()
        (key, captured, bundle, name, colour, text, cut, withheld, masked) = row[:9]
        connection, status, w360, w1280, day = row[9:]
        code = media_code(withheld, status, w360, w1280)
        app = None
        if bundle is not None:
            app = {"bundle_id": bundle, "name": name or bundle, "colour": colour}
        return {
            **self.source.describe(),
            "key": key,
            "captured_at": _iso(captured, self.source.zone),
            "day": day.isoformat(),
            "t": int((captured - day_start).total_seconds()),
            "app": app,
            "text": text,
            "text_truncated": bool(cut),
            "withheld": withheld,
            "masked": masked,
            "media": _media(
                connection, self.source.media_kind(SCRUBBER), key, code, status
            ),
        }

    def _instant(self, db: Any, value: datetime | date, end: bool) -> datetime:
        """A time as given, or a local day's start (its end, for an end)."""
        if isinstance(value, datetime):
            return value
        bound = _day_bounds_sql("day")[1 if end else 0]
        found = db.execute(
            sa.text(f"SELECT {bound}"), {"day": value, "tz": self.source.timezone}
        )
        return cast(datetime, found.scalar_one())

    def events(
        self,
        start: datetime | date,
        end: datetime | date,
        sources: tuple[str, ...],
        limit: int,
        after: tuple[datetime, str] | None,
    ) -> dict[str, Any]:
        """Events that started in [start, end), oldest first, paged by keyset on
        (started_at, event_id). One index range per source, merged. A day as
        `end` includes that day."""
        if not 1 <= limit <= MAX_EVENTS:
            raise OperationError(f"The limit must be between 1 and {MAX_EVENTS}")
        if not sources or set(sources) - set(self.source.sources):
            raise OperationError(f"Sources are {', '.join(self.source.sources)}")
        lanes = [self.source.lane(source) for source in sources]
        events = self.source.table("events")
        parameters: dict[str, Any] = {"limit": limit + 1, "max_text": MAX_EVENT_TEXT}
        keyset = ""
        if after is not None:
            parameters.update(after_at=after[0], after_id=after[1])
            keyset = (
                " AND started_at >= :after_at"
                " AND (started_at, event_id) > (:after_at, :after_id)"
            )
        parts = []
        for number, source in enumerate(sources):
            parameters[f"source{number}"] = source
            part = (
                "SELECT source, event_id, started_at, ended_at, kind,"
                " left(title, :max_text) AS title, left(body, :max_text) AS body,"
                " group_key, payload, media_kind, media_key, media_status,"
                " thumbnail_w360, thumbnail_w1280, media_withheld, connection"
                f" FROM {events} WHERE source = :source{number}"
                f" AND started_at >= :lo AND started_at < :hi{keyset}"
                " ORDER BY started_at, event_id LIMIT :limit"
            )
            # The thread names its senders and conversations as they are now.
            lane = lanes[number]
            thread = lane is not None and lane.plugin == THREAD
            parts.append(f"({self._named_messages(part) if thread else part})")
        sql = (
            "SELECT * FROM (" + " UNION ALL ".join(parts) + ") merged"
            " ORDER BY started_at, event_id LIMIT :limit"
        )
        with self._session() as db:
            parameters["lo"] = self._instant(db, start, end=False)
            parameters["hi"] = self._instant(db, end, end=True)
            if parameters["hi"] <= parameters["lo"]:
                raise OperationError("The end must come after the start")
            rows = db.execute(sa.text(sql), parameters).all()
        more = len(rows) > limit
        rows = rows[:limit]
        zone = self.source.zone
        answer = []
        for row in rows:
            (source, event_id, started, ended, kind, title, body, group, payload) = row[
                :9
            ]
            media_kind, media_key, status, w360, w1280, withheld, connection = row[9:]
            event: dict[str, Any] = {
                "source": source,
                "event_id": event_id,
                "started_at": _iso(started, zone),
                "ended_at": _iso(ended, zone) if ended is not None else None,
                "kind": kind,
                "title": title,
                "body": body,
                "group": group,
                "payload": payload,
                "media": None,
            }
            if media_key is not None:
                event["media"] = _media(
                    connection,
                    media_kind,
                    media_key,
                    media_code("withheld" if withheld else None, status, w360, w1280),
                    status,
                )
            answer.append(event)
        last = rows[-1] if rows else None
        return {
            **self.source.describe(),
            "events": answer,
            "next": _cursor([last[2].isoformat(), last[1]]) if more and last else None,
        }

    def _named_messages(self, page: str) -> str:
        """A page of message events with names read now: the conversation's
        `display_name` and the sender's, never the handle the stored payload
        holds. Names change; the events table does not hold them (phase 2a)."""
        return (
            "SELECT e.source, e.event_id, e.started_at, e.ended_at, e.kind, e.title,"
            " e.body, e.group_key, (e.payload - 'sender') || jsonb_build_object("
            f"'display_name', c.display_name, 'sender', {_SENDER}) AS payload,"
            " e.media_kind, e.media_key, e.media_status, e.thumbnail_w360,"
            " e.thumbnail_w1280, e.media_withheld, e.connection"
            f" FROM ({page}) e"
            f" LEFT JOIN {self.source.table('messages')} m"
            " ON m.guid = e.payload->>'guid'"
            f" LEFT JOIN {self.source.table('conversations')} c"
            " ON c.conversation_id = e.group_key"
            f" LEFT JOIN {self.source.table('people')} p"
            " ON p.handle = m.sender_handle"
            " ORDER BY e.started_at, e.event_id"
        )

    def search_screen(
        self,
        query: str,
        app: str | None,
        start: date | None,
        end: date | None,
        limit: int,
        before: tuple[int, str] | None,
    ) -> dict[str, Any]:
        """Screen-text matches grouped into moments (one app, one five-minute
        window), newest first, with a snippet for the moments returned only."""
        words = _query(query, limit)
        frames = self.source.table("screen_frames")
        parameters: dict[str, Any] = {
            "q": words,
            "tz": self.source.timezone,
            "limit": limit + 1,
            "width": MOMENT_SECONDS,
            "headline": _HEADLINE,
            "marks": _START + _STOP,
        }
        conditions = [f"{search_vector('f.text')} @@ q.query"]
        conditions += _range_conditions("f.captured_at", start, end, parameters)
        if app is not None:
            conditions.append("f.bundle_id = :app")
            parameters["app"] = app
        if before is not None:
            # The window's end bounds the time index too.
            conditions.append(
                "f.captured_at < to_timestamp((:before_bucket + 1) * :width)"
                " AND (floor(EXTRACT(EPOCH FROM f.captured_at) / :width) < :before_bucket"
                " OR (floor(EXTRACT(EPOCH FROM f.captured_at) / :width) = :before_bucket"
                " AND coalesce(f.bundle_id, '') > :before_app))"
            )
            parameters.update(before_bucket=before[0], before_app=before[1])
        sql = (
            "WITH q AS (SELECT websearch_to_tsquery('simple', :q) AS query),"
            " moments AS ("
            " SELECT CAST(floor(EXTRACT(EPOCH FROM f.captured_at) / :width) AS bigint)"
            " AS bucket, f.bundle_id, count(*) AS matches,"
            " min(f.captured_at) AS first_at, max(f.captured_at) AS last_at,"
            " (array_agg(f.image_path ORDER BY f.captured_at, f.image_path))[1] AS key"
            f" FROM {frames} f, q WHERE {' AND '.join(conditions)}"
            " GROUP BY 1, 2 ORDER BY 1 DESC, coalesce(f.bundle_id, '') LIMIT :limit)"
            " SELECT m.bucket, m.bundle_id, m.matches, m.first_at, m.last_at, m.key,"
            " ts_headline('simple',"
            f" translate(left(f.text, {SEARCH_CHARACTERS}), :marks, ''), q.query, :headline),"
            " f.withheld, f.media_status, f.thumbnail_w360, f.thumbnail_w1280,"
            f" f.connection, {_local_day_sql('m.first_at')}"
            f" FROM moments m JOIN {frames} f ON f.image_path = m.key, q"
            " ORDER BY m.bucket DESC, coalesce(m.bundle_id, '')"
        )
        apps = _Apps()
        zone = self.source.zone
        with self._session() as db:
            _searchable(db, words)
            rows = db.execute(sa.text(sql), parameters).all()
            more = len(rows) > limit
            rows = rows[:limit]
            moments = []
            for row in rows:
                _, bundle, matches, first_at, last_at, key, marked = row[:7]
                withheld, status, w360, w1280, connection, day = row[7:]
                moments.append(
                    {
                        "key": key,
                        "connection": connection,
                        "at": _iso(first_at, zone),
                        "until": _iso(last_at, zone),
                        "day": day.isoformat(),
                        "app": apps(bundle),
                        "matches": int(matches),
                        "snippet": snippet(marked),
                        "media": media_code(withheld, status, w360, w1280),
                    }
                )
            described = apps.described(self, db)
        last = rows[-1] if rows else None
        return {
            **self.source.describe(),
            "kind": self._plugin_source(SCRUBBER),
            "media_kind": self.source.media_kind(SCRUBBER),
            "moment_seconds": MOMENT_SECONDS,
            "apps": described,
            "moments": moments,
            "next": _cursor([int(last[0]), last[1] or ""]) if more and last else None,
        }

    def _plugin_source(self, plugin: str) -> str:
        lane = next((lane for lane in self.source.lanes if lane.plugin == plugin), None)
        if lane is None:
            raise TimelineUnavailable(f"No lane of the timeline has the {plugin}")
        return lane.source

    def search_events(
        self,
        source: str,
        query: str,
        start: date | None,
        end: date | None,
        limit: int,
        before: tuple[datetime, str] | None,
    ) -> dict[str, Any]:
        """A lane's events whose title or body matches (`events.search`), newest
        first: the search of every lane without a plugin. A lane whose model
        writes an empty search vector finds nothing. The answer keeps the
        browser's words, its first such lane: `visits`, each with its `url` (the
        body) and `site` (the group)."""
        words = _query(query, limit)
        parameters: dict[str, Any] = {
            "q": words,
            "tz": self.source.timezone,
            "limit": limit + 1,
            "headline": _HEADLINE,
            "marks": _START + _STOP,
            "max_text": MAX_EVENT_TEXT,
            "source": source,
        }
        conditions = ["e.source = :source", "e.search @@ q.query"]
        conditions += _range_conditions("e.started_at", start, end, parameters)
        if before is not None:
            conditions.append(
                "e.started_at <= :before_at AND (e.started_at, e.event_id)"
                " < (:before_at, :before_id)"
            )
            parameters.update(before_at=before[0], before_id=before[1])
        sql = (
            "WITH q AS (SELECT websearch_to_tsquery('simple', :q) AS query),"
            " hits AS (SELECT e.event_id, e.started_at, e.title, e.body, e.group_key"
            f" FROM {self.source.table('events')} e, q"
            f" WHERE {' AND '.join(conditions)}"
            " ORDER BY e.started_at DESC, e.event_id DESC LIMIT :limit)"
            " SELECT h.event_id, h.started_at, left(h.title, :max_text),"
            " left(h.body, :max_text), h.group_key,"
            " ts_headline('simple', translate(coalesce(h.title, ''), :marks, ''),"
            " q.query, :headline)"
            " FROM hits h, q ORDER BY h.started_at DESC, h.event_id DESC"
        )
        with self._session() as db:
            _searchable(db, words)
            rows = db.execute(sa.text(sql), parameters).all()
        more = len(rows) > limit
        rows = rows[:limit]
        zone = self.source.zone
        visits = [
            {
                "event_id": event_id,
                "at": _iso(started, zone),
                "title": title,
                "url": body,
                "site": site,
                "snippet": snippet(marked),
            }
            for event_id, started, title, body, site, marked in rows
        ]
        last = rows[-1] if rows else None
        return {
            **self.source.describe(),
            "kind": source,
            "visits": visits,
            "next": _cursor([last[1].isoformat(), last[0]]) if more and last else None,
        }

    # ---- Messages (decision 040, phase 2c) -----------------------------------

    def _messages_tables(self) -> tuple[str, str, str]:
        if self.source.thread is None:
            raise TimelineUnavailable(NO_MESSAGES)
        return (
            self.source.table("conversations"),
            self.source.table("messages"),
            self.source.table("people"),
        )

    def _preview_sql(self, messages: str) -> str:
        """The last dated message of conversation `c`: one probe of the
        (conversation_id, sent_at, guid) index, backwards."""
        return (
            "LEFT JOIN LATERAL (SELECT nullif(left(m.text, :preview), '') AS text,"
            f" m.is_from_me FROM {messages} m"
            " WHERE m.conversation_id = c.conversation_id AND m.sent_at IS NOT NULL"
            " AND NOT m.deleted ORDER BY m.sent_at DESC, m.guid DESC LIMIT 1) l ON true"
        )

    def conversations(
        self, limit: int, after: tuple[datetime | None, str] | None
    ) -> dict[str, Any]:
        """Conversations with at least one message, newest activity first (those
        whose messages are all undated last), paged by keyset on
        (last_message_at, conversation_id)."""
        if not 1 <= limit <= MAX_CONVERSATIONS:
            raise OperationError(f"The limit must be between 1 and {MAX_CONVERSATIONS}")
        conversations, messages, _ = self._messages_tables()
        parameters: dict[str, Any] = {"limit": limit + 1, "preview": PREVIEW_CHARACTERS}
        keyset = ""
        if after is not None:
            parameters["after_id"] = after[1]
            if after[0] is None:
                keyset = " AND last_message_at IS NULL AND conversation_id > :after_id"
            else:
                parameters["after_at"] = after[0]
                keyset = (
                    " AND (last_message_at < :after_at OR last_message_at IS NULL"
                    " OR (last_message_at = :after_at AND conversation_id > :after_id))"
                )
        order = "last_message_at DESC NULLS LAST, conversation_id"
        sql = (
            "SELECT c.conversation_id, c.display_name, c.is_group,"
            " c.participant_count, c.last_message_at, c.message_count, l.text,"
            " l.is_from_me FROM (SELECT * FROM"
            f" {conversations} WHERE message_count > 0{keyset}"
            f" ORDER BY {order} LIMIT :limit) c {self._preview_sql(messages)}"
            f" ORDER BY c.{order.replace(', ', ', c.')}"
        )
        with self._session() as db:
            rows = db.execute(sa.text(sql), parameters).all()
        more = len(rows) > limit
        rows = rows[:limit]
        zone = self.source.zone
        listed = [_conversation(row[:8], zone) for row in rows]
        last = rows[-1] if rows else None
        return {
            **self.source.describe(),
            "conversations": listed,
            "next": _cursor([last[4].isoformat() if last[4] else "", last[0]])
            if more and last
            else None,
        }

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        """One conversation, its people and how many of its messages are undated;
        None if unknown."""
        conversations, messages, people = self._messages_tables()
        sql = (
            "SELECT c.conversation_id, c.display_name, c.is_group,"
            " c.participant_count, c.last_message_at, c.message_count, l.text,"
            " l.is_from_me, c.title, c.first_message_at,"
            " (SELECT count(*) FROM"
            f" {messages} u WHERE u.conversation_id = c.conversation_id"
            " AND u.sent_at IS NULL AND NOT u.deleted),"
            " (SELECT coalesce(jsonb_agg(jsonb_build_object('handle', h.handle,"
            " 'display_name', coalesce(n.display_name, h.handle))"
            " ORDER BY coalesce(n.display_name, h.handle), h.handle), '[]')"
            f" FROM unnest(c.participants) h (handle) LEFT JOIN {people} n"
            " ON n.handle = h.handle)"
            f" FROM {conversations} c {self._preview_sql(messages)}"
            " WHERE c.conversation_id = :id"
        )
        with self._session() as db:
            row = db.execute(
                sa.text(sql), {"id": conversation_id, "preview": PREVIEW_CHARACTERS}
            ).one_or_none()
        if row is None:
            return None
        zone = self.source.zone
        title, first, undated, participants = row[8:]
        return {
            **self.source.describe(),
            **_conversation(row[:8], zone),
            "title": title,
            "first_message_at": _iso(first, zone) if first else None,
            "participants": participants,
            "undated": int(undated),
        }

    def conversation_messages(
        self,
        conversation_id: str,
        limit: int,
        *,
        before: tuple[datetime, str] | None = None,
        after: tuple[datetime, str] | None = None,
        around: str | None = None,
        at: datetime | date | None = None,
    ) -> dict[str, Any] | None:
        """A page of a conversation's dated messages, oldest first, by keyset on
        (sent_at, guid): the latest page, the page before or after a cursor, the
        page with a message in its middle, or the page starting at a time. None
        if the conversation (or `around`'s message) is unknown. `before` and
        `after` are the neighbouring pages' cursors, null at either end."""
        if not 1 <= limit <= MAX_MESSAGES:
            raise OperationError(f"The limit must be between 1 and {MAX_MESSAGES}")
        if sum(value is not None for value in (before, after, around, at)) > 1:
            raise OperationError("Give at most one of before, after, around and at")
        conversations, messages, people = self._messages_tables()
        page = _MessagePage(messages, people, conversation_id)
        with self._session() as db:
            known = db.execute(
                sa.text(f"SELECT 1 FROM {conversations} WHERE conversation_id = :id"),
                {"id": conversation_id},
            ).first()
            if known is None:
                return None
            # Where an empty page sits, for its neighbours' cursors.
            empty_before: tuple[datetime, str] | None = None
            empty_after: tuple[datetime, str] | None = None
            if around is not None:
                target = db.execute(
                    sa.text(
                        f"SELECT sent_at FROM {messages} WHERE guid = :guid"
                        " AND conversation_id = :id AND sent_at IS NOT NULL"
                        " AND NOT deleted"
                    ),
                    {"guid": around, "id": conversation_id},
                ).first()
                if target is None:
                    return None
                key = (target[0], around)
                older = page.rows(db, "<", key, limit - 1)
                newer = page.rows(db, ">=", key, limit)
                ahead = min(len(older), (limit - 1) // 2)
                behind = min(len(newer), limit - ahead)
                ahead = min(len(older), limit - behind)
                rows = older[:ahead][::-1] + newer[:behind]
            elif at is not None:
                start = self._instant(db, at, end=False)
                rows = page.rows(db, ">=", (start, ""), limit)
                empty_before = (start, "")
            elif after is not None:
                rows = page.rows(db, ">", after, limit)
                # Nothing after it: the page before holds everything up to it.
                empty_before = (after[0] + timedelta(microseconds=1), "")
            else:
                rows = page.rows(db, "<", before, limit)[::-1]
                if before is not None:
                    # Nothing before it: the page after starts at it.
                    empty_after = (before[0], "")
            if rows:
                first, last = _message_key(rows[0]), _message_key(rows[-1])
                older_key = first if page.rows(db, "<", first, 1) else None
                newer_key = last if page.rows(db, ">", last, 1) else None
            else:
                older_key = (
                    empty_before
                    if empty_before and page.rows(db, "<", empty_before, 1)
                    else None
                )
                newer_key = (
                    empty_after
                    if empty_after and page.rows(db, ">", empty_after, 1)
                    else None
                )
            names = _people_names(db, people, rows)
        zone = self.source.zone
        return {
            **self.source.describe(),
            "conversation_id": conversation_id,
            "messages": [
                _message(row, names, zone, self.source.media_kind(THREAD))
                for row in rows
            ],
            "before": _message_cursor(older_key),
            "after": _message_cursor(newer_key),
        }

    def conversation_photos(
        self,
        conversation_id: str,
        limit: int,
        before: tuple[datetime, str, int] | None = None,
    ) -> dict[str, Any] | None:
        """A page of a conversation's attachments with thumbnails, newest first,
        from the messages its thread shows, by keyset on (sent_at, guid, the
        attachment's position in its message). None if the conversation is
        unknown. `before` is the next page's cursor, null at the end."""
        if not 1 <= limit <= MAX_PHOTOS:
            raise OperationError(f"The limit must be between 1 and {MAX_PHOTOS}")
        conversations, messages, _ = self._messages_tables()
        parameters: dict[str, Any] = {"id": conversation_id, "count": limit + 1}
        keyset = ""
        if before is not None:
            # The plain bound keeps the index range narrow, as a thread's.
            keyset = (
                " AND m.sent_at <= :key_at AND (m.sent_at, m.guid, a.position)"
                " < (:key_at, :key_guid, :key_position)"
            )
            parameters.update(
                key_at=before[0], key_guid=before[1], key_position=before[2]
            )
        sql = (
            "SELECT m.guid, m.sent_at, m.day, a.position, a.item, m.connection"
            f" FROM {messages} m CROSS JOIN LATERAL"
            " jsonb_array_elements(m.attachments) WITH ORDINALITY a (item, position)"
            f" WHERE {_THREAD_MESSAGES} AND {_ATTACHMENT_THUMBNAILS}{keyset}"
            " ORDER BY m.sent_at DESC, m.guid DESC, a.position DESC LIMIT :count"
        )
        with self._session() as db:
            known = db.execute(
                sa.text(f"SELECT 1 FROM {conversations} WHERE conversation_id = :id"),
                {"id": conversation_id},
            ).first()
            if known is None:
                return None
            rows = db.execute(sa.text(sql), parameters).all()
        more = len(rows) > limit
        rows = rows[:limit]
        zone = self.source.zone
        kind = self.source.media_kind(THREAD)
        last = rows[-1] if rows else None
        return {
            **self.source.describe(),
            "conversation_id": conversation_id,
            "photos": [
                {
                    "guid": guid,
                    "sent_at": _iso(sent, zone),
                    "day": day.isoformat() if day else None,
                    "position": int(position),
                    "attachment": _attachment_row(connection, kind, item),
                }
                for guid, sent, day, position, item, connection in rows
            ],
            "before": _cursor([last[1].isoformat(), last[0], int(last[3])])
            if more and last
            else None,
        }

    def search_messages(
        self,
        query: str,
        conversation_id: str | None,
        start: date | None,
        end: date | None,
        limit: int,
        before: tuple[datetime, str] | None,
    ) -> dict[str, Any]:
        """Dated messages whose text matches, newest first. The match is read
        from the text index alone (`OFFSET 0` keeps the other conditions out of
        that scan): with them, Postgres may walk a conversation's messages in
        time order and compute every text's vector instead."""
        words = _query(query, limit)
        conversations, messages, people = self._messages_tables()
        parameters: dict[str, Any] = {
            "q": words,
            "tz": self.source.timezone,
            "limit": limit + 1,
            "headline": _HEADLINE,
            "marks": _START + _STOP,
        }
        conditions = ["NOT m.deleted", "m.sent_at IS NOT NULL"]
        conditions += _range_conditions("m.sent_at", start, end, parameters)
        if conversation_id is not None:
            conditions.append("m.conversation_id = :conversation")
            parameters["conversation"] = conversation_id
        if before is not None:
            conditions.append(
                "m.sent_at <= :before_at AND (m.sent_at, m.guid) < (:before_at, :before_id)"
            )
            parameters.update(before_at=before[0], before_id=before[1])
        sql = (
            "WITH q AS (SELECT websearch_to_tsquery('simple', :q) AS query),"
            " matched AS (SELECT t.guid, t.conversation_id, t.sent_at, t.deleted"
            f" FROM {messages} t, q WHERE {search_vector('t.text')} @@ q.query"
            " OFFSET 0),"
            " hits AS (SELECT m.guid, m.conversation_id, m.sent_at FROM matched m"
            f" WHERE {' AND '.join(conditions)}"
            " ORDER BY m.sent_at DESC, m.guid DESC LIMIT :limit)"
            " SELECT h.guid, h.conversation_id, c.display_name, h.sent_at, m.day,"
            f" m.is_from_me, {_SENDER}, ts_headline('simple',"
            f" translate(left(m.text, {SEARCH_CHARACTERS}), :marks, ''),"
            " q.query, :headline)"
            f" FROM hits h JOIN {messages} m ON m.guid = h.guid CROSS JOIN q"
            f" LEFT JOIN {conversations} c ON c.conversation_id = h.conversation_id"
            f" LEFT JOIN {people} p ON p.handle = m.sender_handle"
            " ORDER BY h.sent_at DESC, h.guid DESC"
        )
        with self._session() as db:
            _searchable(db, words)
            rows = db.execute(sa.text(sql), parameters).all()
        more = len(rows) > limit
        rows = rows[:limit]
        zone = self.source.zone
        found = [
            {
                "guid": guid,
                "conversation_id": conversation,
                "display_name": name,
                "sent_at": _iso(sent, zone),
                "day": day.isoformat() if day else None,
                "from_me": bool(from_me),
                "sender": sender,
                "snippet": snippet(marked),
            }
            for guid, conversation, name, sent, day, from_me, sender, marked in rows
        ]
        last = rows[-1] if rows else None
        return {
            **self.source.describe(),
            "kind": self._plugin_source(THREAD),
            "messages": found,
            "next": _cursor([last[3].isoformat(), last[0]]) if more and last else None,
        }


class _MessagePage:
    """One side of a conversation's dated messages from a key, along the
    (conversation_id, sent_at, guid) index: older ones newest first, newer ones
    oldest first."""

    def __init__(self, messages: str, people: str, conversation_id: str) -> None:
        self.messages = messages
        self.people = people
        self.conversation_id = conversation_id

    def rows(
        self,
        db: Any,
        side: str,
        key: tuple[datetime, str] | None,
        count: int,
    ) -> list[Any]:
        if count < 1:
            return []
        parameters: dict[str, Any] = {
            "id": self.conversation_id,
            "count": count,
            "max_text": MAX_TEXT,
        }
        keyset = ""
        if key is not None:
            # The plain bound keeps the index range narrow; the row comparison
            # orders ties on sent_at by guid.
            bound = {"<": "<=", ">": ">=", ">=": ">="}[side]
            keyset = (
                f" AND m.sent_at {bound} :key_at"
                f" AND (m.sent_at, m.guid) {side} (:key_at, :key_guid)"
            )
            parameters.update(key_at=key[0], key_guid=key[1])
        direction = "DESC" if side == "<" else "ASC"
        sql = (
            "SELECT m.guid, m.sent_at, m.day, m.is_from_me, m.sender_handle,"
            f" {_SENDER}, m.service, left(m.text, :max_text),"
            " length(m.text) > :max_text, m.text_unreadable, m.is_edited,"
            " m.edited_at, m.is_unsent, m.is_audio, m.reply_to_guid, m.attachments,"
            " m.reactions, m.connection FROM (SELECT * FROM"
            f" {self.messages} m WHERE {_THREAD_MESSAGES}"
            f"{keyset} ORDER BY m.sent_at {direction}, m.guid {direction}"
            f" LIMIT :count) m LEFT JOIN {self.people} p"
            f" ON p.handle = m.sender_handle"
            f" ORDER BY m.sent_at {direction}, m.guid {direction}"
        )
        return list(db.execute(sa.text(sql), parameters).all())


def valid_conversation_id(value: str) -> bool:
    """Whether a value has a conversation id's shape: 32 lower-case hex digits."""
    return bool(_CONVERSATION_ID.fullmatch(value))


def _conversation(row: Any, zone: ZoneInfo) -> dict[str, Any]:
    conversation_id, name, group, count, last, messages, text, from_me = row
    return {
        "conversation_id": conversation_id,
        "display_name": name,
        "is_group": bool(group),
        "participant_count": int(count),
        "last_message_at": _iso(last, zone) if last else None,
        "message_count": int(messages),
        "preview": text,
        "preview_from_me": None if from_me is None else bool(from_me),
    }


def _message_key(row: Any) -> tuple[datetime, str]:
    return (row[1], row[0])


def _message_cursor(key: tuple[datetime, str] | None) -> str | None:
    return _cursor([key[0].isoformat(), key[1]]) if key else None


def _people_names(db: Any, people: str, rows: list[Any]) -> dict[str, str]:
    """Display names of the people who reacted to these messages."""
    handles = sorted(
        {
            reaction["sender"]
            for row in rows
            for reaction in row[16] or []
            if reaction.get("sender")
        }
    )
    if not handles:
        return {}
    found = db.execute(
        sa.text(f"SELECT handle, display_name FROM {people} WHERE handle = ANY(:h)"),
        {"h": handles},
    ).all()
    return {handle: name for handle, name in found}


def _utc_text(value: str | None, zone: ZoneInfo) -> str | None:
    """A time the models wrote as UTC text (timeline_utc_text), with its offset."""
    if not value:
        return None
    return _iso(datetime.fromisoformat(value.replace("Z", "+00:00")), zone)


def _message(
    row: Any, names: dict[str, str], zone: ZoneInfo, kind: str
) -> dict[str, Any]:
    (guid, sent, day, from_me, handle, sender, service, text, cut) = row[:9]
    (unreadable, edited, edited_at, unsent, audio, reply_to) = row[9:15]
    attachments, reactions, connection = row[15:]
    return {
        "guid": guid,
        "sent_at": _iso(sent, zone),
        "day": day.isoformat() if day else None,
        "from_me": bool(from_me),
        "sender": None
        if handle is None
        else {"handle": handle, "display_name": sender},
        "service": service,
        "text": text,
        "text_truncated": bool(cut),
        "text_unreadable": bool(unreadable),
        "edited": bool(edited),
        "edited_at": _iso(edited_at, zone) if edited_at else None,
        "unsent": bool(unsent),
        "audio": bool(audio),
        "reply_to": reply_to,
        "attachments": [
            _attachment_row(connection, kind, item) for item in attachments or []
        ],
        "reactions": [
            {
                "kind": item.get("kind"),
                "from_me": bool(item.get("from_me")),
                "sender": names.get(item["sender"], item["sender"])
                if item.get("sender")
                else None,
                "part": item.get("part"),
                "at": _utc_text(item.get("at"), zone),
            }
            for item in reactions or []
        ],
    }


def _attachment_row(
    connection: str | None, kind: str, item: dict[str, Any]
) -> dict[str, Any]:
    """An attachment as the API gives it, from a message's `attachments` item."""
    return {
        "guid": item.get("guid"),
        "kind": item.get("kind"),
        "mime_type": item.get("mime_type"),
        "bytes": item.get("bytes"),
        "transfer_name": item.get("transfer_name"),
        "media": _attachment_media(connection, kind, item),
    }


def _attachment_media(
    connection: str | None, kind: str, item: dict[str, Any]
) -> dict[str, Any] | None:
    """An attachment's file as a frame's media, of the kind its lane declares;
    null without a media key. Never withheld: no view withholds attachments."""
    key = item.get("media_key")
    if not key:
        return None
    status = item.get("media_status")
    code = media_code(
        None, status, item.get("thumbnail_w360"), item.get("thumbnail_w1280")
    )
    return _media(connection, kind, key, code, status)


# The messages a thread shows (`m` a message, `:id` its conversation): neither
# deleted nor undated. A thread's pages and its pictures read the same ones.
_THREAD_MESSAGES = "m.conversation_id = :id AND NOT m.deleted AND m.sent_at IS NOT NULL"
# An item `a.item` of a message's attachments whose thumbnails are made: what
# `_attachment_media` reads as `thumbnails` (`media_code`), in SQL.
_ATTACHMENT_THUMBNAILS = (
    "coalesce(a.item->>'media_key', '') <> ''"
    " AND a.item->>'media_status' IS NOT NULL"
    " AND a.item->'thumbnail_w360' = 'true'::jsonb"
    " AND a.item->'thumbnail_w1280' = 'true'::jsonb"
)

# A message's sender as shown: the address book's name, else the handle as that
# message writes it (decision 041); null for mine. `m` a message, `p` its person.
_SENDER = (
    "CASE WHEN m.sender_handle IS NULL THEN NULL"
    " ELSE coalesce(p.name, m.sender_written, p.display_name, m.sender_handle) END"
)


def _major(version: Any) -> int | None:
    """A contract version's major: `1` of `1.0`; None for anything else."""
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]{1,6}\.[0-9]{1,6}", version
    ):
        return None
    return int(version.split(".")[0])


def _media(
    connection: str | None, kind: str, key: str, code: int, status: str | None
) -> dict[str, Any]:
    return {
        "connection": connection,
        "kind": kind,
        "key": key,
        "state": _MEDIA_STATES[code],
        "status": status,
    }


NO_WORDS = "The search has no words to look for."
ONLY_EXCLUDED = (
    "The search must look for at least one word: words after a minus only exclude,"
    " and every part joined by 'or' needs a word of its own."
)


def _searchable(db: Any, words: str) -> None:
    """Refuse a search the text index cannot answer. `querytree` keeps the part of
    a query an index can use; it is 'T' when every part is negated (`-a`, `a or
    -b`), which would read every row, and empty when there is no word at all."""
    tree = db.execute(
        sa.text("SELECT querytree(websearch_to_tsquery('simple', :q))"), {"q": words}
    ).scalar_one()
    if tree == "":
        raise OperationError(NO_WORDS)
    if tree == "T":
        raise OperationError(ONLY_EXCLUDED)


def _query(query: str, limit: int) -> str:
    words = query.strip()
    if not words or len(words) > MAX_QUERY:
        raise OperationError(f"The search must have 1 to {MAX_QUERY} characters")
    if not 1 <= limit <= MAX_SEARCH:
        raise OperationError(f"The limit must be between 1 and {MAX_SEARCH}")
    return words


def _range_conditions(
    column: str, start: date | None, end: date | None, parameters: dict[str, Any]
) -> list[str]:
    """Local days from `start` to `end`, inclusive, as bounds on a time column."""
    if start is not None and end is not None and end < start:
        raise OperationError("The range must run forward")
    conditions = []
    if start is not None:
        parameters["start"] = start
        conditions.append(f"{column} >= {_day_bounds_sql('start')[0]}")
    if end is not None:
        parameters["end"] = end
        conditions.append(f"{column} < {_day_bounds_sql('end')[1]}")
    return conditions
