from pathlib import Path

import pytest
from PIL import Image

from photo_audit.embeddings import load_embeddings
from photo_audit.common import AuditError
from photo_audit.vision_encoder import (
    PillowGridEncoder,
    asset_id_for_path,
    build_encoder,
    generate_embedding_rows,
    load_asset_manifest_mapping,
    validate_image_path,
    write_embedding_bundle,
)


def _image(path: Path, color=(10, 80, 140)):
    Image.new("RGB", (32, 24), color).save(path)


def test_pillow_encoder_is_deterministic_and_provenance_is_explicit(tmp_path):
    first = tmp_path / "第一张.png"
    second = tmp_path / "second.jpg"
    _image(first)
    _image(second, (20, 60, 180))
    encoder = PillowGridEncoder(grid_size=8, bins=8)
    left = encoder.encode_image(first)
    right = encoder.encode_image(first)
    assert left == right
    assert len(left) == encoder.provenance.dimensions == 8 * 8 * 3 + 8 * 3
    assert encoder.provenance.deterministic is True
    rows = generate_embedding_rows([second, first], encoder)
    assert [row["asset_id"] for row in rows] == [asset_id_for_path(path) for path in sorted((first, second), key=lambda p: p.as_posix())]
    output = tmp_path / "vectors.csv"
    write_embedding_bundle(output, rows, encoder)
    loaded = load_embeddings(output)
    assert len(loaded) == 2
    assert output.with_suffix(".csv.provenance.json").is_file()


def test_encoder_rejects_non_images_and_unknown_backend(tmp_path):
    text = tmp_path / "notes.txt"
    text.write_text("not an image", encoding="utf-8")
    with pytest.raises(AuditError):
        validate_image_path(text)
    with pytest.raises(AuditError):
        build_encoder("does-not-exist")


def test_generate_embedding_rows_rejects_duplicate_asset_ids(tmp_path):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    _image(first)
    _image(second)
    encoder = PillowGridEncoder(grid_size=4, bins=4)
    rows = generate_embedding_rows([first, second], encoder,
                                   {first.as_posix(): "same", second.as_posix(): "same"})
    assert len({row["asset_id"] for row in rows}) == 2


def test_generate_embedding_rows_disambiguates_same_content_files(tmp_path):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    _image(first)
    second.write_bytes(first.read_bytes())
    encoder = PillowGridEncoder(grid_size=4, bins=4)
    rows = generate_embedding_rows([first, second], encoder)
    assert len({row["asset_id"] for row in rows}) == 2
    assert rows[0]["asset_id"].startswith(asset_id_for_path(first))


def test_asset_manifest_reuses_reference_ids_for_relative_paths(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    first = images / "one.png"
    second = images / "two.jpg"
    _image(first)
    _image(second, (20, 60, 180))
    manifest = tmp_path / "reference_manifest.csv"
    manifest.write_text(
        "asset_id,relative_path,lat,lon\n"
        "ref-one,images/one.png,29.56,106.55\n"
        "ref-two,images/two.jpg,29.57,106.56\n"
        "unmaterialized,images/missing.jpg,29.58,106.57\n",
        encoding="utf-8",
    )
    mapping = load_asset_manifest_mapping(manifest, images, [first, second])
    encoder = PillowGridEncoder(grid_size=4, bins=4)
    rows = generate_embedding_rows([second, first], encoder, mapping)
    assert [row["asset_id"] for row in rows] == ["ref-one", "ref-two"]


def test_asset_manifest_accepts_local_path_and_requires_complete_mapping(tmp_path):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    _image(first)
    _image(second, (20, 60, 180))
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "asset_id,local_path\nref-one,one.png\nref-two,two.png\n",
        encoding="utf-8",
    )
    mapping = load_asset_manifest_mapping(manifest, tmp_path, [first, second])
    assert mapping[first.resolve().as_posix()] == "ref-one"
    assert mapping[second.resolve().as_posix()] == "ref-two"

    incomplete = tmp_path / "incomplete.csv"
    incomplete.write_text("asset_id,local_path\nref-one,one.png\n", encoding="utf-8")
    with pytest.raises(AuditError, match="does not map every input image"):
        load_asset_manifest_mapping(incomplete, tmp_path, [first, second])

