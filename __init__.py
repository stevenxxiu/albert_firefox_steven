import configparser
import itertools
import json
import re
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Generator, Iterator
from contextlib import closing, contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple, TypedDict, override

from albert import openUrl  # pyright: ignore[reportUnknownVariableType]
from albert import setClipboardText  # pyright: ignore[reportUnknownVariableType]
from albert import (
    Action,
    GeneratorQueryHandler,
    Icon,
    Item,
    Matcher,
    PluginInstance,
    QueryContext,
    StandardItem,
)

setClipboardText: Callable[[str], None]
openUrl: Callable[[str], None]

md_iid = '5.0'
md_version = '1.2'
md_name = 'Firefox Steven'
md_license = 'MIT'
md_url = 'https://github.com/stevenxxiu/albert_firefox_steven'
md_authors = ['@stevenxxiu']

ICON_NAME = 'firefox-developer-edition'
FIREFOX_DATA_PATH = Path.home() / '.config/mozilla/firefox/'
PAGE_SIZE = 10


class Place(NamedTuple):
    url: str
    title: str
    last_visit_us: int | None
    url_hash: int


def get_profile_path() -> Path:
    """
    :return: path of the last selected profile if it was used, or the dev profile
    """
    profile = configparser.ConfigParser()
    _ = profile.read(FIREFOX_DATA_PATH / 'profiles.ini')

    last_used_profile = None
    dev_profile = None
    for key, obj in profile.items():
        if not re.match(r'Profile\d+', key):
            continue
        # `Default = 1` indicates the profile was last used. Dev profiles don't have the setting.
        if obj.get('Default', None) == '1':
            last_used_profile = obj['Path']
        elif obj['Name'].startswith('dev-edition-'):
            dev_profile = obj['Path']

    if last_used_profile and (FIREFOX_DATA_PATH / 'places.sqlite').exists():
        return FIREFOX_DATA_PATH / last_used_profile
    if dev_profile and (FIREFOX_DATA_PATH / dev_profile / 'places.sqlite').exists():
        return FIREFOX_DATA_PATH / dev_profile
    raise ValueError


KEEP_DB_SECS = 10
cleanup_timer: threading.Timer | None = None


def rm_temp_db(temp_path: Path) -> None:
    for name in temp_path.name, f'{temp_path.name}-wal', f'{temp_path.name}-shm':
        with suppress(OSError):
            (temp_path.parent / name).unlink(missing_ok=True)


@contextmanager
def open_db(db_path: Path, temp_db_dir: Path) -> Iterator[sqlite3.Connection]:
    global cleanup_timer
    if cleanup_timer:
        cleanup_timer.cancel()
    temp_db_path = temp_db_dir / db_path.name
    if not (temp_db_path.exists() and db_path.stat().st_mtime_ns != temp_db_path.stat().st_mtime_ns):
        _ = shutil.copy(db_path, temp_db_dir)
    wal_path = db_path.parent / f'{db_path.name}-wal'
    if wal_path.exists():
        _ = shutil.copy(wal_path, temp_db_dir)

    try:
        with closing(sqlite3.connect(temp_db_path)) as con:
            yield con
    finally:
        threading.Timer(KEEP_DB_SECS, rm_temp_db, args=(temp_db_path,)).start()


def get_favicons(profile_path: Path, temp_db_dir: Path, url_hashes: list[int]) -> dict[int, bytes]:
    """
    :param profile_path: Profile path
    :return: URL hash to icon data
    """
    with open_db(profile_path / 'favicons.sqlite', temp_db_dir) as conn:
        cur = conn.cursor()
        _ = cur.execute(
            f"""
            SELECT moz_pages_w_icons.page_url_hash, moz_icons.data FROM moz_icons
            INNER JOIN moz_icons_to_pages ON moz_icons.id = moz_icons_to_pages.icon_id
            INNER JOIN moz_pages_w_icons ON moz_icons_to_pages.page_id = moz_pages_w_icons.id
            WHERE moz_pages_w_icons.page_url_hash IN ({', '.join(['?'] * len(url_hashes))})
            """,
            url_hashes,
        )
        return {row[0]: row[1] for row in cur}  # pyright: ignore[reportAny]


