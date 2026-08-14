import random
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.timezone import now as get_current_time

from .models import SimulationScenario
from measurements.models import MeasurementSession, SensorReading

# ============================================================
# 상수 (나중에 바뀔 가능성이 있는 값들은 전부 여기 모아둠)
# ============================================================

# 부스 시연 1회의 실제 진행 시간(초).
DEMO_REAL_SECONDS = 180

# 프론트가 latest-reading을 폴링하는 권장 주기(초). API 2 응답에 그대로 실어보낸다.
POLLING_INTERVAL_SECONDS = 2

# 시연 시나리오 자동 선택 순서 (라운드로빈)
SCENARIO_ROTATION_ORDER = [
    "NORMAL",
    "OVERLOAD",
    "HIGH_TEMPERATURE",
    "HIGH_HUMIDITY",
    "COMPOSITE_RISK",
]

# SensorReading에서 config 기반으로 계산해야 하는 필드 목록
# → 필드 하나 추가/삭제하고 싶으면 여기 + FIELD_PRECISION만 고치면 됨
FIELD_NAMES = [
    "strap_load",
    "humidity",
    "temperature",
    "load_bias",
    "body_deformation_ratio",
]

# 필드별 반올림 자릿수
FIELD_PRECISION = {
    "strap_load": 2,
    "humidity": 2,
    "temperature": 2,
    "load_bias": 4,
    "body_deformation_ratio": 4,
}

# 0 밑으로 내려가면 안 되는 필드 (모델의 MinValueValidator(0)과 맞춤)
FIELD_MIN_CLAMP = {"body_deformation_ratio": 0}

# start/end 패턴일 때 더할 노이즈 크기 (구간 대비 비율)
NOISE_RATIO = 0.02


# ============================================================
# 시나리오 선택
# ============================================================

def pick_next_scenario_type(bag):
    """이 가방이 최근에 쓴 LIVE 시나리오의 다음 순서를 라운드로빈으로 고른다."""
    last_session = (
        MeasurementSession.objects.filter(
            bag=bag, purpose=MeasurementSession.Purpose.LIVE
        )
        .exclude(scenario__isnull=True)
        .order_by("-started_at")
        .first()
    )
    if not last_session or last_session.scenario.scenario_type not in SCENARIO_ROTATION_ORDER:
        return SCENARIO_ROTATION_ORDER[0]

    idx = SCENARIO_ROTATION_ORDER.index(last_session.scenario.scenario_type)
    next_idx = (idx + 1) % len(SCENARIO_ROTATION_ORDER)
    return SCENARIO_ROTATION_ORDER[next_idx]


# ============================================================
# 순수 계산 함수 (DB 건드리지 않음, 값 계산만 함)
# ============================================================

def compute_field_value(field_config, progress_ratio, rng):
    """
    config의 필드 하나를 읽어서 값을 만든다.
    - start/end 있으면: 시간에 따라 선형으로 변하는 값 (+ 약간의 노이즈)
    - min/max 있으면: 그 범위 안에서 매번 랜덤
    """
    if field_config is None:
        return 0

    if "start" in field_config and "end" in field_config:
        start, end = field_config["start"], field_config["end"]
        base = start + (end - start) * progress_ratio
        span = abs(end - start) or 1
        noise = rng.uniform(-NOISE_RATIO, NOISE_RATIO) * span
        return base + noise

    if "min" in field_config and "max" in field_config:
        return rng.uniform(field_config["min"], field_config["max"])

    raise ValueError(f"알 수 없는 config 형식입니다: {field_config}")


