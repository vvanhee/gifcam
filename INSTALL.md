# Pix-E GIF Camera — Modernized Install Guide
**For Raspberry Pi Zero 2 W + Camera Module 3**

---

## What Changed From the Original

| Original | Upgraded |
|---|---|
| `picamera` | `picamera2` (required for Camera Module 3) |
| `GraphicsMagick` | `ffmpeg` (faster, better GIF quality) |
| `RPi.GPIO` | `gpiozero` (simpler, works on Bookworm) |
| JPEG burst (cold-start flicker, uneven timing) | Continuous H.264 video — AE/AWB always converged, sensor-clocked frames |
| Single-threaded encode | GIF + MP4 encoded in parallel (uses all 4 cores) |
| Full resolution capture | 1280×720 (landscape 720p — matches Camera Module 3 native 16:9 sensor AR) |
| GIF only | GIF + MP4 (MP4 has full color, universally playable) |

---

## Part 1: Flash the SD Card

1. Download **Raspberry Pi Imager** on your computer: https://www.raspberrypi.com/software/

2. Insert your SD card.

3. In Imager, choose:
   - **Device:** Raspberry Pi Zero 2 W
   - **OS:** You need **Raspberry Pi OS Lite (64-bit) based on Debian Bookworm**, not the default.
     Imager currently defaults to Trixie (Debian 13) and labels Bookworm as "legacy" — ignore that.
     Navigate to: **Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit) (Legacy, Debian Bookworm)**

     > **Why Bookworm and not Trixie?** All the libraries this project depends on
     > (`picamera2`, `ffmpeg`, `gpiozero`, `avahi`) are well-tested on Bookworm.
     > Trixie is newer and still maturing for single-purpose embedded use. The camera
     > driver documentation also specifically targets Bookworm. Bookworm will continue
     > receiving security updates for years — "legacy" just means it's not the newest,
     > not that it's unsupported.
   - **Storage:** your SD card

4. Click **Next**, then when prompted click **Edit Settings** to pre-configure before writing:

   **General tab:**
   - Set hostname: `pixecam`
   - Set username: `pi` / set a password of your choice
   - Check **Configure wireless LAN**
     - SSID: your WiFi network name (case-sensitive)
     - Password: your WiFi password
     - Wireless LAN country: US (or your country)
   - Set locale and timezone as appropriate

   **Services tab:**
   - Enable SSH: checked
   - Use password authentication

5. Click **Save**, then **Yes** to apply the settings, then **Yes** to write.

> **2.4GHz only:** The Pi Zero 2 W cannot connect to 5GHz networks. If your router
> broadcasts both bands under the same name, it may or may not hand you a 2.4GHz
> connection automatically. If you have trouble connecting, log into your router and
> check whether you can give the 2.4GHz band a separate name, or temporarily disable 5GHz.

---

## Part 2: First Boot, WiFi & SSH

Eject the SD card, insert it into the Zero 2 W, and power on.

The first boot takes longer than usual — allow **90 seconds** before trying to connect.
The Pi needs to expand the filesystem, generate SSH keys, and connect to WiFi all on
first startup.

### Connect via hostname

From your computer's terminal:

```bash
ssh pi@pixecam.local
```

The `.local` address works via mDNS (Bonjour/Avahi). It should work out of the box on:
- **Mac:** always works
- **Linux:** works if `avahi-daemon` is installed (it usually is)
- **Windows 11:** works natively
- **Windows 10:** works if iTunes or Bonjour is installed, otherwise use IP address below

### If pixecam.local doesn't resolve

Find the Pi's IP address from your router's admin page — look for a device named
`pixecam` in the connected devices or DHCP leases list. Then SSH using the IP directly:

```bash
ssh pi@192.168.1.xxx
```

### First connection

The first time you SSH in you'll see a fingerprint warning — this is normal. Type `yes`
to accept. You'll then be prompted for the password you set in Imager.

### Ensure mDNS is running (recommended)

Once SSH'd in, confirm Avahi (the service that makes `.local` addresses work) is
installed and enabled:

```bash
sudo apt install -y avahi-daemon
sudo systemctl enable avahi-daemon
sudo systemctl start avahi-daemon
```

Raspberry Pi OS Lite includes this by default, but it's worth confirming. After this,
`pixecam.local` will reliably resolve on your local network whenever the Pi is powered on.

### Assign a fixed IP (optional but convenient)

