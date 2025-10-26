# Python Photobooth (Raspberry Pi)

A minimal Flask app that provides:

- Live preview via MJPEG stream using rpicam-vid
- High-resolution capture via rpicam-still
- A4 composition with filters (none, black_white, sepia) via Pillow
- Printing via CUPS (lp)

## Prerequisites (Pi)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip rpicam-apps cups libcups2-dev
```

## Setup

```bash
cd python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
source .venv/bin/activate
export PHOTOBOOTH_PHOTOS_DIR="$HOME/photobooth/data/photos"
export PHOTOBOOTH_TEMPLATES_PATH="$(pwd)/public/templates/index.json"
python app.py
# open http://127.0.0.1:8000
```

## Keyboard (in UI)

- S: start session (template screen)
- Space: take photo (countdown → capture)
- P: print (on review)
- J/L: change template (preview only)
- A/D: change filter (review)

## Notes

- Ensure camera is enabled and working:
  - `rpicam-still -o test.jpg`
- Ensure printing queue works:
  - `lpinfo -v` and `lpadmin -p PhotoPrinter -E -v <URI> -m everywhere`
  - `lpoptions -p PhotoPrinter -o media=A4.Borderless -o fit-to-page=false`


