# Troubleshooting

## Common problems

Many checks come back Unsure at the start of a print. The printer reports the print as running
while it levels, heats and purges, and those frames look odd. Set a
[stage sensor](setup.md#stage-sensor) if your printer has one.

Many checks are skipped as Plate not visible or Unsure during the print. Dark frames and low,
bed-level camera angles cause most of these. Turn the chamber or room light on, and set a
[plate area](setup.md#plate-area) so the plate fills more of the frame.

You got a "Print monitoring isn't working" notification. Five checks in a row failed, so the print
isn't being watched. The notification and the entry under Settings > Repairs show the last error;
see [When checks keep failing](alerts.md#when-checks-keep-failing).

A Bambu Lab printer has no camera to pick. ha-bambulab's Image Sensor Camera option replaces the
camera with an `image` entity, which this integration can't read. Turn that option off.

A Bambu Lab printer has no pause button. The button only exists when the printer accepts LAN
control; see the [printer guide](printers.md#bambu-lab).

An Elegoo print ends, or its alert clears, during a filament load. Add `preheating`, `leveling`,
`homing` and `auto_feeding` to the paused states; see the [printer guide](printers.md#elegoo).

## Diagnostics

To report a problem, download the diagnostics from the printer's menu under Settings > Devices &
services. They include the settings, the current state and recent checks, without the API key or
any images.

## Debug logging

```yaml
logger:
  logs:
    custom_components.print_failure_monitor: debug
```
