# 👜 스마트소재 가방을 위한 AI 케어 서비스, KEPT 입니다.

<!-- 대표 이미지 / 서비스 메인 배너 -->
<img width="2880" height="1620" alt="표지" src="https://github.com/user-attachments/assets/abeac5f0-d083-4c4c-8e21-1aeeeb497086" />
<br>
KEPT는 스마트소재 기술이 적용된 MCM 가방이 출시된 미래 상황을 가정해, 제품 구매 이후의 새로운 럭셔리 케어 경험을 제공하는 서비스를 제안합니다.

가방에 적용된 스마트소재가 사용 중 발생하는 온도와 습도, 수분 접촉 등의 물리적·환경적 변화를 감지하고, AI가 이를 제품의 소재와 구조, 최근 사용 이력, 평소 사용 패턴, 외부 환경 정보와 함께 분석합니다. **사용자가 복잡한 센서 수치를 직접 확인하거나 해석할 필요 없이 현재 가방이 어떠한 상태인지, 당장 관리가 필요한지, 필요하다면 어떤 케어를 언제 어떻게 수행해야 하는지를 구체적으로 안내받을 수 있습니다.**

MCM과 같은 럭셔리 가방 사용자는 높은 비용을 지불한 제품을 가능한 한 좋은 상태로 오래 사용하고자 하며, 실제로 더스트백 보관, 오염 제거, 형태 유지 등 다양한 관리 행동을 수행합니다. 그러나 **기존의 관리 방식은 대부분 오염, 변형, 곰팡이와 같은 문제가 이미 눈에 보이기 시작한 이후에 이루어집니다.** 특히 비나 높은 습도에 노출되거나, 무거운 내용물을 장시간 넣어두거나, 고온 환경에 방치하는 등의 상황에서는 제품 내부에서 변화가 진행되고 있어도 사용자가 즉시 알아차리기 어렵습니다. 또한 상태 변화를 발견한 이후에도 가죽이나 합성피혁 등 제품 소재에 따라 적절한 관리 방법이 달라 사용자가 스스로 관리 시점과 방법을 판단해야 한다는 부담이 존재합니다. **잘못된 판단이나 관리 지연은 작은 상태 변화를 큰 손상과 높은 수선 비용으로 이어지게 할 수 있습니다.**

따라서 본 서비스는 단순한 상태 경고에 그치지 않고, **사용자가 직접 확인하기 어려운 제품의 변화를 ‘스마트소재’를 통해 먼저 감지하고 AI가 여러 데이터의 관계를 해석해 현재 가장 필요한 케어 한 가지를 제안합니다.** 또한 안내 이후에도 가방의 상태가 평소 범위로 회복되고 있는지 수치측정과 함께 사용자가 자신의 제품 상태를 지속적으로 이해하고 더 적절한 시점에 관리할 수 있도록 돕습니다. 이를 통해 **MCM의 제품 경험을 구매 시점에서 그치지 않고, 사용과 관리의 전 과정으로 확장하는 개인화된 럭셔리 케어 경험을 제공하고자 합니다.**
<br>
<br>
🔨 **기획 · 디자인 · 개발 기간**  
2026.07.23. - 2026.08.21.

<br>

## 💻 Member

