#!/usr/bin/env python3
"""
gifcam.py — Pix-E GIF Camera
For Raspberry Pi Zero 2 W + Camera Module 3 (IMX708)

Based on the original Pix-E GIF Camera by Nick Brewer
  https://github.com/nickbrewer/gifcam
Substantially rewritten for picamera2, hardware H.264 encoding,
two-pass GIF transcoding, and Telegram/email delivery.

GPIO pins:
  BUTTON_PIN     = 19   shutter button, active LOW with internal pull-up
  BUTTON_LED_PIN = 21   LED inside shutter button
  STATUS_LED_PIN = 12   rear status LED

Workflow:
  Short press + release  →  record a 2-second H.264 burst, saved to PENDING_DIR
  Hold 3 seconds + release  →  encode all pending bursts to MP4 + GIF,
                                move source recordings to ARCHIVE_DIR

Why video recording instead of a JPEG burst:
  • Camera runs in continuous video mode so AE and AWB are always converged
    before the shutter is pressed — no cold-start flicker between frames.
  • Frame timing is governed by the IMX708 sensor clock, not Python sleep(),
    so every frame is evenly spaced regardless of CPU load.
  • AE *and* AWB are both locked immediately before recording starts so
    all frames share identical exposure and colour balance.
  • MAX_SHUTTER_US (1/30 s) replaces the old 1/500 s hard clamp — far better
    in typical indoor and low-light scenes.
  • Hardware H.264 encoding (VPU) runs concurrently with the ISP at negligible
    CPU cost; we then do a two-pass palettegen/paletteuse GIF transcode and a
    full-quality MP4 re-encode when the user long-presses.

LED behaviour:
  Ready                  →  button LED full on,  status LED off
  Button held < 3s       →  button LED full on
  Button held ≥ 3s       →  status LED solid (release now to encode)
  Capturing              →  button LED blinks fast
  Encoding               →  button LED off, status LED blinks slow
  Sending (Telegram/email) → button LED off, status LED blinks slow
  Done                   →  button LED full on,  status LED off
"""

import os
import subprocess
import threading
import queue
from time import sleep, time
from datetime import datetime

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from libcamera import controls
from gpiozero import Button, PWMLED

# ─── BEHAVIOUR VARIABLES ─────────────────────────────────────────────────────
RECORD_DURATION   = 2.0         # seconds of video recorded per burst
SENSOR_FPS        = 30          # sensor / H.264 frame rate during recording
GIF_FPS           = 8           # GIF playback fps  (≈16 frames for a 2 s clip)
H264_BITRATE      = 10_000_000  # 10 Mbps — high-quality 720p master before GIF
REBOUND           = True        # True: ping-pong A→B→A loop
WIDTH             = 1280        # landscape capture; ffmpeg rotates to portrait
HEIGHT            = 720
MAX_SHUTTER_US    = 33_333      # 1/30 s ceiling — was 1/500 s (terrible indoors)
MAX_ANALOGUE_GAIN = 8.0         # IMX708 gain cap before noise becomes objectionable
LONG_PRESS_SECS   = 3.0
PENDING_DIR       = '/home/pi/gifcam/pending'
OUTPUT_DIR        = '/home/pi/gifcam/gifs'
TELEGRAM_CONFIG   = '/home/pi/gifcam/.telegram_config'  # chmod 600
SENT_LOG          = '/home/pi/gifcam/.sent_gifs'
EMAIL_CONFIG      = '/home/pi/gifcam/.email_config'     # chmod 600
EMAIL_SENT_LOG    = '/home/pi/gifcam/.sent_gifs_email'
# ─────────────────────────────────────────────────────────────────────────────

BUTTON_PIN     = 19
BUTTON_LED_PIN = 21
STATUS_LED_PIN = 12

button     = Button(BUTTON_PIN, pull_up=True, bounce_time=0.05)
button_led = PWMLED(BUTTON_LED_PIN)
status_led = PWMLED(STATUS_LED_PIN)

