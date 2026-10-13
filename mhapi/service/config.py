"""Paths and parameters of the service."""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CALIBRATION = REPO / "calibration"
SAFETY_TOML = REPO / "mhapi" / "safety.toml"
CAMERA_URL = "http://raspberrypi.local:8080/stream.mjpg"
PORT = 8742


class Section:
    def __init__(self, d):
        self.__dict__.update(d)

    def __repr__(self):
        return repr(self.__dict__)


@dataclass
class Params:
    heat: Section
    people: Section
    motion: Section
    camera: Section
    printer: Section

    @classmethod
    def load(cls, path=SAFETY_TOML, overrides=None):
        d = tomllib.loads(Path(path).read_text())
        for key, val in (overrides or {}).items():  # "heat.heater_watchdog_s": 5 (tests and the simulator)
            sect, name = key.split(".", 1)
            d[sect][name] = val
        return cls(**{k: Section(v) for k, v in d.items()})


@dataclass
class Paths:
    sessions: Path
    calibration: Path = CALIBRATION
    position_file: Path = None    # last confirmed position, rewritten after every move
    heartbeat: Path = None        # read by mhapi-heatguard (H5)
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        self.sessions = Path(self.sessions)
        self.sessions.mkdir(parents=True, exist_ok=True)
        if self.heartbeat is None:
            self.heartbeat = self.sessions / "heartbeat.json"
        if self.position_file is None:
            self.position_file = self.calibration / "position.json"
