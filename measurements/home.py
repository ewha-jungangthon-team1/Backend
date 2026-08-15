from decimal import ROUND_HALF_UP, Decimal


# load_bias의 부호에 따른 방향 라벨/화면 위치값
LOAD_DIRECTION_LABELS = {"left": "좌측", "right": "우측", "balanced": "균형"}
LOAD_DIRECTION_POSITIONS = {"left": "left_strap", "right": "right_strap", "balanced": "center"}


PRESENTATION_QUANTUM = Decimal("0.01")


def _as_decimal(value):
    return Decimal(str(value))


def _as_presentation_number(value):
    return float(
        _as_decimal(value).quantize(
            PRESENTATION_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    )


def calculate_total_load_kg(strap_load):
    """strap_load kg raw value를 UI용 numeric 전체 하중으로 반환한다."""
    return _as_presentation_number(strap_load)


def calculate_bias_magnitude_percent(load_bias):
    """load_bias의 방향을 제외한 편중 정도(%)를 반환한다."""
    return _as_presentation_number(abs(_as_decimal(load_bias)) * 100)


def calculate_load_distribution_percentages(load_bias):
    """normalized load_bias를 좌우 실제 하중 분포(%)로 변환한다."""
    bias = _as_decimal(load_bias)
    left = (Decimal("50") * (Decimal("1") - bias)).quantize(
        PRESENTATION_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    right = (Decimal("100") - left).quantize(
        PRESENTATION_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    return {
        "left_load_percent": float(left),
        "right_load_percent": float(right),
    }


def calculate_left_load_percent(load_bias):
    return calculate_load_distribution_percentages(load_bias)["left_load_percent"]


def calculate_right_load_percent(load_bias):
    return calculate_load_distribution_percentages(load_bias)["right_load_percent"]


def calculate_shape_deviation_percent(body_deformation_ratio):
    return _as_presentation_number(_as_decimal(body_deformation_ratio) * 100)


def calculate_temperature_c(temperature):
    return _as_presentation_number(temperature)


def calculate_internal_humidity_percent(humidity):
    return _as_presentation_number(humidity)


def calculate_material_moisture_percent(material_moisture_percent):
    if material_moisture_percent is None:
        return None
    return _as_presentation_number(material_moisture_percent)


def build_sensor_presentation_values(
    *,
    strap_load,
    load_bias,
    body_deformation_ratio,
    temperature,
    humidity,
    material_moisture_percent=None,
):
    """DB/API와 무관하게 raw sensor numeric value만 presentation 값으로 변환한다."""
    load_distribution = calculate_load_distribution_percentages(load_bias)
    return {
        "total_load_kg": calculate_total_load_kg(strap_load),
        "bias_magnitude_percent": calculate_bias_magnitude_percent(load_bias),
        **load_distribution,
        "shape_deviation_percent": calculate_shape_deviation_percent(
            body_deformation_ratio
        ),
        "temperature_c": calculate_temperature_c(temperature),
        "internal_humidity_percent": calculate_internal_humidity_percent(humidity),
        "material_moisture_percent": calculate_material_moisture_percent(
            material_moisture_percent
        ),
    }


def determine_load_direction(load_bias):
    """
    load_bias(-1~1)의 부호를 보고 어느 쪽으로 쏠렸는지 판단한다.
    음수 = 왼쪽, 양수 = 오른쪽, 정확히 0 = 균형.
    반환값은 "left" / "right" / "balanced" 중 하나 (라벨 문자열은 LOAD_DIRECTION_LABELS에서 따로 찾음).
    """
    value = float(load_bias)
    if value > 0:
        return "right"
    if value < 0:
        return "left"
    return "balanced"
 
 
def calculate_load_bias_percentage(load_bias):
    """
    기존 Home contract의 정수형 편중 정도를 계산한다.
    abs(load_bias) * 100이며, 좌/우의 실제 하중 분포가 아니다.
    """
    return round(abs(float(load_bias)) * 100)


def calculate_deformation_percentage(deformation_ratio):
    """기존 Home contract의 정수형 형태 편차를 계산한다."""
    return round(float(deformation_ratio) * 100)


def build_smart_material_points(session, latest_reading):
    """기존 Home의 '스마트소재 감지 포인트' contract를 유지한다."""
    load_direction = determine_load_direction(latest_reading["load_bias"])
    load_label = f"{LOAD_DIRECTION_LABELS[load_direction]} 하중"
 
    return [
        {
            "label": "형태 편차",
            "value": f"{calculate_deformation_percentage(latest_reading['body_deformation_ratio'])}%",
        },
        {
            "label": load_label,
            "value": f"{calculate_load_bias_percentage(latest_reading['load_bias'])}%",
        },
        {
            "label": "온도",
            "value": f"{latest_reading['temperature']}℃",
        },
    ]