action_queue   = queue.Queue()
_press_start   = None
_hold_watcher  = None
_busy          = threading.Event()  # set while capture is in progress
_last_metadata = {}                 # latest ISP metadata, updated every frame
_metadata_lock = threading.Lock()   # guards _last_metadata


# ─── LED HELPERS ─────────────────────────────────────────────────────────────

def blink_led(led, on_time, off_time, max_brightness, stop_event):
    while not stop_event.is_set():
        led.value = max_brightness
        sleep(on_time)
        if stop_event.is_set():
            break
        led.value = 0.0
        sleep(off_time)
    led.value = 0.0


def start_blink(led, on_time, off_time, max_brightness):
    stop = threading.Event()
    t = threading.Thread(
        target=blink_led,
        args=(led, on_time, off_time, max_brightness, stop),
        daemon=True,
    )
    t.start()
    return t, stop


def stop_blink(thread, stop_event):
    stop_event.set()
    thread.join()


# ─── BUTTON CALLBACKS ────────────────────────────────────────────────────────

def on_press():
    global _press_start, _hold_watcher

    if _busy.is_set():
        return   # ignore presses while a capture is running

    _press_start = time()
    button_led.value = 1.0

    def hold_watcher():
        while button.is_pressed:
            if time() - _press_start >= LONG_PRESS_SECS:
                status_led.value = 0.2
                return
            sleep(0.05)

    _hold_watcher = threading.Thread(target=hold_watcher, daemon=True)
    _hold_watcher.start()


def on_release():
    global _press_start

    if _busy.is_set() or _press_start is None:
        return   # ignore releases while a capture is running

    held = time() - _press_start
    _press_start = None
    status_led.value = 0.0

    if held >= LONG_PRESS_SECS:
        action_queue.put('process')
    else:
        action_queue.put('capture')


button.when_pressed  = on_press
button.when_released = on_release


# ─── CAMERA ──────────────────────────────────────────────────────────────────

def setup_camera():
    """
    Open the IMX708 in continuous video mode.

    Video mode (vs. still mode) means the ISP, AE, and AWB run non-stop so
    they are already converged on the scene before the user presses the
    shutter.  The Pi's hardware H.264 VPU encoder also operates natively in
    this mode, so recording starts instantly with zero CPU overhead.
    """
    cam = Picamera2()
    config = cam.create_video_configuration(
        main={"size": (WIDTH, HEIGHT)},
        buffer_count=6,
    )
    cam.configure(config)
    cam.set_controls({
        "AwbMode":          controls.AwbModeEnum.Auto,
        "AeMeteringMode":   controls.AeMeteringModeEnum.Matrix,
        "AeConstraintMode": controls.AeConstraintModeEnum.Normal,
        "Saturation":       1.1,
        "Sharpness":        1.5,
        "AfMode":           controls.AfModeEnum.Continuous,
        "AfSpeed":          controls.AfSpeedEnum.Fast,
        "FrameRate":        float(SENSOR_FPS),
    })

    # Cache ISP output metadata on every frame so _lock_scene() can read
    # settled AE/AWB/focus values without any blocking capture_metadata() call.
    def _cache_metadata(request):
        with _metadata_lock:
            global _last_metadata
            _last_metadata = request.get_metadata()
    cam.pre_callback = _cache_metadata

    cam.start()
    sleep(2)    # let AE / AWB converge on first scene
    return cam


