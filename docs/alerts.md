# Checks and alerts

## When checks run

Frames are checked only while the print status entity is in one of the running states (and, with
a [stage sensor](setup.md#stage-sensor), while it reads `printing`). Most failures start in the
first layers, so checks run every 30 seconds for the first 15 minutes of printing, then every
minute. With a stage sensor, those 15 minutes start when the printer starts printing rather than
during the start routine. All three numbers are [options](tuning.md#options).

A frame counts toward an alert only when the plate is visible and a failure question scores above
the yes threshold. Frames where the plate isn't visible, answers between the two thresholds, and
failed API calls are skipped, so none of them ever count as "looks fine". By default an alert needs
3 failed frames among the last 4.

## Alerts

A detected failure creates a persistent notification in Home Assistant and sends an actionable
notification to each notify service you chose in the options, for example
`notify.mobile_app_your_phone`. Both show the frame that triggered the alert, which is the exact
image the model judged, through a link that works for 24 hours. If frames aren't being saved, the
phone notification shows the camera's live image instead.

Tapping the phone notification, or the link in the persistent notification, opens the
[Print failures panel](panel.md#print-failures-panel) on that frame with every question's
probability. If the frame has been cleaned up since, the panel says so and shows the printer's
history.

| Auto-pause | What happens | Notification buttons |
|---|---|---|
| Off (default) | The printer keeps running | Pause, Stop, Continue |
| On | The printer is paused first | Resume, Stop |

Pause, Stop and Resume press the printer's matching buttons, and a button only appears if you set
that printer button during setup. Continue means it was a false alarm; if the integration paused
the printer, Continue resumes it too.

After Continue or Resume, checks stop for the snooze time (15 minutes by default) so the same
failure doesn't alert again right away. Pausing and resuming the print at the printer or in its app
after an alert counts as Continue as well, so a later failure in the same print still alerts.

## When checks keep failing

If 5 checks in a row fail during a print, you get a "Print monitoring isn't working" notification
with the last error, because the print isn't being watched. At the default intervals that's 2.5 to
5 minutes. Typical causes are a provider outage, an API key that stopped working, or Home Assistant
losing its internet connection.

The same problem shows under Settings > Repairs until checks work again, with what to check when
the camera is the cause. A follow-up notification replaces the first once checks work again or the
print ends. Failed checks never count as a failure, so they can't pause the printer.

## Print report

When a watched print ends, a print report goes to the notify services. It says how long the print
ran, how many checks looked fine, were skipped or showed a failure, and whether an alert was sent,
with a strip of six frames from across the print. Turn it off with the Print report option.

## Daily check limit

With a Daily check limit set, automatic checks stop for the rest of the day once that many model
calls were made, and you get one notification saying so. Check now still works.
