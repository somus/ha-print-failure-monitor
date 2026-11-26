# Tuning

## Options

These are under the integration's Configure button.

| Section | Option | Default | Notes |
|---|---|---|---|
| Alerts | Pause the printer automatically | off | Same as the Auto-pause switch |
| | Notify | none | Notify services for actionable notifications |
| | Snooze after Continue | 15 min | |
| | Print report | on | A summary with a strip of frames when a watched print ends |
| Detection | Check every (early) | 30 s | For the early phase of a print |
| | Early phase | 15 min | Counted from when it starts printing. 0 uses the later interval throughout |
| | Then check every | 60 s | For the rest of the print |
| | Frames to remember | 4 | |
| | Failed frames needed | 3 | No more than frames to remember |
| | Yes threshold | 0.7 | An answer above this counts as yes |
| | No threshold | 0.3 | An answer below this counts as no |
| | Compare frames this many layers apart | 20 | Needs a [layer sensor](setup.md#layer-sensor). 0 turns comparison off |
| | Daily check limit | 0 | Model calls per day before automatic checks stop. 0 means no limit |
| Frame history | Save frames | all | All frames, only frames that didn't look fine, or none |
| | Keep for | 7 days | Older frames and results are deleted hourly |
| | Storage limit | 1024 MB | Oldest images are deleted first above this |
| Advanced | Model | provider default | See [Choosing a model](#choosing-a-model) |
| | Base URL | provider default | Sends the provider's request format to a gateway or proxy |
| | Camera description | generic | Sent with every frame |
| | Max image size | 1280 px | Larger frames are downscaled, but never below 256 px on the short side, the smallest providers accept |
| | Request timeout | 30 s | |
| | Questions | built-ins | See [Customizing questions](#customizing-questions) |

Before you change thresholds or questions, label some frames and try the change with
[Test settings](panel.md#labels-and-test-settings).

## Choosing a model

The default and the alternative were picked from tests on 25 public camera frames: 14 failures
(spaghetti, detached parts, nozzle blobs) and 11 healthy prints, from a Bambu Lab P1S, Prusa i3,
Creality Ender, Elegoo Centauri Carbon and other setups. No model raised a false alarm on any
healthy frame.

| Model | Camera description | Failures caught |
|---|---|---|
| `google/gemini-decision-1` (default) | default | 13/14 |
| `openai/gpt-6-luna-decisions` | describes the exact printer and camera position | 9/14, but 5/6 on the P1S with the widest margin of any model |

Gemini works best with the default description; describing the camera in more detail didn't help
it. Results change noticeably with the model, the camera and the description, so if you try a
different model, compare it on your own prints.

A specific description for a Bambu Lab P1S looks like this: *Chamber camera of a Bambu Lab P1S 3D
printer, looking down at the build plate during a print.*

## Customizing questions

Every frame gets one gate question, `plate_visible`, and three failure questions: `spaghetti`,
`detached` and `nozzle_blob`. A frame counts as failed when the plate is visible and any failure
question scores above the yes threshold. The Questions option (under Advanced) changes that set
and takes YAML:

```yaml
warping: true                    # turn on an optional built-in question
nozzle_blob: false               # disable a built-in failure question
spaghetti:                       # reword a built-in; unlisted fields keep their defaults
  no_means: Only solid printed parts, supports, or purge waste near the chute on the left.
stringing:                       # add your own failure question; all three fields are required
  instructions: Are there many fine hair-like strings stretched between printed parts in `frame`?
  yes_means: Thin strings criss-cross the gaps between parts, like a cobweb.
  no_means: Gaps between parts are clear, or only a few strings are visible.
```

`instructions` is the question; refer to the camera image as `` `frame` ``. `yes_means` and
`no_means` describe what a yes and a no look like. Name the specific thing that causes confusion on
your setup, like a purge chute in view. Don't use `true:`, `false:`, `yes:` or `no:` as keys,
because YAML reads them as booleans.

Some rules:

- Question IDs use lowercase letters, numbers and underscores. Each one shows up as an attribute of
  the Failure probability sensor.
- `plate_visible` can be reworded but not disabled, because it stops dark or blocked frames from
  counting as clean. At least one failure question must stay enabled.
- Each added question costs a few extra tokens per frame. The image is sent once per check.

Changing the wording changes the answers, sometimes more than switching models does. In testing,
changing only the camera description moved `gpt-6-luna` from catching 2 of 6 P1S failures to 5 of
6. After any change, watch the probabilities on a few healthy and failed prints before you let it
pause the printer.

### Warping

`warping` is built in but off by default. It asks whether a corner or edge of a part has lifted or
curled off the plate. Every extra question is another chance of a false alarm, and this one is the
least reliable on webcams level with the bed. Turn it on if you print materials or large flat parts
that warp, like ABS, ASA or nylon. It catches clearly curled corners; a slight lift at the far side
of the plate is usually too small to see.

### What isn't covered

Layer shifts are too subtle in a chamber camera view. In testing, a layer-shift question only
scored high on close-ups where the part was already flagged as detached.

Printing into air after a clog or a filament runout shows as a part that stops growing, which a
single frame can't show. The experimental [layer sensor](setup.md#layer-sensor) comparison is the
only check for it.
