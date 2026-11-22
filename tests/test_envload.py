"""The repo .env loads into the environment and does not override or invent values."""
import os
import tempfile
import unittest
from pathlib import Path

import envload


class EnvLoad(unittest.TestCase):
    def test_blank_and_comments_do_not_set_variables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('# comment\nGEMINI_API_KEY=\nexport EMPTY=\nNEONCLIPPING_PORT=9000\n', encoding='utf-8')
            os.environ.pop('GEMINI_API_KEY', None)
            os.environ.pop('EMPTY', None)
            os.environ.pop('NEONCLIPPING_PORT', None)
            self.assertTrue(envload.load(path))
            self.assertNotIn('GEMINI_API_KEY', os.environ)
            self.assertNotIn('EMPTY', os.environ)
            self.assertEqual(os.environ['NEONCLIPPING_PORT'], '9000')
            os.environ['NEONCLIPPING_PORT'] = '1'
            path.write_text('NEONCLIPPING_PORT=9000\nNEW_KEY=abc\n', encoding='utf-8')
            envload.load(path)
            self.assertEqual(os.environ['NEONCLIPPING_PORT'], '1')
            self.assertEqual(os.environ['NEW_KEY'], 'abc')
            os.environ.pop('NEONCLIPPING_PORT', None)
            os.environ.pop('NEW_KEY', None)

    def test_install_scripts_stay_in_this_folder(self):
        root = Path(__file__).resolve().parent.parent
        sh = (root / 'install.sh').read_text(encoding='utf-8')
        ps1 = (root / 'install.ps1').read_text(encoding='utf-8')
        bat = (root / 'NeonClipping-Setup.bat').read_text(encoding='utf-8')
        for text in (sh, ps1, bat):
            self.assertNotIn('NEONCLIPPING_DIR', text)
            self.assertNotIn('archive/refs/heads', text)
            self.assertNotIn('LOCALAPPDATA', text)
        self.assertIn('.venv', sh)
        self.assertIn('ffmpeg python3 python3-pip python3-venv', sh)
        self.assertNotIn('github.com', sh)


if __name__ == '__main__':
    unittest.main()
