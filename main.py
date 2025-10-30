import os
import io
import json
import time
import subprocess
import threading
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
CAMERA_STILL_W, CAMERA_STILL_H = 3372, 2250  # Max resolution for Camera Module 3
CAMERA_VIDEO_W, CAMERA_VIDEO_H = 1686, 1125  # Use lower resolution for faster preview
PREVIEW_W, PREVIEW_H = 1080, 1920  # Preview display size (portrait)

# UI Layout for vertical screen (1440x2560)
SCREEN_W, SCREEN_H = 1440, 2560  # Vertical screen dimensions
BANNER_HEIGHT_RATIO = 0.2047  # 20.47% of screen for FILMOLA banner (~205px)
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

# Toggle HUD visibility for customer-facing mode
SHOW_HUD = False

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
        
        # Store custom camera position for window resize
        self._custom_camera_pos = None  # (x_offset, y_offset, width, height)
        
        # A4 background image (template background)
        self.a4_bg = KivyImage(allow_stretch=True, keep_ratio=False, opacity=1, size_hint=(None, None))
        self.add_widget(self.a4_bg)
        
        # Blur backdrop for selection phase
        self.blur_bg = KivyImage(allow_stretch=True, keep_ratio=True, opacity=0, size_hint=(None, None))
        self.add_widget(self.blur_bg)
        # Cache for blurred background per screen size
        self._blur_cache = None  # (screen_w, screen_h, Texture)
        
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
        
        # Persistent FILMOLA banner at bottom with enhanced styling
        self.banner = BoxLayout(orientation='horizontal', size_hint=(1, None), padding=(22, 10), spacing=18)
        # Decorative background + accent line
        with self.banner.canvas.before:
            Color(0.06, 0.06, 0.08, 0.98)
            self._banner_bg_rect = Rectangle(pos=self.banner.pos, size=self.banner.size)
        with self.banner.canvas.after:
            # Accent top line and soft highlight
            Color(ACCENT[0], ACCENT[1], ACCENT[2], 0.55)
            self._banner_topline = Rectangle(pos=(self.banner.x, self.banner.y + self.banner.height - 2), size=(self.banner.width, 2))
            Color(1, 1, 1, 0.06)
            self._banner_highlight = Rectangle(pos=(self.banner.x, self.banner.y + self.banner.height - 8), size=(self.banner.width, 6))
        def _sync_banner(*_):
            # Only update decorative shapes if they still exist
            try:
                if hasattr(self, '_banner_bg_rect') and self._banner_bg_rect is not None:
                    self._banner_bg_rect.pos = self.banner.pos
                    self._banner_bg_rect.size = self.banner.size
                x, y = self.banner.pos
                w, h = self.banner.size
                if hasattr(self, '_banner_topline') and self._banner_topline is not None:
                    self._banner_topline.pos = (x, y + h - 2)
                    self._banner_topline.size = (w, 2)
                if hasattr(self, '_banner_highlight') and self._banner_highlight is not None:
                    self._banner_highlight.pos = (x, y + h - 8)
                    self._banner_highlight.size = (w, 6)
            except Exception:
                pass
        self.banner.bind(pos=_sync_banner, size=_sync_banner)

        # Left / Center / Right content
        from kivy.uix.boxlayout import BoxLayout as KivyHBox
        left = KivyHBox(orientation='horizontal', size_hint=(0.33, 1), spacing=10)
        center = KivyHBox(orientation='horizontal', size_hint=(0.34, 1))
        right = KivyHBox(orientation='horizontal', size_hint=(0.33, 1))

        self.banner_left = Label(text="FILMOLA", font_size=BANNER_FONT_SIZE, bold=True, color=(1, 1, 1, 1),
                                  halign='left', valign='middle')
        self.banner_left.bind(size=self.banner_left.setter('text_size'))
        left.add_widget(self.banner_left)

        self.banner_center = Label(text="All the Portrait That's Fit to Print", font_size=20, color=(1, 1, 1, 0.78),
                                    halign='center', valign='middle')
        self.banner_center.bind(size=self.banner_center.setter('text_size'))
        center.add_widget(self.banner_center)

        self.banner_right = Label(text="Settings (O)", font_size=18, color=(1, 1, 1, 0.7),
                                   halign='right', valign='middle')
        self.banner_right.bind(size=self.banner_right.setter('text_size'))
        right.add_widget(self.banner_right)

        self.banner.add_widget(left)
        self.banner.add_widget(center)
        self.banner.add_widget(right)
        self.add_widget(self.banner)

        # Try replacing banner content with an image if available
        try:
            self._use_image_banner_if_available()
        except Exception:
            pass
        
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
        if not SHOW_HUD:
            self.hud.opacity = 0
        else:
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

        # Countdown number display - dramatic styling
        self.countdown = Label(
            text="3",
            font_size=280,  # Bigger for more impact
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

        # Titles / instructions overlays - modern styling with backgrounds
        self.title = Label(
            text="",
            font_size=48,  # Larger, bolder title
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
        # Use AnchorLayout to naturally center a GridLayout of thumbnails
        from kivy.uix.anchorlayout import AnchorLayout as KivyAnchor
        self.selection_box = KivyAnchor(
            anchor_x='center', anchor_y='center',
            size_hint=(0.9, None),
            height=420,
            pos_hint={'center_x': 0.5, 'center_y': 0.58}
        )
        self.selection_box.opacity = 0
        self.add_widget(self.selection_box)
        # No panel decoration for selection grid; keep background clean

    def update_hud(self, state: ScreenState, filter_name: str, template_name: str, remaining: int):
        if not SHOW_HUD:
            return
        self.hud_text = f"State: {state} • Filter: {filter_name} • Template: {template_name} • Remaining: {max(remaining,0)}"
        self.hud.text = self.hud_text

    def _use_image_banner_if_available(self):
        """Use a full-bleed banner image if available (no borders/margins)."""
        try:
            # Prefer banner2.jpg, fallback to banner.png
            candidates = [
                Path(__file__).parent / "public/banner.jpg",
                # Path(__file__).parent / "public/banner.png",
            ]
            img_path = next((p for p in candidates if p.exists()), None)
            if not img_path:
                return
            pil = Image.open(str(img_path)).convert('RGB')
            tex = Texture.create(size=pil.size, colorfmt='rgb')
            tex.blit_buffer(pil.tobytes(), colorfmt='rgb', bufferfmt='ubyte')
            tex.flip_vertical()
            # Clear current banner children and add a full-bleed image
            self.banner.clear_widgets()
            # Remove all decorative canvas to avoid borders/lines
            try:
                self.banner.canvas.before.clear()
                self.banner.canvas.after.clear()
            except Exception:
                pass
            # Remove any padding/spacing so image fills exactly
            try:
                self.banner.padding = (0, 0)
                self.banner.spacing = 0
            except Exception:
                pass
            # Add image that stretches to fill banner area
            self.banner_img = KivyImage(texture=tex, allow_stretch=True, keep_ratio=False, size_hint=(1, 1))
            self.banner.add_widget(self.banner_img)
            # Hide any stored refs to previous decorations
            try:
                self._banner_topline = None
                self._banner_highlight = None
                self._banner_bg_rect = None
            except Exception:
                pass
        except Exception as e:
            print(f"[DEBUG] Could not load banner image: {e}")

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
        # Constrain quick preview to the A4 area so it doesn't cover the bottom banner
        try:
            a4_x, a4_y, a4_w, a4_h = self._a4_rect
            # Fit quick image within A4 with some padding
            pad_scale = 0.78
            q_w = int(a4_w * pad_scale)
            q_h = int(a4_h * pad_scale)
            self.quick.size_hint = (None, None)
            self.quick.size = (q_w, q_h)
            # Move the quick preview slightly upward within the A4 area
            x_pos = a4_x + (a4_w - q_w) // 2
            base_y = a4_y + (a4_h - q_h) // 2
            y_offset = int(a4_h * 0.70)  # shift up by 10% of A4 height
            y_pos = min(base_y + y_offset, a4_y + a4_h - q_h)
            self.quick.pos = (x_pos, y_pos)
        except Exception:
            # Fallback to centered sizing
            self.quick.size_hint = (0.8, 0.8)
            self.quick.pos_hint = {'center_x': 0.5, 'center_y': 0.8}
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

    def show_selection(self, thumbs: List[Texture], cursor_index: int, selected_indices: List[int], template_slots: int, animate: bool = True):
        """Display photos in grid layout with numbered selection markers"""
        from kivy.uix.image import Image as KImg
        from kivy.uix.gridlayout import GridLayout
        from kivy.uix.floatlayout import FloatLayout as KivyFloat
        from kivy.uix.label import Label
        from kivy.graphics import Color, Rectangle, Line
        
        self.selection_box.clear_widgets()
        
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
        
        # Dynamically size the selection panel so the grid appears centered and higher on screen
        try:
            if rows == 1:
                self.selection_box.height = 420
            elif rows == 2:
                self.selection_box.height = 620
            else:
                self.selection_box.height = 820
        except Exception:
            pass
        
        # Calculate spacing and sizing for grid, and place a centered GridLayout
        box_w = self.selection_box.width if self.selection_box.width > 0 else Window.width * 0.9
        box_h = self.selection_box.height
        # For 2-column layouts (2x2, 2x3), cluster columns tighter and leave larger side margins
        content_w = box_w * (0.58 if cols == 2 else 0.85)
        spacing_x = 12 if cols == 2 else 20
        spacing_y = 24
        pad_x = 20
        pad_y = 10
        thumb_w = (content_w - (cols - 1) * spacing_x - 2 * pad_x) / max(cols, 1)
        thumb_h = (box_h - (rows - 1) * spacing_y - 2 * pad_y) / max(rows, 1)
        grid_w = cols * thumb_w + (cols - 1) * spacing_x + 2 * pad_x
        grid_h = rows * thumb_h + (rows - 1) * spacing_y + 2 * pad_y
        grid = GridLayout(cols=cols, rows=rows, spacing=(spacing_x, spacing_y), padding=[pad_x, pad_y, pad_x, pad_y],
                          size_hint=(None, None), size=(grid_w, grid_h))
        
        for i, tex in enumerate(thumbs):
            # Each cell contains an image; the current cursor image is enlarged
            cell = KivyFloat(size_hint=(None, None), size=(thumb_w, thumb_h))

            img = KImg(texture=tex, allow_stretch=True, keep_ratio=True)
            img.size_hint = (1, 1)
            img.pos_hint = {'center_x': 0.5, 'center_y': 0.5}

            # Dim non-selected when there are selected items
            if selected_indices and (i not in selected_indices):
                img.color = (0.5, 0.5, 0.5, 0.7)

            # Animate scale for cursor vs others (keeps layout intact)
            if animate:
                try:
                    from kivy.animation import Animation as KAnim
                    target = 1.2 if i == cursor_index else 1.0
                    # start from current to target for smooth transition
                    KAnim(size_hint_x=target, size_hint_y=target, d=0.12, t='out_cubic').start(img)
                except Exception:
                    img.size_hint = (1.2, 1.2) if i == cursor_index else (1, 1)
            else:
                img.size_hint = (1.2, 1.2) if i == cursor_index else (1, 1)

            cell.add_widget(img)
            grid.add_widget(cell)
        self.selection_box.add_widget(grid)
        
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
        
        self.blur_bg.pos = (0, 0)
        self.blur_bg.size = (Ww, Wh)
        # # Blur backdrop and dim overlay must NOT cover the bottom banner area
        # overlay_pos = (0, banner_h)
        # overlay_size = (Ww, Wh - banner_h)
        # self.blur_bg.pos = overlay_pos
        # self.blur_bg.size = overlay_size
        # # Also position the dim widget to exclude the banner
        # try:
        #     self.dim.pos = overlay_pos
        #     self.dim.size = overlay_size
        # except Exception:
        #     pass
        
        # Default preview to A4 area (unless custom position is set)
        if self._custom_camera_pos is None:
            self.preview.pos = (a4_x, a4_y)
            self.preview.size = (a4_w, a4_h)
        else:
            # Re-apply custom camera position after resize
            x_off, y_off, w, h = self._custom_camera_pos
            self.preview.pos = (a4_x + x_off, a4_y + y_off)
            self.preview.size = (w, h)

    def position_camera_simple(self, x_offset: int, y_offset: int, width: int, height: int):
        """Position camera preview with simple pixel values relative to A4 canvas
        
        Args:
            x_offset: pixels from left edge of A4 (0 = left edge)
            y_offset: pixels from bottom edge of A4 (0 = bottom edge)  
            width: camera preview width in pixels
            height: camera preview height in pixels
        """
        # Store custom position so it persists across window resizes
        self._custom_camera_pos = (x_offset, y_offset, width, height)
        
        a4_x, a4_y, a4_w, a4_h = self._a4_rect
        
        # Position relative to A4 canvas
        x = a4_x + x_offset
        y = a4_y + y_offset
        
        self.preview.pos = (x, y)
        self.preview.size = (width, height)
        self.preview.opacity = 1
        print(f"[DEBUG] Camera positioned: ({x},{y}) size:{width}x{height} | A4: ({a4_x},{a4_y}) {a4_w}x{a4_h}")

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
            
            # Use cached blur if screen size hasn't changed
            screen_w, screen_h = Window.size
            if self._blur_cache and self._blur_cache[0:2] == (screen_w, screen_h):
                self.blur_bg.texture = self._blur_cache[2]
                self.blur_bg.opacity = 1
                print("[DEBUG] Using cached full-screen blur background")
                return
            
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
                img = Image.new('RGB', (screen_w, screen_h), (40, 40, 40))  # Dark gray
            
            # Create full-screen blur texture
            
            # Resize template to screen size and blur
            screen_img = img.resize((screen_w, screen_h), Image.BILINEAR)
            # Heavier downsample first for performance on Pi
            small = screen_img.resize((max(1, screen_w//6), max(1, screen_h//6)), Image.BILINEAR)
            blurred_small = small.filter(ImageFilter.GaussianBlur(10))
            blur = blurred_small.resize((screen_w, screen_h), Image.BILINEAR)
            
            # Create texture for full screen
            out = Texture.create(size=(screen_w, screen_h), colorfmt='rgb')
            out.blit_buffer(blur.transpose(Image.FLIP_TOP_BOTTOM).tobytes(), colorfmt='rgb', bufferfmt='ubyte')
            out.flip_vertical()
            
            self.blur_bg.texture = out
            self.blur_bg.opacity = 1
            # Cache the computed blur
            self._blur_cache = (screen_w, screen_h, out)
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
        # Cache thumbnails to avoid reloading/resizing on every selection move (critical on Pi)
        self.thumb_cache: dict[Path, Texture] = {}

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
        # Skip heavy preview work while not needed to keep UI smooth on Pi
        if self.state in (ScreenState.SELECTION, ScreenState.REVIEW, ScreenState.PRINTING):
            return
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
                    rotated_frame = cv2.rotate(full_frame, cv2.ROTATE_180)
                    
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
                    preview_frame = rotated_frame
                    
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
                self._cancel_session()
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
        # Reset thumbnail cache for a fresh capture set
        try:
            self.thumb_cache.clear()
        except Exception:
            pass
        # Always start from template1
        try:
            for i, t in enumerate(self.templates):
                if t.get("id") == "template1":
                    self.template_index = i
                    self.current_template = t
                    self.current_display_w, self.current_display_h = self._get_template_display_size(t)
                    break
        except Exception:
            pass
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
                rotated_arr = cv2.rotate(arr, cv2.ROTATE_180)
                # print(f"[DEBUG CAPTURE] Step 2: Rotated shape: {rotated_arr.shape}")

                # # Step 3: Crop center area (100% width, 40% height) - no resize
                # print(f"[DEBUG CAPTURE] Step 3: Cropping center area...")
                # h, w = rotated_arr.shape[:2]
                # crop_w = int(w)  # Crop 100% of width
                # crop_h = int(h * 0.4)  # Crop 40% of height
                # start_x = (w - crop_w) // 2
                # start_y = (h - crop_h) // 2
                
                cropped_arr = rotated_arr[start_y:start_y+crop_h, start_x:start_x+crop_w]
                # print(f"[DEBUG CAPTURE] Step 3: Cropped shape: {cropped_arr.shape}")

                # Step 3: Use full image (no cropping, no resizing)
                print(f"[DEBUG CAPTURE] Step 3: Using full captured image...")
                captured_arr = cropped_arr
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

        # Show captured photo for 2 seconds with black background
        self.state = ScreenState.QUICK_REVIEW
        
        # Hide template background and camera for black background
        self.root_widget.a4_bg.opacity = 0
        self.root_widget.preview.opacity = 0
        
        try:
            img = Image.open(out_path).convert("RGB")
            kv_tex = Texture.create(size=img.size, colorfmt="rgb")
            kv_tex.blit_buffer(img.tobytes(), colorfmt="rgb", bufferfmt="ubyte")
            kv_tex.flip_vertical()
            self.root_widget.show_quick_texture(kv_tex, seconds=2.0)
            print(f"[DEBUG] Displaying captured photo for 2 seconds with black background...")
        except Exception as e:
            print(f"[DEBUG] Error showing quick review: {e}")

        # After 2 seconds, proceed to next photo or selection (match quick preview duration)
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
            Clock.schedule_once(go_selection, 2.0)
        else:
            print("[DEBUG] More photos needed, starting next countdown after review...")
            def next_photo(*_):
                self._begin_countdown()
            Clock.schedule_once(next_photo, 2.0)

    def _update_selection_hint(self):
        n = self.current_template["slots"]
        cursor = self.selection_cursor + 1
        selected = len(self.selected_indices)
        self.root_widget.hud.text = f"Selection: choose {n} • cursor {cursor}/{len(self.captures)} • selected {selected}/{n}"
        # Update on-screen overlay so count appears under the instruction line
        self.root_widget.set_overlay(
            title=f"Choose {n} photo(s)",
            subtitle=f"Prev/Next to move • Shutter to Select/Deselect\nSelected {selected} / {n}",
            footer="",
            visible=True,
        )
        
        # Build thumbnail textures for selection UI (cached for performance)
        thumbs: List[Texture] = []
        for p in self.captures:
            try:
                tex = self.thumb_cache.get(p)
                if tex is None:
                    img = Image.open(p).convert("RGB")
                    # make thumb smaller with faster filter for Pi
                    tw, th = 360, 240
                    scale = min(tw / img.width, th / img.height)
                    nw, nh = int(img.width * scale), int(img.height * scale)
                    thumb = img.resize((nw, nh), Image.BILINEAR)
                    tex = Texture.create(size=thumb.size, colorfmt='rgb')
                    tex.blit_buffer(thumb.tobytes(), colorfmt='rgb', bufferfmt='ubyte')
                    tex.flip_vertical()
                    self.thumb_cache[p] = tex
                thumbs.append(tex)
            except Exception:
                continue
        
        # Show selection grid with blur background (enable animation on all platforms)
        self.root_widget.show_selection(thumbs, self.selection_cursor, self.selected_indices, n, animate=True)

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
        # Enter PRINTING state to pause preview updates
        self.state = ScreenState.PRINTING
        self._update_hud()
        
        # Animate composed image sliding down
        self._animate_print_slidedown()
        
        # After a short delay, kick off the print in a background thread to avoid blocking UI
        def start_printing(*_):
            self.root_widget.set_overlay(title="Printing...", subtitle="Sending job to printer", footer="", visible=True)
            self.root_widget.hide_selection()
            args = ["lp"]
            if self.printer_name:
                args += ["-d", self.printer_name]
            args += ["-o", "media=A4.Inkjet", "-o", "print-quality=4.5,", str(self.last_composed_path)]
            print(f"[DEBUG] Print command: {' '.join(args)}")

            def run_print():
                print(f"[DEBUG] Printing image: {self.last_composed_path}")
                proc = subprocess.run(args, capture_output=True)
                def after_print(dt=0):
                    if proc.returncode != 0:
                        err = proc.stderr.decode('utf-8', 'ignore')
                        print(f"[DEBUG] Print failed: {err}")
                        self.root_widget.set_overlay(title="Print failed", subtitle=err[:120], footer="Press Cancel to retry", visible=True)
                        # Return to review state so user can retry; resume preview updates
                        self.state = ScreenState.REVIEW
                        self._update_hud()
                    else:
                        print("[DEBUG] Print job sent successfully")
                        self.root_widget.set_overlay(title="Printed!", subtitle="Job sent successfully", footer="", visible=True)
                        # Return to attract after successful print
                        Clock.schedule_once(lambda _dt: self._cancel_session(), 10.0)
                Clock.schedule_once(after_print, 0)

            threading.Thread(target=run_print, daemon=True).start()

        Clock.schedule_once(start_printing, 0.5)
    
    def _animate_print_slidedown(self):
        """Animate the composed template sliding down like paper from printer"""
        from kivy.animation import Animation
        
        # Animate the A4 background (composed template) sliding down
        a4_bg = self.root_widget.a4_bg
        
        if a4_bg.opacity == 0:
            return  # No image to animate
        
        # Store original position
        original_y = a4_bg.y
        
        # Cancel any existing animations on the same widget for smoothness
        try:
            Animation.cancel_all(a4_bg)
        except Exception:
            pass

        # Slide down animation - slide completely off screen
        print("[DEBUG] Animating template slide-down...")
        target_y = -a4_bg.height  # Slide completely off bottom
        anim = Animation(y=target_y, duration=10.0, t='out_cubic')
        
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
        
        # Ensure the background template view is restored (in case slide animation/quick review hid it)
        try:
            from kivy.animation import Animation as KAnim
            try:
                KAnim.cancel_all(self.root_widget.a4_bg)
            except Exception:
                pass
            # Reset A4 background position/size and make it visible again
            a4_x, a4_y, a4_w, a4_h = self.root_widget._a4_rect
            self.root_widget.a4_bg.pos = (a4_x, a4_y)
            self.root_widget.a4_bg.size = (a4_w, a4_h)
            self.root_widget.a4_bg.opacity = 1
        except Exception:
            pass

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
        # Clear cached thumbnails since capture set is discarded
        try:
            self.thumb_cache.clear()
        except Exception:
            pass
        # Reset to template1 for next session
        try:
            for i, t in enumerate(self.templates):
                if t.get("id") == "template1":
                    self.template_index = i
                    self.current_template = t
                    self.current_display_w, self.current_display_h = self._get_template_display_size(t)
                    break
        except Exception:
            pass
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
            
            # Position camera preview - SIMPLE positioning!
            # Adjust these 4 numbers to move/resize the camera:
            #   x_offset: pixels from LEFT edge of template (increase = move right)
            #   y_offset: pixels from BOTTOM edge of template (increase = move up)
            #   width: camera width in pixels
            #   height: camera height in pixels
            self.root_widget.position_camera_simple(
                x_offset=23,   # Position from left
                y_offset=661,   # Position from bottom
                width=1396,      # Camera width
                height=937   # Camera height
            )
            
            self.root_widget.preview.opacity = 1
        except Exception as e:
            print(f"[DEBUG] Error in attract setup: {e}")
            import traceback
            traceback.print_exc()

        # self.root_widget.set_overlay(
        #     title="Pay attendant to start!",
        #     subtitle="Press Shutter button to begin",
        #     footer="",
        #     visible=True,
        # )
        # # Restore default overlay positions/sizes after selection screen
        # try:
        #     self.root_widget.title.font_size = 48
        #     self.root_widget.subtitle.font_size = 20
        #     self.root_widget.title.pos_hint = {'center_x': 0.5, 'center_y': 0.74}
        #     self.root_widget.subtitle.pos_hint = {'center_x': 0.5, 'center_y': 0.66}
        # except Exception:
        #     pass
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
        
        # self.root_widget.set_overlay(
        #     title=f"Template: {self.current_template.get('name', 'Unknown')}",
        #     subtitle=f"Prev/Next to change • {n+2} photos will be taken",
        #     footer="Press Shutter to confirm",
        #     visible=True,
        # )
        # # Restore default overlay positions/sizes after selection screen
        # try:
        #     self.root_widget.title.font_size = 48
        #     self.root_widget.subtitle.font_size = 20
        #     self.root_widget.title.pos_hint = {'center_x': 0.5, 'center_y': 0.74}
        #     self.root_widget.subtitle.pos_hint = {'center_x': 0.5, 'center_y': 0.66}
        # except Exception:
        #     pass
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
            
            # Position camera preview - SIMPLE positioning (same as attract)!
            self.root_widget.position_camera_simple(
                
                x_offset=23,   # Position from left
                y_offset=661,   # Position from bottom
                width=1396,      # Camera width
                height=937   # Camera height
            )
            
            # Ensure preview is visible so customer can see their face
            self.root_widget.preview.opacity = 1
        except Exception as e:
            print(f"[DEBUG] Error in countdown UI setup: {e}")
            import traceback
            traceback.print_exc()
        
        # Clear text overlays (countdown number will be shown instead)
        self.root_widget.set_overlay("", "", "")
        # Restore default overlay positions/sizes after selection screen
        try:
            self.root_widget.title.font_size = 48
            self.root_widget.subtitle.font_size = 20
            self.root_widget.title.pos_hint = {'center_x': 0.5, 'center_y': 0.74}
            self.root_widget.subtitle.pos_hint = {'center_x': 0.5, 'center_y': 0.66}
        except Exception:
            pass
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
        
        selected = len(self.selected_indices)
        self.root_widget.set_overlay(
            title=f"Choose {need} photo(s)",
            subtitle=f"Prev/Next to move • Shutter to Select/Deselect\nSelected {selected} / {need}",
            footer="",
            visible=True,
        )
        # Move overlay to the top and enlarge for better visibility with many thumbnails
        try:
            self.root_widget.title.font_size = 72
            self.root_widget.subtitle.font_size = 24
            self.root_widget.title.pos_hint = {'center_x': 0.5, 'center_y': 0.95}
            self.root_widget.subtitle.pos_hint = {'center_x': 0.5, 'center_y': 0.90}
            # Bring labels to front so they are not covered by the selection panel
            parent = self.root_widget
            try:
                parent.remove_widget(self.root_widget.title)
                parent.add_widget(self.root_widget.title)
                parent.remove_widget(self.root_widget.subtitle)
                parent.add_widget(self.root_widget.subtitle)
            except Exception:
                pass
        except Exception:
            pass
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
            footer="Press Enter to confirm",
            visible=True,
        )
        # Restore default overlay positions/sizes after selection screen
        try:
            self.root_widget.title.font_size = 48
            self.root_widget.subtitle.font_size = 20
            self.root_widget.title.pos_hint = {'center_x': 0.5, 'center_y': 0.74}
            self.root_widget.subtitle.pos_hint = {'center_x': 0.5, 'center_y': 0.66}
        except Exception:
            pass

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
