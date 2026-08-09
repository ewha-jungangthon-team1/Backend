# 프로젝트 설계 컨텍스트 (백엔드 A 담당)

이 문서는 팀과 이미 논의를 마친 설계 내용입니다.
아래 내용을 그대로 따라 구현하세요. 임의로 구조를 바꾸지 마세요.

---

## 1. 프로젝트 개요

가방 제품의 DPP(Digital Product Passport)와 센서 데이터를 제공하는 웹 서비스.
실제 하드웨어(ESP32)는 아직 없고, 백엔드가 센서 데이터를 시뮬레이션으로 생성한다.
사용자는 `Bag.public_token`으로 로그인 없이 공개 DPP 페이지에 접근한다.

## 2. 내 역할 (백엔드 A)

담당: **제품, DPP, 시뮬레이션 데이터의 생산과 조회**
담당 모델: `ProductModel`, `Bag`, `LifecycleRecord`, `SimulationScenario`, `MeasurementSession`, `SensorReading`
(User는 Django 기본 `auth.User` 사용 — 커스텀 User 앱 안 만듦)

백엔드 B 담당: `AnalysisReport` (AI 분석/케어 설명 생성) — 별도 `analysis` 앱, 이번 구현 범위 아님.
단, `AnalysisReport`는 `MeasurementSession`을 1:1로 참조할 예정이니, `MeasurementSession`을 B가 참조하기 쉽게 필드명을 명확히 지어야 함.

## 3. Django 앱 구조 (반드시 이 구조로)

```
products/
    models.py       # ProductModel, Bag
    serializers.py
    views.py         # public_token 기반 DPP 기본 정보 조회 API
    urls.py
    admin.py

dpp/
    models.py       # LifecycleRecord
    serializers.py
    views.py         # 생애주기 기록 목록 조회 API
    urls.py
    admin.py

simulation/
    models.py       # SimulationScenario
    admin.py
    services/
        generator.py         # 공통 센서 데이터 생성기 (순수 함수, DB 모델 import 금지)
        scenario_service.py   # 시나리오 조회 로직
    management/
        commands/
            generate_sensor_history.py
    # views.py, urls.py, serializers.py 없음 (사용자에게 시나리오 API 노출 안 함)

measurements/
    models.py       # MeasurementSession, SensorReading
    serializers.py
    views.py         # 라이브 Polling 조회, 과거 세션 목록/상세, 센서값 목록 API
    urls.py
    admin.py
    services/
        session_service.py    # 과거 세션 생성 로직
        live_service.py         # 라이브 세션 ensure 로직 (get-or-create + 만료 체크)
```

**중요 — import 방향 규칙**: `measurements`는 `simulation`을 import해도 되지만, `simulation`은 절대 `measurements`를 import하면 안 됨 (순환 참조 방지). `simulation/services/generator.py`는 숫자와 config만 다루고 Django 모델을 몰라야 함.

## 4. 모델 상세 (PostgreSQL 타입 기준)

### ProductModel
| 필드 | 타입 | 비고 |
|---|---|---|
| id | BIGINT | PK |
| brand | VARCHAR(100) | |
| model_name | VARCHAR(100) | |
| material | VARCHAR(100) | |
| care_guideline | JSONB | 브랜드 케어 기준 (시연용 가정 데이터, 실제 브랜드 공식 자료 아님) |

### Bag
| 필드 | 타입 | 비고 |
|---|---|---|
| id | BIGINT | PK |
| product_model | FK → ProductModel | |
| owner | FK → User (Django auth.User) | |
| nfc_uid | VARCHAR(100) | NFC 태그 물리적 식별값 |
| public_token | UUID (또는 랜덤 토큰 문자열) | **unique, index 필수**. URL에 노출되는 공개 조회 키. DB의 정수 PK(id)를 URL에 노출하지 않기 위함 |
| created_at | TIMESTAMPTZ | |

- 관계: ProductModel 1 : Bag N / User 1 : Bag N
- `public_token`은 생성 시점에 자동 발급 (예: `uuid.uuid4()`), 재발급 기능은 MVP에서 보류 가능

### LifecycleRecord
| 필드 | 타입 | 비고 |
|---|---|---|
| id | BIGINT | PK |
| bag | FK → Bag | |
| record_type | VARCHAR(50) | "repair"/"recovery"/"ownership_transfer" 등 (시연용 가정) |
| description | TEXT | |
| recorded_at | TIMESTAMPTZ | |

