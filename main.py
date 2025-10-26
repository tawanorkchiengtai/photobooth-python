import os
import io
import json
import time
import subprocess
from enum import Enum
from pathlib import Path
from typing import List, Tuple, Optional

from PIL import Image, ImageOps

from kivy.config import Config
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

from picamera2 import Picamera2
from libcamera import Transform

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

        self.hud = Label(text="", pos=(12, Window.height - 40), font_size=16, color=(1, 1, 1, 1))
        self.add_widget(self.hud)

        self.countdown = Label(text="", font_size=140, bold=True, color=(1, 1, 1, 1))
        self.add_widget(self.countdown)
        self.countdown.opacity = 0

        self.quick = KivyImage(size_hint=(0.8, 0.8), allow_stretch=True, keep_ratio=True)
        self.add_widget(self.quick)
        self.quick.opacity = 0

    def update_hud(self, state: ScreenState, filter_name: str, template_name: str, remaining: int):
        self.hud_text = f"State: {state} • Filter: {filter_name} • Template: {template_name} • Remaining: {max(remaining,0)}"
        self.hud.text = self.hud_text

    def show_countdown(self, n: int):
        self.countdown_value = n
        self.countdown.text = str(n)
        self.countdown.opacity = 1

    def hide_countdown(self):
        self.countdown.opacity = 0

    def show_quick_texture(self, tex: Texture, seconds: float = 1.2):
        self.quick.texture = tex
        self.quick.opacity = 1
        Clock.schedule_once(lambda *_: self.hide_quick(), seconds)

    def hide_quick(self):
        self.quick.opacity = 0


