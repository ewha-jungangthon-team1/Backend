from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from measurements.models import MeasurementSession


def _as_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    return float(value)


def _get_thresholds(care_guideline):
    if not isinstance(care_guideline, dict):
        care_guideline = {}

    temperature_range = care_guideline.get("recommended_temp_range_c")
    max_temperature_c = None
    if isinstance(temperature_range, (list, tuple)) and len(temperature_range) == 2:
        max_temperature_c = _as_float(temperature_range[1])

    return {
        "max_load_kg": _as_float(care_guideline.get("max_load_kg")),
        "max_temperature_c": max_temperature_c,
        "max_humidity_percent": _as_float(care_guideline.get("max_humidity_percent")),
        "max_abs_load_bias": _as_float(care_guideline.get("max_abs_load_bias")),
        "max_body_deformation_ratio": _as_float(
            care_guideline.get("max_body_deformation_ratio")
        ),
    }


def _average(values):
    return sum(values) / len(values)


def _detected_days(values, threshold):
    if threshold is None:
        return None
    return sum(value > threshold for value in values)


def _quantize_decimal(value, decimal_places):
    quantum = Decimal("1").scaleb(-decimal_places)
    return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)


def build_history_daily_series(session):
    daily_series = []

    for reading in session.readings.order_by("sequence"):
        deformation_ratio = _quantize_decimal(
            reading.body_deformation_ratio,
            4,
        )
        daily_series.append(
            {
                "date": timezone.localdate(
                    reading.measured_at, timezone.get_default_timezone()
                ).isoformat(),
                "load_kg": float(_quantize_decimal(reading.strap_load, 2)),
                "deformation_ratio": float(deformation_ratio),
                "deformation_percent": float(
                    _quantize_decimal(deformation_ratio * 100, 2)
                ),
                "moisture_detected": reading.moisture_detected,
            }
        )

    return daily_series


def calculate_history_metrics(session):
    if session is None:
        raise ValueError("session is required.")
    if session.purpose != MeasurementSession.Purpose.HISTORY:
        raise ValueError("Only HISTORY sessions can be analyzed.")
    if session.status != MeasurementSession.Status.COMPLETED:
        raise ValueError("Only COMPLETED sessions can be analyzed.")

    readings = list(session.readings.order_by("sequence"))
    if not readings:
        raise ValueError("At least one SensorReading is required.")

    care_guideline = session.bag.product_model.care_guideline
    thresholds = _get_thresholds(care_guideline)

    loads = [float(reading.strap_load) for reading in readings]
    temperatures = [float(reading.temperature) for reading in readings]
    humidities = [float(reading.humidity) for reading in readings]
    load_biases = [float(reading.load_bias) for reading in readings]
    deformations = [float(reading.body_deformation_ratio) for reading in readings]

    return {
        "reading_count": len(readings),
        "load": {
            "average_kg": _average(loads),
            "max_kg": max(loads),
            "overload_detected_days": _detected_days(
                loads, thresholds["max_load_kg"]
            ),
        },
        "temperature": {
            "average_c": _average(temperatures),
            "max_c": max(temperatures),
            "high_temperature_detected_days": _detected_days(
                temperatures, thresholds["max_temperature_c"]
            ),
        },
        "humidity": {
            "average_percent": _average(humidities),
            "max_percent": max(humidities),
            "high_humidity_detected_days": _detected_days(
                humidities, thresholds["max_humidity_percent"]
            ),
        },
        "moisture": {
            "detected_days": sum(reading.moisture_detected for reading in readings),
            "detected_any": any(reading.moisture_detected for reading in readings),
        },
        "load_bias": {
            "max_absolute": max(abs(value) for value in load_biases),
            "latest": load_biases[-1],
            "biased_days": _detected_days(
                [abs(value) for value in load_biases],
                thresholds["max_abs_load_bias"],
            ),
        },
        "deformation": {
            "max_ratio": max(deformations),
            "latest_ratio": deformations[-1],
            "deformation_detected_days": _detected_days(
                deformations, thresholds["max_body_deformation_ratio"]
            ),
        },
    }
