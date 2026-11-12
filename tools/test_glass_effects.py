import os
import sys
import tempfile
import unittest
from pathlib import Path
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication
from monitor import config
from monitor.database import Database
from monitor.ui import glass_effects as effects
APP = QApplication.instance() or QApplication([])


class EffectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.db = Database(str(Path(self.tmp.name)/'test.db'))
        effects.configure(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
        config.set_theme('dark')

    def test_grading_cache_neutral_and_alpha(self):
        image = QImage(16,16,QImage.Format_RGBA8888)
        image.fill(QColor(100,150,200))
        self.assertEqual(effects.graded_image(image).cacheKey(), image.cacheKey())
        self.db.set_setting(effects.PREFIX+'brightness', 50)
        effects.configure(self.db)
        graded = effects.graded_image(image)
        self.assertEqual(graded.pixelColor(0,0).getRgb(), (50,75,100,255))
        self.assertEqual(graded.cacheKey(), effects.graded_image(image).cacheKey())
        self.assertEqual(image.pixelColor(0,0).getRgb(), (100,150,200,255))

    def test_fit_positions_and_saturation(self):
        image = QImage(100,50,QImage.Format_RGB32)
        image.fill(QColor('red'))
        self.db.set_setting(effects.PREFIX+'fit','contain')
        self.db.set_setting(effects.PREFIX+'saturation',0)
        effects.configure(self.db)
        output = QImage(100,100,QImage.Format_RGB32)
        output.fill(QColor('black'))
        painter = QPainter(output)
        effects.draw_wallpaper(painter,QRectF(0,0,100,100),image)
        painter.end()
        pixel = output.pixelColor(50,50)
        self.assertEqual(pixel.red(),pixel.green())
        self.assertNotEqual(output.pixelColor(50,5),pixel)

    def test_regions_inherit_and_override_independently(self):
        self.db.set_setting(config.S_GLASS_OPACITY,72)
        self.db.set_setting(effects.PREFIX+'card',90)
        effects.configure(self.db)
        self.assertEqual(effects.opacity('sidebar'), .72)
        self.assertEqual(effects.opacity('card'), .90)
        config.set_theme('glass')
        self.assertIn('#Panel',effects.stylesheet())

    def test_framing_preserves_outer_rounded_clip(self):
        image = QImage(40,40,QImage.Format_RGB32)
        image.fill(QColor('red'))
        output = QImage(40,40,QImage.Format_ARGB32_Premultiplied)
        output.fill(Qt.transparent)
        painter = QPainter(output)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0,0,40,40),15,15)
        painter.setClipPath(clip)
        effects.draw_wallpaper(painter,QRectF(0,0,40,40),image)
        painter.end()
        self.assertEqual(output.pixelColor(0,0).alpha(),0)
        self.assertEqual(output.pixelColor(20,20),QColor('red'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