def _lock_scene(camera):
    """
    Snapshot the current AE, AWB, and focus state and freeze all three.

    Metadata is read from _last_metadata, which is populated by the
    camera's pre_callback on every frame — no blocking capture_metadata()
    call is needed.  This avoids a picamera2 hang where capture_metadata()
    blocks indefinitely immediately after stop_recording().

    After set_controls(), we sleep for two frame periods so the pipeline
    flushes in-flight frames and the encoder only sees frames captured
    under the locked parameters.
    """
    with _metadata_lock:
        md = dict(_last_metadata)

    exp      = int(md.get('ExposureTime',  MAX_SHUTTER_US))
    gain     = float(md.get('AnalogueGain', 1.0))
    cgains   = md.get('ColourGains')    # (red_gain, blue_gain) tuple or None
    lens_pos = md.get('LensPosition', 1.0)

    if exp > MAX_SHUTTER_US:
        gain = min(gain * (exp / MAX_SHUTTER_US), MAX_ANALOGUE_GAIN)
        exp  = MAX_SHUTTER_US

    lock = {
        "AeEnable":     False,
        "ExposureTime": exp,
        "AnalogueGain": gain,
        "AfMode":       controls.AfModeEnum.Manual,
        "LensPosition": lens_pos,
    }
    if cgains is not None:
        lock["AwbEnable"]   = False
        lock["ColourGains"] = tuple(cgains)

    camera.set_controls(lock)

    # Wait two frame periods for locked settings to propagate through the
    # pipeline before recording starts (~67 ms at 30 fps).
    sleep(2.0 / SENSOR_FPS)


def _unlock_scene(camera):
    """Restore continuous AE, AWB, and autofocus after a recording."""
    camera.set_controls({
        "AeEnable":  True,
        "AwbEnable": True,
        "AfMode":    controls.AfModeEnum.Continuous,
        "AfSpeed":   controls.AfSpeedEnum.Fast,
    })


# ─── CAPTURE ─────────────────────────────────────────────────────────────────

def capture_burst(camera):
    """
    Record RECORD_DURATION seconds of H.264 video to a new pending burst dir.

    The camera streams continuously so AE, AWB, and PDAF are already settled
    before this function is called.  _lock_scene() snapshots and freezes AE,
    AWB, and focus from the current frame in a single pass (≈100 ms total)
    so recording starts almost immediately after the button is released.

    The button LED blinks only while the encoder is actually rolling so
    subjects can see exactly when they are being recorded.

    Returns the burst directory path.
    """
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    h264_path = os.path.join(PENDING_DIR, f'{timestamp}.h264')

    # Lock AE, AWB, and focus from the current settled frame (≈100 ms).
    # No explicit autofocus cycle is needed — continuous PDAF has kept the
    # lens sharp since the last capture.
    _lock_scene(camera)

    # Button LED blinks for the exact duration of the recording.
    # Fresh encoder each time to avoid V4L2 state accumulation.
    encoder = H264Encoder(H264_BITRATE)
    blink_t, blink_stop = start_blink(button_led, 0.1, 0.1, 1.0)
    try:
        camera.start_recording(encoder, h264_path)
        sleep(RECORD_DURATION)
        camera.stop_recording()
    finally:
        stop_blink(blink_t, blink_stop)

    _unlock_scene(camera)
    print(f'  Saved burst → pending/{timestamp}.h264')
    return h264_path


# ─── ENCODING ────────────────────────────────────────────────────────────────

