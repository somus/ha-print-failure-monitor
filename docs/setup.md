# Setup

Setup has two steps.

1. Decision model: a name for the printer, the [provider](providers.md), its API key, and
   optionally a model. Leave the model empty to use the provider's default.
2. Printer: the camera, the print status entity, the states that mean a print is running or
   paused, an optional [stage sensor](#stage-sensor) and [layer sensor](#layer-sensor), and the
   printer's pause, resume and stop buttons. The buttons are optional, but auto-pause and the
   notification buttons need them.

The [printer guide](printers.md) lists the entity names for each printer integration.

Everything else is under the integration's Configure button and can change at any time (see
[Tuning](tuning.md)). To change the provider, API key or printer entities later, use Reconfigure in
the printer's menu; the frame history and options stay. If the provider rejects the API key, Home
Assistant shows a repair asking for a new one.

## Plate area

Most printer cameras see a lot besides the plate. In the Print failures panel, Plate area shows the
live picture and lets you drag a box over the plate. Checks then send only that part, so the plate
fills more of what the model sees. Only administrators see this button.

Include the space above the plate up to and including the nozzle. From a low chamber camera, such
as Bambu Lab's, prints appear above the plate in the picture rather than on it, and a nozzle blob
forms around the hotend. Leave a margin at the sides as well, since spaghetti can spill past the
plate's edge.

## Camera

The camera matters more than the printer. In testing on Klipper, Creality K1 and K2, Prusa, Ender
and Bambu cameras, healthy prints never raised an alert. Dark frames and very low, bed-level views
were skipped more often, though, and failures in them were caught later or not at all.

Keep the chamber or room light on while printing. If you can, mount a webcam above the plate
rather than level with it.

## Stage sensor

A print's status reads as running during the start routine too, while the printer levels, heats
and purges. Those frames look odd (an empty plate, a purge line, wisps at the nozzle) and cause
most unsure results.

If the printer has a sensor for what it's doing right now, such as ha-bambulab's
`sensor.<device>_current_stage`, set it as the stage sensor. Checks then run only while it reads
`printing`, and Last check shows "Getting ready" in between. If the stage sensor is unavailable,
checks run anyway.

## Layer sensor

This one is experimental.

Some failures don't show in a single frame. After a clog or a filament runout the printer keeps
moving but prints nothing new, and a part that came loose can leave it printing into air. With a
layer sensor set, such as ha-bambulab's `sensor.<device>_current_layer`, each check also sends a
frame from 20 layers earlier and asks whether the part has grown since. The distance is the Compare
frames this many layers apart option. Comparing by layers instead of time keeps slow, wide layers
from looking like a stall. Comparison starts once a print is 20 layers in, and each check sends two
images from then on.

On frame pairs from real prints, parts that had grown scored 0.06 to 0.24, so the comparison
didn't make healthy frames unsure. Pairs where nothing had been printed scored only 0.09 to 0.69,
under the default yes threshold of 0.7. On its own it would rarely alert, and a stalled print
mostly shows up as unsure checks in the panel. Leave the layer sensor empty unless you want to try
it.

## After a restart

A detected failure, a snooze and the recent verdicts survive a Home Assistant restart if it was
down for less than an hour. The buttons on an alert you got before the restart still work.
