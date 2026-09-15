import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from kitten_bot import cat_sync
from kitten_bot.cat_sync import CatFile, CatSyncError, read_manifest, validate_image
from kitten_bot.config import Config
from kitten_bot.photos import Photos

COMMIT = "a" * 40


def blob(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


@pytest.fixture
def images():
    directory = Config.kitten_dir
    return {
        "first.jpg": (directory / "blanket.jpg").read_bytes(),
        "second.jpg": (directory / "sleeping.jpg").read_bytes(),
    }


@pytest.fixture
def github(monkeypatch, images):
    state = {"images": images.copy(), "commit": COMMIT, "downloads": [], "status": 200}
    client_type = httpx.AsyncClient

    def handler(request):
        if state["status"] != 200:
            return httpx.Response(state["status"], text="private server diagnostic")
        if "/git/ref/" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": state["commit"]}})
        if "/contents/" in request.url.path:
            assert request.url.params["ref"] == state["commit"]
            entries = [
                {
                    "type": "file",
                    "name": name,
                    "sha": blob(data),
                    "size": len(data),
                    "download_url": "https://untrusted.example/never-use-this",
                }
                for name, data in state["images"].items()
            ]
            entries.append({"type": "file", "name": "README.txt"})
            return httpx.Response(200, json=entries)
        assert request.url.host == "raw.githubusercontent.com"
        assert f"/{state['commit']}/assets/kittens/" in request.url.path
        name = request.url.path.rsplit("/", 1)[-1]
        state["downloads"].append(name)
        data = state["images"][name]
        return httpx.Response(200, content=data)

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client_type(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cat_sync.httpx, "AsyncClient", client)
    return state


async def test_reload_adds_and_removes_photos_and_survives_restart(tmp_path, github):
    cache = tmp_path / "cats"
    photos = Photos(Config.kitten_dir, cache_dir=cache)
    assert await photos.reload_from_github() == 2
    assert all(path.parent == cache / "objects" for path in photos.files)
    assert photos.commit == COMMIT
    assert len(github["downloads"]) == 2
    original_paths = photos.files
    assert await photos.reload_from_github() == 2
    assert len(github["downloads"]) == 2  # unchanged files aren't downloaded again
    github["images"].pop("first.jpg")
    github["commit"] = "b" * 40
    assert await photos.reload_from_github() == 1
    assert all(path.exists() for path in original_paths)  # an old upload may still be in flight
    reopened = Photos(Config.kitten_dir, cache_dir=cache)
    assert reopened.files == photos.files
    assert reopened.commit == "b" * 40


@pytest.mark.parametrize("failure", ["corrupt", "empty", "rate-limit", "network", "manifest-write"])
async def test_failed_reload_keeps_working_and_persisted_catalog(
    tmp_path,
    github,
    monkeypatch,
    failure,
):
    cache = tmp_path / "cats"
    photos = Photos(Config.kitten_dir, cache_dir=cache)
    await photos.reload_from_github()
    previous = photos.files
    manifest = (cache / "current.json").read_bytes()
    github["commit"] = "b" * 40
    if failure == "corrupt":
        github["images"]["broken.jpg"] = b"not an image"
    elif failure == "empty":
        github["images"] = {}
    elif failure == "rate-limit":
        github["status"] = 429
    elif failure == "network":

        async def failed(*args):
            raise httpx.ConnectError("sensitive network detail")

        monkeypatch.setattr("kitten_bot.photos.fetch_catalog", failed)
    else:
        original = Path.replace

        def fail_manifest(path, target):
            if path.name == "current.json.tmp":
                raise OSError("disk full")
            return original(path, target)

        monkeypatch.setattr(Path, "replace", fail_manifest)
    with pytest.raises(CatSyncError):
        await photos.reload_from_github()
    assert photos.files == previous
    assert photos.commit == COMMIT
    assert (cache / "current.json").read_bytes() == manifest
    assert Photos(Config.kitten_dir, cache_dir=cache).files == previous


async def test_identical_uploads_are_one_photo_even_with_different_extensions(tmp_path, github):
    image = github["images"]["first.jpg"]
    github["images"] = {"one.jpg": image, "two.jpeg": image}
    photos = Photos(Config.kitten_dir, cache_dir=tmp_path / "cats")
    assert await photos.reload_from_github() == 1
    assert len(github["downloads"]) == 1
    assert Photos(Config.kitten_dir, cache_dir=photos.cache_dir).files == photos.files


async def test_corrupt_cache_falls_back_and_can_be_repaired(tmp_path, github):
    cache = tmp_path / "cats"
    photos = Photos(Config.kitten_dir, cache_dir=cache)
    await photos.reload_from_github()
    photos.files[0].write_bytes(b"broken")
    reopened = Photos(Config.kitten_dir, cache_dir=cache)
    assert all(path.parent == Config.kitten_dir for path in reopened.files)
    assert await reopened.reload_from_github() == 2
    assert len(github["downloads"]) == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "../evil.jpg"),
        ("name", "/absolute.png"),
        ("name", "bad\\file.jpg"),
        ("name", "bad\x00.jpg"),
        ("sha", "../../path"),
        ("size", 10_000_000),
        ("size", True),
    ],
)
def test_metadata_cannot_escape_cache_or_exceed_file_limits(field, value):
    entry = {"name": "cat.jpg", "sha": "a" * 40, "size": 100}
    entry[field] = value
    with pytest.raises(CatSyncError):
        CatFile.parse(entry)


def test_content_hash_is_verified_before_image_use(images):
    data = images["first.jpg"]
    with pytest.raises(CatSyncError):
        validate_image(data, CatFile("cat.jpg", "a" * 40, len(data)))


def test_phone_jpeg_with_extra_mpo_frames_is_accepted(images):
    with Image.open(io.BytesIO(images["first.jpg"])) as picture:
        output = io.BytesIO()
        picture.save(output, format="MPO", save_all=True, append_images=[picture])
    data = output.getvalue()
    with Image.open(io.BytesIO(data)) as picture:
        assert picture.format == "MPO"
    validate_image(data, CatFile("phone.JPG", blob(data), len(data)))


def test_manifest_rejects_unsafe_commit(tmp_path):
    (tmp_path / "current.json").write_text(json.dumps({"commit": "../elsewhere", "files": []}))
    with pytest.raises(ValueError):
        read_manifest(tmp_path)
