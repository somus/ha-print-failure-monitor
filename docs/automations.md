# Automations

Every [entity](panel.md#entities) works in your own automations: the sensors as triggers and
conditions, the switches and the button as actions. The integration also fires an event and adds
two actions.

## Failure event

`print_failure_monitor_failure` fires once per detected failure, at the same moment as the
notification and after any auto-pause.

| Field | Value |
|---|---|
| `entry_id` | The monitored printer's config entry |
| `printer` | Its name |
| `camera` | Its camera entity |
| `paused` | Whether auto-pause paused the printer |
| `answers` | Every question's probability for the triggering frame |
| `failures` | Only the failure questions above the yes threshold, e.g. `{"spaghetti": 0.92}` |
| `frame_id` | The triggering frame in the history, or `null` if frames aren't saved |
| `frame_url` | A signed link to that frame that works without a login for 24 hours, or the live camera if frames aren't saved |
| `frame_path` | The same frame as a plain path, or the live camera. Use this as the `image` of a companion app notification: the apps add their own login, and the iOS app breaks signed links |
| `panel_url` | The Print failures panel opened on that frame |

## Actions

| Action | Does | Fields |
|---|---|---|
| `print_failure_monitor.check` | Checks a frame now and returns `result`, `answers` and `error`. During a print it counts toward the failure window | `config_entry_id`; optional `camera` to check a different camera, which only returns the result and changes nothing |
| `print_failure_monitor.snooze` | Clears a detected failure and skips checks, like Continue. The snooze ends when the print does | `config_entry_id`; optional `minutes` (default: the snooze option; `0` ends a snooze and checks right away) |

In the automation editor, the Printer field picks the entry. In YAML, the entry ID is in the URL
of the printer's page under Settings > Devices & services > Print Failure Monitor.

## Examples

Entity IDs depend on your device names; see the [printer guide](printers.md).

Flash a light and announce which failure was seen:

```yaml
triggers:
  - trigger: event
    event_type: print_failure_monitor_failure
actions:
  - action: light.turn_on
    target:
      entity_id: light.office
    data:
      color_name: red
      flash: long
  - action: tts.speak
    target:
      entity_id: tts.home_assistant_cloud
    data:
      media_player_entity_id: media_player.kitchen
      message: >
        {{ trigger.event.data.printer }} may have failed:
        {{ trigger.event.data.failures.keys() | join(', ') | replace('_', ' ') }}.
```

Stop the print if it's still running 10 minutes after an alert. Pausing doesn't clear the alert,
so the condition keeps this from stopping a print you paused to look at:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.my_printer_print_failure
    to: "on"
    for: "00:10:00"
conditions:
  - condition: state
    entity_id: sensor.my_printer_print_status
    state: running
actions:
  - action: button.press
    target:
      entity_id: button.my_printer_stop
```

Auto-pause only overnight:

```yaml
triggers:
  - trigger: time
    at: "23:00:00"
    id: "on"
  - trigger: time
    at: "08:00:00"
    id: "off"
actions:
  - action: "switch.turn_{{ trigger.id }}"
    target:
      entity_id: switch.my_printer_auto_pause
```

Check once the first layer is done and log the result:

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.my_printer_current_layer
    above: 1
actions:
  - action: print_failure_monitor.check
    data:
      config_entry_id: YOUR_ENTRY_ID
    response_variable: check
  - action: logbook.log
    data:
      name: My printer
      message: "First layer: {{ check.result }}"
```
