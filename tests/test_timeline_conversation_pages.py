"""The conversation pages, the Messages kind of search and the Day view's
Messages lane (decision 040, phase 2c, part 2), on a disposable Postgres
(MIROIR_TEST_POSTGRES_BIN, see tests/postgres.py).

The synthetic history of part 1's tests, with example.com addresses and 555
numbers, plus a few messages of its own, is loaded and built by the real
models; the pages read it as the reader role.
"""

import html
import re
from datetime import date
from unittest.mock import patch
from urllib.parse import quote

from src.kernel.errors import OperationError
from src.viewer import STATIC_DIR
from src.viewer.reader import NO_MESSAGES, TimelineReader, TimelineTimeout
from tests import test_timeline_conversations as part_one
from tests.test_messages_models import ALEX, KAI, NOBODY, ROBIN, SAM, message
from tests.test_timeline_conversations import (
    SAM_CHAT,
    ZONE,
    ConversationsFixture,
    raw_configuration,
)
from tests.test_timeline_conversations import history as part_one_history
from tests.test_web import BEARER
from tests.test_web_static import INLINE

HANDLES = (ALEX, SAM, ROBIN, KAI, NOBODY)
LINKED = (
    "see https://docs.example.com/a_(b)?c=1. or ftp://files.example.com/x <b>bold</b>"
)
# Every address a page names: links, images, forms and htmx requests.
ADDRESS = re.compile(r'\s(?:href|src|action|hx-get|hx-post)="([^"]*)"')


def history():
    return [
        *part_one_history(),
        *message("u1", 400, SAM_CHAT, handle=SAM, text=None, text_source="unreadable"),
        *message("l1", 401, SAM_CHAT, me=True, handle=SAM, text=LINKED),
    ]


def addresses(text):
    return [html.unescape(found) for found in ADDRESS.findall(text)]


def item(text, guid):
    """One message of a rendered page, by its guid."""
    found = re.search(
        rf'<li class="message[^"]*" data-guid="{re.escape(guid)}".*?\n</li>',
        text,
        re.DOTALL,
    )
    assert found is not None, guid
    return found.group(0)


def guids(text):
    return re.findall(r'<li class="message[^"]*" data-guid="([^"]+)"', text)


# A tile of a conversation's pictures: its message, its file's key, and where
# its links lead.
TILE = re.compile(
    r'<li class="photo" data-message="([^"]+)">\s*'
    r'<a class="attachment-image" href="/media/m/attachments/([^"?]+)\?size=1280"'
    r' target="_blank"><img src="/media/m/attachments/\2\?size=360"[^>]*></a>\s*'
    r'<p class="photo-links"><time datetime="[^"]+">[^<]+</time><br>'
    r'<a class="photo-original" href="/media/m/attachments/\2" target="_blank">'
    r'Original</a> · <a class="photo-thread" href="([^"]+)">In the thread</a></p>'
)


def tiles(text):
    """The pictures of a rendered page, in order, by their files' keys."""
    return [key for _, key, _ in TILE.findall(text)]


def photo_cursor(text):
    found = re.search(r'<ul class="photos" id="photos" data-before="([^"]*)"', text)
    assert found is not None
    return html.unescape(found.group(1)) or None


def cursor(text, side):
    found = re.search(
        rf'<ol class="messages" id="messages"[^>]* data-{side}="([^"]*)"', text
    )
    assert found is not None
    return html.unescape(found.group(1)) or None