def encode_burst(h264_path):
    """
    Convert a pending H.264 burst to MP4 and GIF in parallel.

    GIF pipeline (two-pass for maximum colour fidelity):
      Pass 1 — palettegen accumulates all selected frames and writes a
               256-colour palette PNG optimised for the exact content.
      Pass 2 — paletteuse quantises the frames using that palette with
               Bayer dithering.

    If REBOUND is enabled, the frame sequence is ping-ponged (A→B→A) before
    palette generation so the palette covers both motion directions and the
    loop plays back smoothly.  The MP4 is also ping-ponged when REBOUND is
    enabled.

    The pending .h264 file is deleted after encoding completes.
    """
    timestamp = os.path.splitext(os.path.basename(h264_path))[0]

    if not os.path.exists(h264_path):
        print(f'  Skipping {timestamp}: .h264 not found.')
        return

    mp4_out = os.path.join(OUTPUT_DIR, f'{timestamp}.mp4')
    gif_out = os.path.join(OUTPUT_DIR, f'{timestamp}.gif')
    palette = os.path.join(PENDING_DIR, f'{timestamp}_palette.png')

    fps_in  = str(SENSOR_FPS)
    fps_out = str(GIF_FPS)

    # Build the frame-selection + rotation + scale filter used in both GIF passes.
    # scale=iw/2:ih/2 halves the resolution after the portrait rotation.
    # For REBOUND, frames are split into a forward copy and a reversed copy
    # which are concatenated to produce the ping-pong sequence.
    if REBOUND:
        frame_filt = (
            f'fps={fps_out},transpose=1,scale=iw/2:ih/2,'
            'split[_f][_r];[_r]reverse[_rev];[_f][_rev]concat=n=2:v=1:a=0'
        )
    else:
        frame_filt = f'fps={fps_out},transpose=1,scale=iw/2:ih/2'

    def do_mp4():
        if REBOUND:
            mp4_cmd = [
                'ffmpeg', '-y',
                '-f', 'h264', '-framerate', fps_in, '-i', h264_path,
                '-filter_complex',
                    'transpose=1,split[_f][_r];[_r]reverse[_rev];[_f][_rev]concat=n=2:v=1:a=0[v]',
                '-map', '[v]',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                mp4_out,
            ]
        else:
            mp4_cmd = [
                'ffmpeg', '-y',
                '-f', 'h264', '-framerate', fps_in, '-i', h264_path,
                '-vf', 'transpose=1',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                mp4_out,
            ]
        subprocess.run(mp4_cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def do_gif():
        # Pass 1: build palette from every frame that will appear in the GIF
        subprocess.run([
            'ffmpeg', '-y',
            '-f', 'h264', '-framerate', fps_in, '-i', h264_path,
            '-filter_complex',
                f'[0:v]{frame_filt}[frames];'
                '[frames]palettegen=max_colors=256:stats_mode=full',
            '-frames:v', '1', palette,
        ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if not os.path.exists(palette):
            print(f'  WARNING: palette generation failed for {timestamp}')
            return

        # Pass 2: quantise frames with the per-clip palette
        subprocess.run([
            'ffmpeg', '-y',
            '-f', 'h264', '-framerate', fps_in, '-i', h264_path,
            '-i', palette,
            '-filter_complex',
                f'[0:v]{frame_filt}[frames];'
                '[frames][1:v]paletteuse=dither=bayer:bayer_scale=5',
            '-loop', '0',
            gif_out,
        ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if os.path.exists(palette):
            os.remove(palette)

    t1 = threading.Thread(target=do_mp4)
    t2 = threading.Thread(target=do_gif)
    t1.start(); t2.start()
    t1.join();  t2.join()

    try:
        os.remove(h264_path)
    except OSError:
        pass
    print(f'  Encoded {timestamp} → MP4 + GIF')


def process_all_pending():
    """Find and encode every H.264 burst file in PENDING_DIR."""
    bursts = sorted([
        os.path.join(PENDING_DIR, f)
        for f in os.listdir(PENDING_DIR)
        if f.endswith('.h264')
    ])
    if not bursts:
        print('  No pending bursts.')
        return 0
    print(f'  Encoding {len(bursts)} burst(s)...')
    for h264_path in bursts:
        encode_burst(h264_path)
    return len(bursts)


def pending_count():
    if not os.path.isdir(PENDING_DIR):
        return 0
    return sum(1 for f in os.listdir(PENDING_DIR) if f.endswith('.h264'))


# ─── TELEGRAM ─────────────────────────────────────────────────────

def _load_telegram_config():
    """Return (bot_token, chat_id) from TELEGRAM_CONFIG, or (None, None)."""
    if not os.path.exists(TELEGRAM_CONFIG):
        return None, None
    cfg = {}
    try:
        with open(TELEGRAM_CONFIG) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, val = line.partition('=')
                    cfg[key.strip()] = val.strip()
    except OSError:
        return None, None
    return cfg.get('BOT_TOKEN'), cfg.get('CHAT_ID')


def _load_email_config():
    """Return (smtp_server, port, from_addr, password, to_addr) from EMAIL_CONFIG,
    or an all-None 5-tuple if the file is absent or incomplete."""
    if not os.path.exists(EMAIL_CONFIG):
        return None, None, None, None, None
    cfg = {}
    try:
        with open(EMAIL_CONFIG) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, val = line.partition('=')
                    cfg[key.strip()] = val.strip()
    except OSError:
        return None, None, None, None, None
    try:
        port = int(cfg.get('SMTP_PORT', '587'))
    except ValueError:
        port = 587
    return (
        cfg.get('SMTP_SERVER', 'smtp.gmail.com'),
        port,
        cfg.get('FROM_ADDRESS'),
        cfg.get('FROM_PASSWORD'),
        cfg.get('TO_ADDRESS'),
    )


def _has_internet(timeout=5):
    """Return True if the Telegram API endpoint is reachable."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(('api.telegram.org', 443))
        sock.close()
        return True
    except OSError:
        return False


def send_pending_gifs():
    """
    Send any MP4s in OUTPUT_DIR that have not yet been sent to Telegram.
    Sent filenames are appended to SENT_LOG so they are never re-sent.
    Does nothing if the config file is absent, credentials are missing,
    or there is no internet connection.
    """
    token, chat_id = _load_telegram_config()
    if not token or not chat_id:
        return

    if not _has_internet():
        print('  Telegram: no internet, skipping.')
        return

    sent = set()
    if os.path.exists(SENT_LOG):
        try:
            with open(SENT_LOG) as fh:
                sent = {line.strip() for line in fh if line.strip()}
        except OSError:
            pass

    try:
        all_mp4s = sorted(f for f in os.listdir(OUTPUT_DIR) if f.endswith('.mp4'))
    except FileNotFoundError:
        return

    unsent = [f for f in all_mp4s if f not in sent]
    if not unsent:
        return

    try:
        import requests as _req
    except ImportError:
        print('  Telegram: "requests" not installed — run: pip3 install requests')
        return

    print(f'  Telegram: sending {len(unsent)} MP4(s)...')
    url = f'https://api.telegram.org/bot{token}/sendVideo'
    for filename in unsent:
        mp4_path = os.path.join(OUTPUT_DIR, filename)
        try:
            with open(mp4_path, 'rb') as fh:
                resp = _req.post(
                    url,
                    data={'chat_id': chat_id, 'supports_streaming': True},
                    files={'video': (filename, fh, 'video/mp4')},
                    timeout=60,
                )
            if resp.json().get('ok'):
                with open(SENT_LOG, 'a') as log:
                    log.write(filename + '\n')
                print(f'    Sent {filename}')
            else:
                print(f'    Rejected {filename}: {resp.text}')
        except Exception as e:
            print(f'    Error sending {filename}: {e}')


def send_pending_gifs_email():
    """
    Send any GIFs in OUTPUT_DIR that have not yet been emailed.
    Sent filenames are appended to EMAIL_SENT_LOG so they are never re-sent.
    Uses Python's built-in smtplib — no extra packages required.
    For Gmail, generate an App Password at myaccount.google.com/apppasswords
    (2-Step Verification must be enabled).
    """
    smtp_server, port, from_addr, password, to_addr = _load_email_config()
    if not from_addr or not password or not to_addr:
        return

    if not _has_internet():
        print('  Email: no internet, skipping.')
        return

    sent = set()
    if os.path.exists(EMAIL_SENT_LOG):
        try:
            with open(EMAIL_SENT_LOG) as fh:
                sent = {line.strip() for line in fh if line.strip()}
        except OSError:
            pass

    try:
        all_gifs = sorted(f for f in os.listdir(OUTPUT_DIR) if f.endswith('.gif'))
    except FileNotFoundError:
        return

    unsent = [f for f in all_gifs if f not in sent]
    if not unsent:
        return

    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email.mime.text import MIMEText
    from email import encoders as _enc

    print(f'  Email: sending {len(unsent)} GIF(s) to {to_addr}...')
    for filename in unsent:
        gif_path = os.path.join(OUTPUT_DIR, filename)
        try:
            msg = MIMEMultipart()
            msg['From']    = from_addr
            msg['To']      = to_addr
            msg['Subject'] = f'gifcam: {filename}'
            msg.attach(MIMEText('New GIF from your gifcam.', 'plain'))

            with open(gif_path, 'rb') as fh:
                part = MIMEBase('image', 'gif')
                part.set_payload(fh.read())
            _enc.encode_base64(part)
            part.add_header('Content-Disposition',
                            f'attachment; filename="{filename}"')
            msg.attach(part)

            with smtplib.SMTP(smtp_server, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(from_addr, password)
                smtp.send_message(msg)

            with open(EMAIL_SENT_LOG, 'a') as log:
                log.write(filename + '\n')
            print(f'    Sent {filename}')
        except Exception as e:
            print(f'    Error emailing {filename}: {e}')


def send_all_gifs():
    """
    Send all unsent GIFs via Telegram and/or email.
    While sending, the button LED is off and the status LED blinks slowly.
    Returns immediately without touching the LEDs if there are no GIF files.
    """
    try:
        has_files = any(f.endswith('.gif') or f.endswith('.mp4') for f in os.listdir(OUTPUT_DIR))
    except FileNotFoundError:
        return
    if not has_files:
        return

    button_led.value = 0.0
    blink_t, blink_stop = start_blink(status_led, 0.3, 0.3, 0.5)
    try:
        send_pending_gifs()
        send_pending_gifs_email()
    finally:
        stop_blink(blink_t, blink_stop)
        status_led.value = 0.0
        button_led.value = 1.0


# ─── MAIN LOOP ───────────────────────────────────────────────────────────────

def main():
    for d in (PENDING_DIR, OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)

    print('Starting gifcam...')
    camera = setup_camera()

    button_led.value = 1.0
    status_led.value = 0.0

    n = pending_count()
    if n:
        print(f'System Ready  ({n} burst(s) pending — hold {LONG_PRESS_SECS:.0f}s to encode)')
    else:
        print('System Ready')

    # Check for unsent GIFs in the background so startup isn't delayed.
    threading.Thread(target=send_all_gifs, daemon=True).start()

    try:
        while True:
            action = action_queue.get()

            if action == 'capture':
                # ── SHORT PRESS: RECORD BURST ─────────────────────────────
                _busy.set()   # block button callbacks for the whole capture
                try:
                    capture_burst(camera)
                except Exception as e:
                    print(f'  Capture error: {e}')
                finally:
                    _busy.clear()
                    button_led.value = 1.0
                    status_led.value = 0.0

                n = pending_count()
                print(f'System Ready  ({n} burst(s) pending — hold {LONG_PRESS_SECS:.0f}s to encode)')

            elif action == 'process':
                # ── LONG PRESS: ENCODE ALL PENDING ───────────────────────
                n = pending_count()
                if n == 0:
                    print('No pending bursts to encode.')
                    button_led.value = 1.0
                    continue

                print(f'Encoding {n} burst(s)...')
                button_led.value = 0.0
                blink_t, blink_stop = start_blink(status_led, 0.15, 0.15, 0.5)

                count = process_all_pending()

                stop_blink(blink_t, blink_stop)
                status_led.value = 0.0
                button_led.value = 1.0

                print(f'Done — {count} burst(s) encoded.')
                send_all_gifs()
                print('System Ready')

    except KeyboardInterrupt:
        print('Shutting down.')
    finally:
        camera.stop()
        button_led.off()
        status_led.off()


if __name__ == '__main__':
    main()
