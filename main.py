import os
import io
import json
import time
import subprocess
from enum import Enum
from pathlib import Path
from typing import List, Tuple, Optional

from PIL import Image, ImageOps

import platform

try:
    import cv2
    import numpy as np
    HAS_OPENCV = True
except Exception:
    HAS_OPENCV = False



from kivy.config import Config
# Don't force fullscreen on macOS during development
if platform.system() != 'Darwin':
    Config.set('graphics', 'fullscreen', 'auto')
Config.set('kivy', 'log_enable', '0')
Config.set('input', 'mouse', 'mouse,disable_multitouch')

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics.texture import Texture
from kivy.properties import StringProperty, NumericProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.image import Image as KivyImage
from kivy.uix.label import Label
from kivy.uix.modalview import ModalView
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.graphics import Color, RoundedRectangle, Rectangle
from kivy.animation import Animation

try:
    from picamera2 import Picamera2
    from libcamera import Transform
    HAS_PICAMERA = True
except Exception:
    HAS_PICAMERA = False

try:
    from gpiozero import Button as GpioButton
    HAS_GPIO = True
except Exception:
    HAS_GPIO = False


PHOTO_DIR = Path(os.environ.get("PHOTOBOOTH_PHOTOS_DIR", str(Path.home() / "photobooth/data/photos")))
TEMPLATES_PATH = Path(os.environ.get("PHOTOBOOTH_TEMPLATES_PATH",
                                     str(Path(__file__).parent / "public/templates/index.json")))
PHOTO_DIR.mkdir(parents=True, exist_ok=True)

A4_W, A4_H = 2480, 3508  # A4 at 300 DPI (standard print resolution)
# Camera Module 3 resolutions:
# - Still: 11.9MP (4608x2592) or 12MP (4056x3040)
# - Video: 4K (3840x2160) or 1080p (1920x1080)
CAMERA_STILL_W, CAMERA_STILL_H = 3840, 2160  # Max resolution for Camera Module 3
CAMERA_VIDEO_W, CAMERA_VIDEO_H = 1920, 1080  # Use lower resolution for faster preview
PREVIEW_W, PREVIEW_H = 1080, 1920  # Preview display size (portrait)

# UI Layout for vertical screen (1440x2560)
SCREEN_W, SCREEN_H = 1440, 2560  # Vertical screen dimensions
BANNER_HEIGHT_RATIO = 0.08  # 8% of screen for FILMOLA banner (~205px)
BANNER_FONT_SIZE = 48  # Font size for FILMOLA text

# Template display sizes (matching templates/index.html)
TEMPLATE_DISPLAY_W = 2592  # Template display width
TEMPLATE_DISPLAY_H = 1843  # Template display height
INACTIVITY_SECONDS = 90
COUNTDOWN_SECONDS = 10

# Simple theme
PANEL_BG = (0, 0, 0, 0.35)
PANEL_BORDER = (1, 1, 1, 0.12)
ACCENT = (0.22, 0.65, 1.0, 1)
RADIUS = 12

GPIO_NEXT = 17
GPIO_ENTER = 27
GPIO_PREV = 22
GPIO_SHUTTER = 23

FILTERS = ["none", "black_white", "sepia", "newspaper"]


class ScreenState(str, Enum):
    ATTRACT = "attract"
    TEMPLATE = "template"
    COUNTDOWN = "countdown"
    CAPTURING = "capturing"
    QUICK_REVIEW = "quick_review"
    SELECTION = "selection"
    REVIEW = "review"
    PRINTING = "printing"


class PreviewWidget(KivyImage):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.allow_stretch = True
        self.keep_ratio = True

    def show_frame(self, frame_rgb):
        h, w, _ = frame_rgb.shape
        if not self.texture or self.texture.size != (w, h):
            self.texture = Texture.create(size=(w, h), colorfmt="rgb")
            self.texture.flip_vertical()
        self.texture.blit_buffer(frame_rgb.tobytes(), colorfmt="rgb", bufferfmt="ubyte")
        self.canvas.ask_update()


class SettingsModal(ModalView):
    def __init__(self, initial_printer: str, on_save, **kwargs):
        super().__init__(**kwargs)
        self.size_hint = (0.6, 0.4)
        layout = BoxLayout(orientation="vertical", padding=16, spacing=8)
        layout.add_widget(Label(text="CUPS Printer Name", font_size=20))
        self.input = TextInput(text=initial_printer or "", multiline=False, size_hint=(1, 0.4))
        layout.add_widget(self.input)
        btns = BoxLayout(orientation="horizontal", size_hint=(1, 0.3), spacing=8)
        btns.add_widget(Button(text="Cancel", on_press=lambda *_: self.dismiss()))
        btns.add_widget(Button(text="Save", on_press=lambda *_: (on_save(self.input.text.strip()), self.dismiss())))
        layout.add_widget(btns)
        self.add_widget(layout)