- 관계: Bag 1 : LifecycleRecord N
- 역할: 원본 센서 데이터가 아니라 "의미 있는 이벤트"만 선별 기록 (DPP에 표시)

### SimulationScenario
| 필드 | 타입 | 비고 |
|---|---|---|
| id | BIGINT | PK |
| code | VARCHAR(50) | 고유 코드, 예: "OVERLOAD_LIVE" (unique) |
| name | VARCHAR(100) | 시나리오명 |
| scenario_type | VARCHAR(30) | NORMAL, OVERLOAD, HIGH_TEMPERATURE, HIGH_HUMIDITY, COMPOSITE_RISK 등 |
| mode | VARCHAR(20) | LIVE 또는 HISTORY |
| logical_duration_seconds | INTEGER | 데이터상 전체 기간 (LIVE는 약 180초, HISTORY는 수 시간) |
| sample_interval_seconds | INTEGER | 측정값 간격 (LIVE는 1~2초, HISTORY는 1~5분) |
| config | JSONB | 구간별 목표값/노이즈 설정 (예: {"phases":[{start_sec, end_sec, strap_load_target}, ...]}) |
| version | INTEGER | 설정 버전 |
| is_active | BOOLEAN | |
| created_at | TIMESTAMPTZ | |

- **사용자에게 시나리오 선택 UI를 노출하지 않음** — 이 테이블은 백엔드 내부에서만 참조
- 관계: SimulationScenario 1 : MeasurementSession N
- 초기 데이터는 fixture 또는 management command로 주입 (구체 방식은 A가 판단해서 진행)

### MeasurementSession
| 필드 | 타입 | 비고 |
|---|---|---|
| id | BIGINT | PK |
| bag | FK → Bag | |
| scenario | FK → SimulationScenario | **nullable** (실제 센서가 나중에 붙을 경우 시나리오 없이도 세션이 존재할 수 있어야 함) |
| purpose | VARCHAR(20) | "live" / "history" / "demo" / "test" |
| seed | BIGINT (또는 INTEGER) | 재현 가능한 난수 시드, 세션 생성 시 반드시 저장 |
| started_at | TIMESTAMPTZ | |
| ended_at | TIMESTAMPTZ | nullable (진행 중이면 null) |
| status | VARCHAR(20) | RUNNING / COMPLETED (시연용 가정 값) |

- 관계: Bag 1 : MeasurementSession N / MeasurementSession 1 : SensorReading N / MeasurementSession 1 : AnalysisReport 1 (B 담당 테이블과 연결)
- **라이브 세션은 하나의 Bag당 동시에 하나만 유효해야 함** — "ensure" 로직 필요 (없으면 생성, 있으면 재사용). 새로고침마다 새 세션이 생기면 안 됨
- **라이브 데이터는 AI 리포트/과거 기록 대상에서 제외** — purpose="live"인 세션은 분석 대상에서 걸러야 함

### SensorReading
| 필드 | 타입 | 비고 |
|---|---|---|
| id | BIGINT | PK |
| session | FK → MeasurementSession | |
| strap_load | DECIMAL | 스트랩 하중 |
| strap_strain | DECIMAL | 스트랩 변형률 |
| humidity | DECIMAL | 습도 |
| moisture_detected | BOOLEAN | 수분 접촉 여부 |
| temperature | DECIMAL | 온도 |
| measured_at | TIMESTAMPTZ | 실제 측정된 것으로 간주하는 시각 (DB 저장 시각인 created_at과 다름) |

- 관계: MeasurementSession 1 : SensorReading N
- 저장 시 **반드시 `bulk_create()`** 사용 (하나씩 `save()` 금지 — 성능 문제)

## 5. 반드시 지켜야 할 설계 원칙 (10가지)

