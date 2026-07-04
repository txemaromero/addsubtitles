#!/usr/bin/env python3
"""
Unit tests for the pure functions in addsubtitles_gui.

Run with:  python -m unittest -v
No FFmpeg required and no window is opened (only imports the module).
"""

import tempfile
import unittest
from pathlib import Path

from addsubtitles_gui import (
    _fps_from_ratio,
    ass_color_from_hex,
    build_force_style,
    detect_srt_encoding,
    ffmpeg_charenc,
    first_cue_center_ms,
    ms_to_srt_time,
    shift_srt_file,
    srt_time_to_ms,
    video_quality_args,
)


class TestTimeConversion(unittest.TestCase):
    def test_srt_time_to_ms(self):
        self.assertEqual(srt_time_to_ms("00:00:00,000"), 0)
        self.assertEqual(srt_time_to_ms("00:00:01,000"), 1000)
        self.assertEqual(srt_time_to_ms("01:02:03,004"), 3723004)

    def test_ms_to_srt_time(self):
        self.assertEqual(ms_to_srt_time(0), "00:00:00,000")
        self.assertEqual(ms_to_srt_time(1000), "00:00:01,000")
        self.assertEqual(ms_to_srt_time(3723004), "01:02:03,004")

    def test_negative_ms_clamped_to_zero(self):
        self.assertEqual(ms_to_srt_time(-5), "00:00:00,000")

    def test_round_trip(self):
        for t in ("00:00:00,000", "01:02:03,004", "23:59:59,999"):
            self.assertEqual(ms_to_srt_time(srt_time_to_ms(t)), t)


class TestAssColor(unittest.TestCase):
    def test_white_and_black(self):
        self.assertEqual(ass_color_from_hex("#FFFFFF"), "&H00FFFFFF")
        self.assertEqual(ass_color_from_hex("#000000"), "&H00000000")

    def test_bgr_order(self):
        # ASS swaps to BGR: pure red -> B=00 G=00 R=FF
        self.assertEqual(ass_color_from_hex("#FF0000"), "&H000000FF")
        self.assertEqual(ass_color_from_hex("#00FF00"), "&H0000FF00")
        self.assertEqual(ass_color_from_hex("#0000FF"), "&H00FF0000")

    def test_without_hash(self):
        self.assertEqual(ass_color_from_hex("FFFFFF"), "&H00FFFFFF")

    def test_invalid_raises(self):
        for bad in ("#FFF", "#GGGGGG", "12345", ""):
            with self.assertRaises(ValueError):
                ass_color_from_hex(bad)


class TestForceStyle(unittest.TestCase):
    def test_includes_requested_fields(self):
        style = build_force_style(
            font="Arial", fontsize=21, color="#FFFFFF", outline_color="#000000",
            outline=2, shadow=1, align=2, margin_v=15, margin_l=None, margin_r=None,
        )
        parts = style.split(",")
        self.assertIn("Fontname=Arial", parts)
        self.assertIn("Fontsize=21", parts)
        self.assertIn("PrimaryColour=&H00FFFFFF", parts)
        self.assertIn("OutlineColour=&H00000000", parts)
        self.assertIn("BorderStyle=1", parts)
        self.assertIn("Alignment=2", parts)
        self.assertIn("MarginV=15", parts)

    def test_omits_empty_fields(self):
        style = build_force_style(
            font=None, fontsize=None, color=None, outline_color=None,
            outline=None, shadow=None, align=None, margin_v=None,
            margin_l=None, margin_r=None,
        )
        # Only the fixed BorderStyle should remain
        self.assertEqual(style, "BorderStyle=1")


