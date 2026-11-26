"""Profitec BLE protocol and async client. Machine writes require explicit opt-in."""
from .protocol import (
    PRESSURE_UUID, STATUS_UUID, CONTROL_UUID, LOG_UUID,
    Pressure, MachineStatus, SettingsBlock, RawPacket,
    decode, decode_pressure, decode_status, decode_settings, settings_request, brew_commands,
)
from .client import ProfitecClient, scan

__all__ = [
    "PRESSURE_UUID", "STATUS_UUID", "CONTROL_UUID", "LOG_UUID", "Pressure",
    "MachineStatus", "SettingsBlock", "RawPacket", "decode", "decode_pressure",
    "decode_status", "decode_settings", "settings_request", "brew_commands", "ProfitecClient", "scan",
]

from .shots import summarize, export_csv
__all__ += ["summarize", "export_csv"]

from .settings import named_settings, setting_command, setting_values, SETTABLE, DoseSettings, GeneralSettings, TimeSettings, PreInfusion, ScheduleWindow
from .profiles import load_profile, save_profile
__all__ += ["named_settings", "setting_command", "setting_values", "SETTABLE", "DoseSettings", "GeneralSettings", "TimeSettings", "PreInfusion", "ScheduleWindow", "load_profile", "save_profile"]

from .settings import schedule_command, setting_value
from .profiles import profile_diff, restore_plan
from .shots import metrics, plot_html
from .monitor import ReadyTracker, ShotRecorder, annotate_shot, list_shots, reindex
from .bbw import brew_by_weight
__all__ += ["schedule_command", "setting_value", "profile_diff", "restore_plan", "metrics", "plot_html",
            "ReadyTracker", "ShotRecorder", "annotate_shot", "list_shots", "reindex", "brew_by_weight"]
