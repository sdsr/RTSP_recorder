# RTSP Recorder

Linux에서 RTSP 카메라 영상을 시간표·날짜·날씨 조건에 따라 MP4로 저장하는 도구입니다. 데스크톱 GUI와 브라우저 UI를 지원하며 기존 ED 카메라 설정과 호환됩니다.

[Linux GUI 배포 ZIP](dist/RTSP_recorder_Linux_GUI_20261027.zip)을 받거나 다음과 같이 저장소를 내려받습니다.

```bash
git clone https://github.com/sdsr/RTSP_recorder.git
cd RTSP_recorder
sudo apt install python3-tk ffmpeg
bash 실행.sh
```

리눅스 데스크톱 GUI 빠른시작: [HTML](manual/리눅스_GUI_빠른시작.html) · [PDF](manual/리눅스_GUI_빠른시작.pdf)

기존 브라우저 UI·명령어 상세 설명서: [HTML](manual/ED_카메라_예약녹화_유지보수_설명서.html) · [PDF](manual/ED_카메라_예약녹화_유지보수_설명서.pdf)

**ED에는 `record.py` 한 파일만 복사하고, 리눅스 데스크톱 창에서 설정·녹화 시작·중지를 할 수 있습니다.** 화면이 JSON 설정을 관리하며, 기존 브라우저 UI·JSON·INI·명령 옵션 방식도 계속 지원합니다. 카메라 주소는 기존 `setup.ini`에서 읽습니다. Python 3.8 이상과 FFmpeg, 데스크톱 창에는 Tkinter, 녹화 실행에는 Linux, 부팅 자동 실행에는 systemd가 필요합니다. NVIDIA GPU·CUDA·EMS·DS·Docker와 별도 pip 패키지는 필요하지 않습니다.

기본 일정은 **매일 한국 시간 06:00~11:00, 12:00~17:00, 18:00~22:00**입니다. 각각 5시간·5시간·4시간, **하루 총 14시간**을 **30분 단위 MP4**로 저장합니다. 원본 H.264/H.265 영상을 재인코딩 없이 저장하며, 음성·AI 검출 박스·분석 정보는 포함하지 않습니다.

## 리눅스 데스크톱 창으로 사용하기

Ubuntu의 바탕화면에 로그인한 상태에서 사용합니다. 처음 한 번 필요한 프로그램을 설치합니다.

```bash
sudo apt update
sudo apt install python3-tk ffmpeg
```

`record.py`가 있는 폴더에서 실행하면 브라우저 없이 녹화 관리 창이 열립니다. 기존 `recording.json`은 그대로 사용하며, 파일이 없으면 기본 설정으로 생성합니다.

```bash
python3 record.py --config recording.json --gui
```

배포 ZIP에 포함된 `실행.sh`를 사용할 때는 해당 폴더에서 `bash 실행.sh`를 실행해도 됩니다. 폴더 이름이나 위치를 고정할 필요는 없습니다.

1. **장비 · 저장 위치**에서 실제 운영 중인 `setup.ini`와 영상 저장 폴더를 선택합니다. **자동 찾기** 결과에서 운영 파일을 고르거나 **파일 선택**으로 직접 지정합니다. 저장 위치는 **폴더 선택**으로 고릅니다.
2. 교차로명·분할 길이·시간표를 확인하고 **설정 저장 · 적용**과 **설정 검사**를 실행합니다.
3. **한 번만 녹화**에서 5분을 지정하고 **시험 녹화 시작**을 누른 뒤 저장 영상을 확인합니다. 시험 녹화는 날짜·시간표·날씨 조건과 관계없이 실행합니다.
4. 계속 사용할 때는 **예약 녹화 시작**을 누릅니다. 지정 시간 밖에서는 대기하며 매일 시간표를 반복합니다.

날씨 조건은 **시간표 · 날씨**에서 켭니다. 개인용은 Open-Meteo를 선택하고 시·군·구와 날씨를 고르면 API 키 없이 사용할 수 있습니다. 그날 예보에 선택한 날씨가 한 번이라도 있으면 그날의 남은 예약 시간대를 녹화합니다. 이미 지난 영상은 복원하지 않습니다.

### 다음부터 앱 메뉴에서 실행

창의 **실행 아이콘 만들기**를 누르거나 아래 명령을 한 번 실행합니다. `sudo` 없이 현재 사용자의 앱 메뉴에 **ED 카메라 녹화**가 등록됩니다.

```bash
python3 record.py --config recording.json --desktop-install
```

아이콘은 현재 `record.py`와 설정 파일의 절대 경로를 사용합니다. 등록 후 폴더를 옮겼다면 새 위치에서 다시 등록하세요. **아이콘 등록은 부팅 자동 녹화를 설정하지 않으며**, 창을 여는 것만으로 녹화를 시작하지도 않습니다.

데스크톱 창에서 시작한 녹화는 창을 유지하는 동안 동작하며 최소화해도 계속됩니다. 녹화 중 창을 닫으면 확인 후 녹화를 정상 종료합니다. 영상 마무리가 지연되면 안내가 표시되며 창이 유지됩니다. 별도로 실행된 서비스는 이 창에서 종료하지 않습니다.

기존 서비스가 녹화 중이라는 안내가 나오고 데스크톱 창으로 운영하려면 먼저 기존 서비스를 중지합니다. 아래 명령은 다음 부팅의 자동 시작도 해제하며 영상과 설정은 남깁니다.

```bash
sudo systemctl disable --now goomi-camera-recorder.service
```

로그인하지 않아도 부팅부터 자동 녹화하려면 아래의 기존 systemd 설치 방식을 사용합니다. 바탕화면이 없는 SSH 환경에서는 `--gui` 대신 `--ui`와 SSH 터널을 사용하세요.

## 브라우저 화면으로 사용하기

최신 `record.py` 한 파일에 **브라우저 화면과 공식 지역 목록까지 포함**되어 있습니다. 현장에 HTML·CSS·JavaScript나 추가 Python 패키지를 따로 복사할 필요가 없습니다.

```bash
cd /home/dis/tools
python3 record.py --config recording.json --ui
```

설정 JSON이 없으면 기본 파일을 생성합니다. 같은 ED의 브라우저에서 **http://127.0.0.1:8765/** 를 엽니다. 화면이 자동으로 열리지 않으면 주소를 직접 입력합니다. 다른 PC에서 접속할 때는 아래 SSH 연결을 유지한 상태에서 그 PC의 브라우저로 같은 주소를 엽니다. `ED_IP`는 실제 장비 IP로 바꿉니다.

