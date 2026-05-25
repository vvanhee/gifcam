# gifcam

A Raspberry Pi GIF camera based on [Pix-E GIF Camera](https://www.hackster.io/nick-brewer/pix-e-gif-camera-323965) by Nick Brewer ([GitHub](https://github.com/nickbrewer/gifcam)). This is an updated and extended version with improved capture quality, optional Telegram and email delivery, and several new features.

## Hardware

- Raspberry Pi Zero 2 W
- Camera Module 3 (IMX708)
- Momentary push button with integrated LED (shutter button) → GPIO 19 / LED GPIO 21
- Status LED → GPIO 12

## How it works

The camera runs continuously in video mode so that auto-exposure and auto-white-balance are always converged before the shutter is pressed. A short button press records a 2-second H.264 burst at 30 fps using the Pi's hardware VPU encoder. A long press (≥ 3 s) encodes all pending bursts into MP4 and GIF files, then sends them automatically.

### Workflow

| Action | Result |
|--------|--------|
| Short press & release | Record a 2-second H.264 burst; saved to `pending/` |
| Hold ≥ 3 s & release | Encode all pending bursts → MP4 + GIF, then send |

### LED feedback

| State | Button LED | Status LED |
|-------|-----------|-----------|
| Ready | Full on | Off |
| Button held < 3 s | Dim (0.3) | Off |
| Button held ≥ 3 s | Dim (0.3) | Solid (release now) |
| Capturing | Fast blink | Off |
| Encoding / Sending | Off | Slow blink |
| Done | Full on | Off |

## Features vs. the original

- **Continuous video mode** — AE and AWB converge before capture; no cold-start flicker between frames.
- **Hardware H.264 encoding** — uses the Pi's VPU; zero CPU overhead during recording.
- **AE + AWB lock** — all frames share identical exposure and colour balance.
- **Rebound (ping-pong) loop** — when `REBOUND = True`, both the GIF *and* MP4 play A→B→A for a smooth loop.
- **Two-pass GIF** — `palettegen` + `paletteuse` with Bayer dithering for maximum colour fidelity.
- **Flat pending storage** — each burst is a single timestamped `.h264` file in `pending/`; no subdirectories.
- **Telegram delivery** — sends the MP4 via `sendVideo` to a configured bot/chat.
- **Email delivery** — sends the GIF as an attachment via SMTP (Gmail App Passwords supported).

## Configuration

### Telegram

Create `/home/pi/gifcam/.telegram_config` (chmod 600):

```
BOT_TOKEN=123456789:ABCdef...
CHAT_ID=-100123456789
```

### Email

Create `/home/pi/gifcam/.email_config` (chmod 600):

```
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
FROM_ADDRESS=you@gmail.com
FROM_PASSWORD=your-app-password
TO_ADDRESS=recipient@example.com
```

For Gmail, generate an App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (requires 2-Step Verification).

## Installation

See [INSTALL.md](INSTALL.md).

## Key settings (`gifcam.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `RECORD_DURATION` | 2.0 s | Length of each burst |
| `SENSOR_FPS` | 30 | Capture frame rate |
| `GIF_FPS` | 8 | GIF playback frame rate |
| `H264_BITRATE` | 10 Mbps | Recording bitrate |
| `REBOUND` | True | Ping-pong loop (A→B→A) |
| `WIDTH` / `HEIGHT` | 1280×720 | Capture resolution (rotated to portrait in output) |
| `MAX_SHUTTER_US` | 33 333 µs | Max exposure (1/30 s) |
| `MAX_ANALOGUE_GAIN` | 8.0 | Max sensor gain |
| `LONG_PRESS_SECS` | 3.0 | Hold time to trigger encode |

## Credits

Based on [Pix-E GIF Camera](https://www.hackster.io/nick-brewer/pix-e-gif-camera-323965) by Nick Brewer.