class PhotoboothApp(App):
    def build(self):
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

        self.picam = Picamera2()
        self.video_config = self.picam.create_preview_configuration(
            main={"size": (1280, 720), "format": "RGB888"},
            transform=Transform(hflip=1),
        )
        self.still_config = self.picam.create_still_configuration(
            main={"size": (1920, 1080), "format": "RGB888"},
            transform=Transform(hflip=1),
        )
        self.picam.configure(self.video_config)
        self.picam.start()

        self.root_widget = PhotoboothRoot()
        self._update_hud()

        Clock.schedule_interval(self._update_preview, 1 / 20.0)
        Clock.schedule_interval(self._check_inactivity, 1.0)

        self._bind_keys_for_dev()
        self._setup_gpio()

        return self.root_widget

    def _load_templates(self):
        try:
            return json.loads(TEMPLATES_PATH.read_text())
        except Exception:
            return [{"id": "single_full", "name": "Single Full", "slots": 1,
                     "rects": [{"leftPct": 10, "topPct": 15, "widthPct": 80, "heightPct": 70}]}]

    def _update_preview(self, *_):
        try:
            frame = self.picam.capture_array("main")
            self.root_widget.preview.show_frame(frame)
        except Exception:
            pass

    def _setup_gpio(self):
        if not HAS_GPIO:
            return
        try:
            self.btn_next = GpioButton(GPIO_NEXT, pull_up=True, bounce_time=0.05)
            self.btn_prev = GpioButton(GPIO_PREV, pull_up=True, bounce_time=0.05)
            self.btn_shutter = GpioButton(GPIO_SHUTTER, pull_up=True, bounce_time=0.05)
            self.btn_enter = GpioButton(GPIO_ENTER, hold_time=3.0, pull_up=True, bounce_time=0.05)

            self.btn_next.when_pressed = lambda: self._on_input("next")
            self.btn_prev.when_pressed = lambda: self._on_input("prev")
            self.btn_shutter.when_pressed = lambda: self._on_input("shutter")
            self.btn_enter.when_pressed = lambda: self._on_input("enter")
            self.btn_enter.when_held = lambda: self._on_input("cancel")
        except Exception:
            pass

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
            if key in (65293,):
                self._on_input("enter")
                return True
            return False
        Window.bind(on_key_down=on_key)

    def _on_input(self, action: str):
        self.last_input_ts = time.time()
        if action == "cancel":
            self._cancel_session()
            return

        if self.state == ScreenState.ATTRACT:
            if action in ("shutter", "enter"):
                self._start_session()
            return

        if self.state == ScreenState.TEMPLATE:
            if action == "next":
                self._cycle_template(+1)
            elif action == "prev":
                self._cycle_template(-1)
            elif action in ("shutter", "enter"):
                self._begin_countdown()
            return

        if self.state == ScreenState.COUNTDOWN:
            if action == "shutter":
                self._capture_now()
            return

        if self.state == ScreenState.QUICK_REVIEW:
            return

        if self.state == ScreenState.SELECTION:
            if action == "next":
                self.selection_cursor = min(len(self.captures) - 1, self.selection_cursor + 1)
                self._update_selection_hint()
            elif action == "prev":
                self.selection_cursor = max(0, self.selection_cursor - 1)
                self._update_selection_hint()
            elif action == "shutter":
                if self.selection_cursor in self.selected_indices:
                    self.selected_indices.remove(self.selection_cursor)
                else:
                    if len(self.selected_indices) < self.current_template["slots"]:
                        self.selected_indices.append(self.selection_cursor)
                self._update_selection_hint()
            elif action == "enter":
                if len(self.selected_indices) == self.current_template["slots"]:
                    self._compose_and_show()
                    self.state = ScreenState.REVIEW
                    self._update_hud()
            return

        if self.state == ScreenState.REVIEW:
            if action == "next":
                self._cycle_filter(+1)
            elif action == "prev":
                self._cycle_filter(-1)
            elif action in ("shutter", "enter"):
                self._print()
            return

    def _check_inactivity(self, *_):
        if self.state != ScreenState.ATTRACT and (time.time() - self.last_input_ts) > INACTIVITY_SECONDS:
            self._cancel_session()

    def _start_session(self):
        self.captures.clear()
        self.selected_indices.clear()
        self.taken_count = 0
        self.to_take = self.current_template["slots"] + 2
        self.state = ScreenState.TEMPLATE
        self._update_hud()

    def _cycle_template(self, delta: int):
        self.template_index = (self.template_index + delta) % len(self.templates)
        self.current_template = self.templates[self.template_index]
        self._update_hud(to_take=self.current_template["slots"] + 2)

    def _cycle_filter(self, delta: int):
        self.filter_index = (self.filter_index + delta) % len(FILTERS)
        self.filter_name = FILTERS[self.filter_index]
        self._update_hud()
        if self.state == ScreenState.REVIEW and self.last_composed_path:
            self._compose_and_show()

    def _begin_countdown(self):
        self.state = ScreenState.COUNTDOWN
        self._update_hud()
        self.count_val = COUNTDOWN_SECONDS
        self.root_widget.show_countdown(self.count_val)
        self.count_ev = Clock.schedule_interval(self._countdown_tick, 1.0)

    def _countdown_tick(self, dt):
        self.count_val -= 1
        if self.count_val <= 0:
            Clock.unschedule(self.count_ev)
            self.root_widget.hide_countdown()
            self._capture_now()
        else:
            self.root_widget.show_countdown(self.count_val)

    def _capture_now(self):
        if self.state not in (ScreenState.COUNTDOWN, ScreenState.TEMPLATE):
            return
        self.state = ScreenState.CAPTURING
        self._update_hud()

        ts = time.strftime("%Y/%m/%d/%H%M%S")
        out_path = PHOTO_DIR / f"{ts}_{len(self.captures)+1}.jpg"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self.picam.switch_mode_and_capture_file(self.still_config, str(out_path))
        except Exception:
            arr = self.picam.capture_array("main")
            img = Image.fromarray(arr, mode="RGB")
            img.save(out_path, "JPEG", quality=95)

        self.captures.append(out_path)
        self.taken_count += 1

        try:
            img = Image.open(out_path).convert("RGB")
            kv_tex = Texture.create(size=img.size, colorfmt="rgb")
            kv_tex.blit_buffer(img.tobytes(), colorfmt="rgb", bufferfmt="ubyte")
            kv_tex.flip_vertical()
            self.root_widget.show_quick_texture(kv_tex, seconds=1.2)
        except Exception:
            pass

        if self.taken_count >= self.current_template["slots"] + 2:
            self.state = ScreenState.SELECTION
            self.selection_cursor = 0
            self.selected_indices = []
            self._update_selection_hint()
            self._update_hud()
        else:
            self._begin_countdown()

    def _update_selection_hint(self):
        n = self.current_template["slots"]
        cursor = self.selection_cursor + 1
        selected = len(self.selected_indices)
        self.root_widget.hud.text = f"Selection: choose {n} • cursor {cursor}/{len(self.captures)} • selected {selected}/{n}"

    def _compose_and_show(self):
        paths = [self.captures[i] for i in self.selected_indices]
        composed = self._compose(paths, self.filter_name, self.current_template)
        self.last_composed_path = composed
        try:
            img = Image.open(composed).convert("RGB")
            kv_tex = Texture.create(size=img.size, colorfmt="rgb")
            kv_tex.blit_buffer(img.tobytes(), colorfmt="rgb", bufferfmt="ubyte")
            kv_tex.flip_vertical()
            self.root_widget.show_quick_texture(kv_tex, seconds=2.0)
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
            return
        args = ["lp"]
        if self.printer_name:
            args += ["-d", self.printer_name]
        args += ["-o", "media=A4.Borderless", "-o", "fit-to-page=false", str(self.last_composed_path)]
        subprocess.run(args)

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
        self.state = ScreenState.ATTRACT
        self.captures.clear()
        self.selected_indices.clear()
        self.taken_count = 0
        self.to_take = 0
        self.last_composed_path = None
        self._update_hud()

    def _update_hud(self, to_take: Optional[int] = None):
        if to_take is not None:
            self.to_take = to_take
        remaining = (self.to_take - self.taken_count) if self.state != ScreenState.ATTRACT else 0
        self.root_widget.update_hud(self.state, self.filter_name,
                                    self.current_template.get("name", "Template"),
                                    remaining)


if __name__ == "__main__":
    PhotoboothApp().run()