```bash
ssh -N -L 8765:127.0.0.1:8765 dis@ED_IP
```

UI는 장비의 로컬 주소에만 열립니다. ED IP의 8765 포트를 외부에 공개하지 않습니다.

| 화면 | 할 수 있는 작업 |
| --- | --- |
| 녹화 현황 | 예약 녹화 시작·중지, 카메라 수·남은 공간·현재 녹화 상태 확인, 예보 확인 |
| 한 번만 녹화 | 지금부터 5분 등으로 시험하거나 오늘 시작 시각 지정. 날짜·날씨 조건을 대체하고 자동 종료 |
| 시간표 · 날씨 | 시간 구간 추가·삭제, 매일 또는 지정 날짜 선택, 예보 조건·지역·제공처 선택, 필요한 경우 인증 설정 |
| 장비 설정 | setup.ini 찾기·선택, 교차로명·저장 폴더·분할 길이·보관 일수 변경 |
| 설정 저장 · 적용 | JSON에 저장. 이 화면에서 시작한 예약 녹화가 실행 중이면 재시작해 적용 |

실행 중인 예약 녹화에 설정을 적용할 때는 저장 경로·FFmpeg·필요한 인증키를 먼저 확인합니다. 시험 녹화 중에는 시험이 끝나거나 중지한 뒤 설정을 저장합니다.

`auto`로 setup.ini를 찾다가 여러 개가 발견되면 **장비 설정 → 설정 찾기**에서 실제 운영 경로를 선택합니다. 비개발자가 JSON 문법을 편집할 필요가 없습니다. 카메라 주소·사용 여부는 기존 setup.ini를 따르며, JSON에 명시한 카메라 목록도 유지합니다.

브라우저 창을 닫는 것만으로 녹화가 중지되지는 않습니다. 녹화를 멈출 때는 화면의 **녹화 중지** 버튼을 사용합니다. 수동으로 UI를 띄운 터미널을 종료하면 이 화면에서 시작한 녹화도 정상 종료합니다. 별도 서비스로 실행 중인 녹화는 먼저 해당 서비스를 중지해야 화면에서 시작할 수 있습니다.

### UI도 부팅 시 자동으로 실행

먼저 화면에서 카메라·저장 위치를 설정하고 검사합니다. 그다음 아래 명령으로 기존 관리 서비스를 UI 방식으로 등록합니다.

```bash
sudo python3 record.py --config recording.json --ui --install
```

서비스 이름은 기존과 같은 `goomi-camera-recorder.service`입니다. UI와 예약 녹화를 부팅 시 자동 시작합니다. UI에서 중지해도 다음 부팅에는 다시 자동 시작하므로, 부팅 자동 실행까지 끄려면 `sudo systemctl disable --now goomi-camera-recorder.service`를 사용합니다. 기존 CLI 설치와 같은 소유 확인·서비스 계정 사전 검사를 거치며, 외부 서비스는 덮어쓰지 않습니다.

서비스를 완전히 제거하려면 `sudo python3 record.py --uninstall`을 실행합니다. 설정·키·저장 영상은 삭제하지 않습니다. 설치 없이 `--ui`만 실행해도 화면을 사용할 수 있습니다.

## 날씨 예보가 맞는 날에 기존 시간표 전체 실행

개인·비상업용은 **Open-Meteo**를 선택하면 API 키·회원가입·연락처 없이 사용할 수 있습니다. 새 설정의 기본 제공처도 Open-Meteo입니다. **날씨 조건 자체는 기본 꺼짐**을 유지합니다. 기존 JSON에 기상청이 지정되어 있으면 화면에서 제공처를 Open-Meteo로 바꾸고 저장합니다.

| 제공처 | API 키 | 필요한 설정 |
| --- | --- | --- |
| Open-Meteo | 없음 | 개인·비상업용 무료 API. 지역만 지정 |
| MET Norway | 없음 | 실제 연락 가능한 개인 이메일 또는 프로젝트 홈페이지를 `met_contact`에 입력 |
| 기상청 공공데이터포털 | 필요 | 해당 서비스의 인증키 등록 |
| 기상청 API허브 | 필요 | API허브 인증키 등록 |

화면의 **시간표 · 날씨 → 예보 제공처**에서 선택합니다. MET Norway에는 회사 정보가 필수인 것이 아니라 운영 주체의 식별·연락 정보가 필요합니다. 실제 운영 시 예제·가짜 연락처를 쓰지 않습니다. Open-Meteo의 무료 API는 비상업용이므로 상업용 운영에는 해당 서비스의 유료 조건이나 다른 제공처를 검토합니다.

**“오늘 예보에 비가 한 번이라도 있으면, 오늘의 06–11시·12–17시·18–22시를 모두 녹화”**하는 조건입니다. 녹화 순간의 실제 비 여부로 판단하지 않습니다.

1. 예보 조건을 켜고 비·눈·맑음·흐림 중 원하는 조건을 선택합니다. 여러 개면 하나만 맞아도 충족합니다.
2. 시·도와 시·군·구를 선택합니다. 읍·면·동을 비우면 시·군·구 대표 지점을 사용합니다.
3. 제공처를 선택합니다. Open-Meteo는 추가 인증 설정이 없습니다.
4. **설정 저장 · 적용 → 예보 조회**로 지역·자료 시각·판정 결과를 확인한 뒤 예약 녹화를 시작합니다.

JSON에 미리 지정할 때는 다음 항목을 추가합니다. `enabled: false`이면 날씨와 관계없이 기존 일정으로 녹화합니다.

```json
"weather_condition": {
  "enabled": true,
  "region": {
    "sido": "부산광역시",
    "sigungu": "해운대구",
    "eupmyeondong": ""
  },
  "conditions": ["rain"],
  "provider": "open_meteo",
  "poll_minutes": 30,
  "met_contact": ""
}
```

제공처 이름은 `open_meteo`, `met_no`, `data_go_kr`, `kma_api_hub`입니다. 기상청 제공처에서는 `api_key_file`을 지정할 수 있으며 기본 파일은 JSON 옆의 `kma_service.key`입니다. 키는 화면에서 등록하거나 기존 `KMA_SERVICE_KEY` 환경 변수로 지정합니다. MET Norway와 Open-Meteo는 이 키를 사용하지 않습니다.

