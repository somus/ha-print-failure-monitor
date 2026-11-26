"""Constants for Print Failure Monitor."""

from __future__ import annotations

from .questions import DEFAULT_CAMERA_DESCRIPTION

DOMAIN = "print_failure_monitor"

# Entry data (set up once)
CONF_PROVIDER = "provider"
CONF_API_KEY = "api_key"
CONF_CAMERA = "camera"
CONF_PRINT_STATUS = "print_status"
CONF_ACTIVE_STATES = "active_states"
CONF_PAUSED_STATES = "paused_states"
CONF_PAUSE_BUTTON = "pause_button"
CONF_RESUME_BUTTON = "resume_button"
CONF_STOP_BUTTON = "stop_button"
CONF_STAGE = "stage"
CONF_STAGE_STATES = "stage_states"
CONF_LAYER = "layer"  # current layer number, for comparing frames a number of layers apart

# Status values that mean a print is paused, not over: Bambu Lab, Moonraker, PrusaLink (attention: waiting for
# the user), OctoPrint (resuming is on its way back), Creality and Elegoo. Used when an entry predates the setting.
DEFAULT_PAUSED_STATES = ["pause", "paused", "pausing", "resuming", "attention", "filament_unload_paused"]
# Stage values during which frames are checked, when a stage sensor is set (ha-bambulab's Current stage).
DEFAULT_STAGE_STATES = ["printing"]

# Entry options (changeable any time)
CONF_ENABLED = "enabled"
CONF_AUTO_PAUSE = "auto_pause"
CONF_NOTIFY = "notify_services"
CONF_SNOOZE = "snooze_minutes"
CONF_INTERVAL = "interval"
CONF_WINDOW = "window"
CONF_REQUIRED = "required"
CONF_YES = "yes_threshold"
CONF_NO = "no_threshold"
CONF_MODEL = "model"
CONF_BASE_URL = "base_url"
CONF_CAMERA_DESCRIPTION = "camera_description"
CONF_MAX_IMAGE_SIZE = "max_image_size"
CONF_TIMEOUT = "timeout"
CONF_QUESTIONS = "questions"
CONF_SAVE_FRAMES = "save_frames"
CONF_RETENTION_DAYS = "retention_days"
CONF_MAX_STORAGE_MB = "max_storage_mb"
CONF_CROP = "crop"  # [left, top, right, bottom] as fractions of the frame, set in the panel; None = whole frame
CONF_EARLY_MINUTES = "early_minutes"
CONF_LATER_INTERVAL = "later_interval"
CONF_COMPARE_LAYERS = "compare_layers"
CONF_PRINT_REPORT = "print_report"
CONF_DAILY_LIMIT = "daily_limit"

DEFAULT_OPTIONS = {
    CONF_ENABLED: True,
    CONF_AUTO_PAUSE: False,
    CONF_NOTIFY: [],
    CONF_SNOOZE: 15,
    CONF_INTERVAL: 30,  # for the first early_minutes of printing, when most failures start
    CONF_EARLY_MINUTES: 15,
    CONF_LATER_INTERVAL: 60,
    CONF_WINDOW: 4,
    CONF_REQUIRED: 3,
    CONF_YES: 0.7,
    CONF_NO: 0.3,
    CONF_MODEL: "",
    CONF_BASE_URL: "",
    CONF_CAMERA_DESCRIPTION: DEFAULT_CAMERA_DESCRIPTION,
    CONF_MAX_IMAGE_SIZE: 1280,
    CONF_TIMEOUT: 30,
    CONF_QUESTIONS: {},
    CONF_SAVE_FRAMES: "all",
    CONF_RETENTION_DAYS: 7,
    CONF_MAX_STORAGE_MB: 1024,
    CONF_CROP: None,
    CONF_COMPARE_LAYERS: 20,  # only used when a layer sensor is set
    CONF_PRINT_REPORT: True,
    CONF_DAILY_LIMIT: 0,  # model calls per day; 0 = no limit
}

# Fired once per detected failure, alongside the notification.
EVENT_FAILURE = f"{DOMAIN}_failure"

# Notification actions, sent as f"{ACTION_PREFIX}{entry_id}_{action}"
ACTION_PREFIX = "PRINT_FAILURE_"
ACTION_PAUSE = "PAUSE"
ACTION_STOP = "STOP"
ACTION_RESUME = "RESUME"
ACTION_CONTINUE = "CONTINUE"
