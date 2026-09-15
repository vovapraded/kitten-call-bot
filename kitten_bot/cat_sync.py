"""Download a validated, immutable snapshot of the public kitten directory."""

import asyncio
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image

REPOSITORY = "vovapraded/kitten-call-bot"
BRANCH = "main"
DIRECTORY = "assets/kittens"
API = f"https://api.github.com/repos/{REPOSITORY}"
MAX_FILE_BYTES = 9_999_999
MAX_TOTAL_BYTES = 200_000_000
MAX_FILES = 200
SHA = re.compile(r"[0-9a-f]{40}")


class CatSyncError(Exception):
    """Only safe, user-facing diagnostics; never raw server bodies or exception text."""


@dataclass(frozen=True)
class CatFile:
    name: str
    sha: str
    size: int

    @property
    def cache_name(self) -> str:
        return self.sha + Path(self.name).suffix.lower()

    @classmethod
    def parse(cls, entry: dict) -> "CatFile":
        name, sha, size = entry.get("name"), entry.get("sha"), entry.get("size")
        if (
            not isinstance(name, str)
            or name in {"", ".", ".."}
            or "/" in name
            or "\\" in name
            or any(ord(char) < 32 for char in name)
            or Path(name).suffix.lower() not in {".jpg", ".jpeg", ".png"}
            or not isinstance(sha, str)
            or not SHA.fullmatch(sha)
            or type(size) is not int
            or not 0 < size <= MAX_FILE_BYTES
        ):
            raise CatSyncError(
                "В каталоге есть некорректный файл или картинка размером 10 МБ и больше."
            )
        return cls(name, sha, size)


def validate_image(data: bytes, entry: CatFile) -> None:
    digest = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
    if len(data) != entry.size or digest != entry.sha:
        raise CatSyncError("Картинка загрузилась не полностью. Попробуйте обновить ещё раз.")
    try:
        with Image.open(io.BytesIO(data)) as picture:
            width, height = picture.size
            if (
                # Phone JPEGs can contain extra MPO frames; validate the primary image.
                picture.format not in {"JPEG", "MPO", "PNG"}
                or width + height > 10_000
                or min(width, height) <= 0
                or max(width, height) / min(width, height) > 20
                or width * height > 20_000_000
            ):
                raise CatSyncError("Одна из картинок не подходит для отправки в Telegram как фото.")
            picture.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise CatSyncError(
            "Не удалось прочитать одну из картинок. Проверьте JPEG/PNG на GitHub."
        ) from None


def read_manifest(cache_dir: Path) -> tuple[str, list[CatFile]]:
    with (cache_dir / "current.json").open("rb") as source:
        raw = source.read(100_001)
    if len(raw) > 100_000:
        raise ValueError("Oversized manifest")
    manifest = json.loads(raw)
    commit = manifest["commit"]
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise ValueError("Invalid commit")
    entries = [CatFile.parse(item) for item in manifest["files"]]
    if not 1 <= len(entries) <= MAX_FILES or sum(e.size for e in entries) > MAX_TOTAL_BYTES:
        raise ValueError("Invalid catalog size")
    return commit, entries


def cached_paths(cache_dir: Path, entries: list[CatFile]) -> tuple[Path, ...]:
    # Deduplicate identical files, even if uploaded under different names.
    return tuple(dict.fromkeys(cache_dir / "objects" / entry.cache_name for entry in entries))


async def read_url(client: httpx.AsyncClient, url: str, limit: int) -> bytes:
    async with client.stream("GET", url) as response:
        if response.status_code in {403, 429}:
            raise CatSyncError("GitHub ограничил запросы. Повторите обновление позже.")
        if response.status_code != 200:
            raise CatSyncError(
                f"GitHub ответил HTTP {response.status_code}. Набор картинок не изменён."
            )
        result = bytearray()
        async for chunk in response.aiter_bytes():
            result.extend(chunk)
            if len(result) > limit:
                raise CatSyncError("Ответ GitHub превышает допустимый размер.")
        return bytes(result)


async def fetch_catalog(cache_dir: Path) -> tuple[str, list[CatFile]]:
    """Stage all files; the caller publishes current.json only on complete success."""
    # Telegram's proxy is deliberately not reused for public GitHub downloads.
    async with httpx.AsyncClient(
        timeout=30,
        trust_env=False,
        follow_redirects=False,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "kitten-call-bot"},
    ) as client:
        ref = json.loads(await read_url(client, f"{API}/git/ref/heads/{BRANCH}", 100_000))
        commit = ref["object"]["sha"]
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise CatSyncError("GitHub вернул некорректную версию каталога.")
        listing = json.loads(
            await read_url(
                client,
                f"{API}/contents/{DIRECTORY}?ref={commit}",
                2_000_000,
            )
        )
        if not isinstance(listing, list):
            raise CatSyncError("На GitHub не найден каталог assets/kittens.")
        entries = []
        for item in listing:
            if not isinstance(item, dict):
                raise CatSyncError("GitHub вернул некорректный список файлов.")
            if item.get("type") == "file" and Path(str(item.get("name", ""))).suffix.lower() in {
                ".jpg",
                ".jpeg",
                ".png",
            }:
                entries.append(CatFile.parse(item))
        if not entries:
            raise CatSyncError("В assets/kittens нет JPEG/PNG. Прежние картинки сохранены.")
        if len(entries) > MAX_FILES or sum(e.size for e in entries) > MAX_TOTAL_BYTES:
            raise CatSyncError("Лимит набора: 200 картинок и 200 МБ суммарно.")
        entries.sort(key=lambda item: item.name)
        entries = list({entry.sha: entry for entry in entries}.values())
        objects = cache_dir / "objects"
        objects.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            target = objects / entry.cache_name
            if target.is_file() and not target.is_symlink():
                try:
                    await asyncio.to_thread(validate_image, target.read_bytes(), entry)
                    continue
                except CatSyncError:
                    pass
            url = (
                f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{DIRECTORY}/"
                f"{quote(entry.name, safe='')}"
            )
            data = await read_url(client, url, min(entry.size, MAX_FILE_BYTES))
            await asyncio.to_thread(validate_image, data, entry)
            temporary = target.with_suffix(target.suffix + ".part")
            temporary.write_bytes(data)
            temporary.replace(target)
        return commit, entries
