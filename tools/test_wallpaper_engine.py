"""Synthetic-only legacy media checks; never accesses an installed Steam library.

Realtime Scene/Web defaults are covered in test_live_wallpaper.py. These checks
explicitly opt into the retained bounded embedded-video/poster compatibility API."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor import wallpaper_engine as we


def legacy_import(*args, **kwargs):
    return we.import_project(*args, prefer_live=False, **kwargs)


def legacy_read(*args, **kwargs):
    return we.read_project(*args, prefer_live=False, **kwargs)

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1kAAAAASUVORK5CYII=")
# A tiny ISO-BMFF fixture: tests extraction/container recognition, not codecs.
MP4 = struct.pack(">I", 24) + b"ftypisom\0\0\0\0isommp42" + struct.pack(">I", 8) + b"mdat"


def pkg(entries: list[tuple[str, bytes]]) -> bytes:
    magic = b"PKGV0001"
    header = struct.pack("<i", len(magic)) + magic + struct.pack("<i", len(entries))
    offset, payload = 0, bytearray()
    for name, raw in entries:
        encoded = name.encode()
        header += struct.pack("<i", len(encoded)) + encoded + struct.pack("<II", offset, len(raw))
        offset += len(raw)
        payload.extend(raw)
    return header + payload


def compressed_video() -> tuple[bytes, bytes]:
    # One literal sequence containing the MP4 header plus A, followed by an
    # overlapping offset=1 match; final empty literal run terminates the block.
    raw = MP4 + b"A" * 1000
    literal = MP4 + b"A"
    match = 999
    block = bytes([0xff, len(literal) - 15]) + literal + b"\x01\x00"
    remaining = match - 4 - 15
    while remaining >= 255:
        block += b"\xff"
        remaining -= 255
    block += bytes([remaining]) + b"\x00"
    chain = struct.pack("<Qii", len(raw), len(raw), len(block)) + block
    return chain, raw


class WallpaperEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="we-test-", dir=Path(__file__).resolve().parents[1])
        self.base = Path(self.tmp.name)
        self.managed = self.base / "monitor" / "data" / "wallpapers" / "managed"

    def tearDown(self):
        self.tmp.cleanup()

    def project(self, name="project", **metadata):
        root = self.base / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
        return root

    def test_vdf_current_legacy_and_malformed(self):
        self.assertEqual(we.parse_libraryfolders('"libraryfolders" { "0" { "path" "D:\\\\Steam" "apps" { "431960" "1" } } "1" { "path" "E:\\\\Other" } }'), ("D:\\Steam", "E:\\Other"))
        self.assertEqual(we.parse_libraryfolders('// comment\n"LibraryFolders" { "TimeNextStatsReport" "1" "1" "E:\\\\SteamLibrary" }'), ("E:\\SteamLibrary",))
        for invalid in ('"libraryfolders" {', '"x" "unterminated', '"x" }', '{ "x" "y" }', '"x" { "a" }', '"x" { "x" {' * 20):
            with self.subTest(invalid=invalid), self.assertRaises(we.WallpaperEngineError):
                we.parse_libraryfolders(invalid)

    def test_multidisk_discovery_and_selection_never_registry(self):
        steam, library = self.base / "steam", self.base / "other-disk"
        (steam / "steamapps").mkdir(parents=True)
        workshop = library / "steamapps" / "workshop" / "content" / "431960"
        workshop.mkdir(parents=True)
        local = steam / "steamapps" / "common" / "wallpaper_engine" / "projects" / "myprojects"
        local.mkdir(parents=True)
        vdf = '"libraryfolders" { "0" { "path" ' + json.dumps(str(steam)) + ' } "1" { "path" ' + json.dumps(str(library)) + ' } }'
        (steam / "steamapps" / "libraryfolders.vdf").write_text(vdf)
        with patch.object(we, "_registry_roots", side_effect=AssertionError("must not probe real machine")):
            self.assertEqual(set(we.discover_roots(steam)), {local.resolve(), workshop.resolve()})
            self.assertEqual(we.discover_roots(workshop), (workshop.resolve(),))
        (steam / "steamapps" / "libraryfolders.vdf").write_text('"broken" {')
        self.assertIn(local, we.discover_roots(steam))

    def test_registry_discovery_mocked_only_and_renamed_install(self):
        steam = self.base / "Steam"
        (steam / "steamapps").mkdir(parents=True)
        (steam / "steamapps" / "appmanifest_431960.acf").write_text('"AppState" { "appid" "431960" "installdir" "WE custom" }')
        projects = steam / "steamapps" / "common" / "WE custom" / "projects" / "defaultprojects"
        projects.mkdir(parents=True)
        with patch.object(we, "_registry_roots", return_value=[steam]), patch.dict("os.environ", {"ProgramFiles(x86)": "", "ProgramFiles": ""}):
            self.assertEqual(we.discover_roots(), (projects,))

    def test_metadata_invalid_json_and_path_traversal(self):
        root = self.project(type="image", file="wallpaper.png", title="测试")
        (root / "wallpaper.png").write_bytes(PNG)
        self.assertEqual(legacy_read(root).title, "测试")
        for name in ("../outside.png", "..\\outside.png", "C:\\Windows\\image.png", "/etc/passwd", "file.png:secret", "sub/../file.png", "NUL.png"):
            (root / "project.json").write_text(json.dumps({"type": "image", "file": name, "preview": "wallpaper.png"}))
            with self.subTest(name=name), self.assertRaises(we.WallpaperEngineError):
                legacy_read(root)
        for text in ("bad", "[]", "null", '"str"'):
            (root / "project.json").write_text(text)
            result = we.scan_projects([root])
            self.assertFalse(result.projects)
            self.assertTrue(result.warnings)

    def test_image_import_dedup_source_unchanged_and_concurrent(self):
        root = self.project(type="image", file="wallpaper.png")
        source = root / "wallpaper.png"
        source.write_bytes(PNG)
        first = legacy_import(root, self.managed)
        second = legacy_import(root, self.managed)
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.path, second.path)
        self.assertEqual(Path(first.path).read_bytes(), PNG)
        self.assertEqual(source.read_bytes(), PNG)
        self.assertEqual(set(p.name for p in root.iterdir()), {"project.json", "wallpaper.png"})
        other_managed = self.base / "race"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: legacy_import(root, other_managed), range(2)))
        self.assertEqual(results[0].path, results[1].path)
        self.assertEqual(len(list(other_managed.iterdir())), 1)

    def test_direct_image_video_and_no_unneeded_assets(self):
        root = self.project(type="video", file="video.mp4", preview="preview.png")
        (root / "video.mp4").write_bytes(MP4)
        (root / "preview.png").write_bytes(PNG)
        (root / "ignored.exe").write_bytes(b"MZdo not run")
        result = legacy_import(root, self.managed)
        self.assertEqual(result.kind, "video")
        self.assertEqual({p.name for p in Path(result.path).parent.iterdir()}, {"wallpaper.mp4", "import.json"})
        self.assertEqual(legacy_import(root / "preview.png", self.managed).kind, "image")
        self.assertTrue(we.is_video("CLIP.MP4"))

    def test_picture_and_preset_natural_first_picture(self):
        root = self.project(type="picture", file="preset.json", preset={"wallpapermode": 2, "customdirectory": "images", "imageswitchtimes": 1})
        images = root / "images"
        images.mkdir()
        (images / "10.png").write_bytes(PNG + b"10")
        (images / "2.png").write_bytes(PNG + b"2")
        project = legacy_read(root)
        self.assertEqual(Path(project.entry).name, "2.png")
        result = legacy_import(project, self.managed)
        self.assertIn("首张", result.message)
        self.assertEqual(Path(result.path).read_bytes(), PNG + b"2")
        single = self.project("single", preset={"image": "x.png"})
        (single / "x.png").write_bytes(PNG)
        self.assertEqual(legacy_import(single, self.managed).kind, "image")

    def test_scene_raw_pkg_tex_video_extracts_without_preview(self):
        root = self.project(type="scene", file="scene.json")
        (root / "scene.pkg").write_bytes(pkg([("scene.json", b"{}"), ("materials/background.tex", b"TEXV0005\0TEXI0001\0" + MP4)]))
        project = legacy_read(root)
        self.assertEqual(project.import_mode, "video")
        result = legacy_import(project, self.managed)
        self.assertEqual(Path(result.path).read_bytes(), MP4)
        self.assertTrue(result.degraded)
        self.assertIn("不包含", result.message)

    def test_scene_lz4_pkg_and_loose_tex_video(self):
        chain, raw = compressed_video()
        root = self.project(type="scene", file="scene.pkg")
        (root / "scene.pkg").write_bytes(pkg([("materials/main.tex", chain)]))
        result = legacy_import(root, self.managed)
        self.assertEqual(Path(result.path).read_bytes(), raw)
        loose = self.project("loose", type="scene", file="scene.json")
        (loose / "scene.json").write_text("{}")
        (loose / "background.tex").write_bytes(b"TEXB0004\0" + MP4)
        self.assertEqual(Path(legacy_import(loose, self.managed).path).read_bytes(), MP4)

    def test_scene_web_fallback_and_application_refusal(self):
        for kind in ("scene", "web"):
            root = self.project(kind, type=kind, file="scene.json" if kind == "scene" else "index.html", preview="preview.png")
            (root / "preview.png").write_bytes(PNG)
            result = legacy_import(root, self.managed)
            self.assertEqual(result.mode, "static_preview")
            self.assertEqual(result.kind, "image")
            self.assertIn("静态预览", result.message)
        root = self.project("application", type="application", file="evil.exe", preview="preview.png")
        (root / "evil.exe").write_bytes(b"MZ")
        (root / "preview.png").write_bytes(PNG)
        with self.assertRaises(we.WallpaperEngineError):
            legacy_import(root, self.managed)
        for kind in ("scene", "web"):
            root = self.project("no-" + kind, type=kind, file="missing")
            with self.assertRaises(we.WallpaperEngineError):
                legacy_import(root, self.managed)

    def test_pkg_traversal_offsets_duplicates_and_counts(self):
        invalid = [pkg([("../secret.mp4", MP4)]), pkg([("x.tex", MP4), ("X.TEX", MP4)])]
        raw = bytearray(pkg([("x.tex", MP4)]))
        # offset is immediately after magic/count, string length and string.
        struct.pack_into("<I", raw, 16 + 4 + len("x.tex"), 0xffffffff)
        invalid += [bytes(raw), struct.pack("<i", 8) + b"PKGV0001" + struct.pack("<i", we.MAX_PKG_ENTRIES + 1), b"bad"]
        for index, data in enumerate(invalid):
            root = self.project(str(index), type="scene", file="scene.pkg")
            (root / "scene.pkg").write_bytes(data)
            with self.subTest(index=index), self.assertRaises(we.WallpaperEngineError):
                legacy_import(root, self.managed)
        self.assertFalse(self.managed.exists())

    def test_oversize_boundaries_no_large_allocation(self):
        root = self.project(type="image", file="huge.png")
        image = root / "huge.png"
        header = bytearray(PNG)
        struct.pack_into(">II", header, 16, 32768, 32768)
        image.write_bytes(header)
        with self.assertRaisesRegex(we.WallpaperEngineError, "尺寸"):
            legacy_import(root, self.managed)
        image.write_bytes(PNG)
        with patch.object(we, "MAX_IMAGE_BYTES", len(PNG) - 1), self.assertRaises(we.WallpaperEngineError):
            legacy_import(root, self.managed)
        self.assertFalse(list(self.managed.glob(".we-import-*")))
        image.write_bytes(PNG)
        with patch.object(we, "MAX_IMAGE_BYTES", len(PNG)):
            self.assertEqual(legacy_import(root, self.managed).kind, "image")
        with self.assertRaises(we.WallpaperEngineError):
            we._lz4_block(b"\0", we.MAX_BLOCK_BYTES + 1, None)
        scene = self.project("oversized-pkg", type="scene", file="scene.pkg", preview="preview.png")
        (scene / "preview.png").write_bytes(PNG)
        (scene / "scene.pkg").write_bytes(pkg([("main.tex", MP4)]))
        with patch.object(we, "MAX_PKG_BYTES", 1):
            self.assertIn("上限", legacy_read(scene).reason)
            self.assertEqual(legacy_import(scene, self.managed).mode, "static_preview")

    def test_cancel_cleans_stage_and_scan_cancel_propagates(self):
        root = self.project(type="image", file="wallpaper.png")
        (root / "wallpaper.png").write_bytes(PNG)
        with self.assertRaises(we.ImportCancelled):
            we.scan_projects([root], cancel=lambda: True)
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls > 4
        with self.assertRaises(we.ImportCancelled):
            legacy_import(root, self.managed, cancel=cancel)
        self.assertFalse(list(self.managed.glob("*")))
        self.assertFalse(list(self.managed.glob(".we-import-*")))
        root = self.project("scene-cancel", type="scene", file="scene.pkg", preview="p.png")
        (root / "scene.pkg").write_bytes(pkg([("main.tex", MP4)]))
        (root / "p.png").write_bytes(PNG)
        with patch.object(we, "_scene_candidate", side_effect=we.ImportCancelled("cancel")), self.assertRaises(we.ImportCancelled):
            legacy_read(root)

    def test_preview_traversal_no_fallback_and_symlink_escape(self):
        (self.base / "outside.png").write_bytes(PNG)
        root = self.project(type="web", file="index.html", preview="../outside.png")
        self.assertEqual(legacy_read(root).import_mode, "unsupported")
        try:
            (root / "linked.png").symlink_to(self.base / "outside.png")
        except OSError:
            return  # Windows does not always grant symlink creation.
        with self.assertRaises(we.WallpaperEngineError):
            we.safe_project_path(root, "linked.png")

    def test_stale_dataclass_cannot_bypass_application_or_path_guard(self):
        root = self.project(type="application", file="evil.exe")
        fake = we.WallpaperProject("x", "fake", str(root), "web", str(self.base / "outside.png"), None, "image")
        with self.assertRaises(we.WallpaperEngineError):
            legacy_import(fake, self.managed)
        (root / "project.json").write_text(json.dumps({"type": "image", "file": "x.png"}))
        (root / "x.png").write_bytes(PNG)
        with self.assertRaises(we.WallpaperEngineError):
            legacy_import(root, root / "managed")

    def test_corrupt_dedup_fails_atomically_without_overwrite(self):
        root = self.project(type="image", file="x.png")
        (root / "x.png").write_bytes(PNG)
        result = legacy_import(root, self.managed)
        Path(result.path).write_bytes(b"tampered")
        with self.assertRaisesRegex(we.WallpaperEngineError, "校验"):
            legacy_import(root, self.managed)
        self.assertEqual(Path(result.path).read_bytes(), b"tampered")
        self.assertFalse(list(self.managed.glob(".we-import-*")))

    def test_metadata_and_decompression_caps(self):
        root = self.project(type="image", file="x.png")
        (root / "x.png").write_bytes(PNG)
        with patch.object(we, "MAX_METADATA_BYTES", 4), self.assertRaisesRegex(we.WallpaperEngineError, "元数据"):
            legacy_read(root)
        chain, _ = compressed_video()
        scene = self.project("compressed-cap", type="scene", file="scene.pkg", preview="p.png")
        (scene / "scene.pkg").write_bytes(pkg([("main.tex", chain)]))
        (scene / "p.png").write_bytes(PNG)
        with patch.object(we, "MAX_BLOCK_BYTES", 100):
            project = legacy_read(scene)
            self.assertEqual(project.import_mode, "static_preview")
            self.assertIn("上限", project.reason)
        # Raw-entry limits are exercised without allocating a large fixture.
        raw_scene = self.project("raw-cap", type="scene", file="scene.pkg", preview="p.png")
        (raw_scene / "scene.pkg").write_bytes(pkg([("main.tex", MP4)]))
        (raw_scene / "p.png").write_bytes(PNG)
        with patch.object(we, "MAX_ASSET_BYTES", len(MP4) - 1):
            self.assertIn("上限", legacy_read(raw_scene).reason)

    def test_scan_bound_and_progress(self):
        root = self.base / "collection"
        root.mkdir()
        for i in range(3):
            project = root / str(i)
            project.mkdir()
            (project / "project.json").write_text('{"type":"web"}')
        status = []
        with patch.object(we, "MAX_PROJECTS", 2):
            result = we.scan_projects([root], progress=status.append)
        self.assertEqual(len(result.projects), 2)
        self.assertTrue(result.warnings)
        self.assertTrue(status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
