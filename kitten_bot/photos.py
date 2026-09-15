"""Local kitten photos with an optional persistent snapshot from GitHub."""

import asyncio
import json
import logging
import random
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

import httpx

from .cat_sync import CatSyncError, cached_paths, fetch_catalog, read_manifest, validate_image

logger = logging.getLogger(__name__)


class Photos:
    def __init__(self, directory: Path, cache_dir: Path | None = None):
        self.cache_dir = cache_dir
        self.commit: str | None = None
        self._reload_lock = asyncio.Lock()
        self.files = tuple(
            sorted(
                path
                for path in directory.iterdir()
                if path.is_file()
                and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
                and 0 < path.stat().st_size < 10_000_000
            )
        )
        if not self.files:
            raise ValueError("В KITTEN_DIR нет JPG/PNG фотографий размером до 10 МБ.")
        if cache_dir is not None and (cache_dir / "current.json").exists():
            try:
                commit, entries = read_manifest(cache_dir)
                paths = cached_paths(cache_dir, entries)
                for entry in entries:
                    path = cache_dir / "objects" / entry.cache_name
                    if path.is_symlink():
                        raise ValueError("Invalid cached path")
                    validate_image(path.read_bytes(), entry)
                self.files, self.commit = paths, commit
            except (OSError, ValueError, KeyError, TypeError, AttributeError, CatSyncError):
                logger.warning("Кэш картинок повреждён; использую встроенных котов до /reloadcats")

    async def reload_from_github(self) -> int:
        if self.cache_dir is None:
            raise CatSyncError("Обновление картинок не настроено. Обновите контейнер бота.")
        async with self._reload_lock:
            old_paths = self.files
            try:
                async with asyncio.timeout(120):
                    commit, entries = await fetch_catalog(self.cache_dir)
                paths = cached_paths(self.cache_dir, entries)
                manifest = {"commit": commit, "files": [asdict(entry) for entry in entries]}
                temporary = self.cache_dir / "current.json.tmp"
                temporary.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                temporary.replace(self.cache_dir / "current.json")
                self.files, self.commit = paths, commit
            except (httpx.HTTPError, TimeoutError):
                raise CatSyncError(
                    "Не удалось связаться с GitHub. Прежние картинки сохранены."
                ) from None
            except OSError:
                raise CatSyncError(
                    "Не удалось сохранить картинки. Проверьте свободное место на виртуалке."
                ) from None
            except (ValueError, KeyError, TypeError, AttributeError):
                raise CatSyncError(
                    "GitHub вернул некорректный ответ. Прежние картинки сохранены."
                ) from None
            finally:
                # Keep the previous set too: a photo can still be uploading while we reload.
                keep = set(old_paths) | set(self.files)
                objects = self.cache_dir / "objects"
                with suppress(OSError):
                    for path in objects.iterdir():
                        if path not in keep and path.is_file():
                            with suppress(OSError):
                                path.unlink()
            return len(self.files)

    def choose(self, previous: str | None = None) -> Path:
        candidates = [path for path in self.files if path.name != previous]
        return random.choice(candidates or self.files)
