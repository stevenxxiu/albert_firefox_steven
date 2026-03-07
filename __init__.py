import configparser
import itertools
import json
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Generator, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable, NamedTuple, TypedDict, override

from albert import (
    Action,
    GeneratorQueryHandler,
    Icon,
    Item,
    Matcher,
    PluginInstance,
    QueryContext,
    StandardItem,
    runDetachedProcess,
)

md_iid = '5.0'
md_version = '1.2'
md_name = 'Firefox'
md_license = 'MIT'
md_url = 'https://github.com/stevenxxiu/albert_firefox_steven'
md_authors = ['@stevenxxiu']

ICON_NAME = 'firefox-developer-edition'
FIREFOX_DATA_PATH = Path.home() / '.config/mozilla/firefox/'
PAGE_SIZE = 10


class Place(NamedTuple):
    url_hash: int
    url: str
    title: str


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


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir = Path(temp_dir)
        _ = shutil.copy(db_path, temp_dir)
        wal_path = db_path.parent / f'{db_path.name}-wal'
        if wal_path.exists():
            _ = shutil.copy(wal_path, temp_dir)

        with closing(sqlite3.connect(temp_dir / db_path.name)) as con:
            yield con


def get_favicons(profile_path: Path, url_hashes: list[int]) -> dict[int, bytes]:
    """
    :param profile_path: Profile path
    :return: URL hash to icon data
    """
    with open_db(profile_path / 'favicons.sqlite') as conn:
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


def query_bookmarks(profile_path: Path, query: str) -> Generator[Place, None, None]:
    with open_db(profile_path / 'places.sqlite') as con:
        cur = con.cursor()

        # Ignore *Firefox* bookmarks menu official bookmarks
        _ = cur.execute('SELECT id FROM moz_bookmarks WHERE title LIKE "Mozilla Firefox" AND fk IS NULL')
        ignored_folders = [res[0] for res in cur.fetchall()]  # pyright: ignore[reportAny]

        # Empty bound parameters aren't allowed
        if not ignored_folders:
            ignored_folders = [-1]

        _ = cur.execute(
            f"""
            SELECT moz_places.url_hash, moz_places.url, moz_bookmarks.title
            FROM moz_bookmarks
            INNER JOIN moz_places ON moz_bookmarks.fk=moz_places.id
            WHERE moz_bookmarks.fk IS NOT NULL
                AND moz_bookmarks.parent NOT IN ({', '.join(['?'] * len(ignored_folders))})
                AND (LOWER(moz_bookmarks.title) LIKE ? OR LOWER(moz_places.url) LIKE ?)
            """,
            [*ignored_folders, f'%{query.lower()}%', f'%{query.lower()}%'],
        )
        for url_hash, url, title in cur:  # pyright: ignore[reportAny]
            yield Place(url_hash, url, title or '')


def query_history(profile_path: Path, query: str, limit: int, offset: int) -> Generator[Place, None, None]:
    with open_db(profile_path / 'places.sqlite') as con:
        cur = con.cursor()
        _ = cur.execute(
            """
            SELECT url_hash, url, title
            FROM moz_places
            WHERE (LOWER(title) LIKE ? OR LOWER(url) LIKE ?)
            ORDER BY last_visit_date DESC
            LIMIT ?
            OFFSET ?
            """,
            [f'%{query.lower()}%', f'%{query.lower()}%', limit, offset],
        )
        for url_hash, url, title in cur:  # pyright: ignore[reportAny]
            yield Place(url_hash, url, title or '')


class FirefoxBaseHandler(GeneratorQueryHandler):  # pyright: ignore[reportImplicitAbstractClass]
    profile_path: Path
    cache_path: Path

    def __init__(self, profile_path: Path, cache_path: Path) -> None:
        GeneratorQueryHandler.__init__(self)
        self.profile_path = profile_path
        self.cache_path = cache_path

    @override
    def name(self) -> str:
        return md_name

    @override
    def synopsis(self, _query: str) -> str:
        return '<query>'

    def get_icon(self, url_hash: int, favicons: dict[int, bytes]) -> Icon:
        return Icon.image(self.cache_path / str(url_hash)) if url_hash in favicons else Icon.theme(ICON_NAME)


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

        places = query_bookmarks(self.profile_path, ctx.query)
        url_hashes: set[int] = set()
        items_with_score: list[tuple[StandardItem, tuple[int, float]]] = []
        favicons: dict[int, bytes] = {}
        for url_hash, url, name in places:
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
            open_url_call: Callable[[str], int] = lambda url=url: runDetachedProcess(['xdg-open', url])  # noqa: E731
            item = StandardItem(
                id=str(url_hash),
                text=name,
                subtext=url,
                icon_factory=lambda url_hash_=url_hash: self.get_icon(url_hash_, favicons),
                actions=[Action('open', 'Open', open_url_call)],
            )
            items_with_score.append((item, score))
        items_with_score.sort(key=lambda item: item[1], reverse=True)

        favicons.update(get_favicons(self.profile_path, list(url_hashes)))
        for path in self.cache_path.iterdir():
            path.unlink()
        for url_hash, icon_data in favicons.items():
            _ = (self.cache_path / str(url_hash)).write_bytes(icon_data)

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
        for path in self.cache_path.iterdir():
            path.unlink()
        for i in itertools.count(0):
            places = query_history(self.profile_path, ctx.query, PAGE_SIZE, i * PAGE_SIZE)
            items: list[Item] = []
            for url_hash, url, name in places:
                url_hashes.add(url_hash)
                open_url_call: Callable[[str], int] = lambda url=url: runDetachedProcess(['xdg-open', url])  # noqa: E731
                items.append(
                    StandardItem(
                        id=str(url_hash),
                        text=name,
                        subtext=url,
                        icon_factory=lambda url_hash_=url_hash: self.get_icon(url_hash_, favicons),
                        actions=[Action('open', 'Open', open_url_call)],
                    )
                )
            favicon_batch = get_favicons(self.profile_path, list(url_hashes))
            for url_hash, icon_data in favicon_batch.items():
                _ = (self.cache_path / str(url_hash)).write_bytes(icon_data)
            favicons.update(favicon_batch)
            yield items


class FirefoxSettings(TypedDict):
    profileName: str


class Plugin(PluginInstance):
    bookmark_handler: FirefoxBookmarkHandler
    history_handler: FirefoxHistoryHandler

    def __init__(self) -> None:
        PluginInstance.__init__(self)
        settings_path = self.configLocation() / 'settings.json'
        if settings_path.exists():
            with settings_path.open() as sr:
                settings: FirefoxSettings = json.load(sr)  # pyright: ignore[reportAny]
                profile_path = FIREFOX_DATA_PATH / settings['profileName']
        else:
            profile_path = get_profile_path()

        cache_path = self.cacheLocation()
        cache_path.mkdir(exist_ok=True)
        self.bookmark_handler = FirefoxBookmarkHandler(profile_path, cache_path)
        self.history_handler = FirefoxHistoryHandler(profile_path, cache_path)

    @override
    def extensions(self) -> list[GeneratorQueryHandler]:
        return [self.bookmark_handler, self.history_handler]
