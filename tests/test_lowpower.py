"""Low-power profile: off on a desktop, on when forced, and it does not change an explicit choice."""
import os
import unittest
from unittest import mock

os.environ.setdefault('NEONCLIPPING_NO_AUTOUPDATE', '1')
import encoders
import lowpower
import narrate


class Profile(unittest.TestCase):
    def tearDown(self):
        os.environ.pop('NEONCLIPPING_LOW_POWER', None)
        os.environ.pop('NEONCLIPPING_ENCODER', None)
        encoders.works.cache_clear()

    def test_desktop_stays_off(self):
        os.environ['NEONCLIPPING_LOW_POWER'] = '0'
        self.assertFalse(lowpower.enabled())
        self.assertEqual(lowpower.x264_preset('fast'), 'fast')
        self.assertEqual(lowpower.threads(), 0)
        self.assertEqual(lowpower.short_size(), (1080, 1920))
        self.assertEqual(lowpower.long_size(), (1920, 1080))
        self.assertEqual(lowpower.short_fps(None), 60)
        self.assertEqual(lowpower.cap_download(1080), 1080)
        self.assertEqual(lowpower.tune_output({'resolution': 1080, 'fps': 0}), {'resolution': 1080, 'fps': 0})

    def test_forced_profile_shrinks_defaults_only(self):
        os.environ['NEONCLIPPING_LOW_POWER'] = '1'
        self.assertTrue(lowpower.enabled())
        self.assertEqual(lowpower.x264_preset('medium'), 'ultrafast')
        self.assertEqual(lowpower.threads(), min(2, os.cpu_count() or 1))
        self.assertEqual(lowpower.short_size(), (720, 1280))
        self.assertEqual(lowpower.long_size(), (1280, 720))
        self.assertEqual(lowpower.short_fps(None), 30)
        self.assertEqual(lowpower.short_fps(60), 60)
        self.assertEqual(lowpower.cap_download(1080), 720)
        self.assertEqual(lowpower.cap_download(1080, touched=True), 1080)
        self.assertEqual(lowpower.cap_download(2160), 2160)
        self.assertEqual(lowpower.asr_model(None), 'tiny')
        self.assertEqual(lowpower.asr_model('hi'), 'base')
        tuned = lowpower.tune_output({'resolution': 1080, 'fps': 0})
        self.assertEqual(tuned['resolution'], 720)
        self.assertEqual(tuned['fps'], 30)
        kept = lowpower.tune_output({'resolution': 1080, 'fps': 60, 'output_touched': True})
        self.assertEqual(kept['resolution'], 1080)
        self.assertEqual(kept['fps'], 60)
        step, width, quality = lowpower.gemini_limits(120, 'overview')
        self.assertGreaterEqual(step, 4)
        self.assertEqual(width, 256)
        self.assertEqual(quality, 8)

    def test_v4l2_is_only_offered_in_the_profile(self):
        encoders.works.cache_clear()
        os.environ.pop('NEONCLIPPING_ENCODER', None)
        with mock.patch.object(encoders, 'works', lambda name: True):
            os.environ['NEONCLIPPING_LOW_POWER'] = '0'
            self.assertEqual([e[1] for e in encoders.h264()], ['h264_nvenc', 'h264_amf', 'h264_qsv', 'h264_videotoolbox', 'libx264'])
            os.environ['NEONCLIPPING_LOW_POWER'] = '1'
            names = [e[1] for e in encoders.h264()]
            self.assertEqual(names[-2:], ['h264_v4l2m2m', 'libx264'])
            self.assertEqual(encoders.h264(cq=23, x264_preset='medium')[-1][:8], ['-c:v', 'libx264', '-crf', '23', '-preset', 'ultrafast', '-threads', str(lowpower.threads())])
        os.environ['NEONCLIPPING_ENCODER'] = 'v4l2m2m'
        os.environ['NEONCLIPPING_LOW_POWER'] = '0'
        with mock.patch.object(encoders, 'works', lambda name: name == 'h264_v4l2m2m'):
            self.assertEqual([e[1] for e in encoders.h264()], ['h264_v4l2m2m', 'libx264'])
        os.environ.pop('NEONCLIPPING_ENCODER', None)

    def test_jpeg_pipe_splits(self):
        blob = b'\xff\xd8' + b'one' + b'\xff\xd9' + b'junk' + b'\xff\xd8' + b'two' + b'\xff\xd9'
        self.assertEqual(narrate.split_jpegs(blob), [b'\xff\xd8one\xff\xd9', b'\xff\xd8two\xff\xd9'])
        self.assertEqual(narrate.split_jpegs(b'\xff\xd8truncated'), [])


if __name__ == '__main__':
    unittest.main()
