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
        import cv2
        import numpy as np
        HAS_OPENCV = True
    except Exception:
        HAS_OPENCV = False

try:
    from gpiozero import Button as GpioButton
    HAS_GPIO = True
except Exception:
    HAS_GPIO = False


PHOTO_DIR = Path(os.environ.get("PHOTOBOOTH_PHOTOS_DIR", str(Path.home() / "photobooth/data/photos")))
TEMPLATES_PATH = Path(os.environ.get("PHOTOBOOTH_TEMPLATES_PATH",
                                     str(Path(__file__).parent / "public/templates/index.json")))
PHOTO_DIR.mkdir(parents=True, exist_ok=True)

A4_W, A4_H = 2480, 3508
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

FILTERS = ["none", "black_white", "sepia"]


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
        self.preview = PreviewWidget()
        self.add_widget(self.preview)

        # HUD with proper positioning (top-left)
        self.hud = Label(
            text="Loading...",
            font_size=18,
            color=(1, 1, 1, 1),
            size_hint=(None, None),
            pos_hint={'x': 0, 'top': 1},
            padding=(10, 10),
            halign='left',
            valign='top'
        )
        self.hud.bind(texture_size=self.hud.setter('size'))
        self.add_widget(self.hud)
        self._decorate_panel(self.hud)

        # Status bar (top-right): camera, printer, settings hint
        self.status = Label(
            text="",
            font_size=16,
            color=(1, 1, 1, 1),
            size_hint=(None, None),
            pos_hint={'right': 1, 'top': 1},
            halign='right',
            valign='top'
        )
        self.status.bind(texture_size=self.status.setter('size'))
        self.add_widget(self.status)
        self._decorate_panel(self.status)

        # Countdown overlay centered
        self.countdown = Label(
            text="",
            font_size=140,
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

        # Titles / instructions overlays
        self.title = Label(
            text="",
            font_size=36,
            bold=True,
            color=(1, 1, 1, 1),
            pos_hint={'center_x': 0.5, 'center_y': 0.74}
        )
        self.title.opacity = 0
        self.add_widget(self.title)

        self.subtitle = Label(
            text="",
            font_size=20,
            color=(1, 1, 1, 1),
            pos_hint={'center_x': 0.5, 'center_y': 0.66}
        )
        self.subtitle.opacity = 0
        self.add_widget(self.subtitle)

        self.footer = Label(
            text="",
            font_size=18,
            color=(1, 1, 1, 1),
            pos_hint={'center_x': 0.5, 'y': 0.02}
        )
        self.footer.opacity = 0
        self.add_widget(self.footer)

        # Selection thumbnails container (created on demand)
        from kivy.uix.boxlayout import BoxLayout as KivyBox
        self.selection_box = KivyBox(
            orientation='horizontal',
            spacing=20,
            size_hint=(0.9, None),
            height=320,
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
        # pop animation each tick
        try:
            Animation.cancel_all(self.countdown)
        except Exception:
            pass
        self.countdown.font_size = 180
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

    def show_selection(self, thumbs: List[Texture], cursor_index: int, selected_indices: List[int]):
        # Rebuild thumbnails each time for simplicity
        from kivy.uix.image import Image as KImg
        self.selection_box.clear_widgets()
        for i, tex in enumerate(thumbs):
            w = KImg(texture=tex, allow_stretch=True, keep_ratio=True)
            # Emphasize cursor by scaling
            w.size_hint = (0.28, 1.0) if i == cursor_index else (0.24, 1.0)
            # Dim unselected when selection made
            if selected_indices and (i not in selected_indices):
                w.color = (1, 1, 1, 0.7)
            self.selection_box.add_widget(w)
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


class PhotoboothApp(App):
    def build(self):
        # On Mac, use windowed mode for testing; on Pi, use fullscreen
        if platform.system() == 'Darwin':
            Window.size = (1280, 720)
            Window.show_cursor = True
        else:
            Window.fullscreen = True
            try:
                Window.show_cursor = False
            except Exception:
                pass

        self.state: ScreenState = ScreenState.ATTRACT
        self.last_input_ts = time.time()
        self.templates = self._load_templates()
        self.template_index = 0
        self.current_template = self.templates[self.template_index]
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

        Clock.schedule_interval(self._update_preview, 1 / 20.0)
        Clock.schedule_interval(self._check_inactivity, 1.0)
        # Clock.schedule_interval(self._check_gpio_status, 5.0)  # Comment out GPIO status check

        # self._bind_keys_for_dev()  # Comment out keyboard controls
        self._setup_gpio()

        return self.root_widget

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
            
            # Create configurations with proper buffer management
            self.video_config = self.picam.create_preview_configuration(
                main={"size": (1280, 720), "format": "RGB888"},
                transform=Transform(hflip=1),
                buffer_count=4,  # Add explicit buffer count
            )
            self.still_config = self.picam.create_still_configuration(
                main={"size": (1920, 1080), "format": "RGB888"},
                transform=Transform(hflip=1),
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
                # Fallback to OpenCV if available
                if HAS_OPENCV:
                    self.use_opencv = True
                    self.cap = cv2.VideoCapture(0)
                    self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                    self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                    print("✓ Fallback to OpenCV")
                else:
                    raise RuntimeError("Picamera2 failed and no OpenCV fallback available")
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
                    frame = self.picam.capture_array("main")
                    # Debug: print frame info
                    if hasattr(self, '_frame_count'):
                        self._frame_count += 1
                    else:
                        self._frame_count = 1
                    if self._frame_count % 30 == 0:  # Print every 30 frames (about once per second)
                        print(f"[DEBUG] Frame shape: {frame.shape}, dtype: {frame.dtype}, min: {frame.min()}, max: {frame.max()}")
                    # Fix color channel swapping for preview only (RGB to BGR)
                    frame = frame[:, :, ::-1]  # Reverse RGB to BGR for display
                    self.root_widget.preview.show_frame(frame)
                except Exception as e:
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
            # Silently handle any other preview errors to prevent crashes
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

    # def _bind_keys_for_dev(self):
    #     def on_key(window, key, scancode, codepoint, modifier):
    #         if key == ord('o'):
    #             self._open_settings()
    #             return True
    #         if key == ord('p'):
    #             self._print()
    #             return True
    #         if key == ord('s'):
    #             self._start_session()
    #             return True
    #         if key == 32:
    #             self._on_input("shutter")
    #             return True
    #         if key in (276, 65361):
    #             self._on_input("prev")
    #             return True
    #         if key in (275, 65363):
    #             self._on_input("next")
    #             return True
    #         if key in (65293, 13):
    #             self._on_input("enter")
    #             return True
    #         if key in (27,):  # ESC to cancel
    #             self._on_input("cancel")
    #             return True
    #         return False
    #     Window.bind(on_key_down=on_key)

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
            if action == "shutter":
                print("[DEBUG] Instant capture!")
                # cancel countdown timer and capture instantly
                try:
                    Clock.unschedule(self.count_ev)
                except Exception:
                    pass
                self.root_widget.hide_countdown()
                self._capture_now()
            elif action in ("next", "prev"):
                print(f"[DEBUG] Template change during countdown: {action}")
                # allow adjusting template during countdown; reset countdown
                try:
                    Clock.unschedule(self.count_ev)
                except Exception:
                    pass
                self.root_widget.hide_countdown()
                if action == "next":
                    self._cycle_template(+1)
                else:
                    self._cycle_template(-1)
                self._begin_countdown()
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
        print(f"[DEBUG] Template changed from {old_index} to {self.template_index}: {self.current_template['name']}")
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
        # # Re-setup GPIO when starting countdown
        # if HAS_GPIO:
        #     print("[DEBUG] Re-setting up GPIO for countdown...")
        #     self._setup_gpio()
        self._update_hud()
        self.count_val = COUNTDOWN_SECONDS
        self.root_widget.show_countdown(self.count_val)
        self.root_widget.set_overlay("", "", "")
        self.root_widget.hide_selection()
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
        if self.state not in (ScreenState.COUNTDOWN, ScreenState.TEMPLATE):
            return
        print(f"[DEBUG] Capturing photo {self.taken_count + 1}/{self.to_take}")
        self.state = ScreenState.CAPTURING
        print(f"[DEBUG] State changed to: {self.state}")
        self._update_hud()

        ts = time.strftime("%Y/%m/%d/%H%M%S")
        out_path = PHOTO_DIR / f"{ts}_{len(self.captures)+1}.jpg"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if self.use_opencv:
            # Capture from OpenCV
            ret, frame = self.cap.read()
            if ret:
                # Convert BGR to RGB and flip for mirror effect
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frame = cv2.flip(frame, 1)
                img = Image.fromarray(frame, mode="RGB")
                img.save(out_path, "JPEG", quality=95)
        else:
            # Capture from Picamera2 with better error handling
            try:
                self.picam.switch_mode_and_capture_file(self.still_config, str(out_path))
            except Exception as e:
                print(f"Still capture failed: {e}")
                try:
                    # Fallback to array capture
                    arr = self.picam.capture_array("main")
                    # RGB888 format should be ready to save directly
                    img = Image.fromarray(arr, mode="RGB")
                    img.save(out_path, "JPEG", quality=95)
                except Exception as e2:
                    print(f"Array capture also failed: {e2}")
                    # Create a placeholder image if all else fails
                    img = Image.new("RGB", (1920, 1080), (128, 128, 128))
                    img.save(out_path, "JPEG", quality=95)

        self.captures.append(out_path)
        self.taken_count += 1
        print(f"[DEBUG] Photo saved: {out_path}")
        print(f"[DEBUG] Progress: {self.taken_count}/{self.to_take} photos taken")

        try:
            img = Image.open(out_path).convert("RGB")
            kv_tex = Texture.create(size=img.size, colorfmt="rgb")
            kv_tex.blit_buffer(img.tobytes(), colorfmt="rgb", bufferfmt="ubyte")
            kv_tex.flip_vertical()
            self.root_widget.show_quick_texture(kv_tex, seconds=1.2)
        except Exception:
            pass

        if self.taken_count >= self.current_template["slots"] + 2:
            print("[DEBUG] All photos taken, moving to selection phase...")
            # Short pause before entering selection (to mimic quick review pause)
            def go_selection(*_):
                self.state = ScreenState.SELECTION
                print(f"[DEBUG] State changed to: {self.state}")
                # # Re-setup GPIO when entering selection
                # if HAS_GPIO:
                #     print("[DEBUG] Re-setting up GPIO for selection...")
                #     self._setup_gpio()
                self.selection_cursor = 0
                self.selected_indices = []
                self._update_selection_hint()
                self._show_selection_ui()
                self._update_hud()
            Clock.schedule_once(go_selection, 0.6)
        else:
            print("[DEBUG] More photos needed, starting next countdown...")
            self._begin_countdown()

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
        self.root_widget.show_selection(thumbs, self.selection_cursor, self.selected_indices)

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
            # Keep composed visible during review (no auto-hide)
            self.root_widget.show_quick_texture(kv_tex, seconds=None)
            self._show_review()
            # hide selection UI explicitly when entering review
            self.root_widget.hide_selection()
        except Exception:
            pass

    def _compose(self, selected_paths: List[Path], filt: str, tpl: dict) -> Path:
        W, H = A4_W, A4_H
        canvas = Image.new("RGB", (W, H), (34, 34, 34))

        def to_rect(r: dict) -> Tuple[int, int, int, int]:
            x = int((r["leftPct"] / 100) * W)
            y = int((r["topPct"] / 100) * H)
            w = int((r["widthPct"] / 100) * W)
            h = int((r["heightPct"] / 100) * H)
            return x, y, w, h

        rects = [to_rect(r) for r in tpl.get("rects", [])]
        for i, p in enumerate(selected_paths):
            if i >= len(rects):
                break
            try:
                img = Image.open(p).convert("RGB")
            except Exception:
                continue
            x, y, w, h = rects[i]
            scale = max(w / img.width, h / img.height)
            nw, nh = int(img.width * scale), int(img.height * scale)
            resized = img.resize((nw, nh), Image.LANCZOS)
            dx = x + (w - nw) // 2
            dy = y + (h - nh) // 2
            canvas.paste(resized, (dx, dy))

        if filt == "black_white":
            canvas = ImageOps.grayscale(canvas).convert("RGB")
        elif filt == "sepia":
            g = ImageOps.colorize(ImageOps.grayscale(canvas), black="#2e1f0f", white="#f4e1c1")
            canvas = g.convert("RGB")

        ts = time.strftime("%Y/%m/%d/%H%M%S")
        out_path = PHOTO_DIR / f"A4_{ts}.jpg"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, format="JPEG", quality=95)
        return out_path

    def _print(self):
        if not self.last_composed_path:
            print("[DEBUG] No composed image to print")
            return
        print(f"[DEBUG] Printing image: {self.last_composed_path}")
        # Show printing overlay
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
            self.root_widget.set_overlay(title="Print failed", subtitle=err[:120], footer="Press Space/Enter to retry", visible=True)
        else:
            print("[DEBUG] Print job sent successfully")
            self.root_widget.set_overlay(title="Printed", subtitle="Job sent successfully", footer="", visible=True)

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
        self.root_widget.set_overlay(
            title="Pay attendant to start!",
            subtitle="Press Enter button to begin",
            footer="",
            visible=True,
        )
        self.root_widget.hide_selection()
        self.root_widget.hide_quick()

    def _show_template(self):
        n = self.current_template["slots"]
        self.root_widget.set_overlay(
            title="Select your template",
            subtitle=f"Use Prev/Next buttons to change. Photos to take: {n+2}",
            footer="Press Shutter button to start",
            visible=True,
        )
        self.root_widget.hide_selection()
        self.root_widget.hide_quick()

    def _show_selection_ui(self):
        need = self.current_template["slots"]
        self.root_widget.set_overlay(
            title=f"Choose {need} photo(s)",
            subtitle="Prev/Next to move • Shutter to Select/Deselect",
            footer=f"Selected {len(self.selected_indices)} / {need}",
            visible=True,
        )
        self.root_widget.hide_quick()

    def _show_review(self):
        self.root_widget.set_overlay(
            title="Review",
            subtitle="Prev/Next to change filter",
            footer="Press Shutter button to print",
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