class ConversationPageTests(ConversationsFixture):
    built = False

    def setUp(self):
        with patch.object(part_one, "history", history):
            super().setUp()

    def get(self, client, url, status=200, **options):
        options.setdefault("follow_redirects", False)
        headers = {**BEARER, **options.pop("headers", {})}
        response = client.get(url, headers=headers, **options)
        self.assertEqual(response.status_code, status, response.text[:2000])
        if response.status_code < 300 or response.status_code >= 400:
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertIsNone(INLINE.search(response.text), url)
        return response

    def thread(self, client, conversation_id, status=200, **parameters):
        return self.get(
            client,
            f"/timeline/conversations/{conversation_id}",
            status,
            params=parameters,
        )

    def fragment(self, client, conversation_id, **parameters):
        response = self.get(
            client,
            f"/timeline/conversations/{conversation_id}/messages",
            params=parameters,
            headers={"HX-Request": "true"},
        )
        self.assertNotIn("<html", response.text)
        self.assertIn('<ol class="messages" id="messages"', response.text)
        return response

    # ---- the list -----------------------------------------------------------

    def test_the_list_is_newest_first_with_its_marks(self):
        client = self.client()
        page = self.get(client, "/timeline/conversations").text
        names = [
            html.unescape(name)
            for name in re.findall(r'<span class="row-name">([^<]*)</span>', page)
        ]
        listed = self.reader.conversations(50, None)["conversations"]
        self.assertEqual(names, [c["display_name"] for c in listed])
        self.assertEqual(names[:3], [SAM, "Book Club", "Alex Example"])
        self.assertIn(
            '<link rel="stylesheet" href="/static/viewer/conversations.css">', page
        )
        self.assertIn(
            '<script type="module" src="/static/viewer/conversations.js">', page
        )
        # The group is marked; a preview I sent says so; each row its time.
        book = page[page.index('data-conversation="' + self.book) :]
        book = book[: book.index("</a>")]
        self.assertIn(
            '<span class="group-mark" title="A group of 3">Group</span>', book
        )
        self.assertIn("invoice paid", book)
        self.assertIn('<time class="row-time" datetime="2026-01-10T', book)
        sam = page[page.index('data-conversation="' + self.sam) :]
        self.assertIn(
            '<span class="mine-mark">You:</span> see https://docs.example.com',
            sam[: sam.index("</a>")],
        )
        self.assertIn(f'href="/timeline/conversations/{self.alex}"', page)
        self.assertNotIn("more-row", page)
        # The header links the conversations.
        self.assertIn('<a href="/timeline/conversations">Conversations</a>', page)
        overview = client.get("/", headers=BEARER).text
        self.assertIn('<a href="/timeline/conversations">Conversations</a>', overview)

    def test_the_list_loads_more_rows_as_it_scrolls(self):
        client = self.client()
        with patch("src.viewer.conversations.LIST_PAGE", 1):
            page = self.get(client, "/timeline/conversations").text
            seen = re.findall(r'data-conversation="([0-9a-f]{32})"', page)
            more = re.search(r'hx-get="([^"]+)" hx-trigger="intersect once"', page)
            while more:
                url = html.unescape(more.group(1))
                self.assertTrue(url.startswith("/timeline/conversations?after="))
                rows = self.get(client, url, headers={"HX-Request": "true"}).text
                self.assertNotIn("<html", rows)
                self.assertTrue(rows.lstrip().startswith("<li>"), rows)
                seen += re.findall(r'data-conversation="([0-9a-f]{32})"', rows)
                more = re.search(r'hx-get="([^"]+)" hx-trigger="intersect once"', rows)
            # Without htmx, the same address is a page of its own.
            later = self.get(client, url).text
            self.assertIn("The newest conversations", later)
        listed = self.reader.conversations(50, None)["conversations"]
        self.assertEqual(seen, [c["conversation_id"] for c in listed])
        refused = self.get(client, "/timeline/conversations?after=forged", 400)
        self.assertIn("Invalid paging cursor", refused.text)

    # ---- a thread -------------------------------------------------------------

    def test_a_group_thread_names_its_people_and_its_senders(self):
        page = self.thread(self.client(), self.book).text
        self.assertIn("<h1>Book Club</h1>", page)
        self.assertIn('data-opened="latest"', page)
        self.assertIn(
            '<p class="people muted" id="people">Alex Example, Kai, Robin Sample</p>',
            page,
        )
        self.assertEqual(guids(page), ["g1", "g2", "g3"])
        self.assertIn(
            '<li class="day-separator" data-day="2026-01-10">'
            '<time datetime="2026-01-10">Saturday 10 January 2026</time></li>',
            page,
        )
        robin, mine, kai = (item(page, g) for g in ("g1", "g2", "g3"))
        self.assertIn('class="message theirs"', robin)
        self.assertIn('<p class="sender">Robin Sample</p>', robin)
        self.assertIn('class="message mine"', mine)
        self.assertNotIn('class="sender"', mine)
        self.assertIn('<p class="sender">Kai</p>', kai)
        # My reply quotes the message it answers, which is on the page.
        self.assertIn(
            f'<p class="reply"><a href="/timeline/conversations/{self.book}?around=g1">'
            '<span aria-hidden="true">↩ </span><span class="visually-hidden">In reply'
            " to: </span>chapter four invoice</a></p>",
            mine,
        )
        self.assertIn(
            '<ul class="reactions" aria-label="Reactions"><li><span aria-hidden="true">'
            '‼️</span><span class="visually-hidden">emphasis</span> Kai</li></ul>',
            mine,
        )
        self.assertIn('<time datetime="2026-01-10T02:50:00-05:00">02:50</time>', robin)
        # The title holds no name: browsers keep titles in their history.
        self.assertIn("<title>Conversation</title>", page)
        # The list beside it, this conversation marked.
        self.assertIn(f'data-conversation="{self.book}" aria-current="page"', page)
        self.assertIn('<a class="back" href="/timeline/conversations">', page)

    def test_every_kind_of_message_attachment_and_reaction_shows(self):
        client = self.client()
        page = self.thread(client, self.alex).text
        self.assertEqual(guids(page), self.dated(self.alex))
        self.assertIn('<p class="people muted" id="people">With Alex Example</p>', page)
        self.assertIn(
            '<p class="muted" id="undated">1 message has no date and is not shown.</p>',
            page,
        )
        # Two days, each with its separator.
        self.assertEqual(page.count('<li class="day-separator"'), 2)
        image = item(page, "a08")
        self.assertIn(
            '<a class="attachment-image" href="/media/m/attachments/p1.jpeg?size=1280"'
            ' target="_blank"><img src="/media/m/attachments/p1.jpeg?size=360"'
            ' alt="Attachment" loading="lazy" decoding="async"></a>',
            image,
        )
        stored = item(page, "a16")
        self.assertIn(
            '<a class="file-tile" href="/media/m/attachments/p2.png" target="_blank">'
            '<span class="file-name">Attachment</span>'
            '<span class="file-about">image/png</span></a>',
            stored,
        )
        unknown = item(page, "a03")
        self.assertIn(
            '<span class="file-tile file-absent"><span class="file-name">Attachment'
            '</span><span class="file-about">File state not known yet</span></span>',
            unknown,
        )
        self.assertNotIn("/media/", unknown)
        missing = item(page, "a11")
        self.assertIn('<span class="file-about">Not available</span>', missing)
        self.assertNotIn("/media/", missing)
        loved = item(page, "a19")
        self.assertIn(
            '<li><span aria-hidden="true">❤️</span><span class="visually-hidden">love'
            "</span> Alex Example</li>",
            loved,
        )
        self.assertIn(
            '<li><span aria-hidden="true">👍</span><span class="visually-hidden">like'
            "</span> You</li>",
            loved,
        )
        self.assertIn('<span aria-hidden="true">↩ </span>', item(page, "a06"))
        self.assertIn("note 5</a></p>", item(page, "a06"))
        self.assertIn('<span class="edited">Edited 00:11</span>', item(page, "a14"))
        unsent = item(page, "a01")
        self.assertIn('<p class="unsent">Unsent</p>', unsent)
        self.assertNotIn('class="text"', unsent)
        self.assertIn('<p class="audio">Audio message</p>', item(page, "a09"))
        self.assertIn('<p class="muted">The text is cut here.</p>', item(page, "a17"))
        self.assertIn('<p class="text">the invoice for Alex</p>', item(page, "a04"))
        sam = self.thread(client, self.sam).text
        self.assertIn(
            '<p class="unreadable">The text could not be read.</p>', item(sam, "u1")
        )

    def test_text_is_text_and_only_web_addresses_are_links(self):
        page = self.thread(self.client(), self.sam).text
        self.assertIn(
            '<p class="text">see <a href="https://docs.example.com/a_(b)?c=1"'
            ' rel="noreferrer" target="_blank">https://docs.example.com/a_(b)?c=1</a>'
            ". or ftp://files.example.com/x &lt;b&gt;bold&lt;/b&gt;</p>",
            item(page, "l1"),
        )
        self.assertNotIn("<b>bold", page)
        self.assertNotIn('href="ftp:', page)

    def test_around_marks_a_message_and_at_opens_a_day(self):
        client = self.client()
        dated = self.dated(self.alex)
        with patch("src.viewer.conversations.MESSAGE_PAGE", 5):
            page = self.thread(client, self.alex, around="a19").text
            self.assertIn('data-opened="around"', page)
            shown = guids(page)
            self.assertEqual(shown, dated[dated.index("a19") - 2 :][:5])
            self.assertIn(
                'data-guid="a19" data-day="2026-01-09" data-run="Alex Example"'
                ' id="marked"',
                page,
            )
            self.assertEqual(page.count('id="marked"'), 1)
            self.assertIn('<a id="older" href="/timeline/conversations/', page)
            self.assertIn('<a id="newer" href="/timeline/conversations/', page)
            day = self.thread(client, self.alex, at="2026-01-10").text
            self.assertIn('data-opened="at"', day)
            self.assertEqual(guids(day), [g for g in dated if g in guids(day)])
            first_of_day = [
                guid
                for (guid,) in self.query(
                    f"select guid from {self.schema_name}.messages where "
                    f"conversation_id = '{self.alex}' and day = '2026-01-10' "
                    "order by sent_at, guid limit 5"
                )
            ]
            self.assertEqual(guids(day), first_of_day)
            self.assertIn('name="at" value="2026-01-10"', day)
            self.assertIn('<details class="thread-tools" open>', day)
            # A time with its offset opens there too.
            at_time = self.thread(client, self.alex, at="2026-01-10T00:10:00-05:00")
            self.assertEqual(guids(at_time.text)[:3], ["a01", "a06", "a14"])
            # A message that is not here: the latest page, and why.
            missing = self.thread(client, self.alex, around="g1").text
            self.assertIn('id="missing-message"', missing)
            self.assertEqual(guids(missing), dated[-5:])
            self.assertNotIn('id="marked"', missing)
        for parameters, words in (
            ({"around": "a19", "at": "2026-01-10"}, "at most one of"),
            ({"before": "forged"}, "Invalid paging cursor"),
            ({"at": "Tuesday"}, "at must be a date"),
            ({"around": "x" * 300}, "around must be a message"),
        ):
            with self.subTest(parameters=parameters):
                refused = self.thread(client, self.alex, 400, **parameters)
                self.assertIn(words, html.unescape(refused.text))

    def test_fragments_page_both_ways_without_gap_or_repeat(self):
        client = self.client()
        dated = self.dated(self.alex)
        with patch("src.viewer.conversations.MESSAGE_PAGE", 4):
            latest = self.thread(client, self.alex).text
            seen = guids(latest)
            self.assertEqual(seen, dated[-4:])
            self.assertIsNone(cursor(latest, "after"))
            self.assertNotIn('id="newer"', latest)
            before = cursor(latest, "before")
            while before:
                older = self.fragment(client, self.alex, before=before).text
                seen = guids(older) + seen
                before = cursor(older, "before")
            self.assertEqual(seen, dated)
            start = self.thread(client, self.alex, at="2026-01-01").text
            forward = guids(start)
            self.assertIsNone(cursor(start, "before"))
            after = cursor(start, "after")
            while after:
                newer = self.fragment(client, self.alex, after=after).text
                forward += guids(newer)
                after = cursor(newer, "after")
            self.assertEqual(forward, dated)
            # Without JavaScript the links page as whole pages.
            link = re.search(r'<a id="older" href="([^"]+)"', latest)
            assert link is not None
            older_page = self.get(client, html.unescape(link.group(1))).text
            self.assertIn('data-opened="before"', older_page)
            self.assertEqual(guids(older_page), dated[-8:-4])
            # A reply whose message is on another page links to it.
            reply = self.thread(client, self.alex, at="2026-01-10T00:10:00-05:00").text
            self.assertIn(
                f'<p class="reply"><a href="/timeline/conversations/{self.alex}'
                '?around=a19">Reply to an earlier message</a></p>',
                item(reply, "a06"),
            )
        # A day after the last message: an empty page, its placeholder marked
        # for the script to remove when the older page arrives, and that page
        # brings none of its own.
        with patch("src.viewer.conversations.MESSAGE_PAGE", 4):
            late = self.thread(client, self.alex, at="2099-01-01").text
            self.assertEqual(guids(late), [])
            self.assertIn('<li class="empty-page">No dated messages here.</li>', late)
            self.assertIsNone(cursor(late, "after"))
            older = self.fragment(client, self.alex, before=cursor(late, "before"))
            self.assertEqual(guids(older.text), dated[-4:])
            self.assertNotIn("empty-page", older.text)
        script = (STATIC_DIR / "conversations.js").read_text()
        self.assertIn('querySelectorAll(".empty-page")', script)
        unknown = self.get(
            client,
            f"/timeline/conversations/{self.alex}/messages?around=g1",
            404,
            headers={"HX-Request": "true"},
        )
        self.assertIn("That message is not in this conversation.", unknown.text)

    # ---- pictures -------------------------------------------------------------

    def photos(self, client, conversation_id, status=200, fragment=False, **params):
        headers = {"HX-Request": "true"} if fragment else {}
        response = self.get(
            client,
            f"/timeline/conversations/{conversation_id}/photos",
            status,
            params=params,
            headers=headers,
        )
        if status == 200:
            self.assertEqual("<html" in response.text, not fragment)
            self.assertIn('<ul class="photos" id="photos"', response.text)
        return response

    def pictures(self):
        """For one test, a made thumbnail on more of Alex's messages: a00, a16
        (beside its stored file), a19, a08 (after p1), a18 (then deleted) and
        az (undated)."""
        table = f"{self.schema_name}.messages"
        chosen = "('a00', 'a08', 'a16', 'a19', 'a18', 'az')"
        where = f"conversation_id = '{self.alex}' and guid in {chosen}"
        kept = self.query(
            f"select guid, attachments::text, deleted from {table} where {where}"
        )
        self.assertEqual(len(kept), 6)

        def restore():
            for guid, attachments, deleted in kept:
                written = attachments.replace("'", "''")
                self.query(
                    f"update {table} set attachments = '{written}'::jsonb,"
                    f" deleted = {deleted} where guid = '{guid}'"
                )

        self.addCleanup(restore)
        self.query(
            f"update {table} set attachments = attachments || jsonb_build_array("
            "jsonb_build_object('guid', 'q-' || guid, 'media_key', 'q-' || guid ||"
            " '.jpeg', 'kind', 'image', 'mime_type', 'image/jpeg', 'bytes', 2000,"
            " 'transfer_name', 'IMG-' || guid || '.jpeg', 'media_status', 'made',"
            " 'thumbnail_w360', true, 'thumbnail_w1280', true))"
            f" where {where}"
        )
        self.query(f"update {table} set deleted = true where guid = 'a18'")

    def test_a_thumbnail_opens_its_original_and_the_header_its_pictures(self):
        page = self.thread(self.client(), self.alex).text
        self.assertIn(
            '<img src="/media/m/attachments/p1.jpeg?size=360" alt="Attachment"'
            ' loading="lazy" decoding="async"></a><a class="attachment-original"'
            ' href="/media/m/attachments/p1.jpeg" target="_blank">Original</a>',
            item(page, "a08"),
        )
        # A stored file without thumbnails opens itself; the others open nothing.
        for guid in ("a16", "a03", "a11"):
            self.assertNotIn("Original", item(page, guid))
        self.assertIn(
            f'<a id="photos-link" href="/timeline/conversations/{self.alex}/photos">'
            "Photos</a>",
            page,
        )

    def test_the_pictures_page_newest_first(self):
        self.pictures()
        client = self.client()
        # By (sent_at, guid, position), newest first: a19 alone at its minute,
        # then a16, a08 and a00 sent in the same minute, a08's second picture
        # before its first.
        expected = ["q-a19.jpeg", "q-a16.jpeg", "q-a08.jpeg", "p1.jpeg", "q-a00.jpeg"]
        page = self.photos(client, self.alex).text
        self.assertEqual(tiles(page), expected)
        self.assertEqual(len(TILE.findall(page)), page.count('<li class="photo"'))
        # Each tile's thread link opens the thread at its message.
        senders = ["a19", "a16", "a08", "a08", "a00"]
        self.assertEqual([guid for guid, _, _ in TILE.findall(page)], senders)
        for guid, _, thread in TILE.findall(page):
            self.assertEqual(
                html.unescape(thread),
                f"/timeline/conversations/{self.alex}?around={guid}",
            )
        self.assertIn(
            '<time datetime="2026-01-09T23:50:00-05:00">09 Jan 2026 23:50</time>', page
        )
        self.assertIn('alt="IMG-a19.jpeg"', page)
        opened = self.thread(client, self.alex, around="a19").text
        self.assertIn('id="marked"', item(opened, "a19"))
        # The thread withholds deleted and undated messages: so do its pictures.
        # Stored files and those not known yet have no thumbnail to show.
        for absent in ("q-a18", "q-az", "p2.png", "p3.jpeg"):
            self.assertNotIn(absent, page)
        self.assertIsNone(photo_cursor(page))
        self.assertNotIn('id="more"', page)
        self.assertIn("<h1>Alex Example</h1>", page)
        self.assertIn(
            f'<a id="to-thread" href="/timeline/conversations/{self.alex}">', page
        )
        self.assertIn("<title>Pictures</title>", page)
        self.assertIn(f'data-conversation="{self.alex}" aria-current="page"', page)
        with patch("src.viewer.conversations.PHOTO_PAGE", 2):
            first = self.photos(client, self.alex).text
            seen = tiles(first)
            self.assertEqual(seen, expected[:2])
            before = photo_cursor(first)
            while before:
                more = self.photos(client, self.alex, fragment=True, before=before)
                seen += tiles(more.text)
                before = photo_cursor(more.text)
            self.assertEqual(seen, expected)
            # Without JavaScript, "More" pages as whole pages.
            link = re.search(r'<a id="more" href="([^"]+)">More</a>', first)
            assert link is not None
            second = self.get(client, html.unescape(link.group(1))).text
            self.assertIn("<html", second)
            self.assertEqual(tiles(second), expected[2:4])
            link = re.search(r'<a id="more" href="([^"]+)">More</a>', second)
            assert link is not None
            last = self.get(client, html.unescape(link.group(1))).text
            self.assertEqual(tiles(last), expected[4:])
            self.assertNotIn('id="more"', last)
        script = (STATIC_DIR / "conversations.js").read_text()
        self.assertIn('getElementById("photos")', script)

    def test_pictures_of_an_empty_or_unknown_conversation(self):
        client = self.client()
        for conversation_id in (self.book, self.sam):
            page = self.photos(client, conversation_id).text
            self.assertIn(
                '<li class="empty-page">No pictures in this conversation</li>', page
            )
            self.assertNotIn('<li class="photo"', page)
        for fragment in (False, True):
            unknown = self.photos(client, "0" * 32, 404, fragment=fragment)
            self.assertIn("Unknown conversation", unknown.text)
        for before, words in (
            ("forged", "Invalid paging cursor"),
            (
                cursor(self.thread(client, self.alex, at="2026-01-09").text, "after"),
                "Invalid paging cursor",
            ),
        ):
            with self.subTest(before=before):
                refused = self.photos(client, self.alex, 400, before=before)
                self.assertIn(words, html.unescape(refused.text))
        with self.assertRaises(OperationError):
            self.reader.conversation_photos(self.alex, 0)
        self.assertIsNone(self.reader.conversation_photos("0" * 32, 5))

    # ---- search ---------------------------------------------------------------

    def search(self, client, fields, status=200):
        response = client.post(
            "/timeline/search", data=fields, headers=BEARER, follow_redirects=False
        )
        self.assertEqual(response.status_code, status, response.text[:2000])
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertIsNone(INLINE.search(response.text))
        return response.text

    def test_the_search_has_a_messages_kind(self):
        client = self.client()
        form = self.get(client, "/timeline/search").text
        self.assertIn(
            '<input type="radio" name="kind" value="messages" > Messages', form
        )
        self.assertIn(
            'placeholder="Words on screen, in page titles or in messages"', form
        )
        found = self.search(client, {"q": "invoice", "kind": "messages"})
        self.assertIn(
            '<input type="radio" name="kind" value="messages" checked>', found
        )
        opened = re.findall(
            r'<a href="/timeline/conversations/([0-9a-f]{32})\?around=([^"]+)">', found
        )
        self.assertEqual(
            opened,
            [
                (self.sam, "s1"),
                (self.book, "g3"),
                (self.book, "g1"),
                (self.alex, "a04"),
            ],
        )
        kai = found[found.index("around=g3") :]
        kai = kai[: kai.index("</article>")]
        self.assertIn("<strong>Book Club</strong>", kai)
        self.assertIn('<span class="muted">Kai</span>', kai)
        self.assertIn("<mark>invoice</mark> paid", kai)
        self.assertIn(
            '<time datetime="2026-01-10T02:52:00-05:00">2026-01-10 02:52:00</time>', kai
        )
        nothing = self.search(client, {"q": "absent", "kind": "messages"})
        self.assertIn("Nothing found in messages.", nothing)

    def test_a_thread_searches_its_own_messages(self):
        client = self.client()
        page = self.thread(client, self.alex).text
        self.assertIn(
            '<form class="thread-search" method="post" action="/timeline/search"', page
        )
        self.assertIn('<input type="hidden" name="kind" value="messages">', page)
        self.assertIn(
            f'<input type="hidden" name="conversation" value="{self.alex}">', page
        )
        found = self.search(
            client, {"q": "invoice", "kind": "messages", "conversation": self.alex}
        )
        self.assertEqual(re.findall(r'\?around=([^"]+)">', found), ["a04"])
        self.assertIn(
            f'<input type="checkbox" name="conversation" value="{self.alex}" checked>'
            " Only in Alex Example (messages)",
            found,
        )
        # The filter goes with the next page, in the body.
        with patch("src.viewer.timeline_pages.SEARCH_PAGE", 1):
            first = self.search(
                client, {"q": "invoice", "kind": "messages", "conversation": self.book}
            )
            self.assertEqual(re.findall(r'\?around=([^"]+)">', first), ["g3"])
            fields = dict(
                re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', first)
            )
            self.assertEqual(fields["conversation"], self.book)
            more = client.post(
                "/timeline/search",
                data={k: html.unescape(v) for k, v in fields.items()},
                headers={**BEARER, "HX-Request": "true"},
            ).text
            self.assertEqual(re.findall(r'\?around=([^"]+)">', more), ["g1"])
        # Another kind leaves the conversation filter.
        browser = self.search(
            client, {"q": "invoice", "kind": "browser", "conversation": self.alex}
        )
        self.assertNotIn('name="conversation"', browser)

    # ---- the Day view -----------------------------------------------------------

    def test_the_day_view_has_a_messages_lane(self):
        client = self.client()
        counts = {
            row["day"]: row["messages"]["events"]
            for row in self.reader.days(
                date(2026, 1, 9), date(2026, 1, 10), self.source.sources
            )["days"]
        }
        day = self.get(client, "/timeline/2026-01-10").text
        self.assertIn(
            f"0 screen frames, 0 browser visits, {counts['2026-01-10']} messages.", day
        )
        # A day of messages alone is not empty.
        self.assertNotIn("Nothing was recorded on this day.", day)
        self.assertIn('<canvas id="lane-messages" tabindex="0"', day)
        self.assertIn('<span class="message-marks" id="message-marks"></span>', day)
        self.assertIn('<div class="near-pair">', day)
        self.assertIn('<ul id="messages-near" class="visits"></ul>', day)
        self.assertIn('<input type="radio" name="kind" value="messages" >', day)
        # Its days are shaded by their messages too.
        self.assertIn(
            '<a href="/timeline/2026-01-10" class="heat heat-2 selected"', day
        )
        self.assertIn('<a href="/timeline/2026-01-09" class="heat heat-1"', day)
        self.assertEqual(counts["2026-01-09"], 6)
        # The lanes are the page's data; the thread's plugin links each mark.
        self.assertIn(
            'data-lane="messages" data-plugin="thread" data-colour="lane"', day
        )
        script = (STATIC_DIR / "timeline.js").read_text()
        self.assertIn("source: lane.source", script)
        self.assertNotIn('"messages"', script)
        self.assertIn("threadUrl(event.group, payload.guid)", script)

    def test_without_messages_the_pages_are_as_before(self):
        client = self.client(raw_configuration({"browser": "b"}))
        day = self.get(client, "/timeline/2026-01-10").text
        self.assertIn("0 screen frames, 0 browser visits. Times in", day)
        self.assertIn("Nothing was recorded on this day.", day)
        for absent in ("lane-messages", "messages-near", "near-pair", "Conversations"):
            self.assertNotIn(absent, day)
        self.assertIn(
            '<a href="/timeline/2026-01-10" class="heat heat-0 selected"', day
        )
        form = self.get(client, "/timeline/search").text
        self.assertNotIn('value="messages"', form)
        self.assertIn('placeholder="Words on screen or in page titles"', form)
        refused = self.search(client, {"q": "invoice", "kind": "messages"}, 400)
        self.assertIn("kind must be screen or browser", refused)
        for url in (
            "/timeline/conversations",
            f"/timeline/conversations/{self.alex}",
            f"/timeline/conversations/{self.alex}/messages",
        ):
            with self.subTest(url=url):
                page = self.get(client, url, 503)
                self.assertIn(html.escape(NO_MESSAGES), page.text)

    # ---- rules of every page ------------------------------------------------------

    def test_errors_as_the_other_timeline_pages(self):
        client = self.client()
        urls = (
            "/timeline/conversations",
            f"/timeline/conversations/{self.alex}",
            f"/timeline/conversations/{self.alex}/messages",
            f"/timeline/conversations/{self.alex}/photos",
        )
        unconfigured = self.client(database=None)
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(
                    client.get(url, follow_redirects=False).headers["location"],
                    "/login",
                )
                expired = client.get(url, headers={"HX-Request": "true"})
                self.assertEqual(
                    (expired.status_code, expired.headers["hx-redirect"]),
                    (401, "/login"),
                )
                self.get(unconfigured, url, 503)
                with (
                    patch.object(
                        TimelineReader,
                        "conversations",
                        side_effect=TimelineTimeout("Slow"),
                    ),
                    patch.object(
                        TimelineReader,
                        "conversation_messages",
                        side_effect=TimelineTimeout("Slow"),
                    ),
                    patch.object(
                        TimelineReader,
                        "conversation_photos",
                        side_effect=TimelineTimeout("Slow"),
                    ),
                ):
                    self.assertIn("Slow", self.get(client, url, 504).text)
        # An unknown id is 404; a handle or anything not an id, before any query.
        self.assertIn(
            "Unknown conversation",
            self.get(client, f"/timeline/conversations/{'0' * 32}", 404).text,
        )
        with patch.object(TimelineReader, "_session", side_effect=AssertionError):
            for bad in (quote(ALEX, safe=""), quote(SAM, safe=""), "a" * 31, "A" * 32):
                for url in (
                    f"/timeline/conversations/{bad}",
                    f"/timeline/conversations/{bad}/messages",
                    f"/timeline/conversations/{bad}/photos",
                ):
                    with self.subTest(url=url):
                        self.get(client, url, 404)

    def test_no_handle_or_search_words_in_any_address(self):
        client = self.client()
        pages = [self.get(client, "/timeline/conversations").text]
        with patch("src.viewer.conversations.MESSAGE_PAGE", 4):
            for conversation_id in (self.alex, self.book, self.sam):
                pages.append(self.thread(client, conversation_id).text)
                pages.append(self.thread(client, conversation_id, at="2026-01-09").text)
            pages.append(self.thread(client, self.alex, around="a19").text)
            pages.append(self.photos(client, self.alex).text)
            latest = pages[1]
            pages.append(
                self.fragment(client, self.alex, before=cursor(latest, "before")).text
            )
        pages.append(self.search(client, {"q": "invoice", "kind": "messages"}))
        pages.append(
            self.search(
                client, {"q": "invoice", "kind": "messages", "conversation": self.book}
            )
        )
        pages.append(self.get(client, "/timeline/2026-01-10").text)
        found = [address for page in pages for address in addresses(page)]
        self.assertGreater(len(found), 100)
        for handle in HANDLES:
            for written in {handle, quote(handle, safe=""), handle.lstrip("+")}:
                for address in found:
                    self.assertNotIn(written, address)
        self.assertFalse([a for a in found if "invoice" in a])
        # Thread addresses name ids only.
        for address in found:
            if address.startswith("/timeline/conversations/"):
                self.assertRegex(
                    address,
                    r"^/timeline/conversations/[0-9a-f]{32}(/messages|/photos)?(\?|$)",
                )

    def test_the_times_are_the_analysis_time_zone(self):
        page = self.thread(self.client(), self.alex).text
        self.assertEqual(ZONE, "America/Toronto")
        # 04:30 UTC on 10 January is 23:30 the evening before in Toronto.
        first = item(page, self.dated(self.alex)[0])
        self.assertIn('data-day="2026-01-09"', first)
        self.assertIn(">23:30</time>", first)
        self.assertIn("Friday 09 January 2026", page)
