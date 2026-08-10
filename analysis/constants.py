from enum import Enum


class RuleCode(str, Enum):
    HIGH_LOAD = "HIGH_LOAD"
    HIGH_TEMPERATURE = "HIGH_TEMPERATURE"
    HIGH_HUMIDITY = "HIGH_HUMIDITY"
    MOISTURE = "MOISTURE"
    LOAD_BIAS = "LOAD_BIAS"
    DEFORMATION = "DEFORMATION"


class Severity(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    DANGER = "DANGER"
    GOOD = "GOOD"
