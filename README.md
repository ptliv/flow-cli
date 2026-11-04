# flow-cli

Google Flow (Imagen / Veo) 자동화를 위한 CLI 래퍼입니다. 오픈소스 [ffroliva/gflow-cli](https://github.com/ffroliva/gflow-cli) (MIT) 를 기반으로, 로컬 Dachapgo 브라우저 서버(CDP) 연동 브리지(`gflow_cli/dachapgo_bridge.py`)와 `dachapgo-status` / `dachapgo-stop` 서브커맨드를 추가했습니다.

## 구성
- `gflow.py` / `gflow.cmd` — 엔트리포인트 (Windows 는 `gflow.cmd`)
- `_upstream/gflow-cli/` — Python Flow CLI 코어 (수정본, MIT, © Flavio Oliva)
- `_upstream/google-flow-cli/` — Node.js / MCP Flow CLI (MIT, © Zahed Rastgar)

## 설치
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # 값 채우기
python gflow.py --help
```

Dachapgo 연동을 쓰지 않으면 `DACHAPGO_*` 값은 비워 두면 되고, 표준 `gflow` 명령(`image t2i`, `video i2v`, `auth login` 등)은 그대로 동작합니다.

## 라이선스
각 업스트림 디렉터리의 `LICENSE`(MIT)를 따릅니다.

## 계정 로테이션 / Account rotation

여러 Google Flow 계정(gflow 프로필)을 돌려 쓰는 래퍼입니다. / Rotates jobs across several gflow profiles.

1. 각 계정 로그인 / log in each account: `python gflow.py auth login --profile acct1` (… acct6)
2. `.env` 에 `GFLOW_ROTATE_PROFILES=acct1,acct2,...` 설정 (`.env.example` 참고).

```bash
# 크레딧이 가장 많은 계정으로 1건 실행 / run once on the profile with most credits
python gflow.py rotate run -- video t2v "a cat surfing" --model veo-3
# 라운드로빈 / round-robin (no credit check)
python gflow.py rotate run --strategy round-robin -- image t2i "a cat"
# 프롬프트 파일 일괄 실행 (.txt 한 줄 1개, .jsonl {"prompt": ...}) / batch
python gflow.py rotate batch prompts.txt -- image t2i {prompt}
# 상태 (크레딧/사용량/쿨다운) / status; --refresh 는 브라우저로 크레딧 재조회
python gflow.py rotate status [--refresh] [--json]
python gflow.py rotate reset [acct1]   # 쿨다운 해제 / clear cooldown
```

동작 / Behaviour
- 크레딧은 `gflow credits user --json` 으로 읽고 `GFLOW_ROTATE_CREDITS_TTL`초(기본 1800) 동안 캐시합니다. / Credits cached with TTL.
- gflow 종료 코드에 따라 해당 계정을 쿨다운하고 다음 계정으로 재시도 / On these exit codes the profile cools down and the next one is tried:
  `4` rate limit(1h), `37` insufficient credits(6h), `3`/`8` auth expired/missing(24h), `38` account chooser, `39` no Flow access(24h), `36` host migrated(6h), `10` WAF(30m), `25`/`31` Flow UI flap(5m), 출력에 `429`/`RESOURCE_EXHAUSTED`/`quota` 포함 시(1h).
- 그 외 오류(예: `5` 콘텐츠 정책)는 다른 계정을 낭비하지 않도록 바로 종료합니다. / Other errors stop immediately.
- 사용 가능한 계정이 없으면 종료 코드 `75`. / Exit 75 when no profile is available.
- 작업 간 대기 `GFLOW_ROTATE_DELAY` + 랜덤 `GFLOW_ROTATE_JITTER`초. / Delay + jitter between batch jobs.
- 상태와 로그: `~/.gflow-rotate/state.json`, `usage.jsonl` (`GFLOW_ROTATE_STATE_DIR`). 

테스트 / Tests: `pip install pytest && python -m pytest tests` (가짜 gflow 사용 / mocked gflow).

> Google 이용약관상 여러 계정으로 한도를 우회하는 행위는 계정 제재 위험이 있습니다. 본인 소유·유료 계정에만 사용하세요. / Use only with accounts you own; multi-account use may violate Google's terms.