If you want the Pi to always have the same IP address, log into your router's admin page
and look for **DHCP reservation** or **static lease**. Assign the Pi's MAC address to a
fixed IP like `192.168.1.50`. The Pi's MAC address is shown in your router's connected
devices list next to the `pixecam` hostname.

---

## Part 3: Update the System

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

SSH back in after the reboot.

---

## Part 4: Install Dependencies

```bash
sudo apt install -y ffmpeg git python3-picamera2 python3-gpiozero libcamera-apps
```

That's it — no pip required. Everything is installed system-wide via apt.

> **Note:** `python3-picamera2` pulls in libcamera automatically. The Camera Module 3
> is auto-detected on Bookworm — no `raspi-config` camera enable step needed.

---

## Part 5: Install the gifcam Code

```bash
mkdir -p /home/pi/gifcam/gifs /home/pi/gifcam/pending
cd /home/pi/gifcam
```

Copy `gifcam.py` and `launcher.sh` to `/home/pi/gifcam/` via scp from your computer:

```bash
# Run this on your COMPUTER, not the Pi:
scp gifcam.py launcher.sh pi@pixecam.local:/home/pi/gifcam/
```

Back on the Pi, make the launcher executable:

```bash
chmod +x /home/pi/gifcam/launcher.sh
```

---

## Part 6: Verify the Camera Works

Test that the Camera Module 3 is detected:

```bash
rpicam-hello --timeout 5000
```

You should see camera info printed without errors.
Note: the old `libcamera-hello` command was renamed to `rpicam-hello` in current Bookworm — they are the same thing. If you get an error, check that
the ribbon cable is fully seated in both connectors (camera and Pi), with the
blue side facing the correct direction.

---

## Part 7: Test the Script Manually

Run it once from the command line to confirm everything works before setting up autostart:

```bash
cd /home/pi/gifcam
python3 gifcam.py
```

Press the shutter button. You should see something like:
```
Starting gifcam...
System Ready
```

After a short press (capture):
```
  Saved burst → pending/20260519-120000.h264
System Ready  (1 burst(s) pending — hold 3s to encode)
```

After a long press (encode):
```
Encoding 1 burst(s)...
  Encoded 20260519-120000 → MP4 + GIF
Done — 1 burst(s) encoded.
System Ready
```

Press Ctrl+C to stop.

---

## Part 8: Set Up Autostart at Boot

```bash
crontab -e
```

Choose nano (option 1 or 2 depending on your system). Add this line at the bottom:

```
@reboot sh /home/pi/gifcam/launcher.sh
```

Save and exit (Ctrl+X, then Y, then Enter).

---

## Part 9 (Optional): Access GIFs Over WiFi via Samba

Install Samba so you can browse and pull GIFs from any computer on your network:

```bash
sudo apt install -y samba samba-common-bin
```

Add the share config:

```bash
sudo nano /etc/samba/smb.conf
```

Scroll to the bottom and add:

```ini
[gifs]
  comment = Pix-E GIFs and Videos
  path = /home/pi/gifcam/gifs
  browseable = yes
  read only = no
  guest ok = yes

```

Restart Samba:

```bash
sudo systemctl restart smbd
```

On your computer, browse to `\\pixecam` (Windows) or `smb://pixecam.local` (Mac/Linux).

---

## Customizing the Camera Behaviour

Edit the variables at the top of `gifcam.py`:

```python
RECORD_DURATION   = 2.0         # seconds of H.264 video captured per burst
SENSOR_FPS        = 30          # sensor / encoder frame rate during recording
GIF_FPS           = 8           # GIF playback fps  (≈16 frames for a 2 s clip)
H264_BITRATE      = 10_000_000  # 10 Mbps — quality of the H.264 master
REBOUND           = True        # True: ping-pong A→B→A loop  False: A→B→A→B
WIDTH             = 1280        # landscape capture; ffmpeg rotates to portrait
HEIGHT            = 720
MAX_SHUTTER_US    = 33_333      # 1/30 s shutter ceiling (good for indoors)
MAX_ANALOGUE_GAIN = 8.0         # IMX708 gain cap before noise is objectionable
LONG_PRESS_SECS   = 3.0         # seconds to hold button to trigger encoding
```

## How to Use

**Taking a shot:** Press and release the button quickly. The button LED will blink during capture. Shots are saved to `pending/` and not yet encoded.

**Encoding:** Hold the button for 3 seconds until the status LED lights up, then release. All pending bursts will be encoded to MP4 + GIF. The raw H.264 recording is deleted after encoding completes.

