#!/bin/bash
# launcher.sh — starts gifcam.py at boot
# Called by crontab: @reboot sh /home/pi/gifcam/launcher.sh

# Wait for the system to finish booting before starting
sleep 10

# Change to the gifcam directory
cd /home/pi/gifcam

# Log output for debugging; view with: tail -f /home/pi/gifcam/gifcam.log
python3 gifcam.py >> /home/pi/gifcam/gifcam.log 2>&1
