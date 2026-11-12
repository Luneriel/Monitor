"""Cached wallpaper grading, framing and independent glass surface controls."""
from dataclasses import dataclass
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QHBoxLayout, QSlider, QVBoxLayout, QWidget
from .. import config, i18n

PREFIX = 'glass.effects.'
DEFAULTS = dict(fit='cover', x=50, y=50, scale=100, brightness=100,
                contrast=100, saturation=100, sidebar=-1, card=-1, popup=-1, border=35)
_values = dict(DEFAULTS)
_cache_key = None
_cache_image = QImage()
_base_opacity = 44


def clear_cache():
    global _cache_key, _cache_image
    _cache_key = None
    _cache_image = QImage()


def text(zh, en):
    return en if i18n.current_language() == 'en' else zh


def configure(db):
    global _values, _base_opacity
    try:
        _base_opacity = max(15, min(90, int(db.get_float(config.S_GLASS_OPACITY, 44))))
    except (ValueError, OverflowError):
        _base_opacity = 44
    values = {}
    for key, default in DEFAULTS.items():
        if key == 'fit':
            value = db.get_setting(PREFIX + key, default)
            values[key] = value if value in ('cover', 'contain', 'stretch') else default
        else:
            limits = {'scale': (50, 150), 'brightness': (40, 160), 'contrast': (40, 200),
                      'saturation': (0, 100), 'sidebar': (-1, 100), 'card': (-1, 100),
                      'popup': (-1, 100)}.get(key, (0, 100))
            try:
                value = int(float(db.get_setting(PREFIX + key, str(default))))
            except (ValueError, OverflowError):
                value = default
            values[key] = max(limits[0], min(limits[1], value))
    if any(values[key] != _values[key] for key in ('brightness', 'contrast', 'saturation')):
        clear_cache()
    _values = values


def live_style():
    return dict(fit=_values['fit'], x=_values['x'], y=_values['y'], scale=_values['scale'],
                brightness=_values['brightness']/100, contrast=_values['contrast']/100,
                saturation=_values['saturation']/100)


def opacity(region):
    value = _values.get(region, -1)
    return (_base_opacity if value < 0 else max(15, value)) / 100


def graded_image(image):
    global _cache_key, _cache_image
    key = (image.cacheKey(), _values['brightness'], _values['contrast'], _values['saturation'])
    if key == _cache_key:
        return _cache_image
    if image.isNull() or key[1:] == (100, 100, 100):
        clear_cache()
        return image
    source = image.convertToFormat(QImage.Format_RGBA8888)
    if _values['saturation'] < 100:
        gray = source.convertToFormat(QImage.Format_Grayscale8).convertToFormat(QImage.Format_RGBA8888)
        painter = QPainter(source)
        painter.setOpacity(1 - _values['saturation']/100)
        painter.drawImage(0, 0, gray)
        painter.end()
    if _values['brightness'] != 100 or _values['contrast'] != 100:
        brightness, contrast = _values['brightness']/100, _values['contrast']/100
        table = bytes(max(0, min(255, round(((v-127.5)*contrast+127.5)*brightness))) for v in range(256))
        data = bytearray(source.constBits())
        for channel in range(3):
            data[channel::4] = bytes(data[channel::4]).translate(table)
        source = QImage(bytes(data), source.width(), source.height(), source.bytesPerLine(),
                        QImage.Format_RGBA8888).copy()
    _cache_key, _cache_image = key, source
    return source


def draw_wallpaper(painter, rect, image):
    if image.isNull():
        return
    image = graded_image(image)
    iw, ih = image.width(), image.height()
    fit = _values['fit']
    ratio = (min if fit == 'contain' else max)(rect.width()/iw, rect.height()/ih)
    if fit == 'stretch':
        width, height = rect.width(), rect.height()
    else:
        width, height = iw*ratio, ih*ratio
    width *= _values['scale']/100
    height *= _values['scale']/100
    target = QRectF(rect.x()+(rect.width()-width)*_values['x']/100,
                    rect.y()+(rect.height()-height)*_values['y']/100, width, height)
    painter.save()
    painter.setClipRect(rect, Qt.IntersectClip)
    painter.fillRect(rect, QColor('#111827'))
    painter.drawImage(target, image)
    painter.restore()


