# load_bias의 부호에 따른 방향 라벨/화면 위치값
LOAD_DIRECTION_LABELS = {"left": "좌측", "right": "우측", "balanced": "균형"}
LOAD_DIRECTION_POSITIONS = {"left": "left_strap", "right": "right_strap", "balanced": "center"}
 
 
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
    """화면에 표시할 '스마트소재 감지 포인트' 목록(형태편차/좌우하중/온도)을 만든다."""
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
 