class PhotoboothRoot(FloatLayout):
    hud_text = StringProperty("")
    countdown_value = NumericProperty(3)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
        # A4 background image (template background)
        self.a4_bg = KivyImage(allow_stretch=True, keep_ratio=False, opacity=1, size_hint=(None, None))
        self.add_widget(self.a4_bg)
        
        # Blur backdrop for selection phase
        self.blur_bg = KivyImage(allow_stretch=True, keep_ratio=True, opacity=0, size_hint=(None, None))
        self.add_widget(self.blur_bg)
        
        # Dim overlay for selection phase
        from kivy.uix.widget import Widget
        self.dim = Widget(opacity=0)
        with self.dim.canvas:
            self._dim_color = Color(0, 0, 0, 0.45)   # 45% dim
            self._dim_rect = Rectangle(pos=self.pos, size=self.size)
        self.dim.bind(pos=lambda *_: setattr(self._dim_rect, "pos", self.pos),
                      size=lambda *_: setattr(self._dim_rect, "size", self.size))
        self.add_widget(self.dim)   # Add on top of preview, under selection UI
        
        # Camera preview
        self.preview = PreviewWidget()
        self.preview.size_hint = (None, None)
        self.add_widget(self.preview)
        
        # Persistent FILMOLA banner at bottom
        self.banner = BoxLayout(orientation='horizontal', size_hint=(1, None))
        self.banner_bg = (0.1, 0.1, 0.1, 0.95)
        self.banner_lbl = Label(text="FILMOLA", font_size=BANNER_FONT_SIZE, bold=True, color=(1,1,1,1))
        self.banner.add_widget(self.banner_lbl)
        self.add_widget(self.banner)
        
        # Initialize layout
        self._compute_layout()
        Window.bind(on_resize=lambda *_: self._compute_layout())

        # HUD with proper positioning (top-left) - back to landscape
        self.hud = Label(
            text="Loading...",
            font_size=18,  # Back to normal font size
            color=(1, 1, 1, 1),
            size_hint=(None, None),
            pos_hint={'x': 0, 'top': 1},
            padding=(10, 10),  # Back to normal padding
            halign='left',
            valign='top'
        )
        self.hud.bind(texture_size=self.hud.setter('size'))
        self.add_widget(self.hud)
        self._decorate_panel(self.hud)

        # Status bar (top-right): camera, printer, settings hint - back to landscape
        self.status = Label(
            text="",
            font_size=16,  # Back to normal font size
            color=(1, 1, 1, 1),
            size_hint=(None, None),
            pos_hint={'right': 1, 'top': 1},
            halign='right',
            valign='top'
        )
        self.status.bind(texture_size=self.status.setter('size'))
        self.add_widget(self.status)
        self._decorate_panel(self.status)

        # Countdown overlay centered - back to landscape
        self.countdown = Label(
            text="",
            font_size=140,  # Back to normal font size
            bold=True,
            color=(1, 1, 1, 1),
            pos_hint={'center_x': 0.5, 'center_y': 0.5}
        )
        self.countdown.opacity = 0
        self.add_widget(self.countdown)

        # Center image overlay (quick review or composed image)
        self.quick = KivyImage(
            size_hint=(0.8, 0.8),
            allow_stretch=True,
            keep_ratio=True,
            pos_hint={'center_x': 0.5, 'center_y': 0.5}
        )
        self.quick.opacity = 0
        self.add_widget(self.quick)
        self._decorate_panel(self.quick, pad=(18, 18), radius=16)

        # Titles / instructions overlays - back to landscape
        self.title = Label(
            text="",
            font_size=36,  # Back to normal font size
            bold=True,
            color=(1, 1, 1, 1),
            pos_hint={'center_x': 0.5, 'center_y': 0.74}
        )
        self.title.opacity = 0
        self.add_widget(self.title)

        self.subtitle = Label(
            text="",
            font_size=20,  # Back to normal font size
            color=(1, 1, 1, 1),
            pos_hint={'center_x': 0.5, 'center_y': 0.66}
        )
        self.subtitle.opacity = 0
        self.add_widget(self.subtitle)

        self.footer = Label(
            text="",
            font_size=18,  # Back to normal font size
            color=(1, 1, 1, 1),
            pos_hint={'center_x': 0.5, 'y': 0.02}
        )
        self.footer.opacity = 0
        self.add_widget(self.footer)

        # Selection thumbnails container (created on demand) - back to landscape
        from kivy.uix.boxlayout import BoxLayout as KivyBox
        self.selection_box = KivyBox(
            orientation='horizontal',  # Back to horizontal for landscape
            spacing=20,  # Back to normal spacing
            size_hint=(0.9, None),  # Back to normal size
            height=320,  # Back to normal height
            pos_hint={'center_x': 0.5, 'center_y': 0.55}
        )
        self.selection_box.opacity = 0
        self.add_widget(self.selection_box)
        self._decorate_panel(self.selection_box, pad=(12, 12), radius=14)

    def update_hud(self, state: ScreenState, filter_name: str, template_name: str, remaining: int):
        self.hud_text = f"State: {state} • Filter: {filter_name} • Template: {template_name} • Remaining: {max(remaining,0)}"
        self.hud.text = self.hud_text

    def show_countdown(self, n: int):
        self.countdown_value = n
        self.countdown.text = str(n)
        self.countdown.opacity = 1
        # pop animation each tick - back to landscape
        try:
            Animation.cancel_all(self.countdown)
        except Exception:
            pass
        self.countdown.font_size = 180  # Back to normal max size
        Animation(font_size=140, d=0.25, t='out_quad').start(self.countdown)

    def hide_countdown(self):
        self.countdown.opacity = 0

    def show_quick_texture(self, tex: Texture, seconds: Optional[float] = 1.2):
        self.quick.texture = tex
        self.quick.opacity = 1
        if seconds:
            Clock.schedule_once(lambda *_: self.hide_quick(), seconds)

    def hide_quick(self):
        self.quick.opacity = 0

    # Overlay helpers
    def set_status(self, camera_label: str, printer_label: str):
        self.status.text = f"Camera: {camera_label}    Printer: {printer_label}    Settings (O)"

    def set_overlay(self, title: str = "", subtitle: str = "", footer: str = "", visible: bool = True):
        self.title.text = title
        self.subtitle.text = subtitle
        self.footer.text = footer
        self.title.opacity = 1 if visible and title else 0
        self.subtitle.opacity = 1 if visible and subtitle else 0
        self.footer.opacity = 1 if visible and footer else 0

    def show_selection(self, thumbs: List[Texture], cursor_index: int, selected_indices: List[int], template_slots: int):
        """Display photos in grid layout with numbered selection markers"""
        from kivy.uix.image import Image as KImg
        from kivy.uix.floatlayout import FloatLayout
        from kivy.uix.label import Label
        from kivy.graphics import Color, Rectangle, Line
        
        self.selection_box.clear_widgets()
        
        # Determine grid layout based on template slots (photos to choose from = slots + 2)
        total_photos = len(thumbs)
        if template_slots == 1:
            # 1 slot template → 3 photos in 1 row
            cols, rows = 3, 1
        elif template_slots == 2:
            # 2 slot template → 4 photos in 2 rows (2x2 grid)
            cols, rows = 2, 2
        elif template_slots == 4:
            # 4 slot template → 6 photos in 3 rows (2x3 grid)
            cols, rows = 2, 3
        else:
            # Fallback: square-ish grid
            cols = min(3, total_photos)
            rows = (total_photos + cols - 1) // cols
        
        # Calculate spacing and sizing for grid
        spacing = 20
        thumb_w = (self.selection_box.width - spacing * (cols + 1)) / cols
        thumb_h = (self.selection_box.height - spacing * (rows + 1)) / rows
        
        for i, tex in enumerate(thumbs):
            # Calculate grid position
            col = i % cols
            row = i // cols
            x = spacing + col * (thumb_w + spacing)
            y = self.selection_box.height - spacing - (row + 1) * (thumb_h + spacing)
            
            # Container for photo + marker
            container = FloatLayout(size_hint=(None, None), size=(thumb_w, thumb_h), pos=(x, y))
            
            # Photo thumbnail - make cursor photo bigger to show selection
            img = KImg(texture=tex, allow_stretch=True, keep_ratio=True)
            
            # Make current cursor photo bigger to show selection
            if i == cursor_index:
                img.size_hint = (1.2, 1.2)  # 20% bigger
                img.pos_hint = {'center_x': 0.5, 'center_y': 0.5}
            else:
                img.size_hint = (1, 1)
                img.pos_hint = {'x': 0, 'y': 0}
            
            # Dim unselected when selections made
            if selected_indices and (i not in selected_indices):
                img.color = (0.5, 0.5, 0.5, 0.7)
            
            container.add_widget(img)
            
            # Add border if selected (instead of green circle)
            if i in selected_indices:
                with container.canvas.after:
                    Color(1, 1, 1, 1)  # White border for selected
                    border_line = Line(rectangle=(0, 0, thumb_w, thumb_h), width=5)
                # Bind to update border position when container moves
                def update_border(instance, *args):
                    border_line.rectangle = (0, 0, instance.width, instance.height)
                container.bind(pos=update_border, size=update_border)
            
            self.selection_box.add_widget(container)
        
        self.selection_box.opacity = 1

    def hide_selection(self):
        self.selection_box.opacity = 0

    # Styling helpers
    def _decorate_panel(self, widget, pad=(10, 8), radius=RADIUS, bg_rgba=PANEL_BG, border_rgba=PANEL_BORDER):
        # Draw rounded translucent panel behind widget and keep it synced
        with widget.canvas.before:
            Color(*bg_rgba)
            widget._bg = RoundedRectangle(radius=[radius], pos=(widget.x - pad[0], widget.y - pad[1]), size=(widget.width + pad[0]*2, widget.height + pad[1]*2))
            Color(*border_rgba)
            widget._border = RoundedRectangle(radius=[radius], pos=(widget.x - pad[0], widget.y - pad[1]), size=(widget.width + pad[0]*2, widget.height + pad[1]*2))

        def _sync(*_):
            x = widget.x - pad[0]
            y = widget.y - pad[1]
            w = widget.width + pad[0]*2
            h = widget.height + pad[1]*2
            widget._bg.pos = (x, y)
            widget._bg.size = (w, h)
            widget._border.pos = (x, y)
            widget._border.size = (w, h)

        widget.bind(pos=_sync, size=_sync)

    # Layout computation for vertical screen with banner
    def _compute_layout(self):
        """Compute layout for vertical screen (1440x2560) with A4 area + FILMOLA banner"""
        Ww, Wh = Window.size
        banner_h = int(Wh * BANNER_HEIGHT_RATIO)
        
        # Banner at bottom
        self.banner.height = banner_h
        self.banner.pos = (0, 0)
        
        # A4 area above banner
        a4_area_h = Wh - banner_h
        a4_aspect = A4_W / A4_H  # 0.707
        
        # Scale A4 to fit optimally
        a4_w_by_width = Ww
        a4_h_by_width = int(a4_w_by_width / a4_aspect)
        
        a4_h_by_height = a4_area_h
        a4_w_by_height = int(a4_h_by_height * a4_aspect)
        
        if a4_h_by_width <= a4_area_h:
            a4_w, a4_h = a4_w_by_width, a4_h_by_width
        else:
            a4_w, a4_h = a4_w_by_height, a4_h_by_height
        
        # Center A4 area
        a4_x = int((Ww - a4_w) / 2)
        a4_y = banner_h + int((a4_area_h - a4_h) / 2)
        
        self._a4_rect = (a4_x, a4_y, a4_w, a4_h)
        print(f"[DEBUG] Layout: A4=({a4_x},{a4_y},{a4_w}x{a4_h}), Banner={banner_h}px")
        
        # Position A4 background
        self.a4_bg.pos = (a4_x, a4_y)
        self.a4_bg.size = (a4_w, a4_h)
        
        # Blur backdrop covers entire screen
        self.blur_bg.pos = (0, 0)
        self.blur_bg.size = (Ww, Wh)
        
        # Default preview to A4 area
        self.preview.pos = (a4_x, a4_y)
        self.preview.size = (a4_w, a4_h)

    def map_rect_pct_to_screen(self, leftPct: float, topPct: float, widthPct: float, heightPct: float) -> Tuple[int,int,int,int]:
        """Convert template percentage coordinates to screen pixels"""
        a4_x, a4_y, a4_w, a4_h = self._a4_rect
        x = a4_x + int(a4_w * (leftPct/100.0))
        y = a4_y + int(a4_h * (topPct/100.0))
        w = int(a4_w * (widthPct/100.0))
        h = int(a4_h * (heightPct/100.0))
        return x, y, w, h

    def position_preview_in_rect(self, rect_pct: dict):
        """Position camera preview within template rect coordinates"""
        x, y, w, h = self.map_rect_pct_to_screen(
            rect_pct.get('leftPct',2.6), rect_pct.get('topPct',67),
            rect_pct.get('widthPct',45), rect_pct.get('heightPct',30))
        self.preview.pos = (x, y)
        self.preview.size = (w, h)
        self.preview.opacity = 1
        print(f"[DEBUG] Preview positioned: ({x},{y}) {w}x{h}")

    def set_a4_background_path(self, path: Optional[str]):
        """Set the A4 background image from template path"""
        try:
            full = None
            if path:
                p = Path(path)
                full = p if p.is_absolute() else (Path(__file__).parent / p)
            if full and full.exists():
                img = Image.open(str(full)).convert('RGB')
                tex = Texture.create(size=img.size, colorfmt='rgb')
                tex.blit_buffer(img.tobytes(), colorfmt='rgb', bufferfmt='ubyte')
                tex.flip_vertical()
                self.a4_bg.texture = tex
                self.a4_bg.opacity = 1
                print(f"[DEBUG] A4 background set: {path}")
            else:
                self.a4_bg.opacity = 0
        except Exception as e:
            print(f"[DEBUG] Error setting A4 background: {e}")
            self.a4_bg.opacity = 0

    # Backdrop helpers for selection phase
    def show_dim(self, alpha=0.45):
        """Show dim overlay"""
        self.dim.opacity = 1
        self._dim_color.rgba = (0, 0, 0, alpha)

    def hide_dim(self):
        """Hide dim overlay"""
        self.dim.opacity = 0

    def show_blur_background(self):
        """Show blurred background covering entire screen for selection phase"""
        try:
            from PIL import ImageFilter
            from kivy.core.window import Window
            
            # Create a screenshot-like blur of the entire screen
            # Use the A4 background texture if available, otherwise create a solid color
            if self.a4_bg.texture:
                # Use the template background and extend it to full screen
                tex = self.a4_bg.texture
                w, h = tex.size
                buf = tex.pixels
                img = Image.frombytes('RGB', (w,h), buf, 'raw', 'RGB', 0, 1).transpose(Image.FLIP_TOP_BOTTOM)
            else:
                # Create a solid dark background
                screen_w, screen_h = Window.size
                img = Image.new('RGB', (screen_w, screen_h), (40, 40, 40))  # Dark gray
            
            # Create full-screen blur texture
            screen_w, screen_h = Window.size
            
            # Resize template to screen size and blur
            screen_img = img.resize((screen_w, screen_h), Image.BILINEAR)
            small = screen_img.resize((max(1, screen_w//4), max(1, screen_h//4)), Image.BILINEAR)
            blurred_small = small.filter(ImageFilter.GaussianBlur(12))  # Stronger blur
            blur = blurred_small.resize((screen_w, screen_h), Image.BILINEAR)
            
            # Create texture for full screen
            out = Texture.create(size=(screen_w, screen_h), colorfmt='rgb')
            out.blit_buffer(blur.transpose(Image.FLIP_TOP_BOTTOM).tobytes(), colorfmt='rgb', bufferfmt='ubyte')
            out.flip_vertical()
            
            self.blur_bg.texture = out
            self.blur_bg.opacity = 1
            print("[DEBUG] Full-screen blur background shown")
        except Exception as e:
            print(f"[DEBUG] Error showing blur: {e}")
            # Fallback to dim overlay
            self.show_dim(0.6)

    def hide_blur(self):
        """Hide blur background"""
        self.blur_bg.opacity = 0


class PhotoboothApp(App):
    def build(self):
        # Set window size for vertical screen (1440x2560)
        print(f"[DEBUG] Setting window size to {SCREEN_W}x{SCREEN_H}")
        if platform.system() == 'Darwin':
            Window.size = (SCREEN_W, SCREEN_H)  # Vertical screen for photobooth
            Window.show_cursor = True
        else:
            Window.fullscreen = True
            Window.size = (SCREEN_W, SCREEN_H)  # Vertical screen for photobooth
            try:
                Window.show_cursor = False
            except Exception:
                pass

        self.state: ScreenState = ScreenState.ATTRACT
        self.last_input_ts = time.time()
        self.templates = self._load_templates()
        self.template_index = 0
        self.current_template = self.templates[self.template_index]
        
        # Current display size (changes based on template)
        self.current_display_w, self.current_display_h = self._get_template_display_size(self.current_template)
        self.filter_index = 0
        self.filter_name = FILTERS[self.filter_index]

        self.to_take = 0
        self.taken_count = 0
        self.captures: List[Path] = []
        self.selected_indices: List[int] = []
        self.selection_cursor = 0
        self.last_composed_path: Optional[Path] = None

        self.printer_name = ""
        self._load_printer_name()

        # Initialize camera (Picamera2 on Pi, OpenCV on Mac)
        print("[DEBUG] Initializing camera...")
        self._init_camera()

        self.root_widget = PhotoboothRoot()
        self._update_hud()
        # Initial status + attract overlay
        cam_label = "Pi Camera" if HAS_PICAMERA else ("FaceTime HD Camera" if not HAS_PICAMERA else "Camera")
        self.root_widget.set_status(cam_label, self.printer_name or "-")
        print(f"[DEBUG] Camera initialized: {cam_label}")
        print(f"[DEBUG] Printer configured: {self.printer_name or 'None'}")
        self._show_attract()

        Clock.schedule_interval(self._update_preview, 1 / 60.0)  # 60 FPS
        Clock.schedule_interval(self._check_inactivity, 1.0)
        # Clock.schedule_interval(self._check_gpio_status, 5.0)  # Comment out GPIO status check

        # Enable keyboard controls for development (especially on Mac)
        self._bind_keys_for_dev()
        
        # Only setup GPIO on Raspberry Pi
        if HAS_GPIO:
            self._setup_gpio()

        return self.root_widget

    def _get_template_display_size(self, template):
        """Calculate display size based on template slots"""
        slots = template.get("slots", 1)
        
        if slots == 1:
            # Single photo - full size
            return TEMPLATE_DISPLAY_W, TEMPLATE_DISPLAY_H
        elif slots == 2:
            # Two photos - half height each
            return TEMPLATE_DISPLAY_W, TEMPLATE_DISPLAY_H
        elif slots == 3:
            # Three photos - third height each
            return TEMPLATE_DISPLAY_W, TEMPLATE_DISPLAY_H
        else:
            # Default to full size
            return TEMPLATE_DISPLAY_W, TEMPLATE_DISPLAY_H

    def _load_templates(self):
        try:
            tpls = json.loads(TEMPLATES_PATH.read_text())
        except Exception:
            tpls = [{"id": "single_full", "name": "Single Full", "slots": 1,
                     "rects": [{"leftPct": 10, "topPct": 15, "widthPct": 80, "heightPct": 70}]}]
        # If only one template exists, add a couple of built-ins so Left/Right works during dev
        if isinstance(tpls, list) and len(tpls) <= 1:
            tpls = tpls + [
                {"id": "two_stack", "name": "Two Vertical", "slots": 2,
                 "rects": [
                     {"leftPct": 10, "topPct": 8, "widthPct": 80, "heightPct": 42},
                     {"leftPct": 10, "topPct": 50, "widthPct": 80, "heightPct": 42}
                 ]},
                {"id": "three_strip", "name": "Three Strip", "slots": 3,
                 "rects": [
                     {"leftPct": 20, "topPct": 8, "widthPct": 60, "heightPct": 28},
                     {"leftPct": 20, "topPct": 36, "widthPct": 60, "heightPct": 28},
                     {"leftPct": 20, "topPct": 64, "widthPct": 60, "heightPct": 28}
                 ]}
            ]
        return tpls

    def _init_camera(self):
        """Initialize camera - Picamera2 on Pi, OpenCV on Mac"""
        if HAS_PICAMERA:
            self.use_opencv = False
            self.picam = Picamera2()
            
            # Create configurations with proper buffer management - Camera Module 3 optimized with rotation
            # Use smaller resolution for preview to increase frame rate
            self.video_config = self.picam.create_preview_configuration(
                main={"size": (CAMERA_VIDEO_W, CAMERA_VIDEO_H), "format": "RGB888"},  # Use lower resolution for faster preview
                transform=Transform(hflip=1, vflip=0, rotation=0),  # No rotation - we'll handle it in software
                buffer_count=8,  # Increase buffer count for smoother preview
            )
            self.still_config = self.picam.create_still_configuration(
                main={"size": (CAMERA_STILL_W, CAMERA_STILL_H), "format": "RGB888"},  # 4056x3040 for Camera Module 3
                transform=Transform(hflip=1, vflip=0, rotation=0),  # No rotation - we'll handle it in software
                buffer_count=2,  # Add explicit buffer count
            )
            
            # Add some camera tuning for better stability
            self.picam.set_controls({"ExposureTime": 10000, "AnalogueGain": 1.0})
            
            try:
                self.picam.configure(self.video_config)
                self.picam.start()
                print("✓ Using Picamera2 (Raspberry Pi)")
            except Exception as e:
                print(f"Warning: Picamera2 initialization failed: {e}")
                raise RuntimeError("Picamera2 initialization failed")
        elif HAS_OPENCV:
            self.use_opencv = True
            self.cap = cv2.VideoCapture(0)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            print("✓ Using OpenCV (MacBook camera)")
        else:
            raise RuntimeError("No camera backend available. Install picamera2 (Pi) or opencv-python (Mac)")

    def _update_preview(self, *_):
        try:
            if self.use_opencv:
                ret, frame = self.cap.read()
                if ret:
                    # Convert BGR to RGB and flip horizontally for mirror effect
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    frame = cv2.flip(frame, 1)
                    self.root_widget.preview.show_frame(frame)
            else:
                # Use try-catch for Picamera2 to handle buffer errors gracefully
                try:
                    # Initialize frame counter if not exists
                    if not hasattr(self, '_frame_count'):
                        self._frame_count = 0
                    self._frame_count += 1
                    
                    # Step 1: Capture full resolution image
                    if self._frame_count == 1:
                        print(f"[DEBUG] Step 1: Capturing frame from camera...")
                    full_frame = self.picam.capture_array("main")
                    print(f"[DEBUG] Capture completed, frame is {'None' if full_frame is None else 'valid'}")
                    if full_frame is None or full_frame.size == 0:
                        print("[DEBUG] Invalid frame received")
                        return
                    
                    # Step 2: Debug frame info
                    
                    if self._frame_count == 1 or self._frame_count % 100 == 0:  # Print less frequently
                        print(f"[DEBUG] Step 2: Full frame shape: {full_frame.shape}, dtype: {full_frame.dtype}")
                        print(f"[DEBUG] Step 2: Min: {full_frame.min()}, Max: {full_frame.max()}, Size: {full_frame.size}")
                    

                    # Calculate A4 portrait crop area (center crop)
                    # crop_w = 1833  # Crop 60% of width
                    # crop_h = 2592  # Crop 80% of height
                    # start_x = 1388
                    # start_y = 0
                    
                    # # Crop the center area
                    # cropped_frame = full_frame[start_y:start_y+crop_h, start_x:start_x+crop_w]
                    
                    # # Resize to display size (1080x1920 for portrait)
                    # display_frame = cv2.resize(cropped_frame, (1833, 2592))
                    
                    # # Rotate 90 degrees for portrait display
                    # rotated_frame = cv2.rotate(display_frame, cv2.ROTATE_90_CLOCKWISE)
                    
                    # # Fix color channel swapping for preview only (RGB to BGR)
                    # rotated_frame = rotated_frame[:, :, ::-1]  # Reverse RGB to BGR for display
                    
                    # Step 3: Rotate frame first
                    # if self._frame_count == 1:
                    #     print(f"[DEBUG] Step 3: Rotating frame 90 degrees...")
                    # # rotated_frame = cv2.rotate(full_frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
                    
                    # if self._frame_count == 1:
                    #     print(f"[DEBUG] Step 3: Rotated shape: {rotated_frame.shape}")

                    # # Step 4: Crop center area (100% width, 40% height) - no resize
                    # if self._frame_count == 1:
                    #     print(f"[DEBUG] Step 4: Cropping center area...")
                    # h, w = rotated_frame.shape[:2]
                    # crop_w = int(w)  # Crop 100% of width
                    # crop_h = int(h * 0.4)  # Crop 40% of height
                    # start_x = (w - crop_w) // 2
                    # start_y = (h - crop_h) // 2
                    
                    # cropped_frame = rotated_frame[start_y:start_y+crop_h, start_x:start_x+crop_w]
                    
                    
                    # if self._frame_count == 1:
                    #     print(f"[DEBUG] Step 4: Cropped shape: {cropped_frame.shape}")

                    # # Step 5: No resize - use cropped frame directly
                    # if self._frame_count == 1:
                    #     print(f"[DEBUG] Step 5: No resize - using cropped frame directly")
                    preview_frame = full_frame
                    
                    if self._frame_count == 1:
                        print(f"[DEBUG] Step 5: Preview shape: {preview_frame.shape}, dtype: {preview_frame.dtype}")
                        print(f"[DEBUG] Step 5: Preview min: {preview_frame.min()}, max: {preview_frame.max()}")
                    
                    # Step 6: Fix color channel swapping for preview (RGB to BGR)
                    if self._frame_count == 1:
                        print(f"[DEBUG] Step 6: Converting RGB to BGR for display...")
                    preview_frame = preview_frame[:, :, ::-1]  # Reverse RGB to BGR for display
                    
                    # Step 7: Display frame
                    if self._frame_count == 1:
                        print(f"[DEBUG] Step 7: Sending frame to preview widget...")
                    self.root_widget.preview.show_frame(preview_frame)
                    
                    if self._frame_count == 1:
                        print(f"[DEBUG] Step 7: Frame sent successfully!")
                except Exception as e:
                    # Show the error that occurred
                    print(f"[DEBUG] Picamera2 error: {e}")
                    # If capture fails, try to restart the camera
                    if "Failed to queue buffer" in str(e) or "Input/output error" in str(e):
                        try:
                            print("Camera buffer error detected, attempting restart...")
                            self.picam.stop()
                            time.sleep(0.1)  # Brief pause
                            self.picam.start()
                        except Exception as restart_e:
                            print(f"Camera restart failed: {restart_e}")
                            # Switch to OpenCV fallback if available
                            if HAS_OPENCV and not self.use_opencv:
                                print("Switching to OpenCV fallback...")
                                self.use_opencv = True
                                self.cap = cv2.VideoCapture(0)
                                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        except Exception as e:
            # Show any preview errors for debugging
            print(f"[DEBUG] Preview error: {e}")
            import traceback
            traceback.print_exc()
            pass

    def _setup_gpio(self):
        if not HAS_GPIO:
            print("[DEBUG] GPIO not available, skipping GPIO setup")
            return
        try:
            print("[DEBUG] Setting up GPIO buttons...")
            
            # # Close existing buttons if they exist
            # if hasattr(self, 'btn_next') and self.btn_next:
            #     self.btn_next.close()
            # if hasattr(self, 'btn_prev') and self.btn_prev:
            #     self.btn_prev.close()
            # if hasattr(self, 'btn_shutter') and self.btn_shutter:
            #     self.btn_shutter.close()
            # if hasattr(self, 'btn_enter') and self.btn_enter:
            #     self.btn_enter.close()

            self.btn_next = GpioButton(GPIO_NEXT, pull_up=True, bounce_time=0.05)
            self.btn_prev = GpioButton(GPIO_PREV, pull_up=True, bounce_time=0.05)
            self.btn_shutter = GpioButton(GPIO_SHUTTER, pull_up=True, bounce_time=0.05)
            self.btn_enter = GpioButton(GPIO_ENTER, hold_time=3.0, pull_up=True, bounce_time=0.05)

            # Bind events with debug output - use Clock.schedule_once to avoid thread issues
            self.btn_next.when_pressed = lambda: Clock.schedule_once(lambda dt: (print("[DEBUG] GPIO Next pressed"), self._on_input("next")), 0)
            self.btn_prev.when_pressed = lambda: Clock.schedule_once(lambda dt: (print("[DEBUG] GPIO Prev pressed"), self._on_input("prev")), 0)
            self.btn_shutter.when_pressed = lambda: Clock.schedule_once(lambda dt: (print("[DEBUG] GPIO Shutter pressed"), self._on_input("shutter")), 0)
            self.btn_enter.when_pressed = lambda: Clock.schedule_once(lambda dt: (print("[DEBUG] GPIO Enter pressed"), self._on_input("enter")), 0)
            self.btn_enter.when_held = lambda: Clock.schedule_once(lambda dt: (print("[DEBUG] GPIO Enter held (cancel)"), self._on_input("cancel")), 0)
            
            print(f"[DEBUG] GPIO buttons configured: Next={GPIO_NEXT}, Prev={GPIO_PREV}, Shutter={GPIO_SHUTTER}, Enter={GPIO_ENTER}")
        except Exception as e:
            print(f"[DEBUG] GPIO setup failed: {e}")
            pass

    def _test_gpio_buttons(self):
        """Test if GPIO buttons are still responsive"""
        try:
            # Simple test - check if button objects exist and have callbacks
            return (hasattr(self, 'btn_next') and hasattr(self.btn_next, 'when_pressed') and
                    hasattr(self, 'btn_prev') and hasattr(self.btn_prev, 'when_pressed') and
                    hasattr(self, 'btn_shutter') and hasattr(self.btn_shutter, 'when_pressed') and
                    hasattr(self, 'btn_enter') and hasattr(self.btn_enter, 'when_pressed'))
        except Exception:
            return False

    def _bind_keys_for_dev(self):
        def on_key(window, key, scancode, codepoint, modifier):
            if key == ord('o'):
                self._open_settings()
                return True
            if key == ord('p'):
                self._print()
                return True
            if key == ord('s'):
                self._start_session()
                return True
            if key == 32:
                self._on_input("shutter")
                return True
            if key in (276, 65361):
                self._on_input("prev")
                return True
            if key in (275, 65363):
                self._on_input("next")
                return True
            if key in (65293, 13):
                self._on_input("enter")
                return True
            if key in (27,):  # ESC to cancel
                self._on_input("cancel")
                return True
            return False
        Window.bind(on_key_down=on_key)

    def _on_input(self, action: str):
        self.last_input_ts = time.time()
        print(f"[DEBUG] Button pressed: {action}")  # Add debug output
        
        # # Check if GPIO buttons are still working
        # if HAS_GPIO and hasattr(self, 'btn_next'):
        #     try:
        #         # Test if GPIO buttons are still responsive
        #         if not hasattr(self, '_gpio_last_check'):
        #             self._gpio_last_check = time.time()
        #         elif time.time() - self._gpio_last_check > 5:  # Check every 5 seconds
        #             self._gpio_last_check = time.time()
        #             # Re-setup GPIO if needed
        #             if not self._test_gpio_buttons():
        #                 print("[DEBUG] GPIO buttons not responding, re-setting up...")
        #                 self._setup_gpio()
        #     except Exception as e:
        #         print(f"[DEBUG] GPIO check failed: {e}")
        #         # Force re-setup GPIO on any error
        #         print("[DEBUG] Force re-setting up GPIO due to error...")
        #         self._setup_gpio()
        
        if action == "cancel":
            print("[DEBUG] Cancelling session...")
            self._cancel_session()
            return

        if self.state == ScreenState.ATTRACT:
            if action in ("shutter", "enter"):
                print("[DEBUG] Starting new session...")
                self._start_session()
            return

        if self.state == ScreenState.TEMPLATE:
            if action == "next":
                print("[DEBUG] Next template")
                self._cycle_template(+1)
            elif action == "prev":
                print("[DEBUG] Previous template")
                self._cycle_template(-1)
            elif action in ("shutter", "enter"):
                print("[DEBUG] Starting countdown...")
                self._begin_countdown()
            return

        if self.state == ScreenState.COUNTDOWN:
            if action in ("shutter", "enter"):
                print("[DEBUG] Instant capture!")
                # Stop countdown timer
                try:
                    Clock.unschedule(self.count_ev)
                except Exception:
                    pass
                self.root_widget.hide_countdown()
                self._capture_now()
            elif action in ("next", "prev"):
                print("[DEBUG] Template changes disabled during countdown")
                return  # Ignore template changes during countdown
            elif action == "cancel":
                print("[DEBUG] Cancelling session...")
                self._cancel()
            return

        if self.state == ScreenState.QUICK_REVIEW:
            print("[DEBUG] In quick review state - no action")
            return

        if self.state == ScreenState.SELECTION:
            if action == "next":
                print("[DEBUG] Selection cursor next")
                self.selection_cursor = min(len(self.captures) - 1, self.selection_cursor + 1)
                self._update_selection_hint()
            elif action == "prev":
                print("[DEBUG] Selection cursor previous")
                self.selection_cursor = max(0, self.selection_cursor - 1)
                self._update_selection_hint()
            elif action == "shutter":
                if self.selection_cursor in self.selected_indices:
                    print(f"[DEBUG] Deselecting photo {self.selection_cursor}")
                    self.selected_indices.remove(self.selection_cursor)
                else:
                    if len(self.selected_indices) < self.current_template["slots"]:
                        print(f"[DEBUG] Selecting photo {self.selection_cursor}")
                        self.selected_indices.append(self.selection_cursor)
                self._update_selection_hint()
            elif action == "enter":
                print(f"[DEBUG] Proceeding with {len(self.selected_indices)} selected photos")
                # proceed when enough selected; otherwise ignore
                if len(self.selected_indices) >= self.current_template["slots"]:
                    self._compose_and_show()
                    self.state = ScreenState.REVIEW
                    # # Re-setup GPIO when entering review
                    # if HAS_GPIO:
                    #     print("[DEBUG] Re-setting up GPIO for review...")
                    #     self._setup_gpio()
                    self._update_hud()
            return

        if self.state == ScreenState.REVIEW:
            if action == "next":
                print("[DEBUG] Next filter")
                self._cycle_filter(+1)
            elif action == "prev":
                print("[DEBUG] Previous filter")
                self._cycle_filter(-1)
            elif action in ("shutter", "enter"):
                print("[DEBUG] Printing photo...")
                self._print()
            return

    # def _check_gpio_status(self, *_):
    #     """Check GPIO status periodically and re-setup if needed"""
    #     if not HAS_GPIO:
    #         return
    #     try:
    #         if not self._test_gpio_buttons():
    #             print("[DEBUG] GPIO buttons not responding, re-setting up...")
    #             self._setup_gpio()
    #     except Exception as e:
    #         print(f"[DEBUG] GPIO status check failed: {e}")

    def _check_inactivity(self, *_):
        if self.state != ScreenState.ATTRACT and (time.time() - self.last_input_ts) > INACTIVITY_SECONDS:
            print(f"[DEBUG] Inactivity timeout ({INACTIVITY_SECONDS}s), cancelling session")
            self._cancel_session()

    def _start_session(self):
        print("[DEBUG] Starting new photobooth session")
        self.captures.clear()
        self.selected_indices.clear()
        self.taken_count = 0
        self.to_take = self.current_template["slots"] + 2
        self.state = ScreenState.TEMPLATE
        print(f"[DEBUG] State changed to: {self.state}")
        # # Re-setup GPIO when starting session
        # if HAS_GPIO:
        #     print("[DEBUG] Re-setting up GPIO for new session...")
        #     self._setup_gpio()
        self._update_hud()
        self._show_template()

    def _cycle_template(self, delta: int):
        if not self.templates:
            return
        old_index = self.template_index
        self.template_index = (self.template_index + delta) % len(self.templates)
        self.current_template = self.templates[self.template_index]
        
        # Update display size based on new template
        self.current_display_w, self.current_display_h = self._get_template_display_size(self.current_template)
        
        print(f"[DEBUG] Template changed from {old_index} to {self.template_index}: {self.current_template['name']}")
        print(f"[DEBUG] Display size updated to: {self.current_display_w}x{self.current_display_h}")
        # Update toTake following N+2 rule (1->3, 2->4, 3->5)
        self.to_take = self.current_template["slots"] + 2
        self.taken_count = 0
        self._update_hud(to_take=self.to_take)
        # refresh overlays per state
        if self.state == ScreenState.TEMPLATE:
            self._show_template()
        elif self.state == ScreenState.COUNTDOWN:
            self._show_template()

    def _cycle_filter(self, delta: int):
        old_filter = self.filter_name
        self.filter_index = (self.filter_index + delta) % len(FILTERS)
        self.filter_name = FILTERS[self.filter_index]
        print(f"[DEBUG] Filter changed from {old_filter} to {self.filter_name}")
        self._update_hud()
        if self.state == ScreenState.REVIEW and self.last_composed_path:
            self._compose_and_show()

    def _begin_countdown(self):
        print("[DEBUG] Starting countdown...")
        self.state = ScreenState.COUNTDOWN
        print(f"[DEBUG] State changed to: {self.state}")
        
        # Restore template background and camera visibility after quick review
        self.root_widget.a4_bg.opacity = 1
        self.root_widget.preview.opacity = 1
        self._update_hud()
        
        # Show countdown UI with camera preview visible (like attract phase)
        self._show_countdown_ui()
        
        self.count_val = COUNTDOWN_SECONDS
        self.root_widget.show_countdown(self.count_val)
        self.count_ev = Clock.schedule_interval(self._countdown_tick, 1.0)

    def _countdown_tick(self, dt):
        self.count_val -= 1
        print(f"[DEBUG] Countdown: {self.count_val}")
        if self.count_val <= 0:
            Clock.unschedule(self.count_ev)
            self.root_widget.hide_countdown()
            self._capture_now()
        else:
            self.root_widget.show_countdown(self.count_val)

    def _capture_now(self):
        print(f"[DEBUG] Capturing photo {len(self.captures) + 1}...")
        self.state = ScreenState.CAPTURING
        self._update_hud()

        ts = time.strftime("%Y/%m/%d/%H%M%S")
        out_path = PHOTO_DIR / f"{ts}_{len(self.captures)+1}.jpg"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if self.use_opencv:
            # ... (ส่วน OpenCV เหมือนเดิม) ...
            ret, frame = self.cap.read()
            if ret:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frame = cv2.flip(frame, 1)
                img = Image.fromarray(frame)
                img.save(out_path, "JPEG", quality=95)
        else:
                img = Image.new("RGB", (A4_W, A4_H), (128, 128, 128))
                img.save(out_path, "JPEG", quality=95)
                # Step 1: Capture image at CAMERA_STILL_W x CAMERA_STILL_H size
                print(f"[DEBUG CAPTURE] Step 1: Capturing image at {CAMERA_STILL_W}x{CAMERA_STILL_H}...")
                # Capture using still config (this will temporarily switch to still mode)
                arr = self.picam.switch_mode_and_capture_array(self.still_config)
                print(f"[DEBUG CAPTURE] Step 1: Captured shape: {arr.shape}, dtype: {arr.dtype}")
                print(f"[DEBUG CAPTURE] Step 1: Min: {arr.min()}, Max: {arr.max()}")
                
                # Step 2: Rotate image 90 degrees counterclockwise
                # print(f"[DEBUG CAPTURE] Step 2: Rotating 90 degrees...")
                # rotated_arr = cv2.rotate(arr, cv2.ROTATE_90_COUNTERCLOCKWISE)
                # print(f"[DEBUG CAPTURE] Step 2: Rotated shape: {rotated_arr.shape}")

                # # Step 3: Crop center area (100% width, 40% height) - no resize
                # print(f"[DEBUG CAPTURE] Step 3: Cropping center area...")
                # h, w = rotated_arr.shape[:2]
                # crop_w = int(w)  # Crop 100% of width
                # crop_h = int(h * 0.4)  # Crop 40% of height
                # start_x = (w - crop_w) // 2
                # start_y = (h - crop_h) // 2
                
                # cropped_arr = rotated_arr[start_y:start_y+crop_h, start_x:start_x+crop_w]
                # print(f"[DEBUG CAPTURE] Step 3: Cropped shape: {cropped_arr.shape}")

                # Step 3: Use full image (no cropping, no resizing)
                print(f"[DEBUG CAPTURE] Step 3: Using full captured image...")
                captured_arr = arr
                print(f"[DEBUG CAPTURE] Step 3: Final shape: {captured_arr.shape}")
                
                # Step 4: Fix color channel swapping for capture (RGB to BGR)
                print(f"[DEBUG CAPTURE] Step 4: Converting RGB to BGR for capture...")
                captured_arr = captured_arr[:, :, ::-1]  # Reverse RGB to BGR for capture
                
                # Step 5: Save image with lower quality to reduce file size
                print(f"[DEBUG CAPTURE] Step 5: Saving to {out_path}...")
                img = Image.fromarray(captured_arr, mode="RGB")
                img.save(out_path, "JPEG", quality=75)  # Reduced quality for smaller file size
                print(f"[DEBUG CAPTURE] Step 5: Saved successfully!")
                
                # Step 6: Switch back to video config for continued preview
                self.picam.switch_mode(self.video_config)
                print(f"[DEBUG CAPTURE] Step 6: Switched back to video config")

        self.captures.append(out_path)
        if not hasattr(self, 'taken_count'):
            self.taken_count = 0
        self.taken_count += 1
        print(f"[DEBUG] Photo saved: {out_path}")
        print(f"[DEBUG] Progress: {self.taken_count}/{self.to_take} photos taken")

        # Show captured photo for 3 seconds with black background
        self.state = ScreenState.QUICK_REVIEW
        
        # Hide template background and camera for black background
        self.root_widget.a4_bg.opacity = 0
        self.root_widget.preview.opacity = 0
        
        try:
            img = Image.open(out_path).convert("RGB")
            kv_tex = Texture.create(size=img.size, colorfmt="rgb")
            kv_tex.blit_buffer(img.tobytes(), colorfmt="rgb", bufferfmt="ubyte")
            kv_tex.flip_vertical()
            self.root_widget.show_quick_texture(kv_tex, seconds=3.0)
            print(f"[DEBUG] Displaying captured photo for 3 seconds with black background...")
        except Exception as e:
            print(f"[DEBUG] Error showing quick review: {e}")

        # After 3 seconds, proceed to next photo or selection
        if self.taken_count >= self.to_take:
            print("[DEBUG] All photos taken, moving to selection phase...")
            def go_selection(*_):
                self.state = ScreenState.SELECTION
                print(f"[DEBUG] State changed to: {self.state}")
                self.selection_cursor = 0
                self.selected_indices = []
                self._update_selection_hint()
                self._show_selection_ui()
                self._update_hud()
            Clock.schedule_once(go_selection, 3.0)
        else:
            print("[DEBUG] More photos needed, starting next countdown after review...")
            def next_photo(*_):
                self._begin_countdown()
            Clock.schedule_once(next_photo, 3.0)

    def _update_selection_hint(self):
        n = self.current_template["slots"]
        cursor = self.selection_cursor + 1
        selected = len(self.selected_indices)
        self.root_widget.hud.text = f"Selection: choose {n} • cursor {cursor}/{len(self.captures)} • selected {selected}/{n}"
        
        # Build thumbnail textures for selection UI
        thumbs: List[Texture] = []
        for p in self.captures:
            try:
                img = Image.open(p).convert("RGB")
                # make thumb
                tw, th = 480, 320
                scale = min(tw / img.width, th / img.height)
                nw, nh = int(img.width * scale), int(img.height * scale)
                thumb = img.resize((nw, nh), Image.LANCZOS)
                tex = Texture.create(size=thumb.size, colorfmt='rgb')
                tex.blit_buffer(thumb.tobytes(), colorfmt='rgb', bufferfmt='ubyte')
                tex.flip_vertical()
                thumbs.append(tex)
            except Exception:
                continue
        
        # Show selection grid with blur background
        self.root_widget.show_selection(thumbs, self.selection_cursor, self.selected_indices, n)

    def _compose_and_show(self):
        print(f"[DEBUG] Composing image with {len(self.selected_indices)} photos")
        paths = [self.captures[i] for i in self.selected_indices]
        composed = self._compose(paths, self.filter_name, self.current_template)
        self.last_composed_path = composed
        print(f"[DEBUG] Composed image saved: {composed}")
        try:
            img = Image.open(composed).convert("RGB")
            kv_tex = Texture.create(size=img.size, colorfmt="rgb")
            kv_tex.blit_buffer(img.tobytes(), colorfmt="rgb", bufferfmt="ubyte")
            kv_tex.flip_vertical()
            # Show composed template as A4 background (not as overlay)
            self.root_widget.a4_bg.texture = kv_tex
            self.root_widget.a4_bg.opacity = 1
            print("[DEBUG] Composed template set as background")
            self._show_review()
            # hide selection UI and preview overlay
            self.root_widget.hide_selection()
            self.root_widget.hide_quick()
        except Exception as e:
            print(f"[DEBUG] Error showing composed: {e}")

    def _make_vintage_newspaper(self, img: Image.Image, intensity: str = 'medium') -> Image.Image:
        """Apply vintage newspaper effect to photo for 1974 newspaper authenticity"""
        from PIL import ImageEnhance, ImageFilter
        
        # 1. Convert to grayscale (newspapers were mostly B&W in 1974)
        img = ImageOps.grayscale(img).convert('RGB')
        
        # 2. Add slight sepia/aged paper tone
        img = ImageOps.colorize(ImageOps.grayscale(img), 
                                black="#1a1410",  # Dark brownish-black
                                white="#e8dcc8")  # Aged paper white
        
        # 3. Reduce contrast (faded newspaper print look)
        contrast = ImageEnhance.Contrast(img)
        img = contrast.enhance(0.75)
        
        # 4. Add film grain/noise for authenticity
        if HAS_OPENCV:
            arr = np.array(img, dtype=np.float32)
            grain_intensity = 15 if intensity == 'light' else 25 if intensity == 'medium' else 35
            grain = np.random.normal(0, grain_intensity, arr.shape)
            arr = np.clip(arr + grain, 0, 255)
            img = Image.fromarray(arr.astype(np.uint8))
        
        # 5. Slight blur (old lens/printing effect)
        img = img.filter(ImageFilter.GaussianBlur(radius=0.3))
        
        # 6. Slight brightness reduction (aged look)
        brightness = ImageEnhance.Brightness(img)
        img = brightness.enhance(0.92)
        
        return img

    def _compose(self, selected_paths: List[Path], filt: str, tpl: dict) -> Path:
        W, H = A4_W, A4_H
        
        # Load background template if available
        background_path = tpl.get("background")
        if background_path and Path(background_path).exists():
            try:
                canvas = Image.open(background_path).convert("RGB")
                # Ensure it's the right size
                if canvas.size != (W, H):
                    canvas = canvas.resize((W, H), Image.LANCZOS)
            except Exception as e:
                print(f"[DEBUG] Failed to load background {background_path}: {e}")
                canvas = Image.new("RGB", (W, H), (34, 34, 34))
        else:
            # Default solid color background
            canvas = Image.new("RGB", (W, H), (34, 34, 34))

        def to_rect(r: dict) -> Tuple[int, int, int, int]:
            x = int((r["leftPct"] / 100) * W)
            y = int((r["topPct"] / 100) * H)
            w = int((r["widthPct"] / 100) * W)
            h = int((r["heightPct"] / 100) * H)
            return x, y, w, h

        # Check if template has vintage effect enabled
        apply_vintage = tpl.get("vintage_effect", False)
        
        rects = [to_rect(r) for r in tpl.get("rects", [])]
        for i, p in enumerate(selected_paths):
            if i >= len(rects):
                break
            try:
                img = Image.open(p).convert("RGB")
            except Exception:
                continue
            
            # Apply vintage newspaper effect if template requires it
            if apply_vintage:
                img = self._make_vintage_newspaper(img, intensity='medium')
            
            # Apply user-selected filter to photo only (not template background)
            if filt == "black_white":
                img = ImageOps.grayscale(img).convert("RGB")
            elif filt == "sepia":
                g = ImageOps.colorize(ImageOps.grayscale(img), black="#2e1f0f", white="#f4e1c1")
                img = g.convert("RGB")
            elif filt == "newspaper":
                img = self._make_vintage_newspaper(img, intensity='medium')
            
            # Resize and paste onto canvas
            x, y, w, h = rects[i]
            scale = max(w / img.width, h / img.height)
            nw, nh = int(img.width * scale), int(img.height * scale)
            resized = img.resize((nw, nh), Image.LANCZOS)
            dx = x + (w - nw) // 2
            dy = y + (h - nh) // 2
            canvas.paste(resized, (dx, dy))

        ts = time.strftime("%Y/%m/%d/%H%M%S")
        out_path = PHOTO_DIR / f"A4_{ts}.jpg"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, format="JPEG", quality=95)
        return out_path

    def _print(self):
        """Print with slide-down animation"""
        if not self.last_composed_path:
            print("[DEBUG] No composed image to print")
            return
        
        print(f"[DEBUG] Starting print with slide-down animation...")
        
        # Animate composed image sliding down
        self._animate_print_slidedown()
        
        # After animation completes, send to printer
        def send_to_printer(*_):
            print(f"[DEBUG] Printing image: {self.last_composed_path}")
            self.root_widget.set_overlay(title="Printing...", subtitle="Sending job to printer", footer="", visible=True)
            self.root_widget.hide_selection()
            
            args = ["lp"]
            if self.printer_name:
                args += ["-d", self.printer_name]
            args += ["-o", "media=A4.Borderless", "-o", "fit-to-page=false", str(self.last_composed_path)]
            print(f"[DEBUG] Print command: {' '.join(args)}")
            proc = subprocess.run(args, capture_output=True)
            if proc.returncode != 0:
                err = proc.stderr.decode('utf-8', 'ignore')
                print(f"[DEBUG] Print failed: {err}")
                self.root_widget.set_overlay(title="Print failed", subtitle=err[:120], footer="Press Cancel to retry", visible=True)
            else:
                print("[DEBUG] Print job sent successfully")
                self.root_widget.set_overlay(title="Printed!", subtitle="Job sent successfully", footer="", visible=True)
                # Return to attract after successful print
                Clock.schedule_once(lambda dt: self._cancel(), 3.0)
        
        Clock.schedule_once(send_to_printer, 2.0)  # Wait for animation
    
    def _animate_print_slidedown(self):
        """Animate the composed template sliding down like paper from printer"""
        from kivy.animation import Animation
        
        # Animate the A4 background (composed template) sliding down
        a4_bg = self.root_widget.a4_bg
        
        if a4_bg.opacity == 0:
            return  # No image to animate
        
        # Store original position
        original_y = a4_bg.y
        
        # Slide down animation (2 seconds) - slide completely off screen
        print("[DEBUG] Animating template slide-down...")
        target_y = -a4_bg.height  # Slide completely off bottom
        anim = Animation(y=target_y, duration=5.0, t='in_out_quad')
        
        def on_complete(*_):
            # Reset position and hide after animation
            a4_bg.y = original_y
            a4_bg.opacity = 0
            print("[DEBUG] Slide-down animation complete")
        
        anim.bind(on_complete=on_complete)
        anim.start(a4_bg)

    def _open_settings(self):
        SettingsModal(self.printer_name, on_save=self._save_printer).open()

    def _save_printer(self, name: str):
        self.printer_name = name or ""
        try:
            (PHOTO_DIR / "printer.json").write_text(json.dumps({"printer": self.printer_name}))
        except Exception:
            pass

    def _load_printer_name(self):
        try:
            data = json.loads((PHOTO_DIR / "printer.json").read_text())
            self.printer_name = data.get("printer", "")
        except Exception:
            self.printer_name = ""

    def _cancel_session(self):
        print("[DEBUG] Cancelling photobooth session")
        
        # Stop countdown timer if it's running
        if hasattr(self, 'count_ev'):
            try:
                Clock.unschedule(self.count_ev)
                print("[DEBUG] Countdown timer stopped")
            except Exception:
                pass
        
        # Hide countdown display
        self.root_widget.hide_countdown()
        
        self.state = ScreenState.ATTRACT
        print(f"[DEBUG] State changed to: {self.state}")
        # # Re-setup GPIO when cancelling session
        # if HAS_GPIO:
        #     print("[DEBUG] Re-setting up GPIO for attract mode...")
        #     self._setup_gpio()
        self.captures.clear()
        self.selected_indices.clear()
        self.taken_count = 0
        self.to_take = 0
        self.last_composed_path = None
        self._update_hud()
        self._show_attract()

    def _update_hud(self, to_take: Optional[int] = None):
        if to_take is not None:
            self.to_take = to_take
        remaining = (self.to_take - self.taken_count) if self.state != ScreenState.ATTRACT else 0
        self.root_widget.update_hud(self.state, self.filter_name,
                                    self.current_template.get("name", "Template"),
                                    remaining)

    # ---------- UI overlay convenience ----------
    def _show_attract(self):
        """Attract phase: Show template1 background with camera preview in rect position"""
        try:
            # Use template1 for attract phase
            template1 = next((t for t in self.templates if t.get("id") == "template1"), None)
            if not template1:
                template1 = self.templates[0] if self.templates else self.current_template
            
            # Set template1 as A4 background
            bg_path = template1.get("background")
            print(f"[DEBUG] Attract: template1 background={bg_path}")
            self.root_widget.set_a4_background_path(bg_path)
            
            # Position camera preview in template1's rect
            rects = template1.get("rects", [])
            if rects:
                print(f"[DEBUG] Attract: Positioning preview in rect={rects[0]}")
                self.root_widget.position_preview_in_rect(rects[0])
            
            self.root_widget.preview.opacity = 1
        except Exception as e:
            print(f"[DEBUG] Error in attract setup: {e}")
            import traceback
            traceback.print_exc()

        self.root_widget.set_overlay(
            title="Pay attendant to start!",
            subtitle="Press Shutter button to begin",
            footer="",
            visible=True,
        )
        self.root_widget.hide_selection()
        self.root_widget.hide_quick()
        self.root_widget.hide_dim()
        self.root_widget.hide_blur()

    def _show_template(self):
        """Template selection: Show full A4 template, no preview"""
        n = self.current_template["slots"]
        
        # Show current template as full A4 background
        bg_path = self.current_template.get("background")
        print(f"[DEBUG] Template select: {self.current_template.get('name')} bg={bg_path}")
        self.root_widget.set_a4_background_path(bg_path)
        
        # Hide camera preview during template selection
        self.root_widget.preview.opacity = 0
        
        self.root_widget.set_overlay(
            title=f"Template: {self.current_template.get('name', 'Unknown')}",
            subtitle=f"Prev/Next to change • {n+2} photos will be taken",
            footer="Press Shutter to confirm",
            visible=True,
        )
        self.root_widget.hide_selection()
        self.root_widget.hide_quick()
        self.root_widget.hide_dim()
        self.root_widget.hide_blur()

    def _show_countdown_ui(self):
        """Countdown phase: Show template1 background with camera preview (like attract)"""
        try:
            # Use template1 for countdown phase (same as attract)
            template1 = next((t for t in self.templates if t.get("id") == "template1"), None)
            if not template1:
                template1 = self.templates[0] if self.templates else self.current_template
            
            # Set template1 as A4 background
            bg_path = template1.get("background")
            print(f"[DEBUG] Countdown: template1 background={bg_path}")
            self.root_widget.set_a4_background_path(bg_path)
            
            # Position camera preview in template1's rect (same as attract)
            rects = template1.get("rects", [])
            if rects:
                print(f"[DEBUG] Countdown: Positioning preview in rect={rects[0]}")
                self.root_widget.position_preview_in_rect(rects[0])
            
            # Ensure preview is visible so customer can see their face
            self.root_widget.preview.opacity = 1
        except Exception as e:
            print(f"[DEBUG] Error in countdown UI setup: {e}")
            import traceback
            traceback.print_exc()
        
        # Clear text overlays (countdown number will be shown instead)
        self.root_widget.set_overlay("", "", "")
        self.root_widget.hide_selection()
        self.root_widget.hide_quick()
        self.root_widget.hide_dim()
        self.root_widget.hide_blur()

    def _show_selection_ui(self):
        """Selection phase: Show blurred background with photo grid"""
        need = self.current_template["slots"]
        
        # Hide camera preview during selection
        self.root_widget.preview.opacity = 0
        
        # Show blurred background (blurs the current displayed content)
        print("[DEBUG] Selection: Showing blurred background")
        self.root_widget.show_blur_background()
        
        # Show dim overlay for better contrast
        self.root_widget.show_dim(alpha=0.5)
        
        self.root_widget.set_overlay(
            title=f"Choose {need} photo(s)",
            subtitle="Prev/Next to move • Shutter to Select/Deselect",
            footer=f"Selected {len(self.selected_indices)} / {need}",
            visible=True,
        )
        self.root_widget.hide_quick()

    def _show_review(self):
        """Filter selection: Show composed template as background with cycling filters"""
        # Hide blur/dim backgrounds and camera to show clean composed template
        self.root_widget.hide_blur()
        self.root_widget.hide_dim()
        self.root_widget.hide_selection()
        self.root_widget.preview.opacity = 0
        
        self.root_widget.set_overlay(
            title=f"Filter: {self.filter_name}",
            subtitle="Prev/Next to change filter",
            footer="Press Enter to print",
            visible=True,
        )

    def on_stop(self):
        """Clean up camera resources when app stops"""
        try:
            if hasattr(self, 'picam') and self.picam:
                self.picam.stop()
                self.picam.close()
        except Exception:
            pass
        try:
            if hasattr(self, 'cap') and self.cap:
                self.cap.release()
        except Exception:
            pass


if __name__ == "__main__":
    if not HAS_PICAMERA and not HAS_OPENCV:
        print("ERROR: No camera backend available!")
        print("  On Raspberry Pi: sudo apt install python3-picamera2")
        print("  On Mac/Dev: pip3 install opencv-python")
        exit(1)
    PhotoboothApp().run()