1. 사용자 화면에 시뮬레이션 시나리오 선택 버튼을 노출하지 않는다.
2. 새로고침할 때마다 새로운 라이브 세션을 생성하지 않는다 (ensure 로직으로 기존 세션 재사용).
3. 라이브 데이터는 과거 기록 및 AI 리포트 대상에 포함하지 않는다.
4. API 요청 처리 코드 안에서 `sleep()`을 사용하지 않는다.
5. 여러 센서값을 저장할 때는 `bulk_create()`를 사용한다.
6. 센서값은 완전 무작위가 아니라 시나리오 구간과 직전 값을 기반으로 생성한다.
7. 사용한 `random_seed`를 `MeasurementSession`에 저장한다.
8. `measured_at`(측정 시각)과 DB 저장 시각(`created_at`)을 구분한다.
9. 라이브 데이터와 과거 데이터는 동일한 공통 센서 생성기를 재사용한다.
10. 라이브 세션과 과거 세션의 목적 및 데이터 사용 범위를 명확히 구분한다 (purpose 필드).

## 6. 라이브 vs 과거 데이터 생성 방식 차이

| 구분 | 라이브 | 과거 기록 |
|---|---|---|
| 목적 | 현재 센서·즉시 케어 | 통계·AI 리포트 |
| 논리적 시간 | 약 2~3분 (180초) | 약 4~8시간 |
| 측정 간격 | 1~2초 | 1~5분 |
| 상태 | RUNNING | COMPLETED |
| 리포트 포함 | 제외 | 포함 |
| 생성 시점 | DPP 접속 또는 관리자 실행 | 시연 전 초기 데이터 생성 (management command) |
| 사용자 노출 | 실시간 센서 영역 (Polling) | 최근 기록·리포트 |

**중요 — 라이브 데이터도 실시간으로 하나씩 만들지 않는다.** 세션 생성 시점에 90개(또는 설정된 개수)의 SensorReading을 `bulk_create()`로 한 번에 미리 만들어 `measured_at`을 미래 시각까지 찍어둔다. Polling API는 `measured_at <= 현재시각`인 것 중 최신 것을 반환해서, 실제로는 이미 다 만들어진 데이터인데도 시간이 흐르며 새 값이 나오는 것처럼 보이게 한다. API 요청 안에서 `sleep()`으로 시간을 흘려보내지 않는다 (원칙 4).

## 7. 내가(A) 만들 API 목록

| # | Method | URL | 목적 |
|---|---|---|---|
| 1 | GET | `/api/dpp/{public_token}/` | public_token으로 가방 기본 정보(ProductModel 포함) 조회 |
| 2 | GET | `/api/dpp/{public_token}/lifecycle/` | 생애주기 기록 목록 조회 |
| 3 | GET | `/api/dpp/{public_token}/live/` | 현재 라이브 센서값 Polling 조회 (없으면 ensure 로직으로 생성) |
| 4 | GET | `/api/dpp/{public_token}/sessions/` | 과거 측정 세션 목록 조회 |
| 5 | GET | `/api/dpp/{public_token}/sessions/{session_id}/` | 특정 과거 세션 상세 조회 |
| 6 | GET | `/api/dpp/{public_token}/sessions/{session_id}/readings/` | 특정 과거 세션의 센서값 목록 조회 |

과거 데이터 생성은 API가 아니라 **management command**로 만든다 (`generate_sensor_history`).
API가 아닌 이유: 심사위원/사용자가 직접 트리거할 기능이 아니라, 시연 전에 개발자가 미리 실행해서 데이터를 채워두는 용도이기 때문.

## 8. 오늘 진행할 순서

1. 모델 전체 구현 (ProductModel, Bag+public_token, LifecycleRecord, SimulationScenario, MeasurementSession+scenario/purpose/seed, SensorReading) + migration
2. Admin 등록 + 초기 데이터 (가방 1개, 시나리오 기본값, LifecycleRecord 샘플)
3. 기본 조회 API 3종 (public_token DPP 조회 / LifecycleRecord 목록 / 과거 세션 목록·상세)
4. (시간이 되면) 공통 센서 데이터 생성기
5. (시간이 되면) 과거 데이터 생성 management command

## 9. 코드 작성 시 규칙

파일을 하나 작성/수정할 때마다, diff 다음에 아래 형식으로 반드시 설명할 것:

### [파일 역할]
### [핵심 개념] (쉬운 설명 → 정확한 용어 순서)
### [설계 이유]
### [문제점 및 리스크]
### [팀원과 논의 필요]

- 설명 없이 코드만 던지지 말 것
- 여러 파일을 한 번에 다 짜지 말 것, 하나씩 순서대로
- 사용자가 "다음"이라고 말하기 전까지 다음 파일로 넘어가지 말 것
