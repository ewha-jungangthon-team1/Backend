# ============================================================
# 홈 화면 전용 가공 로직
# ============================================================

# 이 온도(℃)를 넘는 상태가 얼마나 지속됐는지 계산할 때 쓰는 기준값
HIGH_TEMPERATURE_THRESHOLD = 30

# load_bias의 부호에 따른 방향 라벨/화면 위치값
LOAD_DIRECTION_LABELS = {"left": "좌측", "right": "우측", "balanced": "균형"}
LOAD_DIRECTION_POSITIONS = {"left": "left_strap", "right": "right_strap", "balanced": "center"}


def calculate_exposure_duration_minutes(session, field_name, threshold):
    """
    가장 최근 값부터 거슬러 올라가면서, threshold를 계속 넘는 구간이
    몇 분 동안 이어지고 있는지 계산한다. (예: "고온 노출 36분")
    """
    readings = session.readings.order_by("-sequence")
    interval_seconds = session.scenario.sample_interval_seconds

    duration_seconds = 0
    for reading in readings:
        if float(getattr(reading, field_name)) < threshold:
            break
        duration_seconds += interval_seconds

    return duration_seconds // 60


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
    load_bias(-1~1)를 화면에 보여줄 0~100 크기(%)로 바꾼다.
    방향(좌/우)은 여기서 다루지 않고 determine_load_direction()이 따로 판단한다.
    → 예: load_bias=-0.68 이면 "좌측 68%", load_bias=0.68이면 "우측 68%"
    """
    return round(abs(float(load_bias)) * 100)


def calculate_deformation_percentage(deformation_ratio):
    """body_deformation_ratio(0~1)를 퍼센트 문자열용 숫자로 바꾼다."""
    return round(float(deformation_ratio) * 100)


def build_smart_material_points(session, latest_reading):
    """화면에 표시할 '스마트소재 감지 포인트' 목록(형태편차/좌우하중/고온노출)을 만든다."""
    exposure_minutes = calculate_exposure_duration_minutes(
        session, "temperature", HIGH_TEMPERATURE_THRESHOLD
    )

    load_direction = determine_load_direction(latest_reading["load_bias"])
    load_label = f"{LOAD_DIRECTION_LABELS[load_direction]} 하중"
    load_position = LOAD_DIRECTION_POSITIONS[load_direction]

    return [
        {
            "position": "body_left",
            "label": "형태 편차",
            "value": f"{calculate_deformation_percentage(latest_reading['body_deformation_ratio'])}%",
        },
        {
            "position": load_position,
            "label": load_label,
            "value": f"{calculate_load_bias_percentage(latest_reading['load_bias'])}%",
        },
        {
            "position": "top_handle",
            "label": "고온 노출",
            "value": f"{exposure_minutes}분",
        },
    ]


def get_ai_summary_placeholder(session):
    """
    TODO(B): AnalysisReport 모델이 만들어지면 이 함수 내부만 실제 조회 로직으로 교체한다.
    지금은 아직 분석 기능이 없으므로 빈 값을 반환한다. (반환 형태(키 이름)는 유지할 것)
    """
    return {"status_text": None, "reasoning": None}


def get_priority_care_placeholder(session):
    """TODO(B): CareAction 모델이 만들어지면 이 함수 내부만 실제 조회 로직으로 교체한다."""
    return {"summary": None, "link": None}