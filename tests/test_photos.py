import random

import pytest

from kitten_bot.photos import Photos


def write_asset(directory, name, content=b"photo placeholder for selection tests"):
    path = directory / name
    path.write_bytes(content)
    return path


def test_selection_uses_supported_nonempty_files_below_telegram_size_limit(tmp_path):
    expected = {
        write_asset(tmp_path, "one.jpg"),
        write_asset(tmp_path, "two.JPEG"),
        write_asset(tmp_path, "three.PNG"),
    }
    write_asset(tmp_path, "empty.jpg", b"")
    write_asset(tmp_path, "ignored.gif")
    write_asset(tmp_path, "notes.txt")
    (tmp_path / "directory.jpg").mkdir()
    with (tmp_path / "too-large.jpg").open("wb") as oversized:
        oversized.truncate(10_000_000)
    photos = Photos(tmp_path)
    assert set(photos.files) == expected


@pytest.mark.parametrize("with_unsupported_file", [False, True])
def test_empty_photo_collection_fails_at_startup(tmp_path, with_unsupported_file):
    if with_unsupported_file:
        write_asset(tmp_path, "notes.txt")
        write_asset(tmp_path, "empty.png", b"")
    with pytest.raises(ValueError, match="KITTEN_DIR"):
        Photos(tmp_path)


def test_random_selection_avoids_consecutive_repeats_and_can_choose_every_photo(
    tmp_path, monkeypatch
):
    expected = {write_asset(tmp_path, f"kitten-{index}.jpg") for index in range(4)}
    generator = random.Random(42)
    monkeypatch.setattr("kitten_bot.photos.random.choice", generator.choice)
    photos = Photos(tmp_path)
    previous = None
    selected = set()
    for _ in range(100):
        photo = photos.choose(previous)
        assert photo in expected
        assert photo.name != previous
        selected.add(photo)
        previous = photo.name
    assert selected == expected


def test_two_photos_alternate_when_previous_filename_is_supplied(tmp_path):
    first = write_asset(tmp_path, "first.jpg")
    second = write_asset(tmp_path, "second.jpg")
    photos = Photos(tmp_path)
    assert photos.choose(first.name) == second
    assert photos.choose(second.name) == first


def test_single_photo_remains_usable_even_when_it_was_sent_last(tmp_path):
    only = write_asset(tmp_path, "only.jpg")
    photos = Photos(tmp_path)
    assert photos.choose() == only
    assert photos.choose(only.name) == only


def test_missing_previous_filename_does_not_break_selection(tmp_path):
    available = write_asset(tmp_path, "available.jpg")
    assert Photos(tmp_path).choose("removed.jpg") == available