| 역할 | 이름 | GitHub | 담당 |
| --- | --- | --- | --- |
| AI / Backend | 명아령 | [@github-id](https://github.com/github-id) | AI, 백엔드 기능 개발 |
| Backend | 이연우 | [@github-id](https://github.com/github-id) | 백엔드 기능 개발 |

<br>

## 🛠️ Tech Stack

| Category   | Tech Stack                                                                                                                                                                                                                              |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Language   | ![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge\&logo=python\&logoColor=white)                                                                                                                                 |
| Framework  | ![Django](https://img.shields.io/badge/Django-092E20?style=for-the-badge\&logo=django\&logoColor=white) ![DRF](https://img.shields.io/badge/Django%20REST%20Framework-ff1709?style=for-the-badge\&logo=django\&logoColor=white)         |
| Database   | ![SQLite](https://img.shields.io/badge/SQLite-07405E?style=for-the-badge\&logo=sqlite\&logoColor=white)                                                                                                                                 |
| Deployment | ![PythonAnywhere](https://img.shields.io/badge/PythonAnywhere-1D9FD7?style=for-the-badge\&logo=pythonanywhere\&logoColor=white) ![Gabia Cloud](https://img.shields.io/badge/Gabia%20Cloud-0072CE?style=for-the-badge\&logoColor=white)  |
| AI         | ![OpenAI](https://img.shields.io/badge/OpenAI-412991?style=for-the-badge\&logo=openai\&logoColor=white)                                                                                                                                 |
| Interface  | ![REST API](https://img.shields.io/badge/REST%20API-009688?style=for-the-badge\&logo=fastapi\&logoColor=white) ![NFC](https://img.shields.io/badge/NFC--based%20Product%20Access-00599C?style=for-the-badge\&logo=nfc\&logoColor=white) |

<br>

## 📁 File Directory

```
Backend
├── .github/                        # GitHub Actions 워크플로우 (CI/CD 등)
│
├── accounts/                       # 사용자 계정 앱 (인증/회원 관리)
│   ├── migrations/                 # DB 마이그레이션 파일
│   ├── admin.py                    # Django 관리자 페이지 설정
│   ├── apps.py                     # 앱 설정
│   ├── models.py                   # 계정 관련 DB 모델
│   ├── tests.py                    # 테스트 코드
│   └── views.py                    # API 뷰
│
├── analysis/                       # 가방 상태 분석 & AI 케어 추천 핵심 로직
│   ├── ai/                         # OpenAI API 연동 모듈
│   │   ├── client.py                # OpenAI API 클라이언트
│   │   ├── contracts.py             # AI 응답 스키마/데이터 계약 정의
│   │   ├── errors.py                # AI 관련 예외 처리
│   │   ├── fallbacks.py             # AI 호출 실패 시 대체(fallback) 응답
│   │   ├── generation.py            # AI 응답 생성 로직
│   │   ├── history.py               # 분석/케어 이력 관리
│   │   ├── live_care.py             # 실시간 케어 안내 생성
│   │   └── prompts.py               # AI 프롬프트 템플릿
│   ├── migrations/                 # DB 마이그레이션 파일
│   ├── admin.py                    # 관리자 페이지 설정
│   ├── apps.py                     # 앱 설정
│   ├── comparisons.py              # 평소 사용 패턴 대비 비교 로직
│   ├── constants.py                # 상수 정의
│   ├── live_care_context.py        # 실시간 케어 판단을 위한 컨텍스트 구성
│   ├── live_care.py                # 실시간 케어 제안 로직
│   ├── live_rules.py               # 실시간 케어 판단 규칙
│   ├── live_state.py               # 실시간 상태(온습도 등) 판단 로직
│   ├── metrics.py                  # 상태 지표 계산
│   ├── models.py                   # 분석 관련 DB 모델
│   ├── presentation.py             # 응답 포맷팅/사용자 노출용 가공 계층
│   ├── rules.py                    # 상태 판단 규칙
│   ├── serializers.py              # DRF 시리얼라이저
│   ├── services.py                 # 비즈니스 로직 서비스 계층
│   ├── test_live_care_api.py       # 실시간 케어 API 테스트
│   ├── test_live_care_content.py   # 케어 안내 콘텐츠 테스트
│   ├── test_live_care.py           # 실시간 케어 로직 테스트
│   ├── test_live_rules.py          # 케어 판단 규칙 테스트
│   ├── test_live_state.py          # 실시간 상태 판단 테스트
│   ├── tests.py                    # 기타 테스트 코드
│   ├── urls.py                     # URL 라우팅
│   └── views.py                    # API 뷰
│
├── configs/                        # Django 프로젝트 전역 설정
│   ├── asgi.py                     # ASGI 서버 설정
│   ├── settings.py                 # 프로젝트 설정 (DB, 앱, 환경변수 등)
│   ├── urls.py                     # 루트 URL 라우팅
│   └── wsgi.py                     # WSGI 서버 설정
│
├── measurements/                   # 센서 측정 데이터(온도/습도 등) 처리 앱
│   ├── migrations/                 # DB 마이그레이션 파일
│   ├── admin.py                    # 관리자 페이지 설정
│   ├── apps.py                     # 앱 설정
│   ├── home.py                     # 홈 화면 관련 API/뷰
│   ├── models.py                   # 측정 데이터 DB 모델
│   ├── serializers.py              # DRF 시리얼라이저
│   ├── tests.py                    # 테스트 코드
│   ├── urls.py                     # URL 라우팅
│   └── views.py                    # API 뷰
│
├── media/                          # 사용자 업로드 파일 저장 디렉토리
│
├── products/                       # 제품(가방) 정보 관리 앱
│   ├── fixtures/                   # 초기 데모용 데이터 (fixture)
│   ├── management/                 # 커스텀 manage.py 커맨드
│   ├── migrations/                 # DB 마이그레이션 파일
│   ├── admin.py                    # 관리자 페이지 설정
│   ├── apps.py                     # 앱 설정
│   ├── models.py                   # 제품 관련 DB 모델
│   ├── serializers.py              # DRF 시리얼라이저
│   ├── test_seed_demo_history_data.py  # 데모용 히스토리 데이터 시딩 테스트
│   ├── test_seed_demo_live_data.py     # 데모용 실시간 데이터 시딩 테스트
│   ├── tests.py                    # 테스트 코드
│   ├── urls.py                     # URL 라우팅
│   └── views.py                    # API 뷰
│
├── simulation/                     # 센서 데이터 시뮬레이션(데모/테스트용) 앱
│   ├── migrations/                 # DB 마이그레이션 파일
│   ├── admin.py                    # 관리자 페이지 설정
│   ├── apps.py                     # 앱 설정
│   ├── models.py                   # 시뮬레이션 관련 DB 모델
│   ├── scenarios.json              # 시뮬레이션 시나리오 데이터
│   ├── serializers.py              # DRF 시리얼라이저
│   ├── services.py                 # 시뮬레이션 로직
│   ├── test_live_state_api.py      # 실시간 상태 API 테스트
│   ├── tests.py                    # 테스트 코드
│   ├── urls.py                     # URL 라우팅
│   └── views.py                    # API 뷰
│
├── .gitignore
├── check_polling.py                # 주기적 상태 체크(폴링) 스크립트
├── db.sqlite3.before_demo_backup   # 데모 전 DB 백업 파일
├── manage.py                       # Django 프로젝트 관리 스크립트
├── PROJECT_CONTEXT.md              # 프로젝트 컨텍스트 문서
├── README.md
└── requirements.txt                # 파이썬 패키지 의존성 목록
```