class TestDetectEncoding(unittest.TestCase):
    def _write(self, data: bytes) -> Path:
        tmp = Path(self.tmp.name) / "s.srt"
        tmp.write_bytes(data)
        return tmp

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_utf8_bom(self):
        p = self._write(b"\xef\xbb\xbf1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        self.assertEqual(detect_srt_encoding(p), "utf-8-sig")

    def test_utf16_bom(self):
        p = self._write("1\nHello".encode("utf-16"))
        self.assertEqual(detect_srt_encoding(p), "utf-16")

    def test_plain_utf8(self):
        # Non-ASCII text that is valid UTF-8
        p = self._write("Crème brûlée".encode("utf-8"))
        self.assertEqual(detect_srt_encoding(p), "utf-8")

    def test_cp1252_fallback(self):
        # 0xE9 = 'é' in cp1252, but invalid on its own as UTF-8 -> cp1252 fallback
        p = self._write("caf\xe9".encode("cp1252"))
        self.assertEqual(detect_srt_encoding(p), "cp1252")


class TestFfmpegCharenc(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(ffmpeg_charenc("utf-8-sig"), "UTF-8")
        self.assertEqual(ffmpeg_charenc("utf-16"), "UTF-16")
        self.assertEqual(ffmpeg_charenc("utf-8"), "utf-8")
        self.assertEqual(ffmpeg_charenc("cp1252"), "cp1252")


class TestFpsFromRatio(unittest.TestCase):
    def test_integer_fps(self):
        self.assertEqual(_fps_from_ratio("30/1"), "30")
        self.assertEqual(_fps_from_ratio("25/1"), "25")

    def test_fractional_fps(self):
        self.assertEqual(_fps_from_ratio("30000/1001"), "29.97")
        self.assertEqual(_fps_from_ratio("24000/1001"), "23.98")

    def test_invalid(self):
        self.assertEqual(_fps_from_ratio("30"), "?")
        self.assertEqual(_fps_from_ratio("30/0"), "?")
        self.assertEqual(_fps_from_ratio("x/y"), "?")


class TestFirstCueCenter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write(self, content: str) -> Path:
        p = self.dir / "s.srt"
        p.write_text(content, encoding="utf-8")
        return p

    def test_center_of_first_cue(self):
        p = self._write(
            "1\n00:00:02,000 --> 00:00:04,000\nHello\n\n"
            "2\n00:00:10,000 --> 00:00:12,000\nGoodbye\n"
        )
        # centre of the first block: (2000 + 4000) / 2 = 3000
        self.assertEqual(first_cue_center_ms(p), 3000)

    def test_none_when_no_times(self):
        p = self._write("text without timings\n")
        self.assertIsNone(first_cue_center_ms(p))


class TestVideoQualityArgs(unittest.TestCase):
    def test_software_uses_crf(self):
        self.assertEqual(video_quality_args("libx264", 18), ["-crf", "18"])
        self.assertEqual(video_quality_args("libx265", 20), ["-crf", "20"])

    def test_nvenc_uses_cq(self):
        self.assertEqual(video_quality_args("h264_nvenc", 23), ["-cq", "23"])
        self.assertEqual(video_quality_args("hevc_nvenc", 23), ["-cq", "23"])

    def test_qsv_uses_global_quality(self):
        self.assertEqual(video_quality_args("h264_qsv", 25), ["-global_quality", "25"])

    def test_amf_uses_cqp(self):
        self.assertEqual(
            video_quality_args("h264_amf", 22),
            ["-rc", "cqp", "-qp_i", "22", "-qp_p", "22"],
        )

    def test_videotoolbox_uses_qv(self):
        self.assertEqual(video_quality_args("h264_videotoolbox", 30), ["-q:v", "30"])

    def test_unknown_defaults_to_crf(self):
        self.assertEqual(video_quality_args("libvpx-vp9", 31), ["-crf", "31"])


class TestShiftSrt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _shift(self, content: str, offset: float) -> str:
        src = self.dir / "in.srt"
        dst = self.dir / "out.srt"
        src.write_text(content, encoding="utf-8")
        shift_srt_file(src, offset, dst, encoding="utf-8")
        return dst.read_text(encoding="utf-8")

    def test_positive_shift(self):
        out = self._shift("1\n00:00:01,000 --> 00:00:02,000\nHello\n", 1.0)
        self.assertIn("00:00:02,000 --> 00:00:03,000", out)
        self.assertIn("Hello", out)

    def test_block_fully_before_zero_is_dropped(self):
        out = self._shift("1\n00:00:00,500 --> 00:00:01,000\nHello\n", -2.0)
        self.assertNotIn("Hello", out)

    def test_renumbering_after_drop(self):
        content = (
            "1\n00:00:00,500 --> 00:00:01,000\nOne\n\n"
            "2\n00:00:05,000 --> 00:00:06,000\nTwo\n"
        )
        out = self._shift(content, -2.0)
        # The first block is discarded; the second becomes index 1
        self.assertNotIn("One", out)
        self.assertIn("Two", out)
        self.assertTrue(out.lstrip().startswith("1"))

    def test_partial_block_clamped_to_zero(self):
        # Starts before 0 but ends after -> the start is clamped to 0
        out = self._shift("1\n00:00:01,000 --> 00:00:03,000\nHello\n", -1.5)
        self.assertIn("00:00:00,000 --> 00:00:01,500", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
