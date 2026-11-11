import importlib.util
import unittest

SPEC = importlib.util.spec_from_file_location(
    "petcam_csi_stream",
    "stage-petcam/00-install-files/files/usr/local/lib/petcam/csi_stream.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CSIStreamTests(unittest.TestCase):
    def test_extracts_concatenated_jpeg_frames(self):
        first = b"\xff\xd8first\xff\xd9"
        second = b"\xff\xd8second\xff\xd9"
        buffer = bytearray(b"noise" + first + second)
        self.assertEqual(MODULE.extract_jpeg_frames(buffer), [first, second])
        self.assertEqual(buffer, bytearray())

    def test_preserves_partial_jpeg_until_end_marker_arrives(self):
        frame = b"\xff\xd8partial-frame\xff\xd9"
        buffer = bytearray(frame[:-2])
        self.assertEqual(MODULE.extract_jpeg_frames(buffer), [])
        buffer.extend(frame[-2:])
        self.assertEqual(MODULE.extract_jpeg_frames(buffer), [frame])
        self.assertEqual(buffer, bytearray())


if __name__ == "__main__":
    unittest.main()