**Getting your files:** Connect to the network and access `\\\\pixecam` (Windows) or `smb://pixecam.local` (Mac/Linux). The `gifs/` folder has your MP4 and GIF files.

## Directory Structure

```
/home/pi/gifcam/
  pending/          H.264 burst recordings waiting to be encoded
    20260519-120000.h264
  gifs/             finished MP4 and GIF output
    20260519-120000.mp4
    20260519-120000.gif
```

The `.h264` file is deleted automatically after encoding.

After editing, restart the script (or reboot).

---

## GPIO Wiring Reference

These match the original Pix-E schematic — no rewiring needed if you're reusing the old case:

| GPIO Pin | Function |
|---|---|
| 19 | Shutter button input (active LOW, internal pull-up enabled) |
| 21 | Button LED (full on when ready, blinks during capture) |
| 12 | Status LED (blinks while processing) |

> If your button is on a different pin, change `BUTTON_PIN` in `gifcam.py`.

---

## Debugging

If something goes wrong at boot, check the log:

```bash
tail -f /home/pi/gifcam/gifcam.log
```

Common issues:
- **Camera not found:** ribbon cable not seated; try reseating both ends
- **GPIO errors:** check your pin numbers match the wiring
- **ffmpeg not found:** run `sudo apt install -y ffmpeg` again
- **picamera2 import error:** run `sudo apt install -y python3-picamera2`

---

## Part 10 (Optional): Send GIFs via Email

The camera can email every new GIF to you automatically after encoding.
Python's built-in `smtplib` is used — no extra packages needed.

### Gmail setup (recommended)

Gmail requires an **App Password** instead of your regular account password.

1. Make sure **2-Step Verification** is turned on for your Google account:
   [myaccount.google.com/security](https://myaccount.google.com/security)

2. Generate an App Password:
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   - App name: `gifcam` (or anything you like)
   - Copy the 16-character password it gives you.

3. On the Pi, create the config file:

```bash
nano /home/pi/gifcam/.email_config
```

Paste the following, substituting your own values:

```ini
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
FROM_ADDRESS=you@gmail.com
FROM_PASSWORD=xxxx xxxx xxxx xxxx
TO_ADDRESS=recipient@example.com
```

Save and exit (Ctrl+X, Y, Enter), then lock the permissions:

```bash
chmod 600 /home/pi/gifcam/.email_config
```

That's it. After the next encode cycle, any new GIFs will be emailed to `TO_ADDRESS`
as attachments. Each filename is recorded in `.sent_gifs_email` so it is never
sent twice, even across reboots.

### LED behaviour while sending

While the camera is transmitting GIFs (via email or Telegram), the **button LED
turns off** and the **status LED blinks slowly**. When sending is complete both
LEDs return to their normal ready state.

### Other SMTP providers

The same config works for any SMTP server that supports STARTTLS on port 587.
Just change `SMTP_SERVER` and `SMTP_PORT` as needed.

---

## Part 11 (Optional): Send MP4s via Telegram

The camera can send every new MP4 to a Telegram chat automatically after encoding.
This uses the `requests` package — install it first:

```bash
pip3 install requests
```

### Create a Telegram bot

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts. Copy the **bot token** it gives you.
3. Start a conversation with your new bot (search for it by name and press **Start**).
4. To get your **chat ID**, open this URL in a browser (replace `<TOKEN>` with your token):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Send any message to your bot first, then refresh the page. Look for `"id":` inside the `"chat"` object — that number is your chat ID.

### Configure the Pi

```bash
nano /home/pi/gifcam/.telegram_config
```

Paste the following, substituting your own values:

```ini
BOT_TOKEN=1234567890:ABCdefGhIJKlmNoPQRsTUVwxyz
CHAT_ID=987654321
```

Save and exit (Ctrl+X, Y, Enter), then lock the permissions:

```bash
chmod 600 /home/pi/gifcam/.telegram_config
```

After the next encode cycle, any new MP4s will be sent to your Telegram chat.
Each filename is recorded in `.sent_gifs` so it is never sent twice.

---

## Why MP4 Alongside the GIF?

GIF is limited to 256 colors per frame, which causes banding and dithering on
colorful subjects. The MP4 stores the same frames in H.264 with full 16 million
color depth, so you get a faithful record of what was actually captured. Both
files are encoded in parallel, so the extra MP4 adds almost no extra wait time.