def calculate_total_reading_count(scenario):
    """이 시나리오가 총 몇 개의 측정값으로 이루어지는지 계산."""
    return max(scenario.logical_duration_seconds // scenario.sample_interval_seconds, 1)


def is_moisture_trigger(config, sequence, total_count):
    """이 sequence가 수분접촉 이벤트가 발생하는 시점인지 판단."""
    moisture_cfg = config.get("moisture_event", {"enabled": False})
    if not moisture_cfg.get("enabled"):
        return False
    trigger_index = int(total_count * moisture_cfg.get("trigger_at_ratio", 0.5))
    return sequence == trigger_index


def calculate_progress(session):
    """
    지금(now) 이 세션이 논리적으로 몇 번째 지점까지 와 있는지 계산한다.
    반환값: (target_sequence, total_count, 그 구간 안에서의 진행비율 0~1)
    """
    scenario = session.scenario
    elapsed_real = min((timezone.now() - session.started_at).total_seconds(), DEMO_REAL_SECONDS)
    progress_ratio = elapsed_real / DEMO_REAL_SECONDS
    logical_elapsed = progress_ratio * scenario.logical_duration_seconds

    total_count = calculate_total_reading_count(scenario)
    raw_index = logical_elapsed / scenario.sample_interval_seconds
    target_sequence = min(int(raw_index), total_count - 1)
    local_ratio = (raw_index - target_sequence) if target_sequence < total_count - 1 else 0.0

    return target_sequence, total_count, local_ratio


def _lerp(a, b, ratio):
    return float(a) + (float(b) - float(a)) * ratio


# ============================================================
# 측정값 생성 (핵심: 항상 "한 번에 딱 1개"만 만든다)
# ============================================================

def generate_single_reading(session, sequence, total_count):
    """
    지정된 sequence 하나에 대한 SensorReading을 만든다.
    이미 존재하면 다시 만들지 않고 그대로 반환한다 (idempotent).
    → 이 함수가 유일하게 SensorReading을 생성하는 지점.
      HISTORY든 LIVE든 전부 이 함수를 한 번씩 호출해서 채운다.
    """
    existing = session.readings.filter(sequence=sequence).first()
    if existing:
        return existing

    scenario = session.scenario
    config = scenario.config
    rng = random.Random(session.seed + sequence)  # sequence별로 다른 값이지만, 재현은 가능
    progress = sequence / max(total_count - 1, 1)

    values = {}
    for field in FIELD_NAMES:
        raw_value = compute_field_value(config.get(field), progress, rng)
        if field in FIELD_MIN_CLAMP:
            raw_value = max(raw_value, FIELD_MIN_CLAMP[field])
        values[field] = round(raw_value, FIELD_PRECISION[field])

    return SensorReading.objects.create(
        session=session,
        sequence=sequence,
        measured_at=session.started_at + timedelta(seconds=sequence * scenario.sample_interval_seconds),
        moisture_detected=is_moisture_trigger(config, sequence, total_count),
        **values,
    )


def ensure_readings_up_to_now(session):
    """
    지금 이 순간 나와 있어야 할 시퀀스까지, 하나씩 순서대로 생성한다.
    (여러 개를 한 번에 만들지 않고, 매 호출마다 필요한 만큼만 채움 — 보통은 0~1개씩만 새로 생김)
    """
    target_sequence, total_count, _ = calculate_progress(session)
    latest = None
    for seq in range(target_sequence + 1):
        latest = generate_single_reading(session, seq, total_count)
    return latest


def calculate_overall_progress_ratio(session):
    """세션 전체 기준으로 지금 몇 % 진행됐는지 (0.0~1.0). 폴링 종료 시점 판단에 사용."""
    elapsed_real = (timezone.now() - session.started_at).total_seconds()
    return min(max(elapsed_real / DEMO_REAL_SECONDS, 0.0), 1.0)


def calculate_scheduled_end_at(session):
    """
    이 세션이 언제 끝날 예정인지 계산한다. DB에 저장하지 않고 항상 즉석 계산한다.
    - LIVE: started_at + DEMO_REAL_SECONDS (데모 진행시간 기준)
    - HISTORY: 이미 끝난 기록이므로 ended_at을 그대로 사용
    """
    if session.ended_at:
        return session.ended_at
    return session.started_at + timedelta(seconds=DEMO_REAL_SECONDS)


# ============================================================
# 세션 생명주기
# ============================================================

def create_simulation_session(bag, scenario_code, random_seed=None, started_at=None):
    """
    scenario_code를 직접 지정해서 세션을 만든다.
    - HISTORY: 이미 지나간 기록이므로 지금 전부 만들어서 확정해둔다.
    - LIVE: 지금은 아무것도 만들지 않는다. 폴링될 때마다 그 순간 필요한 값만 생성된다.
    반환값: (session, 예정된 총 reading 개수)
    """
    try:
        scenario = SimulationScenario.objects.get(code=scenario_code, is_active=True)
    except SimulationScenario.DoesNotExist as exc:
        raise ValueError(f"존재하지 않는 시나리오 코드입니다: {scenario_code}") from exc

    if started_at is None:
        started_at = timezone.now()

    is_history = scenario.mode == SimulationScenario.Mode.HISTORY
    total_count = calculate_total_reading_count(scenario)

    session = MeasurementSession.objects.create(
        bag=bag,
        scenario=scenario,
        purpose=MeasurementSession.Purpose.HISTORY if is_history else MeasurementSession.Purpose.LIVE,
        seed=random_seed if random_seed is not None else random.randint(1, 10**9),
        started_at=started_at,
        ended_at=(started_at + timedelta(seconds=scenario.logical_duration_seconds)) if is_history else None,
        status=MeasurementSession.Status.COMPLETED if is_history else MeasurementSession.Status.RUNNING,
    )

    if is_history:
        # 과거 기록은 실시간으로 지켜볼 필요가 없는 '이미 끝난 상황'이므로 지금 다 채워둔다.
        for seq in range(total_count):
            generate_single_reading(session, seq, total_count)

    return session, total_count


def close_session(session, ended_at=None):
    """세션 상태만 COMPLETED로 바꾼다. 분석(AI/규칙판단)은 여기서 하지 않는다."""
    session.status = MeasurementSession.Status.COMPLETED
    session.ended_at = ended_at or timezone.now()
    session.save(update_fields=["status", "ended_at"])
    return session


@transaction.atomic
def ensure_live_session(bag):
    """
    RUNNING 상태인 LIVE 세션이 있으면 그대로 반환.
    없으면 다음 시나리오(라운드로빈)로 새 세션을 만든다 (데이터는 아직 안 채워짐).

    select_for_update()로 새로고침 연타 같은 동시 요청에도 세션이 중복 생성되지 않도록 막는다.
    ※ SQLite에서는 로우 단위가 아니라 DB 전체 잠금으로 동작하지만, 데모 규모에선 문제 없음.
    """
    existing = (
        MeasurementSession.objects.select_for_update()
        .filter(
            bag=bag,
            purpose=MeasurementSession.Purpose.LIVE,
            status=MeasurementSession.Status.RUNNING,
        )
        .first()
    )
    if existing:
        elapsed = (timezone.now() - existing.started_at).total_seconds()
        if elapsed <= DEMO_REAL_SECONDS:
            return existing, False
        # 앱을 닫고 가버려서 데모 시간이 다 지났는데도 RUNNING으로 남아있던 세션 → 정리
        existing.status = MeasurementSession.Status.COMPLETED
        existing.ended_at = timezone.now()
        existing.save(update_fields=["status", "ended_at"])

    scenario_type = pick_next_scenario_type(bag)
    scenario_code = f"{scenario_type}_LIVE"  # fixture의 code 규칙과 동일 (예: OVERLOAD_LIVE)
    session, _total_count = create_simulation_session(bag, scenario_code)
    return session, True


# ============================================================
# 폴링 API가 사용하는 조회 함수
# ============================================================

def get_latest_reading(session):
    """
    지금 이 순간 화면에 보여줘야 할 센서값을 반환한다.
    - LIVE: 지금 시점까지 필요한 값을 그때그때 하나씩 생성하며 따라감
    - HISTORY: 이미 만들어진 마지막 값을 그대로 반환
    화면이 부드럽게 움직이도록, 방금 값과 그 직전 값 사이를 구간 진행률만큼
    보간해서 '보여주기'만 한다 (DB에 새로 저장하지 않음).
    """
    observed_at = get_current_time()

    if session.purpose == MeasurementSession.Purpose.HISTORY:
        latest = session.readings.order_by("-sequence").first()
        local_ratio = 0.0
        progress_ratio = 1.0
    else:
        _target_sequence, _total_count, local_ratio = calculate_progress(session)
        latest = ensure_readings_up_to_now(session)
        progress_ratio = calculate_overall_progress_ratio(session)

        # 시연 시간 종료 시 Session 자동 종료
        if progress_ratio >= 1.0 and session.status == MeasurementSession.Status.RUNNING:
            close_session(session)

    if latest is None:
        return None

    previous = (
        session.readings.filter(sequence=latest.sequence - 1).first()
        if latest.sequence > 0
        else None
    )

    if previous is not None:
        display = {
            field: round(_lerp(getattr(previous, field), getattr(latest, field), local_ratio), FIELD_PRECISION[field])
            for field in FIELD_NAMES
        }
        moisture_detected = bool(previous.moisture_detected or latest.moisture_detected)
    else:
        display = {field: float(getattr(latest, field)) for field in FIELD_NAMES}
        moisture_detected = latest.moisture_detected

    return {
        "session_id": session.id,
        "sequence": latest.sequence,
        "measured_at": latest.measured_at,
        "observed_at": observed_at,
        "scenario_type": session.scenario.scenario_type,
        "progress_ratio": round(progress_ratio, 3),
        "is_finished": progress_ratio >= 1.0,
        "moisture_detected": moisture_detected,
        **display,
    }

def get_latest_session_for_bag(bag):
    return bag.sessions.order_by("-started_at").first()
