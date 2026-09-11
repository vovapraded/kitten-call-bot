"""Local kitten photos: no external image service or API key required."""

import random
from pathlib import Path


class Photos:
    def __init__(self, directory: Path):
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

    def choose(self, previous: str | None = None) -> Path:
        candidates = [path for path in self.files if path.name != previous]
        return random.choice(candidates or self.files)