def query_bookmarks(profile_path: Path, temp_db_dir: Path, query: str) -> Generator[Place, None, None]:
    with open_db(profile_path / 'places.sqlite', temp_db_dir) as con:
        cur = con.cursor()

        # Ignore *Firefox* bookmarks menu official bookmarks
        _ = cur.execute('SELECT id FROM moz_bookmarks WHERE title LIKE "Mozilla Firefox" AND fk IS NULL')
        ignored_folders = [res[0] for res in cur.fetchall()]  # pyright: ignore[reportAny]

        # Empty bound parameters aren't allowed
        if not ignored_folders:
            ignored_folders = [-1]

        _ = cur.execute(
            f"""
            SELECT moz_places.url, moz_bookmarks.title, moz_places.url_hash
            FROM moz_bookmarks
            INNER JOIN moz_places ON moz_bookmarks.fk=moz_places.id
            WHERE moz_bookmarks.fk IS NOT NULL
                AND moz_bookmarks.parent NOT IN ({', '.join(['?'] * len(ignored_folders))})
                AND (LOWER(moz_bookmarks.title) LIKE ? OR LOWER(moz_places.url) LIKE ?)
            """,
            [*ignored_folders, f'%{query.lower()}%', f'%{query.lower()}%'],
        )
        for url, title, url_hash in cur:  # pyright: ignore[reportAny]
            yield Place(url, title or '', None, url_hash)


def query_history(
    profile_path: Path, temp_db_dir: Path, query: str, limit: int, offset: int
) -> Generator[Place, None, None]:
    with open_db(profile_path / 'places.sqlite', temp_db_dir) as con:
        cur = con.cursor()
        _ = cur.execute(
            """
            SELECT url, title, last_visit_date, url_hash
            FROM moz_places
            WHERE (LOWER(title) LIKE ? OR LOWER(url) LIKE ?)
            ORDER BY last_visit_date DESC
            LIMIT ?
            OFFSET ?
            """,
            [f'%{query.lower()}%', f'%{query.lower()}%', limit, offset],
        )
        for url, title, last_visit_date, url_hash in cur:  # pyright: ignore[reportAny]
            yield Place(url, title or '', last_visit_date, url_hash)


