# Printers

Replace `<device>` with your printer's device name as it appears in its entity IDs.

| Integration | Camera | Print status | Running states | Pause button | Stage sensor | Layer sensor |
|---|---|---|---|---|---|---|
| Bambu Lab ([ha-bambulab](https://github.com/greghesp/ha-bambulab)) | `camera.<device>_camera` | `sensor.<device>_print_status` | `running` | `button.<device>_pause` | `sensor.<device>_current_stage` | `sensor.<device>_current_layer` |
| Klipper ([Moonraker](https://github.com/marcolivierarsenault/moonraker-home-assistant)) | `camera.<device>_webcam` | `sensor.<device>_current_print_state` | `printing` | `button.<device>_pause_print` | none | `sensor.<device>_current_layer` |
| [OctoPrint](https://www.home-assistant.io/integrations/octoprint/) | `camera.octoprint_camera` | `sensor.octoprint_current_state` | `printing`, `printing_sd`, `printing_streaming` | `button.octoprint_pause_job` | none | none |
| [PrusaLink](https://www.home-assistant.io/integrations/prusalink/) | a separate webcam | `sensor.<device>` | `printing` | `button.<device>_pause_job` | none | none |
| Creality stock firmware ([ha_creality_ws](https://github.com/3dg1luk43/ha_creality_ws)) | `camera.<device>_printer_camera` | `sensor.<device>_print_status` | `printing` | `button.<device>_pause_print` | none | `sensor.<device>_working_layer` |
| Elegoo Centauri Carbon ([elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant)) | `camera.<device>_chamber_camera` | `sensor.<device>_print_status` | `printing` | `button.<device>_pause_print` | not needed | `sensor.<device>_current_layer` |

Each integration also has matching resume and stop buttons. Moonraker and PrusaLink call stop
"cancel": `button.<device>_cancel_print` and `button.<device>_cancel_job`.

Any other webcam works the same way once it's in Home Assistant as a camera entity.

## Paused states

The paused states default to `pause`, `paused`, `pausing`, `resuming`, `attention` (PrusaLink
waiting for you) and `filament_unload_paused` (Elegoo), which covers every integration above.
While the printer is paused, checks stop but a detected failure and a snooze are kept. A status
that's unavailable for up to 10 minutes counts as paused too. Any other state ends the print.

## Bambu Lab

Use the `camera` entity. If you turned on ha-bambulab's Image Sensor Camera option, you get an
`image` entity instead, which this integration can't read.

The pause button only exists when the printer accepts LAN control, which on newer firmware means
LAN Only mode with Developer Mode on. Turn the chamber light on while printing so frames aren't too
dark to judge.

## Klipper (Moonraker)

The camera entity is named after the webcam configured in Moonraker, so it may not end in
`_webcam`. Don't use `sensor.<device>_printer_state`: it reports Klipper's state, not the print's.
The layer sensor is exact only when the slicer sends `SET_PRINT_STATS_INFO`; otherwise Moonraker
estimates it from the Z height.

## OctoPrint

The entities have no device name in their IDs. A second OctoPrint instance gets `_2` at the end.

## PrusaLink

Its only camera entity is the job thumbnail, not a live view. Add your webcam with Home
Assistant's generic camera or ESPHome integration and use that entity.

## Elegoo

The print status reads `preheating`, `leveling`, `homing` or `auto_feeding` during the start
routine and `printing` only while it prints, so the start routine is skipped without a stage
sensor. Add those four to the paused states as well. Otherwise a filament load in the middle of a
print counts as the print ending, which clears an alert and sends the print report early.
