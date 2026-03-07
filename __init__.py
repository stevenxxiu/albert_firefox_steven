import configparser
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
md_description = 'Open Firefox bookmarks'
md_license = 'MIT'
md_url = 'https://github.com/stevenxxiu/albert_firefox_steven'
md_authors = ['@stevenxxiu']

ICON_NAME = 'firefox-developer-edition'
FIREFOX_DATA_PATH = Path.home() / '.config/mozilla/firefox/'


class Bookmark(NamedTuple):
    url_hash: int
    name: str
    url: str


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


def get_bookmarks(profile_path: Path) -> list[Bookmark]:
    with open_db(profile_path / 'places.sqlite') as con:
        cur = con.cursor()

        # Ignore *Firefox* bookmarks menu official bookmarks
        _ = cur.execute('SELECT id FROM moz_bookmarks WHERE title LIKE "Mozilla Firefox" AND fk IS NULL')
        ignored_folders = [res[0] for res in cur.fetchall()]  # pyright: ignore[reportAny]

        # Empty bound parameters aren't allowed
        if not ignored_folders:
            ignored_folders = [-1]

        _ = cur.execute(
            """
            SELECT moz_places.url_hash, moz_places.url, moz_bookmarks.title
            FROM moz_bookmarks
            INNER JOIN moz_places ON moz_bookmarks.fk=moz_places.id
            WHERE moz_bookmarks.fk IS NOT NULL
              AND moz_bookmarks.parent NOT IN (?)
            """,
            ignored_folders,
        )
        return [Bookmark(url_hash, url, title or '') for url_hash, title, url in cur]  # pyright: ignore[reportAny]


class FirefoxSettings(TypedDict):
    profileName: str


class Plugin(PluginInstance, GeneratorQueryHandler):
    profile_path: Path

    def __init__(self) -> None:
        PluginInstance.__init__(self)
        GeneratorQueryHandler.__init__(self)

        settings_path = self.configLocation() / 'settings.json'
        if settings_path.exists():
            with settings_path.open() as sr:
                settings: FirefoxSettings = json.load(sr)  # pyright: ignore[reportAny]
                self.profile_path = FIREFOX_DATA_PATH / settings['profileName']
        else:
            self.profile_path = get_profile_path()

    @override
    def synopsis(self, _query: str) -> str:
        return '<query>'

    @override
    def defaultTrigger(self):
        return 'br '

    def get_icon(self, url_hash: int, favicons: dict[int, bytes]) -> Icon:
        return Icon.image(self.cacheLocation() / str(url_hash)) if url_hash in favicons else Icon.theme(ICON_NAME)

    @override
    def items(self, ctx: QueryContext) -> Generator[list[Item], None, None]:
        matcher = Matcher(ctx.query)

        bookmarks = get_bookmarks(self.profile_path)
        url_hashes = {url_hash for (url_hash, _url, _name) in bookmarks}

        favicons = get_favicons(self.profile_path, list(url_hashes))

        items_with_score: list[tuple[StandardItem, tuple[int, float]]] = []
        for i, (url_hash, url, name) in enumerate(bookmarks):
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
                id=str(i),
                text=name,
                subtext=url,
                icon_factory=lambda url_hash_=url_hash: self.get_icon(url_hash_, favicons),
                actions=[Action('open', 'Open', open_url_call)],
            )
            items_with_score.append((item, score))
        items_with_score.sort(key=lambda item: item[1], reverse=True)

        self.cacheLocation().mkdir(exist_ok=True)
        for path in self.cacheLocation().iterdir():
            path.unlink()
        for url_hash, icon_data in favicons.items():
            _ = (self.cacheLocation() / str(url_hash)).write_bytes(icon_data)

        yield [item for item, _score in items_with_score]