조건은 `rain`(비 계열), `snow`(눈 계열), `clear`(맑음), `cloudy`(구름 또는 흐림)입니다. 기상청의 강수형태, Open-Meteo의 WMO 날씨 코드, MET Norway의 날씨 기호를 이 조건으로 변환합니다. 비/눈·진눈깨비로 명시된 예보는 비와 눈 모두에 해당합니다. 강수확률 숫자만으로 비라고 판단하지 않습니다. 제공처마다 모델과 해상도가 달라 예보 결과도 다를 수 있습니다.

```text
client29_요트경기장_West_비예보일_20261019_060000_KST_000000.mp4
```

당일 조건을 한 번 충족하면 판정을 저장해 남은 일정을 유지합니다. 재시작하거나 이후 예보가 바뀌어도 해당 날짜의 판정을 유지하며, 지역·조건·제공처를 바꾸면 이전 판정을 재사용하지 않습니다. 오후에 처음 조건이 맞으면 이후 남은 예약 구간만 녹화합니다. 이미 지나간 영상은 복원할 수 없습니다. 자정을 넘는 일정은 시작 날짜를 기준으로 합니다.

Open-Meteo에서는 한국 시간 기준 당일 전체 시간별 데이터와 앞뒤 날짜를 받습니다. MET Norway는 과거 예보를 제공하지 않으므로 처음 실행하기 전에 지나간 시간대는 확인하지 못할 수 있습니다. 실행 중 받은 예보와 승인된 날짜는 저장해 유지합니다. MET Norway의 6시간·12시간 집계 예보는 정확한 강수 시각을 추정하지 않고 표시된 시간 범위가 겹치는 날짜에 적용합니다.

### 데이터 사용량과 캐시

- 예보는 **카메라마다 받지 않고 장비의 한 지역을 한 번 조회해 공유**합니다.
- 기본 간격은 30분이며 작은 무작위 지연을 더해 요청이 정각에 몰리지 않게 합니다.
- gzip/deflate 압축, 서버의 Expires/Cache-Control, ETag/Last-Modified 조건부 요청을 사용합니다. 유효한 캐시가 있으면 버튼을 다시 눌러도 추가 다운로드하지 않습니다.
- 304 응답이면 저장한 데이터를 재사용하고, 호출 제한이 발생하면 Retry-After를 지킵니다. 캐시는 저장 폴더의 `.weather-http-*.json`에 보관해 프로그램 재시작 후에도 재사용합니다.
- 화면에는 해당 지역의 당일 **수신 응답 본문 바이트와 요청 횟수**를 표시합니다. HTTPS·요청 헤더·IP 등 부가 통신량을 포함한 통신요금 계측값은 아닙니다.
- 2026-10-19 해운대구 실측 Open-Meteo 응답 본문은 압축 후 **711바이트**였습니다. 이를 30분마다 48번 모두 다시 받는다고 계산하면 약 **34KB/일**입니다. 실제 크기는 지역·예보·압축에 따라 달라집니다.
- 브라우저 UI 응답도 gzip으로 압축하며, 화면이 보이지 않을 때 상태 조회를 멈춥니다. 원격 UI 통신량과 카메라 영상 수신량은 예보 조회량과 별도입니다.

인터넷 연결이 필요합니다. 예보를 받지 못하거나 유효한 자료가 없으면 새로운 날씨 조건 녹화는 대기하고 원인을 표시합니다. 이미 조건을 충족한 당일은 남은 일정을 유지합니다. 5분 시험 녹화는 날씨 조건과 관계없이 사용할 수 있습니다.

### 자료 출처