def _color(value, alpha):
    color = QColor(value)
    color.setAlpha(round(alpha*255))
    return color.name(QColor.HexArgb)


def stylesheet():
    if config.current_theme() != config.THEME_GLASS:
        return ''
    border = _color('#A7B8D8', _values['border']/100)
    return f'''#NavBar {{ background: {_color('#182338', opacity('sidebar'))}; border-right-color: {border}; }}
    #Panel {{ background: {_color('#182338', opacity('card'))}; border-color: {border}; }}
    #PopupContainer {{ background: {_color('#202C43', opacity('popup'))}; border-color: {border}; }}'''


def border_color():
    return _color('#A7B8D8', _values['border']/100)


def add_settings(page, column, row_class):
    from .notify_page import _Section
    from .popups import SelectBox
    from . import theme
    configure(page.db)
    section = _Section('image', text('壁纸取景与画面', 'Wallpaper framing and color'),
                       text('调整取景和画面，不修改原文件。', 'Adjust framing and color without editing files.'),
                       page, expanded=False)
    section.setObjectName('Panel')
    page.effect_controls = {}
    fit = SelectBox([('cover', text('覆盖', 'Cover')), ('contain', text('适应', 'Fit')),
                     ('stretch', text('拉伸', 'Stretch'))], current=_values['fit'], parent=section.content)
    fit.current_changed.connect(lambda value: page._glass_value_changed(PREFIX+'fit', value))
    page.effect_controls['fit'] = fit
    section.content_layout.addWidget(row_class('image', text('适应方式', 'Fit mode'), '', fit, section.content))
    labels = [('x','水平位置','Horizontal',0,100), ('y','垂直位置','Vertical',0,100),
              ('scale','缩放','Scale',50,150), ('brightness','亮度','Brightness',40,160),
              ('contrast','对比度','Contrast',40,200), ('saturation','饱和度','Saturation',0,100),
              ('sidebar','侧栏不透明度','Sidebar opacity',15,100),
              ('card','卡片不透明度','Card opacity',15,100),
              ('popup','下拉面板不透明度','Popup opacity',15,100),
              ('border','边框对比度','Border contrast',0,100)]
    for key, zh, en, low, high in labels:
        control = QWidget(section.content)
        layout = QHBoxLayout(control)
        layout.setContentsMargins(0,0,0,0)
        slider = QSlider(Qt.Horizontal, control)
        slider.setRange(low, high)
        value = round(opacity(key)*100) if key in ('sidebar','card','popup') else _values[key]
        slider.setValue(value)
        slider.setFixedWidth(config.sx(112))
        number = theme.label(str(value)+'%', config.FONT_SMALL)
        number.setMinimumWidth(config.sx(38))
        layout.addWidget(slider)
        layout.addWidget(number)
        slider.valueChanged.connect(lambda value, k=key, label=number:
                                    page._glass_value_changed(PREFIX+k, value, label, '%'))
        page.effect_controls[key] = (slider, number)
        section.content_layout.addWidget(row_class('image', text(zh,en), '', control, section.content))
    column.addWidget(section)
    page.effects_section = section


def reset(page):
    for key, value in DEFAULTS.items():
        page.db.set_setting(PREFIX+key, value)
    configure(page.db)
    for key, control in getattr(page, 'effect_controls', {}).items():
        if key == 'fit':
            control.set_current('cover')
        else:
            slider, label = control
            value = round(opacity(key)*100) if key in ('sidebar','card','popup') else _values[key]
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
            label.setText(str(value)+'%')
