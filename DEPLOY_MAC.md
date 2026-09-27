# 맥북에서 정훈봇 상시 실행

Docker Desktop과 Docker Compose를 설치한 맥북에서 한 컨테이너로 실행합니다. 웹 서버가 아니므로 포트를 열 필요는 없습니다. `compose.yaml`의 `restart: unless-stopped`는 Docker가 다시 시작되면 봇 컨테이너도 다시 시작합니다.

## 1. 운영 파일 옮기기

기존 Windows 봇을 **먼저 중지**하고 아래 파일을 맥북의 프로젝트 폴더로 옮깁니다. 두 기기에서 같은 봇을 동시에 실행하면 예약 발송이 중복될 수 있습니다. Git으로 코드를 옮기더라도 Git에서 제외된 파일은 별도로 복사해야 합니다.

| Windows 프로젝트 | 맥북 프로젝트 | 용도 |
| --- | --- | --- |
| `.env` | `.env` | API 키와 봇 토큰 |
| `photos/` 안의 사진 | `photos/` | 인물 참조 사진 |
| `persona.txt` | `data/persona.txt` | 정훈 말투 요약 |
| `guilds.sqlite3` | `data/guilds.sqlite3` | 서버별 채널 설정 |
| `guild-jobs/` 안의 DB | `data/guild-jobs/` | 이전 버전 예약 이전용. 새 버전은 최초 실행 때 공통 예약으로 이전 |
| `scheduled.sqlite3` | `data/scheduled.sqlite3` | 새 버전의 공통 예약, 생성 결과, 서버별 게시 내역 |

복사 전에 맥북에서 `mkdir -p photos data/guild-jobs data/output`을 실행합니다. 새 설치라면 `guild-jobs/`와 `scheduled.sqlite3`는 직접 만들 필요가 없습니다. 이전 미리보기 버튼은 새로 만들어야 합니다. 기존 예약에 `ready` 회차가 있다면 해당 생성 이미지를 `output/`에 함께 보관해야 재생성 없이 공유할 수 있습니다. `posting` 회차는 기존 채널 게시 여부를 확인한 뒤 전환하세요.

## 2. 빌드와 실행

프로젝트 폴더에서 실행합니다.

```bash
docker compose config -q
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=50 bot
docker compose exec bot python bot.py plan
```

로그에 `Logged in as 정훈봇`이 보이고 `plan`에 공통 예약 시각과 등록된 서버가 나오면 연결된 상태입니다. 코드나 `uv.lock`을 갱신한 뒤에는 `docker compose up -d --build`를 실행합니다. 멈출 때는 `docker compose stop`을 사용합니다. `data/`와 `photos/`는 호스트 폴더에 남습니다.

## 3. 맥북 전원 설정

Docker Desktop의 설정에서 **Start Docker Desktop when you sign in to your computer**를 켜고, 재부팅 후 사용자 계정에 로그인되도록 운영합니다. 맥북을 전원 어댑터에 연결하고 macOS의 **배터리 → 옵션 → 전원 어댑터 사용 시 디스플레이가 꺼져 있어도 자동 잠자기 방지**를 켭니다. 덮개를 닫고 운영할 계획이라면 맥북의 외부 디스플레이 사용 조건을 확인하세요. 잠자기 상태에서는 예약 시각에 봇이 실행되지 않습니다.

Docker Desktop 자동 시작: <https://docs.docker.com/desktop/settings-and-maintenance/settings/>

macOS 잠자기 설정: <https://support.apple.com/guide/mac-help/set-sleep-and-wake-settings-mchle41a6ccd/mac>
덮개를 닫은 상태 사용: <https://support.apple.com/102282>

## 로컬 uv 실행

Docker 없이 맥북 터미널에서 점검할 때는 `uv sync --frozen` 후 `uv run --frozen bot.py plan` 또는 `uv run --frozen bot.py run`을 사용합니다. Docker와 로컬 실행을 동시에 켜지 마세요. 프로젝트 기본값은 현재 폴더에 데이터를 저장하고, `BOT_DATA_DIR`을 지정하면 해당 폴더에 저장합니다.

Docker Desktop이 새 컨테이너를 실행하지 못할 때는 `launchd`로 같은 uv 환경을 상시 실행할 수 있습니다. 이 경우 `persona.txt`, 서버 설정 DB, 예약 DB는 위 표처럼 `data/`에 둡니다. Docker 컨테이너를 먼저 중지한 다음 맥북의 프로젝트 폴더에서 실행합니다.

```bash
python3 scripts/install_launchd.py
launchctl print gui/$(id -u)/com.gonge1018.compy-bot
tail -n 30 data/bot.stderr.log
```

로그에 `Logged in as 정훈봇`이 보이면 연결된 상태입니다. 로그인된 사용자 세션에서 자동 시작하며, 오류로 종료되면 `launchd`가 다시 실행합니다. 전원 어댑터 사용 중에는 `caffeinate -s`로 유휴 잠자기를 막습니다. 덮개를 닫을 때의 잠자기는 별도로 확인해야 합니다. Docker Compose로 전환할 때는 먼저 `launchctl bootout gui/$(id -u)/com.gonge1018.compy-bot`으로 이 서비스를 중지하세요.
