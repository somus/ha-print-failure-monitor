# Entities and panel

## Entities

Each monitored printer gets a device with these entities.

| Entity | Shows |
|---|---|
| Print failure | On once enough recent frames show a failure. Off again after Continue, after you pause and resume the print at the printer, or when the print ends |
| Failure probability | The highest failure-question probability in the latest frame. Attributes hold every question's probability |
| Last check | What the latest check concluded: Looks fine, Failure seen, Unsure, Plate not visible, Error, Snoozed, Print paused, Getting ready, Daily limit reached or Idle. Attributes hold the recent verdicts, the model, the last call's cost, the last error and the model calls made today |
| Last checked frame | The exact image sent to the model on the latest check |
| Monitoring | Turns checking on and off |
| Auto-pause | Pauses the printer automatically when a failure is detected |
| Check now | Runs one check immediately. Between prints or during a snooze it shows the result without counting toward an alert, which is useful for testing the setup |

All of them work in your own [automations](automations.md).

## Print failures panel

The integration adds a Print failures page to the sidebar. For each printer, it shows stats for
the chosen range (24 hours, 7 days or 30 days): checks and prints, failures seen and skipped
frames, alerts and how many you dismissed as false alarms with Continue or Resume, the cost the
provider reported, and how much space saved frames take.

Two charts follow. Failure probability over time plots the highest failure-question probability
for every check, marking the alert threshold and the frames where a failure was seen. Frames per
day splits each day into failure seen, skipped (unsure, plate not visible or error) and looks
fine. Both charts have a table view, and hovering shows the exact values.

Below them is a gallery of checked frames that you can filter by result. Click a frame to see it
full size with every question's probability.

Plate area is described in [Setup](setup.md#plate-area).

## Labels and Test settings

In a frame's details, an administrator can mark what the frame really showed: a failure, or
nothing wrong. The Labeled tile counts false alarms (frames you marked fine that were judged
failed) and misses (frames you marked a failure that weren't). Labeled frames are never cleaned
up, so over time they become a test set for new wording or thresholds. The Labeled filter shows
all of them from any date.

Test settings tries other thresholds or questions on your labeled frames before you save them. It
shows the false alarms and misses with your current settings next to the new ones, and which frames
would change. New thresholds reuse the stored answers and cost nothing. Changed questions ask the
model again for each labeled frame with a saved image, up to 100. Save these settings applies them
and reloads the printer.

Labels, Test settings and Plate area are only available to administrators.

## Frame history and cleanup

Every check's result is recorded for the stats. Whether its image is kept depends on Save frames:
all frames (the default), only frames that didn't look fine, or none.

Every hour, frames and results older than Keep for (7 days by default) are deleted. If saved
frames still take more than the Storage limit (1 GB by default), the oldest images go first and
their results stay in the stats. At the default settings a print uses about 12 MB per hour.

History is stored under `print_failure_monitor/` in your Home Assistant config directory and is
deleted when you remove the printer from the integration. Images are only served to signed-in
users.