- [Open-Meteo API 문서](https://open-meteo.com/en/docs) · [무료/상업용 사용 조건](https://open-meteo.com/en/pricing)
- [MET Norway Locationforecast](https://api.met.no/doc/locationforecast/HowTO) · [서비스 이용 조건](https://api.met.no/doc/TermsOfService) · [출처 표시 정책](https://api.met.no/doc/License)
- 공개 예보 데이터의 출처는 각 제공처이며 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 등 해당 제공 조건을 따릅니다. 이 프로그램은 원본 날씨 코드를 녹화 조건으로 분류해 표시합니다.
- [기상청 공공데이터포털](https://www.data.go.kr/data/15084084/openapi.do) · [기상청 API허브](https://apihub.kma.go.kr/apiInfo.do)
- 내장 행정지역·좌표 출처: [기상청 동네예보 지점 좌표, 2026년 7월 1일 기준](https://data.kma.go.kr/community/board/detailBoard.do?bbrdTypeNo=4&bbrdNo=68822). 지역 목록과 예보 제공처는 별개입니다.

기존 `weather` 문자열은 자동 날씨 조건을 사용하지 않을 때 파일명에 넣는 수동 표시입니다. 자동 판단은 `weather_condition`으로 설정합니다.

## 한 번만 녹화: 실행할 때 시간 지정

최신 `record.py`로 교체한 뒤 `--record-minutes`에 녹화할 분을 지정합니다. **JSON을 수정하지 않고 즉시 녹화하며, 시간이 끝나면 파일을 마무리하고 프로그램도 자동 종료합니다.** 설치는 필요하지 않습니다.

```bash
# JSON의 카메라·저장 위치·이름·날씨는 유지하고 지금부터 5분만 녹화
python3 record.py --config recording.json --record-minutes 5

# 오늘 18:30에 시작해 10분만 녹화한 뒤 종료 (한국 시간)
python3 record.py --config recording.json --start-at 18:30 --record-minutes 10
```

| 옵션 | 의미 |
| --- | --- |
| `--record-minutes 5` | 준비를 마친 시점부터 5분간 녹화 후 자동 종료. `10`이면 10분 |
| `--start-at 18:30` | 오늘 한국 시간 18:30에 한 번 시작. 반드시 `--record-minutes`와 함께 사용 |
| `--segment-minutes 1` | 선택 사항. 녹화 도중 파일을 약 1분씩 나눔. 전체 녹화 시간은 `--record-minutes`가 결정 |

이 모드에서는 설정 파일의 `recording_dates`, `time_ranges`, `schedules`를 이번 실행에만 대체합니다. JSON·INI 파일과 기존 서비스 설정은 수정하지 않습니다. 날짜가 과거로 설정되어 있어도 즉시 시험할 수 있으며, 다음 날 자동으로 반복하지 않습니다. 카메라 접속 지연·끊김이 있으면 실제 영상 길이는 지정한 시간보다 짧아질 수 있습니다.

`--start-at`은 **오늘의 아직 지나지 않은 시각**만 허용합니다. 이미 지난 시각이면 오류를 표시하며 다음 날로 자동 변경하지 않습니다. 자정을 넘겨 종료하는 것은 가능합니다. `--record-minutes`는 1초(`0.016666666666666666`분)부터 1,440분까지 지정할 수 있습니다.

JSON을 지정하지 않고 실행하려면 기존 카메라 설정 경로를 명시합니다. 옆에 `recording.ini`가 있으면 해당 INI의 다른 설정을 사용하고, 없으면 내장 기본값을 사용합니다.

```bash
python3 record.py \
  --setup-ini /home/dis/실제프로젝트/config/setup.ini \
  --record-minutes 5
```

검사만 하려면 같은 명령에 `--check`를 붙입니다. `--record-minutes`는 설치·설정 생성 명령 및 기존 반복 일정 옵션(`--time-ranges`, `--start-times`, `--duration-hours`)과 함께 사용하지 않습니다. 기존 서비스가 같은 저장 폴더를 사용 중이라면 먼저 `sudo systemctl stop goomi-camera-recorder.service`로 중지하고, 시험 후 필요하면 `sudo systemctl start goomi-camera-recorder.service`로 다시 시작합니다.

## JSON으로 교차로명·저장 폴더·날씨·실행 날짜 지정

최신 `record.py`를 복사한 폴더에서 다음 명령을 실행합니다. JSON은 현장에서 만들 수 있으므로 예제 파일을 따로 복사할 필요가 없습니다. 이미 있는 파일은 덮어쓰지 않습니다.

```bash
python3 record.py --init-config --config recording.json
nano recording.json
python3 record.py --config recording.json --check
```

다음은 **2026년 10월 15일에만** 기본 세 구간을 녹화하는 전체 JSON 예제입니다. 날짜·교차로명·날씨·저장 위치를 실제 현장 값으로 바꿉니다. JSON은 큰따옴표를 사용하며 주석이나 마지막 항목 뒤 쉼표를 허용하지 않습니다.

```json
{
  "setup_ini": "auto",
  "output_dir": "/home/dis/recordings",
  "intersection_name": "요트경기장",
  "weather": "맑음",
  "recording_dates": ["2026-10-15"],
  "cameras": "auto",
  "time_ranges": ["06:00-11:00", "12:00-17:00", "18:00-22:00"],
  "segment_minutes": 30,
  "retention_days": 0,
  "min_free_gb": 5,
  "ffmpeg": "ffmpeg",
  "reconnect_seconds": 10,
  "stall_seconds": 30
}
```

| 설정 | 동작 |
| --- | --- |
| `intersection_name` | 모든 카메라 파일명에 사용할 교차로명. `""` 또는 생략하면 기존 `client번호.ini`의 이름 사용 |
| `output_dir` | 저장할 폴더. `"auto"`이면 기존 프로젝트의 `runtime/scheduled_recordings` |
| `weather` | 직접 입력한 날씨를 파일명에 추가. 예: `"맑음"`, `"흐림"`, `"비"`, `"눈"`. `""` 또는 생략하면 날씨 부분 제외 |
| `recording_dates` | 녹화를 시작할 한국 날짜 목록. 예: `["2026-10-15", "2026-10-17"]`. `[]` 또는 생략하면 매일 반복 |
| `time_ranges` | 각 지정 날짜에 실행할 시간 구간. 기본은 06–11시, 12–17시, 18–22시 |
| `segment_minutes` | MP4 분할 길이(분). 30분은 `30`, 1시간은 `60` |
| `setup_ini` | `"auto"`로 탐색하거나 사용할 설정의 절대 경로 지정 |
| `cameras` | `"auto"`는 `INUSE=True`인 카메라. 일부만 지정할 때는 아래 카메라 목록 사용 |

`--init-config --config recording.json`으로 생성한 기본값은 `intersection_name`·`weather`가 `""`, `recording_dates`가 `[]`, 저장 위치가 `"auto"`입니다. 기본 생성 파일은 매일 반복하므로 **하루만 녹화하려면 날짜를 반드시 입력**합니다. 설정 파일을 저장하고 검사·재시작해야 수정한 값이 적용됩니다.

현재 운영 파일이 `/home/dis/2026busan_sis/config/setup.ini`라면 `setup_ini`에 그 절대 경로를 입력합니다. 자동 탐색에서 여러 파일을 찾으면 임의로 하나를 고르지 않습니다. 상대 경로는 `recording.json`이 있는 폴더를 기준으로 해석합니다.

### 날짜와 날씨의 적용 범위

- `recording_dates`는 **녹화 구간의 시작 날짜**입니다. 예를 들어 날짜가 `2026-10-15`, 시간이 `23:00-01:00`이면 15일 23시부터 16일 01시까지 녹화합니다.
- 지정하지 않은 날짜에는 새 구간을 시작하지 않습니다. 모든 지정 일정이 끝나도 프로그램은 대기하며, 자동 종료하지 않습니다. 수동 실행은 `Ctrl+C`로 종료합니다.
- 지정 날짜의 녹화 구간 중간에 실행하면 남은 시간만 녹화합니다. 과거에 놓친 영상은 복원하지 않습니다.
- 날씨는 **사용자가 입력한 고정 문구**입니다. 자동 조회하거나 날짜마다 자동 변경하지 않으며, 여러 지정 날짜에도 같은 값을 사용합니다. 날씨를 바꾸려면 JSON을 수정하고 재시작합니다.

기존 `CLIENTNO`와 방향은 계속 기존 설정에서 읽습니다. 위 예제에서 클라이언트 번호가 29, 방향이 서쪽이면 다음과 같이 저장합니다.

```text
/home/dis/recordings/cam1/2026-10-15/run_시작시각_고유번호/
  client29_요트경기장_West_맑음_20261015_060000_KST_000000.mp4
```

시각은 각 분할 파일 생성을 ED가 처음 확인한 한국 시간입니다. 파일명에 사용할 수 없는 문자·공백은 `_`로 정리하고, 너무 긴 이름·날씨는 줄입니다. 순번은 각 run 안에서 증가합니다.

### JSON으로 실행하거나 서비스 전환

```bash
# 직접 실행: 설치하지 않아도 지정 날짜·시간에 녹화, Ctrl+C로 종료
python3 record.py --config recording.json

# 부팅 자동 실행: 검사 성공 후 JSON을 참조하도록 설치
python3 record.py --config recording.json --check && \
  sudo python3 record.py --config recording.json --install
```

**JSON을 사용할 때는 `--config recording.json`을 명시합니다.** 옵션을 빼면 기존 `recording.ini` 또는 내장 기본값을 사용합니다. INI와 JSON의 설정은 합쳐지지 않으므로 기존 저장 위치·카메라 선택·보관 정책도 JSON에 옮겨 적습니다. INI로 이미 설치한 서비스도 위 설치 명령을 한 번 실행해야 JSON을 참조합니다. 별도 서비스 계정을 사용했다면 기존과 같은 `--user`를 지정합니다.

JSON 참조로 설치한 뒤 설정을 바꿀 때는 다음 절차를 사용합니다. 실행 중인 서비스와 수동 실행을 같은 저장 폴더로 동시에 시작하지 않습니다.

```bash
nano recording.json
python3 record.py --config recording.json --check && \
  sudo systemctl restart goomi-camera-recorder.service
```

### 카메라별로 이름·날씨가 다른 경우

`"cameras": "auto"` 대신 다음 목록을 사용합니다. 명시한 카메라는 `INUSE`와 관계없이 선택하며, 카메라 항목의 값이 공통 값보다 우선합니다. 카메라 항목의 `intersection_name: ""`는 기존 INI 이름, `weather: ""`는 날씨 제외를 뜻합니다.

```json
"cameras": [
  {"name": "cam1", "section": "CAM1"},
  {"name": "cam2", "section": "CAM2", "intersection_name": "다른교차로", "weather": "비"}
]
```

`name`은 저장 하위 폴더에 쓰는 카메라 식별자로 기존처럼 영문·숫자·`_`·`-`를 사용합니다. 한글 교차로명은 `intersection_name`에 적습니다. 이전 JSON의 `schedules`(`start`·`duration_hours`)도 지원하며 `time_ranges`와 동시에 지정하지 않습니다.

아래 1–6절은 기존 INI 설정을 계속 사용하는 경우의 절차입니다. 날짜 제한과 직접 입력한 이름·날씨는 위 JSON 방식에서 설정합니다.

## 1. INI 방식: 한 파일 복사 → 설정 생성 → 설치

이 저장소 루트의 `record.py`를 ED에서 읽을 수 있는 위치에 복사합니다. **`/home/dis/record.py`나 바탕화면에 두어도 됩니다.** 프로젝트의 `tools` 폴더에 넣을 필요가 없습니다. `recording.ini`는 현장에서 생성하므로 따로 반입하지 않아도 됩니다. 기존 `setup.ini`의 카메라 설정은 생성 명령이 수정하지 않습니다.

```text
/home/dis/
├── record.py                ← 새로 복사할 파일 한 개
├── recording.ini            ← 명령으로 생성한 뒤 현장 설정 편집
└── 실제프로젝트/
    └── config/
        ├── setup.ini        ← 기존 카메라 URL·INUSE·CLIENTNO 설정
        └── client29.ini     ← 기존 교차로 이름·카메라 방향 정보의 예
```

아래 명령은 **`record.py`를 둔 폴더**에서 실행합니다. 바탕화면에 복사했다면 첫 줄을 해당 폴더 경로로 바꿉니다.

```bash
cd /home/dis
python3 record.py --init-config
nano recording.ini
python3 record.py --check
sudo python3 record.py --install
```

`--init-config`는 `record.py` 옆에 한국어 설명이 포함된 `recording.ini`를 생성합니다. **설정 파일 생성에는 FFmpeg나 실제 `setup.ini`가 필요하지 않습니다.** 먼저 파일을 만들고 현장 값을 편집한 뒤 검사·설치하면 됩니다. 같은 명령을 다시 실행해도 기존 INI는 덮어쓰지 않고 보존하므로, 이미 파일이 있다면 해당 파일을 편집합니다.

설치는 서비스 계정으로 설정 검사를 수행한 다음 `goomi-camera-recorder.service`를 등록하고 즉시 시작합니다. 다음 부팅부터도 자동 실행됩니다. 현재 녹화 시간 밖이면 대기합니다. 설정 파일을 만들지 않고 `--install`을 실행해도 기본 `recording.ini`를 자동 생성하지만, 현장 설정을 먼저 확인하는 위 순서를 권장합니다.

Python과 FFmpeg가 없는 인터넷 연결 Ubuntu에서는 검사·설치 전에 다음을 실행합니다. 폐쇄망에서는 대상 Ubuntu 버전·CPU 구조에 맞는 패키지와 의존성을 미리 반입합니다. 녹화 실행에는 인터넷이 필요하지 않습니다.

```bash
sudo apt-get update
sudo apt-get install -y python3 ffmpeg
```

## 2. recording.ini 편집

생성되는 설정은 다음 구조입니다. `;`로 시작하는 줄에 설명을 적을 수 있습니다.

```ini
[CAMERAS]
; 기존 setup.ini 자동 탐색. 여러 개면 사용할 파일의 절대 경로 지정
setup_ini = auto
; auto는 INUSE=True인 카메라. 직접 선택하려면 CAM1, CAM2
cameras = auto

[RECORDING]
; 매일 한국 시간 기준 녹화 구간: 하루 총 14시간
time_ranges = 06:00-11:00, 12:00-17:00, 18:00-22:00
; 파일 분할 간격(분): 30분은 30, 1시간은 60
segment_minutes = 30
; auto는 카메라 설정이 속한 프로젝트의 runtime/scheduled_recordings
output_dir = auto
; 0은 자동 삭제 안 함
retention_days = 0
; 최소 여유 공간(GiB)
min_free_gb = 5
ffmpeg = ffmpeg
reconnect_seconds = 10
stall_seconds = 30
```

| 바꿀 항목 | 편집할 값 | 예시 |
| --- | --- | --- |
| 기존 카메라 설정 경로 | `[CAMERAS]`의 `setup_ini` | `/home/dis/2026goomi/config/setup.ini` |
| 대상 카메라 | `[CAMERAS]`의 `cameras` | `auto` 또는 `CAM1, CAM2` |
| 매일 녹화 시간 | `[RECORDING]`의 `time_ranges` | `06:00-11:00, 12:00-17:00, 18:00-22:00` |
| 파일 분할 간격 | `segment_minutes` | 30분은 `30`, 1시간은 `60` |
| 저장 위치 | `output_dir` | `auto` 또는 `/mnt/recordings` |
| 보관 일수 | `retention_days` | `0`은 삭제 안 함, 양수는 지난 관리 대상 영상 삭제 |
| 최소 여유 공간 | `min_free_gb` | `5`이면 여유 공간 5 GiB 미만에서 녹화 중단 |
| FFmpeg 실행 파일 | `ffmpeg` | `ffmpeg` 또는 설치한 실행 파일의 절대 경로 |
| 재연결 간격 | `reconnect_seconds` | 기본 `10`초 |
| 영상 멈춤 판단 시간 | `stall_seconds` | 기본 `30`초 |

**분할을 1시간으로 바꾸려면 `segment_minutes = 60`으로 수정하면 됩니다.** 긴 실행 옵션을 다시 입력할 필요가 없습니다. 설치 전이면 설정 검사 후 설치하고, 설치 후라면 아래의 “설정 변경 반영” 절차로 재시작합니다.

상대 경로는 **`recording.ini`가 있는 폴더**를 기준으로 해석합니다. 별도 SSD나 다른 프로젝트의 `setup.ini`는 절대 경로를 권장합니다. `output_dir = auto`는 현재 터미널 위치나 코드 파일 위치가 아니라 선택한 `setup.ini`의 프로젝트를 기준으로 합니다. 예를 들어 `/home/dis/2026goomi/config/setup.ini`를 사용하면 `/home/dis/2026goomi/runtime/scheduled_recordings`에 저장합니다.

### 녹화 구간과 파일 분할

`time_ranges`에는 한국 시간 기준 `HH:MM-HH:MM` 구간을 쉼표로 구분해 넣습니다. 녹화 구간의 **전체 길이**와 `segment_minutes`의 **파일 분할 간격**은 별개입니다. 2시간 구간은 30분 분할이면 약 4개, 60분 분할이면 약 2개 파일이 됩니다. 키프레임 간격 때문에 실제 분할 길이가 달라질 수 있고 마지막 파일은 짧을 수 있습니다. 재접속하면 파일 수가 늘어날 수 있습니다.

겹치거나 맞닿는 구간은 하나로 이어집니다. `23:00-01:00`은 다음 날 01:00까지 녹화하며 시작·종료 시각이 같은 구간은 오류입니다. 장비의 실제 시스템 시각이 정확해야 하며 예약 판단은 항상 한국 시간을 사용합니다.

## 3. 기존 setup.ini와 카메라 선택

`recording.ini`의 `setup_ini = auto`일 때 다음 순서로 카메라 설정을 찾습니다.

1. 스크립트 위치에서 최대 4단계 상위 폴더의 `config/setup.ini`를 가까운 순서로 확인합니다.
2. 주변에서 찾지 못하면 `/home/dis` 아래를 재귀 검색합니다.
3. 재귀 검색 후보가 한 개면 사용하고, 여러 개면 후보 경로를 표시한 뒤 중단합니다.

**재귀 검색에서 `2025goomi`와 `2026goomi`의 설정을 모두 찾으면 임의로 선택하지 않습니다.** 사용할 파일을 확인하고 `recording.ini`에 정확한 경로를 적습니다.

```ini
[CAMERAS]
setup_ini = /home/dis/실제프로젝트/config/setup.ini
cameras = auto
```

`/home/dis` 재귀 검색에서는 숨김·캐시·가상환경·`node_modules` 폴더와 심볼릭 링크를 제외합니다. 접근할 수 없는 폴더 때문에 검색을 완료하지 못한 경우에도 `setup_ini`에 정확한 경로를 지정합니다. `/home/dis` 밖의 설정도 절대 경로로 지정할 수 있습니다.

`cameras = auto`는 기존 `setup.ini`의 `CAM1`, `CAM2` 등 섹션 중 **`INUSE=True`인 카메라**를 선택하고 각 섹션의 **`URL`**을 읽습니다. `CAMADDR`만으로는 녹화할 수 없습니다.

```ini
; 기존 setup.ini 안의 카메라 설정 예시
[CAM1]
INUSE=True
URL=rtsp://USER:PASSWORD@192.168.0.101:554/STREAM_PATH
```

위 값은 설명용입니다. 실제 IP·포트·스트림 경로·계정으로 바꿉니다. 이 저장소에는 실제 장비의 `setup.ini`를 포함하지 않습니다. 운영 장비의 설정 파일에 **실제 `rtsp://` 주소를 지정하세요.** AVI·HTTP 주소는 카메라 입력으로 사용할 수 없습니다.

특정 카메라만 선택하려면 `recording.ini`에서 `cameras = CAM1, CAM2`로 지정합니다. 쉼표 대신 공백으로 구분해도 됩니다. 직접 지정한 카메라는 `INUSE` 값과 관계없이 녹화합니다.

RTSP 계정·암호에 `@`, `:`, `/` 같은 예약 문자가 있으면 URL 인코딩합니다. 계정 정보가 담긴 `setup.ini`를 일반 배포본에 포함하지 않으며 실제 서비스 계정이 읽을 수 있도록 권한을 관리합니다.

### 영상 파일명의 교차로·방향 정보

영상 파일명에는 기존 설정의 클라이언트 번호·교차로 이름·방향을 사용합니다. `recording.ini`에 새 항목을 추가할 필요가 없습니다.

1. `setup.ini`의 해당 카메라 섹션에서 `CLIENTNO`를 읽습니다.
2. `setup.ini`와 **같은 폴더**의 `client{CLIENTNO}.ini`를 읽습니다.
3. 그 파일의 `[INTERSECTION]` 섹션에서 `NAME`과 `CAMDIR`를 읽습니다.

예를 들어 `[CAM1]`의 `CLIENTNO=29`이면 **`client29.ini`**를 사용합니다. 카메라 섹션 이름이 CAM1이라는 이유로 `client1.ini`를 선택하지 않습니다. 예를 들어 `NAME=요트경기장`, `CAMDIR=4`이므로 파일명에 `client29_요트경기장_West`가 들어갑니다.

| CAMDIR | 파일명 방향 | CAMDIR | 파일명 방향 |
| --- | --- | --- | --- |
| `1` | `North` | `5` | `North-East` |
| `2` | `East` | `6` | `East-South` |
| `3` | `South` | `7` | `South-West` |
| `4` | `West` | `8` | `West-North` |

파일이 없거나 값을 읽을 수 없어도 녹화는 계속하고 경고를 남깁니다. 확인할 수 없는 부분은 `UnknownIntersection`, `UnknownDirection` 등으로 표시하며, 클라이언트 번호도 알 수 없으면 `clientUnknown_cam1`처럼 구분합니다. 파일명에 쓸 수 없는 문자는 정리합니다. 현장에 이미 있는 설정 파일을 읽는 기능이므로, 배포할 코드 파일은 계속 `record.py` 하나입니다.

## 4. SSD와 서비스 계정

별도 SSD를 사용한다면 `recording.ini`의 `output_dir`에 실제 저장 경로를 적고, 마운트 여부·여유 공간·권한을 확인합니다. 다음은 현재 로그인 계정으로 서비스를 실행할 때의 예입니다.

```bash
findmnt -T /mnt/recordings
df -h /mnt/recordings
# SOURCE가 의도한 SSD인지 확인한 뒤 폴더 준비
sudo install -d -m 700 -o "$(id -un)" -g "$(id -gn)" /mnt/recordings
```

기본 서비스 계정은 `sudo`를 실행한 계정입니다. 다른 계정으로 운영하거나 root로 직접 로그인했다면 설치 명령에 `--user 실제계정명`을 추가합니다. 해당 계정이 `record.py`·`recording.ini`·`setup.ini`를 읽고 저장 폴더에 쓸 수 있어야 합니다. 파일명에 교차로·방향 정보를 넣으려면 해당 `client번호.ini`도 읽을 수 있어야 합니다. `sudo` 설치 중 생성한 `recording.ini`는 선택한 서비스 계정 소유로 만듭니다.

SSD는 부팅 때 서비스보다 먼저 마운트되어야 합니다. 마운트가 빠진 디렉터리에 저장하면 시스템 디스크를 사용할 수 있습니다.

## 5. 설정 검사와 시험 녹화

```bash
python3 record.py --check
```

`--check`는 `recording.ini`를 읽고 로컬 설정·FFmpeg·저장 경로 권한을 검사합니다. 카메라에 연결하거나 녹화 폴더를 만들지 않으므로 **검사 성공은 실제 녹화 성공을 뜻하지 않습니다.** 다른 서비스 계정을 쓴다면 그 계정으로 검사합니다.

`record.py` 옆에 INI가 있으면 자동으로 사용합니다. 아직 INI가 없다면 `--check`와 직접 실행은 내장 기본값으로 동작하며 파일을 자동 생성하지 않습니다. 설정 파일을 만들려면 `--init-config`를 사용합니다.

빠른 현장 시험은 다음 명령으로 현재 한국 시간부터 90초간 실행할 수 있습니다. 이 실행에만 일정·분할 간격·출력 폴더를 임시 적용하고 `recording.ini`는 바꾸지 않습니다. 카메라 설정은 INI에서 읽습니다. 이미 서비스가 실행 중이면 시험 전에 `sudo systemctl stop goomi-camera-recorder.service`로 중지합니다.

```bash
# 실제 운영 저장 폴더로 변경: 별도 SSD라면 /mnt/recordings 등 사용
RECORD_DIR="/home/dis/실제프로젝트/runtime/scheduled_recordings"
timeout --signal=INT --kill-after=10s 90s \
  python3 record.py \
  --start-times "$(TZ=Asia/Seoul date +%H:%M)" \
  --duration-hours 0.1 --segment-minutes 0.5 \
  --output-dir "$RECORD_DIR/_test"
find "$RECORD_DIR/_test" -type f -name '*.mp4'
```

`timeout`의 종료 코드 124는 90초 경과 시 나올 수 있으며, 수동 종료는 `Ctrl+C`입니다. 로그·파일 생성뿐 아니라 **저장된 MP4를 재생해 실제 영상을 확인**해야 합니다.

`RECORD_DIR`를 실제 운영 저장 폴더의 절대 경로로 맞춥니다. 시험은 그 아래 `_test`에 저장되며 같은 디스크의 여유 공간이 필요합니다. 시험 후 운영 서비스에서도 예약 녹화가 되는지 확인합니다. 시험 중 중지한 서비스는 `sudo systemctl start goomi-camera-recorder.service`로 다시 시작합니다.

## 6. 설정 변경 반영과 서비스 운영

일정·분할·카메라 목록·저장 위치를 바꾸려면 **`recording.ini` 편집 → 검사 성공 → 서비스 재시작** 순서로 진행합니다. 일반 설정 변경에는 재설치가 필요하지 않습니다.

```bash
nano recording.ini
python3 record.py --check && \
  sudo systemctl restart goomi-camera-recorder.service
```

설치된 서비스는 `--config`로 지정한 **`recording.ini`를 시작할 때마다 읽습니다.** 파일을 저장하는 것만으로 실행 중인 서비스에 반영되지는 않습니다. 기존 `setup.ini`의 URL을 바꿨을 때도 검사 후 재시작합니다. `cameras = auto`이면 재시작 시 `INUSE` 값을 다시 읽고, 직접 목록을 지정했다면 그 목록을 사용합니다.

INI가 이미 있는데 `--install`이나 `--print-unit`에 녹화 설정 옵션을 함께 넣으면 거부됩니다. **INI를 편집한 뒤 `sudo python3 record.py --install`을 사용합니다.** 처음 INI가 없는 상태에서는 호환용 CLI 옵션으로 초기 값을 생성할 수 있지만, 이후 운영 설정은 INI에서 관리합니다. 직접 실행·검사의 임시 CLI 옵션은 INI에 저장되지 않습니다.

```bash
# 생성할 서비스 내용을 확인만 하기
python3 record.py --print-unit
# 상태와 최근 로그
systemctl is-enabled goomi-camera-recorder.service
systemctl status goomi-camera-recorder.service --no-pager
sudo journalctl -u goomi-camera-recorder.service -n 80 --no-pager
```

서비스의 `active`는 녹화 시간 외 대기 상태에서도 정상입니다. 실제 녹화 여부는 예약 구간의 로그와 저장 파일로 확인합니다. 재시작 중에는 짧은 녹화 공백이 생길 수 있습니다.

서비스에는 `record.py`와 `recording.ini`의 절대 경로가 저장됩니다. 설치 후 둘의 위치를 옮겼다면 새 위치에서 `--install`을 다시 실행합니다. 기존 `setup.ini`를 옮겼다면 INI의 경로를 수정하고 검사 후 재시작합니다. 같은 이름의 외부 서비스 또는 drop-in이 있으면 설치기가 중단합니다. 서비스와 수동 녹화를 같은 저장 폴더로 동시에 실행하지 마세요.

파일명 기능을 적용할 때는 설치된 위치의 `record.py`를 새 파일로 교체하고 검사 후 서비스를 재시작하면 됩니다. 추가 INI 설정이나 파일명용 설정 파일을 새로 배포할 필요가 없습니다. 기존에 저장한 영상의 이름은 자동으로 바꾸지 않습니다.

### 이전 CLI 설치에서 INI 방식으로 전환

이전 버전에서 설치한 서비스는 실행 옵션에 녹화 시간을 직접 저장했을 수 있습니다. **`record.py` 파일을 교체하는 것만으로 서비스 설정이 INI 방식으로 바뀌지는 않습니다.**

새 파일로 교체한 뒤 `--init-config`로 INI를 생성하고, 기존에 사용하던 카메라·저장소·보관 정책 등을 INI에 반영합니다. 검사에 성공하면 **한 번만 `sudo python3 record.py --install`을 실행**해 서비스를 INI 참조 방식으로 전환합니다. 별도 서비스 계정을 사용했다면 같은 `--user`도 지정합니다. 이후에는 INI 편집 후 재시작만 하면 됩니다.

기본 INI는 하루 14시간의 새 세 구간을 사용합니다. 이전 07시·17시 일정이나 다른 사용자 지정 일정을 유지하려면 설치 전에 `time_ranges`를 원하는 값으로 수정합니다.

## 동작과 보관

저장 폴더 구조는 `저장폴더/카메라/녹화시작일/run_시작시각_고유번호/`이며, 그 안에 다음과 같은 MP4 파일을 만듭니다.

```text
client29_요트경기장_West_20261013_153000_KST_000000.mp4
```

| 파일명 부분 | 의미 |
| --- | --- |
| `client29` | 해당 CAM 섹션의 `CLIENTNO=29` |
| `요트경기장` | `client29.ini`의 `[INTERSECTION] NAME` |
| `West` | 같은 섹션의 `CAMDIR=4` |
| `20261013_153000_KST` | 파일 생성 확인 시각(한국 시간): 2026년 10월 13일 15시 30분 00초 |
| `000000` | 해당 run 안의 분할 파일 순번. 0부터 증가하며 새 run에서는 다시 시작 |

**파일명의 시각은 ED가 각 파일 생성을 처음 확인한 시각**입니다. 약 0.2초 간격으로 확인하므로 카메라 원본 타임스탬프나 정확한 첫 프레임 촬영 시각을 의미하지 않습니다. 녹화 중 새 분할 파일이 생기면 각각의 생성 확인 시각을 붙입니다.

파일은 잠시 `client29_요트경기장_West_pending_000000.mp4`처럼 생성된 뒤 위 이름으로 바뀔 수 있습니다. Linux에서는 이름을 바꾼 뒤에도 같은 파일에 녹화가 계속됩니다. 시작·종료·오류 처리 중에는 `pending` 이름이 남을 수 있으며, 이름 변경에 실패하더라도 영상을 지우지 않습니다. 최종 이름이 보인다고 녹화가 끝난 파일이라는 뜻은 아닙니다.

- 재접속하면 새 run 폴더를 만듭니다. 날짜 폴더는 해당 run을 시작한 날이며 자정을 넘겨도 같은 run이면 유지됩니다. 파일은 fragmented MP4 방식이며, 실제 카메라 영상으로 사용할 플레이어의 재생을 확인하세요.
- 기존 MKV 녹화 파일을 MP4로 자동 변환하지 않습니다.
- 녹화 시간 도중 시작하거나 재부팅하면 남은 구간을 녹화합니다. 장비 중단이나 카메라 단절 동안의 영상은 복원하지 않습니다.
- 카메라가 끊기면 녹화 구간 안에서 재접속합니다. 디스크 여유 공간이 기준 미만이면 녹화를 멈춥니다. 기본값에서는 공간 확보를 위해 기존 영상을 삭제하지 않습니다.
- `retention_days`를 양수로 설정하면 파일 수정 시각 기준으로 보관 기간이 지난 관리 대상 영상을 시작 시와 이후 매시간 정리합니다. 관리 표식이 있는 녹화 폴더의 MP4와 기존 MKV 모두 대상입니다. 현재 쓰는 파일과 관련 없는 파일은 제외합니다. 필요한 보관 기간과 용량을 먼저 정하세요.
- 로그에서는 RTSP 주소를 숨깁니다. FFmpeg 실행 인수에는 주소가 포함되므로 장비 계정과 프로세스 조회 권한은 제한하세요.

## 중지와 제거

```bash
# 일시 중지: 다음 부팅의 자동 실행 설정은 유지
sudo systemctl stop goomi-camera-recorder.service
# 다시 시작
sudo systemctl start goomi-camera-recorder.service
# 서비스 중지·자동 실행 해제·등록 제거
sudo python3 record.py --uninstall
```

제거는 이 도구가 관리하는 `goomi-camera-recorder.service`만 대상으로 하며, 설정 파일과 녹화 파일은 남겨 둡니다.

## 이전 JSON 설정 호환

기존 JSON 설치는 `--config`로 경로를 명시해 계속 사용할 수 있습니다. 새 JSON 생성과 이름·날씨·날짜 설정은 위 JSON 절차를 참고합니다.

```bash
python3 record.py --config /실제/경로/recording.json --check
```

JSON의 명시적 `cameras` 목록은 `INUSE`와 관계없이 선택하고, `"auto"`는 `INUSE=True`인 카메라를 선택합니다. `segment_minutes`와 기존 `segment_seconds`는 하나만 지정하며, 둘 다 없으면 30분 분할입니다. 기존 `install.py` 설치 경로도 호환용으로 유지됩니다.

## 개발 검증

저장소 루트에서 다음 명령으로 테스트합니다. 현장에 테스트 파일을 복사할 필요는 없습니다.

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v
```

실제 카메라 연결·코덱·저장 영상 재생 가능 여부는 현장에서 별도로 확인합니다.

UI 소스는 `ui.html`, 예보·UI 서버 구성 요소는 `components/`, 공식 지역 목록은 `weather_regions.json`에서 관리합니다. 수정 후 `python3 build_embedded.py`로 `record.py`에 내장합니다. **배포 대상은 빌드한 record.py 한 파일**입니다. 브라우저 UI는 Python 표준 라이브러리로 실행하며, 개발용 빌드 도구·테스트·지역 원본 JSON을 현장에 복사할 필요가 없습니다.

GUI 위젯 통합 테스트는 그래픽 환경에서 `ED_TEST_DESKTOP_DISPLAY=1 python3 -m unittest test_desktop_gui -v`로 실행합니다. 실제 카메라나 서비스는 테스트 중 변경하지 않습니다.