class FirefoxBaseHandler(GeneratorQueryHandler):  # pyright: ignore[reportImplicitAbstractClass]
    profile_path: Path
    temp_db_dir: Path
    favicon_dir: Path

    def __init__(self, profile_path: Path, temp_db_dir: Path, favicon_dir: Path) -> None:
        GeneratorQueryHandler.__init__(self)
        self.profile_path = profile_path
        self.temp_db_dir = temp_db_dir
        self.favicon_dir = favicon_dir

    @override
    def name(self) -> str:
        return md_name

    @override
    def synopsis(self, _query: str) -> str:
        return '<query>'

    def get_icon(self, url_hash: int, favicons: dict[int, bytes]) -> Icon:
        return Icon.image(self.favicon_dir / str(url_hash)) if url_hash in favicons else Icon.theme(ICON_NAME)

    def create_item(
        self, url: str, title: str, last_visit_us: int | None, url_hash: int, favicons: dict[int, bytes]
    ) -> StandardItem:
        open_call = lambda url_=url: openUrl(url_)  # noqa: E731
        copy_call = lambda title_=title, url_=url: setClipboardText(f'[{title_}]({url_})')  # noqa: E731
        subtext = url
        if last_visit_us is not None:
            last_visit_dt = datetime.fromtimestamp(last_visit_us // 1000000)
            subtext = f'<font color="dimgray">{last_visit_dt.strftime("%Y-%m-%d %H:%M")}</font> {subtext}'
        return StandardItem(
            id=str(url_hash),
            text=title,
            subtext=subtext,
            icon_factory=lambda url_hash_=url_hash: self.get_icon(url_hash_, favicons),
            actions=[
                Action('open', 'Open', open_call),
                Action('copy', 'Copy to clipboard', copy_call),
            ],
        )


class FirefoxBookmarkHandler(FirefoxBaseHandler):
    @override
    def id(self) -> str:
        return f'{md_iid}.bookmark'

    @override
    def defaultTrigger(self):
        return 'fb '

    @override
    def description(self) -> str:
        return 'Open Firefox bookmarks'

    @override
    def items(self, ctx: QueryContext) -> Generator[list[Item], None, None]:
        matcher = Matcher(ctx.query)

        places = query_bookmarks(self.profile_path, self.temp_db_dir, ctx.query)
        url_hashes: set[int] = set()
        items_with_score: list[tuple[StandardItem, tuple[int, float]]] = []
        favicons: dict[int, bytes] = {}
        for url, name, _last_visit_date, url_hash in places:
            url_hashes.add(url_hash)
            score: tuple[int, float] | None = None
            if not score:
                match = matcher.match(name)
                if match:
                    assert isinstance(match.score, float)
                    score = (2, match.score)
            if not score:
                match = matcher.match(url)
                if match:
                    assert isinstance(match.score, float)
                    score = (1, match.score)
            if not score:
                continue
            items_with_score.append((self.create_item(url, name, None, url_hash, favicons), score))
        items_with_score.sort(key=lambda item: item[1], reverse=True)

        favicons.update(get_favicons(self.profile_path, self.temp_db_dir, list(url_hashes)))
        for path in self.favicon_dir.iterdir():
            path.unlink()
        for url_hash, icon_data in favicons.items():
            _ = (self.favicon_dir / str(url_hash)).write_bytes(icon_data)

        yield [item for item, _score in items_with_score]


class FirefoxHistoryHandler(FirefoxBaseHandler):
    @override
    def id(self) -> str:
        return f'{md_iid}.history'

    @override
    def defaultTrigger(self):
        return 'fh '

    @override
    def description(self) -> str:
        return 'Open Firefox history'

    @override
    def items(self, ctx: QueryContext) -> Generator[list[Item], None, None]:
        url_hashes: set[int] = set()
        favicons: dict[int, bytes] = {}
        for path in self.favicon_dir.iterdir():
            path.unlink()
        for i in itertools.count(0):
            places = query_history(self.profile_path, self.temp_db_dir, ctx.query, PAGE_SIZE, i * PAGE_SIZE)
            items: list[Item] = []
            for url, name, last_visit_us, url_hash in places:
                url_hashes.add(url_hash)
                items.append(self.create_item(url, name, last_visit_us, url_hash, favicons))
            favicon_batch = get_favicons(self.profile_path, self.temp_db_dir, list(url_hashes))
            for url_hash, icon_data in favicon_batch.items():
                _ = (self.favicon_dir / str(url_hash)).write_bytes(icon_data)
            favicons.update(favicon_batch)
            yield items


class FirefoxSettings(TypedDict):
    profileName: str


def clean_tmp(prefix: str) -> None:
    """
    Delete any temporary directories, that could've been created from a previous crash.
    """
    for temp_dir in Path(tempfile.gettempdir()).glob(f'{prefix}*'):
        for child in temp_dir.iterdir():
            child.unlink()
        temp_dir.rmdir()


class Plugin(PluginInstance):
    TEMP_DB_PREFIX: str = 'albert_firefox_steven_db_'
    FAVICON_PREFIX: str = 'albert_firefox_steven_favicon_'
    bookmark_handler: FirefoxBookmarkHandler
    history_handler: FirefoxHistoryHandler
    favicon_dir: Path
    temp_db_dir: Path

    def __init__(self) -> None:
        PluginInstance.__init__(self)
        settings_path = self.configLocation() / 'settings.json'
        if settings_path.exists():
            with settings_path.open() as sr:
                settings: FirefoxSettings = json.load(sr)  # pyright: ignore[reportAny]
                profile_path = FIREFOX_DATA_PATH / settings['profileName']
        else:
            profile_path = get_profile_path()

        clean_tmp(self.FAVICON_PREFIX)
        self.temp_db_dir = Path(tempfile.mkdtemp(prefix=self.TEMP_DB_PREFIX))
        self.favicon_dir = Path(tempfile.mkdtemp(prefix=self.FAVICON_PREFIX))
        self.bookmark_handler = FirefoxBookmarkHandler(profile_path, self.temp_db_dir, self.favicon_dir)
        self.history_handler = FirefoxHistoryHandler(profile_path, self.temp_db_dir, self.favicon_dir)

    def __del__(self) -> None:
        shutil.rmtree(self.favicon_dir)
        shutil.rmtree(self.temp_db_dir)

    @override
    def extensions(self) -> list[GeneratorQueryHandler]:
        return [self.bookmark_handler, self.history_handler]
