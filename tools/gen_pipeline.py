"""docs/pipeline.md 생성기.

박스 다이어그램은 한글이 2칸이라 손으로 정렬하면 반드시 어긋난다.
표시 폭을 계산해 채우는 이 스크립트가 단일 출처다.
"""

import unicodedata

W = 100  # 다이어그램 전체 표시 폭
BOX = 58  # 단계 박스 폭
GAP = 2
ANN = W - 4 - BOX - GAP  # 우측 stack/수치 열 폭

MD: list[str] = []
BUF: list[str] = []
RAIL = ["║"]


def dw(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, n: int) -> str:
    return s + " " * max(0, n - dw(s))


def md(*lines: str) -> None:
    MD.extend(lines)


def bx(s: str = "") -> None:
    BUF.append(s)


def dia() -> None:
    """모아둔 박스 줄을 코드 블록으로 내보낸다."""
    md("```")
    MD.extend(BUF)
    md("```", "")
    BUF.clear()


def row(s: str = "") -> None:
    r = RAIL[0]
    bx(r + pad(s, W - 2) + r)


def ftop(title: str, double: bool) -> None:
    a, b, c = ("╔", "═", "╗") if double else ("┌", "─", "┐")
    t = f" {title} " if title else ""
    bx(a + b * 2 + t + b * (W - 4 - dw(t)) + c)


def fbot(double: bool, conn: bool = False) -> None:
    a, b, c = ("╚", "═", "╝") if double else ("└", "─", "┘")
    bx(a + (b * 24 + "╪" + b * (W - 27) if conn else b * (W - 2)) + c)


def fit(s: str, n: int) -> str:
    if dw(s) > n:
        OVER.append((dw(s) - n, s))
    return pad(s, n)


OVER: list[tuple[int, str]] = []


def step(title: str, lines=(), ann=(), last: bool = False) -> None:
    ann = list(ann) + [""] * (len(lines) + 4)
    row("  ┌" + "─" * (BOX - 2) + "┐" + " " * GAP + fit(ann[0], ANN))
    row("  │ " + fit(title, BOX - 4) + " │" + " " * GAP + fit(ann[1], ANN))
    for i, x in enumerate(lines):
        row("  │ " + fit("   " + x, BOX - 4) + " │" + " " * GAP + fit(ann[i + 2], ANN))
    row("  └" + ("─" * (BOX - 2) if last else "─" * 21 + "┬" + "─" * (BOX - 24)) + "┘")


def down() -> None:
    row(" " * 24 + "▼")


def notes(*lines: str) -> None:
    """박스 밖 전폭 주석. 긴 근거는 좁은 박스에 쑤셔넣지 않는다."""
    for x in lines:
        row("      " + x)


CONN = " " * 25 + "▼"


def plain(*rows: str) -> None:
    old = RAIL[0]
    RAIL[0] = "│"
    ftop("", False)
    for r in rows:
        row("  " + r)
    fbot(False)
    RAIL[0] = old


def core(title: str) -> None:
    RAIL[0] = "║"
    ftop(title, True)


def side(title: str) -> None:
    RAIL[0] = "│"
    ftop("곁다리 · " + title, False)


def check() -> None:
    if OVER:
        for over, t in sorted(OVER, reverse=True):
            print(f"  +{over:2d}  {t}")
        raise SystemExit(f"박스 폭 초과 {len(OVER)}줄")
    bad = [i for i, ln in enumerate(MD, 1) if ln and ln[0] in "║│╔╚┌└" and dw(ln) != W]
    if bad:
        raise SystemExit(f"정렬 어긋남 {len(bad)}줄: {bad[:10]}")
    print(f"정렬 OK · {len(MD)}줄 · 폭 {W}")


# ══════════════════════════════════════════════════════════════════════════
#  머리말
# ══════════════════════════════════════════════════════════════════════════
md(
    "# 파이프라인",
    "",
    "> 도메인(`app/domain/`) 11개를 단락으로 나눈다. 각 단락에 **파이프라인 + 스택 요약**이 들어간다.",
    "> 각 단락은 그 도메인의 **대표 경로 1건**(로그인 1회 · 글 작성 1건 · 업로드 1건 …)을 끝까지",
    "> 따라가고, 나머지 엔드포인트는 아래 곁다리 박스로 잇는다. 엔드포인트 55개가 모두 들어 있다.",
    ">",
    "> 본문의 모든 수치(TTL 240s · top 10 · 상한 5 · 임계값 …)는 **코드 상수 그대로**다 — 추정값이 아니다.",
    ">",
    "> **읽는 법:** `╔═╗` = 대표 경로(본류) / `┌─┐` = 곁다리 · 도메인 밖 / `[ … ]` = 외부 인프라 ·",
    "> 다른 도메인 / `★` = 폴백 · 게이트 등 분기점 / `──▶` = 외부 호출.",
    "",
    "---",
    "",
    "## 0 · 도메인 한눈에",
    "",
    "| 도메인 | 엔드포인트 | 한 줄 | 경로 성격 | 핵심 불변식 |",
    "|---|---|---|---|---|",
    "| **auth** | 4 | 가입·로그인·회전·폐기 | 동기 | 로그아웃만 fail-closed |",
    "| **users** | 7 | 내 프로필·비밀번호·차단·탈퇴 | 동기 CRUD | 탈퇴는 소프트, 하드 삭제는 잡 |",
    "| **posts** | 7 | 글 작성·목록·상세·트렌딩·삭제 | 동기 | 멱등 키 · 조회수는 버퍼링 |",
    "| **comments** | 5 | 2단 트리 댓글 | 동기 | 기본 인기순 · 대댓글은 항상 최신순 |",
    "| **likes** | 4 | 글·댓글 좋아요 | 동기 | 회수는 가시성 술어를 안 탄다 |",
    "| **media** | 5 | presign 업로드·소프트 삭제 | 동기 + 주기 잡 | 객체는 행보다 늦게 생기고 먼저 사라진다 |",
    "| **chat** | 6 | DM 방·메시지·WS | 동기 + WS | at-most-once · 진실은 DB |",
    "| **notifications** | 3 | 목록·읽음·SSE | 동기 + SSE | 발행은 반드시 커밋 이후 |",
    "| **dogs** | 1 | 대표견 지정 (프로필 upsert 입구는 users) | 동기 | 대표견은 소유자당 1마리 |",
    "| **reports** | 1 | 신고 접수 | 동기 | 임계값 도달 시 자동 블라인드 |",
    "| **admin** | 12 | 모더레이션·정지·삭제 | 동기 | 판정은 각 도메인 — admin은 소비만 |",
    "",
    "---",
    "",
    "## 1 · 도메인 의존 관계",
    "",
    "화살표는 `부르는 쪽 → 불리는 쪽`(import 방향)이다.",
    "",
)
bx("   싱크 ─ 다른 도메인을 부르지 않는다. 여기서 화살표가 멈춘다")
bx("   ┌──────────┐       ┌────────────────┐")
bx("   │  media   │       │ notifications  │")
bx("   └──────────┘       └────────────────┘")
bx("        ▲                     ▲")
bx("        │ 6곳이 부른다        │ comments · likes 만 부른다")
bx("        │                     │ ─ 알림이 생기는 곳은 이 둘뿐")
bx("")
bx("   양방향 6쌍 ─ 서로를 import 한다")
bx("   ┌──────────────────┬──────────────────────────────────────────────────────┐")
bx("   │  auth  ↔ users   │  가입·인증  ↔  프로필 변경 시 인증 캐시 무효화        │")
bx("   │  posts ↔ users   │  작성자 조회  ↔  탈퇴 시 like_count 보정             │")
bx("   │  posts ↔ comments│  글 삭제가 댓글을 정리  ↔  댓글이 글 가시성을 검증    │")
bx("   │  posts ↔ likes   │  같은 이유 — 삭제 캐스케이드  ↔  대상 검증            │")
bx("   │  comments↔ users │  알림 수신자·작성자 조회                              │")
bx("   │  dogs  ↔ users   │  강아지 프로필 upsert 의 입구가 users 의 PATCH /me    │")
bx("   └──────────────────┴──────────────────────────────────────────────────────┘")
bx("")
bx("   ★ 이벤트로 끊지 않은 이유 — 글 삭제가 최종 일관성이 되면 반쯤 지워진 글이")
bx("     보이는 창이 생긴다. 한 트랜잭션에 묶으려면 서로를 알아야 한다.")
bx("")
bx("   단방향")
bx("     chat    ──▶  dogs · media · users")
bx("     likes   ──▶  comments · notifications · posts")
bx("     reports ──▶  comments · posts · users")
bx("     admin   ──▶  auth · comments · media · posts · reports · users")
bx("     auth    ──▶  media                    가입 시 프로필 이미지")
bx("")
bx("   ★ admin 은 소비만 한다. 판정을 재구현하면 규칙이 두 벌이 되어 어긋난다 —")
bx("     읽기와 상태 전이만 하고 캐스케이드·멱등 시맨틱은 각 도메인 서비스에 맡긴다.")
bx("")
bx("   피호출 횟수   users 7 · media 6 · comments 5 · posts 5 · auth 2 · dogs 2 ·")
bx("                notifications 2 · reports 1 · likes 1")
dia()


md(
    "---",
    "",
    "## 2 · 모든 요청이 지나는 길",
    "",
    "엔드포인트 55개가 예외 없이 같은 스택을 통과한다. `main.py`의 등록 순서를 **뒤집어**",
    "읽어야 한다 — 나중에 등록한 것이 더 바깥이다.",
    "",
)
plain("REQUEST          어떤 엔드포인트든 예외 없음")
bx(CONN)
core("공통 미들웨어 (등록 역순 = 바깥부터)")
row()
step(
    "① REQUEST ID        ULID 발급",
    ["contextvars · X-Request-ID · 응답 본문까지"],
    ["", "추적 단일 키"],
)
down()
step(
    "② PROXY             X-Forwarded-For",
    ["진짜 클라이언트 IP 복원"],
    ["", "컴포즈 서브넷 고정", "안 하면 전 요청이 IP", "하나로 수렴해 429"],
)
down()
step(
    "③ GZIP              1KB 이상 응답 압축",
    [],
    ["", "관측보다 바깥 —", "압축 시간이 지연", "측정을 안 더럽히게"],
)
down()
step(
    "④ OBSERVABILITY     보안 헤더 · 접근 로그 · 메트릭",
    ["요청수 / 지연 / 에러"],
    ["", "세 겹을 한 겹으로", "요청당 할당 1/3"],
)
down()
step("⑤ HOST · CORS       Host 검증 · 출처 허용", [], ["", "브라우저 경계"])
down()
step(
    "⑥ RATE LIMIT        IP별 고정창",
    ["★초과 → 429. 여기서 끝난다"],
    ["──▶ [ Redis ]", "check_fixed_window()", "라우트 매칭 전이라", "path 라벨 __unmatched__"],
)
fbot(True, conn=True)
bx(CONN)
plain("HANDLER          app/api/v1 — 여기서부터 도메인별로 갈린다")
dia()

md(
    "| 왜 | |",
    "|---|---|",
    "| rate limit이 **가장 안쪽** | 바깥에 두면 429가 CORS 헤더 없이 나가 브라우저가 429 대신 "
    "CORS 에러를 본다. 클라이언트가 `Retry-After`를 읽을 방법이 사라지고 로그·메트릭에도 안 잡힌다 |",
    "| 경로별 정책이 다름 | 로그인·가입 업로드는 Redis가 죽어도 **프로세스 메모리로 계속 센다**. "
    "그 외 전역은 그냥 통과시킨다(가용성 우선) |",
    "",
)

md(
    "---",
    "",
    "## 3 · 계층 구조",
    "",
    "도메인 11개가 전부 같은 네 층이다. 하나만 이해하면 나머지는 읽힌다.",
    "",
)
core("app/domain/<도메인>/")
row()
step(
    "router              HTTP 표면만",
    ["입력 검증 → 서비스 호출 → api_response()", "인증 · 세션은 DI가 끝낸 뒤 진입한다"],
    ["", "트랜잭션을 열지 않는다", "get_current_user", "get_master_db / slave"],
)
down()
step(
    "service             ★ 트랜잭션을 여는 유일한 층",
    ["async with db.begin()", "CUD 한 요청 = 한 트랜잭션"],
    ["", "비즈니스 규칙", "도메인 간 조율"],
)
down()
step(
    "repository          SQL · ORM만",
    ["커밋 권한이 없다"],
    ["", "그래서 두 리포지터리를", "한 트랜잭션에 묶을 수 있다"],
)
down()
step(
    "writer / reader     조회는 reader",
    ["단, 쓰기 직후 읽기는 writer로"],
    ["──▶ [ PostgreSQL ]", "AsyncSessionLocal", "AsyncSessionLocalReader"],
    last=True,
)
fbot(True)
dia()

md(
    "커밋 권한이 서비스에만 있다는 규칙 하나가 꽤 많은 걸 결정한다. 게시글 삭제는 게시글",
    "soft-delete + 댓글 soft-delete + 좋아요 삭제 셋을 건드리는데, 자연히 한 트랜잭션에 묶인다.",
    "리포지터리가 각자 커밋할 수 있었다면 중간에 실패했을 때 반쯤 지워진 게시글이 남았을 것이다.",
    "",
)


md(
    "---",
    "",
    "## 4 · 배포 구성",
    "",
    "코드는 **인스턴스 3\\~10대 · 상태는 전부 인스턴스 밖**을 전제로 쓰여 있다. 그래서 분산 잠금과",
    "Pub/Sub 팬아웃이 존재한다. 실제 라이브 데모는 그 축소판 — 한 대에서 compose로 돈다.",
    "코드는 바뀌지 않는다. `READER_DB_URL`이 비면 writer로 폴백할 뿐이다.",
    "",
)
bx("   코드가 전제하는 것                      실제 데모 배포 (compose · 컨테이너 5개)")
bx("   ─────────────────────                  ────────────────────────────────────")
bx("   로드밸런서                              Caddy 2      TLS 자동 · 80 · 443만 열림")
bx("     /livez · /readyz 로 준비된 대상만       flush_interval -1 없으면 SSE가")
bx("        │                                   프록시 버퍼에 갇혀 실시간이 아니게 된다")
bx("        ▼                                        │")
bx("   앱 인스턴스 3~10대 · stateless           backend    gunicorn -w 2 · uvicorn worker")
bx("   Celery 워커                              worker     celery · 큐 2종 · 동시 2")
bx("        │                                   ★ 마이그레이션은 backend 에서만 —")
bx("        │                                     worker 와 동시 실행 시 alembic 경합")
bx("        ▼")
bx("   ┌─────────────────────────────────────────────────────────────────────────┐")
bx("   │  인스턴스 밖 공유 상태 — 어느 인스턴스가 처리해도 같은 결과              │")
bx("   │                                                                         │")
bx("   │  [ PostgreSQL writer ]  모든 쓰기        postgres:15 한 대가 둘 다 겸함  │")
bx("   │  [ PostgreSQL reader ]  조회 분산                                        │")
bx("   │  [ Redis ]  캐시 · 잠금 · 채널 · 브로커  redis:7 · appendonly · 256MB    │")
bx("   │                                          allkeys-lru                     │")
bx("   │  [ S3 ]  이미지 본체 · 서명만 발급       AWS S3 — compose 밖, 유일한 외부 │")
bx("   └─────────────────────────────────────────────────────────────────────────┘")
bx("")
bx("   ★ 인스턴스끼리 직접 통신하는 화살표가 없다. 서로에게 할 말은 전부 Redis 를 거친다 —")
bx("     실시간 전달도 상대 인스턴스에 바로 보내지 않고 채널을 통한다.")
bx("   ★ Redis 는 allkeys-lru. 조회수 버퍼처럼 TTL 없는 키가 있어 volatile-* 는 상한에")
bx("     닿는 순간 쓰기가 에러로 떨어진다. 앱이 Redis 실패를 흡수하니 버리는 쪽이 낫다.")
dia()

md(
    "---",
    "",
    "## 5 · 장애 시 동작",
    "",
    "기본 태도는 **가용성 > 순간 정합성**이다. 무엇이 죽었을 때 코드가 실제로 무엇을 하는지로 적는다.",
    "각 도메인 그림의 `★` 표시가 아래 표의 어느 줄인지 가리킨다.",
    "",
    "| 무엇이 죽나 | 코드가 하는 일 | 사용자가 느끼는 것 |",
    "|---|---|---|",
    "| **PostgreSQL writer** | `/readyz`가 503 → 로드밸런서가 이 인스턴스를 트래픽에서 뺀다 | "
    "서비스 불가. 유일한 hard 의존성 |",
    "| **Redis 전체** | rate limit 통과(로그인만 메모리로 계속 셈) · 인증 캐시 미스 → DB 조회 · "
    "조회수는 writer에 직접 +1 · 멱등성 비활성 · 크로스 인스턴스 실시간 중단 · **로그아웃은 실패** | "
    "서비스는 돈다. 조금 느려지고, 다른 인스턴스에 붙은 상대와의 실시간만 끊긴다 |",
    "| reader 복제 지연 | 쓰기 직후 읽기는 writer로 보내는 규약으로 덮는다. 채팅 재동기(`after`)도 "
    "master를 읽는다 | 방금 쓴 것은 제대로 보인다 |",
    "| S3 | confirm이 명확한 실패 응답. 예약 행이 남아 스위퍼가 회수 | "
    "이미지 업로드만 실패. 글쓰기·읽기는 정상 |",
    "| Celery 브로커 | 같은 멱등 키로 인라인 발송 폴백. `enqueue` 소켓에 타임아웃이 걸려 있어 "
    "요청이 매달리지 않는다 | 푸시가 한 번만 간다. 재시도가 없어질 뿐 |",
    "| pubsub 리스너 | 0.5초 → 30초 백오프 재연결. 유휴 10초면 PING, pong이 5초 없으면 끊고 재연결 | "
    "재연결까지 다른 인스턴스발 메시지 유실. 같은 인스턴스 안 전달은 계속 |",
    "| DB가 먹통(응답 없음) | 프로브에 **이중 취소**로 상한. psycopg는 첫 `CancelledError`를 잡아 "
    "서버 취소를 시도한 뒤 같은 쿼리를 다시 기다린다 | `/readyz`가 매달리지 않고 503으로 떨어진다 |",
    "| 인스턴스 재시작 | 준비된 인스턴스에만 트래픽. 종료 시 조회수 flush · 인라인 발송을 드레인 | "
    "WebSocket · SSE 재연결. 그 외 무중단 |",
    "| 느린 SSE 클라이언트 | 메모리 큐(100건)가 차면 신규 이벤트를 버린다 | "
    "실시간 알림을 놓칠 수 있다. 목록에는 남아 있다 |",
    "| 죽어가는 WebSocket | 전송 5초 타임아웃 후 소켓을 **실제로 닫는다** | "
    "클라이언트가 끊김을 감지해 재연결한다 |",
    "",
    "등록만 지우고 소켓을 안 닫으면 클라이언트는 연결이 살아 있는 줄 알고 계속 보내면서 수신만",
    "조용히 잃는다. 재연결 로직도 안 뜬다. 그래서 반드시 닫는다.",
    "",
    "---",
    "",
)


N = [5]


def domain(name: str, eps: str, *blurb: str) -> None:
    N[0] += 1
    md(f"## {N[0]} · {name}", "", f"`{eps}`", "", *blurb, "", "### 파이프라인", "")


def stack(*rows: str) -> None:
    md("### 스택 요약", "", "| 레이어 | 기술 | 메모 |", "|---|---|---|", *rows, "", "---", "")


# ══════════════════════════════════ auth ══════════════════════════════════
domain(
    "auth",
    "POST /v1/auth/signup · login · refresh · logout",
    "access는 짧게(30분) 응답 본문으로, refresh는 길게(7일) HttpOnly 쿠키로 나간다.",
    "아래는 **로그인 1회** 경로.",
)
plain(
    "CLIENT           [ 브라우저 · 앱 ]", " " * 22 + "│  POST /v1/auth/login  { email, password }"
)
bx(CONN)
plain(
    "MIDDLEWARE       § 2 · 로그인은 전용 고정창 rate limit",
    "                 Redis 장애 시 프로세스 메모리로 계속 센다",
)
bx(CONN)
core("auth 도메인 코어 (FastAPI · async) ─ 대표 경로: 로그인 1건")
row()
row("   AuthService.login()" + " " * 38 + "stack / 수치")
step("① LOOKUP            get_by_email", [], ["──▶ [ PostgreSQL reader ]"])
down()
step(
    "② VERIFY            비밀번호 대조",
    ["CPU 바운드 → 스레드풀", "★실패 → 401"],
    ["", "bcrypt + pepper", "이벤트루프 비차단", "평문은 어디에도 없음"],
)
down()
step(
    "③ ISSUE             토큰 두 장",
    ["access  → 응답 본문", "refresh → HttpOnly 쿠키 (JS 접근 불가)"],
    ["", "access   30분", "refresh   7일"],
)
down()
step(
    "④ PERSIST           refresh 지문 보관",
    ["다이제스트만 — 유출돼도 위조 불가"],
    ["──▶ [ Redis ]", "user_refresh:{id}"],
)
fbot(True, conn=True)
bx(CONN)
bx("   Set-Cookie refresh_token (httpOnly · secure · samesite)")
bx("   응답 body = { accessToken, … }   ← refresh는 body에 없다")
bx("                                     XSS 한 번에 장기 자격증명이 털리지 않게")
bx("")
side("이후 모든 보호 엔드포인트가 지나는 곳")
row()
row("   Authorization: Bearer …  ──▶  resolve_access_token_user()")
step("① VERIFY            서명 + 폐기된 jti인지", [], ["──▶ [ Redis ]"])
down()
step(
    "② SNAPSHOT          사용자 스냅샷 조회",
    ["★히트 → DB 왕복 0회", "★미스 → 1건 조회 후 캐시 채움"],
    ["──▶ [ Redis ]", "user:auth:{id} · 240s", "", "──▶ [ PostgreSQL reader ]"],
)
down()
step(
    "③ GATE              비활성 계정은 여기서 끝",
    ["스냅샷에 상태가 있어 DB를 안 본다"],
    [],
    last=True,
)
fbot(False)
bx("")
side("회전(RTR)과 폐기")
row()
row("   POST /v1/auth/refresh   (쿠키의 refresh)")
step(
    "CAS                 비교 + 교체를 Lua 한 번에",
    ["★같다   → 새 토큰 두 장. 옛것은 즉시 무효", "★다르다 → 거부. 이미 쓴 토큰이다"],
    ["──▶ [ Redis ]", "원자적", "동시 갱신도 한 건만 성공"],
    last=True,
)
row()
row("   POST /v1/auth/logout")
step(
    "REVOKE              jti 폐기 목록 + 지문 삭제",
    ["★Redis 장애 → 여기만 fail-closed"],
    ["──▶ [ Redis ]", "남은 수명만큼", "다른 경로는 전부 fail-open"],
    last=True,
)
row('      "로그아웃됐습니다"라고 응답해 놓고 토큰이 살아 있는 쪽이 더 위험하다')
fbot(False)
bx("")
side("가입")
row()
row("   POST /v1/auth/signup")
step(
    "HASH → STORE        pepper 섞어 bcrypt(스레드풀)",
    ["평문은 어디에도 남지 않는다", "프로필 이미지는 media signup/confirm 을 거친 id만"],
    ["──▶ [ PostgreSQL writer ]", "users"],
    last=True,
)
fbot(False)
dia()
stack(
    "| **API** | FastAPI (async) · `/v1/auth/*` 4개 | signup · login · refresh · logout |",
    "| **계정 저장** | PostgreSQL — 조회는 reader, 가입은 writer | `users` |",
    "| **비밀번호** | bcrypt + pepper, 스레드풀 | CPU 바운드 — 이벤트루프 비차단 |",
    "| **토큰 전달** | access=응답 본문 / refresh=HttpOnly 쿠키 | 권한 경계 — XSS로 장기 자격증명 유출 차단 |",
    "| **세션 상태** | Redis `user_refresh:{id}` 지문 · jti 폐기 목록 | 다이제스트만 · 회전은 Lua CAS |",
    "| **인증 캐시** | Redis `user:auth:{id}` · 240s | 히트 시 DB 왕복 0회 · 비활성 상태 포함 |",
    "| **복원력** | 전부 fail-open, **로그아웃만 fail-closed** | 보안 경계에서는 가용성보다 정확성 |",
)


# ══════════════════════════════════ users ═════════════════════════════════
domain(
    "users",
    "GET /v1/users/availability · me · me/blocks   PATCH me · me/password   "
    "DELETE me   POST /{id}/block",
    "내 프로필과 계정 상태. 아래는 **프로필 수정 1건** 경로 — 이미지 교체가 걸려 있어",
    "media 도메인과 맞닿는 유일한 지점이다.",
)
plain("CLIENT           PATCH /v1/users/me  { nickname?, imageId?, … }")
bx(CONN)
core("users 도메인 코어 ─ 대표 경로: 프로필 수정 1건")
row()
row("   UserService.update_user_profile()" + " " * 24 + "stack / 수치")
step(
    "① AUTHZ             CurrentUser 로 본인만",
    ["남의 /me 는 존재하지 않는다"],
    ["", "DI가 끝낸 뒤 진입"],
)
down()
step(
    "② ATTACH GUARD      imageId 가 붙일 수 있는 것인지",
    ["assert_images_attachable() · FOR SHARE", "★소프트 삭제와 경쟁 — 어느 쪽이든 뒤가 기다린다"],
    ["──▶ [ PostgreSQL writer ]", "images", "잠그지 않으면 지운", "이미지가 붙는다"],
)
down()
step(
    "③ UPDATE            users 갱신 + 강아지 목록 교체",
    [
        "profile_image_id 만 바꾼다 (옛것은 안 건드림)",
        "dogs 가 오면 DogService.upsert_dog_profile() 위임",
    ],
    ["──▶ [ PostgreSQL writer ]", "고아 정리는 media 스위퍼", "강아지 로직은 § 14"],
)
down()
step(
    "④ INVALIDATE        인증 스냅샷 무효화",
    ["안 지우면 240초 동안 옛 프로필이 보인다"],
    ["──▶ [ Redis ]", "user:auth:{id}"],
)
fbot(True, conn=True)
bx(CONN)
bx("   200 { user }")
bx("")
side("차단")
row()
row("   POST /v1/users/{target_user_id}/block   ·   GET /v1/users/me/blocks")
step(
    "TOGGLE              있으면 해제, 없으면 차단",
    ["★자기 자신은 거부 (400)", "차단은 목록·댓글·좋아요 가시성 술어로 퍼진다"],
    ["──▶ [ PostgreSQL writer ]", "user_blocks", "한 트랜잭션"],
    last=True,
)
fbot(False)
bx("")
side("탈퇴 — 요청은 표시만, 실제 삭제는 잡")
row()
row("   DELETE /v1/users/me")
step(
    "SOFT DELETE         탈퇴 표시",
    ["요청 안에서 하드 삭제하지 않는다"],
    ["──▶ [ PostgreSQL writer ]", "users"],
)
down()
step(
    "PURGE JOB           탈퇴 N일 경과분 하드 삭제",
    [
        "→ 삭제 전에 like_count 를 깎지 않으면",
        "  카운트가 영구히 부풀어 있다 (자가 치유 없음)",
        "청크 200건마다 begin() — 한 트랜잭션에",
        "다 태우면 락·부하가 커진다",
    ],
    [
        "",
        "purge_withdrawn_users()",
        "",
        "카운트는 각 도메인",
        "저장소가 소유하므로",
        "조율은 여기서",
    ],
    last=True,
)
notes(
    "★ 좋아요 행은 FK CASCADE 로 사라지지만 게시글·댓글은 SET NULL 로 살아남는다",
)
fbot(False)
bx("")
side("나머지")
row()
step(
    "GET /users/availability   닉네임 중복 확인",
    ["비로그인 가능 · reader"],
    ["──▶ [ PostgreSQL reader ]"],
    last=True,
)
step(
    "GET /users/me             내 프로필",
    ["reader 1쿼리"],
    ["──▶ [ PostgreSQL reader ]"],
    last=True,
)
step(
    "PATCH /users/me/password  비밀번호 변경",
    ["옛 비밀번호 대조 후 재해싱 (bcrypt · 스레드풀)"],
    ["──▶ [ PostgreSQL writer ]"],
    last=True,
)
fbot(False)
dia()
stack(
    "| **API** | FastAPI (async) · `/v1/users/*` 7개 | 전부 본인 스코프 (`/me`) |",
    "| **저장소** | PostgreSQL — 조회 reader / 변경 writer | `users` · `user_blocks` |",
    "| **이미지 연결** | `assert_images_attachable()` · `FOR SHARE` | 권한 경계 — media 소프트 삭제와 경쟁 |",
    "| **캐시 일관성** | 프로필 변경 시 `user:auth:{id}` 무효화 | 안 지우면 240초 동안 옛 값 |",
    "| **탈퇴** | 소프트 표시 + 주기 purge 잡 | 하드 삭제 전 `like_count` 보정 필수 |",
    "| **차단** | 토글(있으면 해제) · 자기 차단 금지 | 가시성 술어로 목록·댓글·좋아요에 전파 |",
)


# ══════════════════════════════════ posts ═════════════════════════════════
domain(
    "posts",
    "POST /v1/posts   GET /v1/posts · /{id} · /trending · /trending-hashtags   "
    "PATCH · DELETE /{id}",
    "게시글 7개. 아래는 **글 작성 1건**(멱등 키가 걸린 경로) — 전송 버튼을 두 번 누르거나",
    "자동 재시도가 돌면 글이 두 개 생기는 것을 막는다.",
)
plain("CLIENT           POST /v1/posts  + X-Idempotency-Key")
bx(CONN)
core("posts 도메인 코어 ─ 대표 경로: 글 작성 1건 (멱등)")
row()
row("   PostService.create_post()" + " " * 32 + "stack / 수치")
step(
    "① FINGERPRINT       sha256(user_id : key)",
    ["남의 키와 충돌하지 않고 남의 응답도 안 열린다"],
    ["", "사용자별 지문"],
)
down()
step(
    "② REPLAY            이미 만든 적 있나",
    ["★있다 → 저장된 응답 그대로 반환", "        requestId만 이번 요청 것으로 교체"],
    ["──▶ [ Redis ]", "idemp:post:create:res:{fp}", "TTL 1시간"],
)
down()
step(
    "③ CLAIM             처리 중인 같은 키 선점",
    ["★이미 잠겨 있다 → 409"],
    ["──▶ [ Redis ]", "SET …:lock:{fp} NX EX", "TTL 120초"],
)
down()
step(
    "④ CREATE            참조 검증 → posts INSERT",
    ["카테고리 · 해시태그 · 이미지 연결까지", "async with db.begin() 한 트랜잭션"],
    ["──▶ [ PostgreSQL writer ]", "posts · post_images", "post_hashtags"],
)
down()
step(
    "⑤ SETTLE            결과 보관 + 잠금 해제를 동시에",
    ["asyncio.gather → 지연이 합이 아니라 max", "실패했으면 잠금만 해제 · 해제는 CAS"],
    ["──▶ [ Redis ]", "내 토큰일 때만 지운다"],
)
fbot(True, conn=True)
bx(CONN)
bx("   201 { postId, … }")
bx("")
bx("   ★ Redis가 없거나 장애면 ②~⑤가 통째로 빠지고 그냥 만든다 — 멱등성은 잃되 글쓰기는 유지")
bx("")
side("상세 조회 — 조회수는 DB가 아니라 Redis에 쌓는다")
row()
row("   GET /v1/posts/{id}")
step(
    "① READ              게시글 1건",
    ["reader · 1쿼리"],
    ["──▶ [ PostgreSQL reader ]", "저장된 view_count"],
)
down()
step(
    "② DEDUP             최근에 본 사람인지",
    ["★이미 있다 → 집계에서 제외"],
    ["──▶ [ Redis ]", "view:…:viewer:{k}", "SET NX EX 1시간"],
)
down()
step(
    "③ INCR              조회수 +1 — DB가 아니라 Redis에",
    ["돌려받은 값이 곧 아직 DB에 안 들어간 조회수", "★Redis 장애 → writer에 직접 +1"],
    ["──▶ [ Redis ]", "HINCRBY views:{v}:buffer"],
)
down()
step("④ MERGE             보이는 값 = 저장값 + 미반영분", [], [], last=True)
fbot(False)
bx("")
side("조회수 flush — 요청과 무관한 배경 루프 (기본 5분)")
row()
step(
    "① LOCK              여러 대 중 한 대만",
    ["죽어도 2분 뒤 풀려 다음 주기에 다른 대가 이어받는다"],
    ["──▶ [ Redis ]", "try_acquire_lock() TTL 120초"],
)
down()
step(
    "② DETACH            버퍼를 통째로 떼어낸다",
    [
        "★RENAME 은 원자적 — 이 순간 이후의 증가는",
        " 자동으로 새 빈 버퍼에 쌓인다",
        "읽고 지우면 그 사이 조회가 통째로 날아간다",
    ],
    ["──▶ [ Redis ]", "RENAME → drain:{ulid}"],
)
down()
step(
    "③ APPLY             게시글별 UPDATE 한 번",
    [
        "★성공 → drain 폐기. 뒤이은 DEL 실패는 무시",
        "★실패 → 버퍼로 되돌림",
        "커밋 성공 후엔 절대 되돌리지 않는다 —",
        "되돌리면 조회수가 부풀어 오른다",
    ],
    ["──▶ [ PostgreSQL writer ]", "단일 트랜잭션"],
    last=True,
)
fbot(False)
bx("")
side("트렌딩 — 캐시가 본체다")
row()
row("   GET /v1/posts/trending          ·   GET /v1/posts/trending-hashtags")
step(
    "POOL                차단 무관 랭킹을 통째로 캐시",
    [
        "풀 = limit(10) × 3 — 차단 저자가 상위에 있어도",
        "limit 을 채울 headroom",
    ],
    [
        "──▶ [ Redis ] get_or_compute_json",
        "cache:trending_posts:{cat}",
        "TTL 300초 · 창 24h 고정",
        "",
        "cache:trending_hashtags",
        "TTL 600초",
    ],
    last=True,
)
notes(
    "★ 사용자별로 캐시하면 키가 폭발한다 → 풀을 공유하고 차단 필터는 요청별 오버레이",
    "★ 24h 결과가 3건 미만 → 7일·좋아요순 폴백",
)
row("      ★해시태그 전체기간 폴백은 별도 키에 TTL 3600초 — 창이 없으면")
row("        idx_posts_feed_latest 를 못 써 전 이력을 해시 집계한다. 데이터가")
row("        희소한 초기엔 그게 상시 경로라, 같은 TTL 에 묶으면 10분마다 다시 돈다")
fbot(False)
bx("")
side("목록 · 수정 · 삭제")
row()
step(
    "GET /v1/posts             목록",
    ["cursor 페이지네이션 · 차단 술어 포함 · reader"],
    ["──▶ [ PostgreSQL reader ]"],
    last=True,
)
step(
    "PATCH /v1/posts/{id}      수정",
    ["작성자 본인만 · 이미지 재연결은 users 와 같은 문"],
    ["──▶ [ PostgreSQL writer ]"],
    last=True,
)
step(
    "DELETE /v1/posts/{id}     삭제 — 한 트랜잭션에서 셋",
    [
        "① posts soft-delete",
        "② 그 글의 댓글 soft-delete",
        "③ 좋아요 행 삭제",
    ],
    ["──▶ [ PostgreSQL writer ]", "posts", "comments", "post_likes"],
    last=True,
)
notes(
    "★ 리포지터리에 커밋 권한이 없어 자연히 한 트랜잭션 — 각자 커밋했다면 반쯤 지워진 글이",
    "  남았을 것",
)
fbot(False)
dia()
stack(
    "| **API** | FastAPI (async) · `/v1/posts/*` 7개 | 작성 1 · 조회 4 · 수정 1 · 삭제 1 |",
    "| **저장소** | PostgreSQL — 조회 reader / 변경 writer | `posts` · `post_images` · `post_hashtags` |",
    "| **멱등성** | Redis 결과 캐시(1h) + 잠금(120s) · 지문 = `sha256(user_id:key)` | "
    "둘 다 필요 — 잠금만이면 처리 후 재시도를, 캐시만이면 동시 도착을 못 막는다 |",
    "| **조회수** | Redis `HINCRBY` 버퍼 + 5분 주기 flush · 분산 잠금 | "
    "정확도를 일부 포기 — Redis가 죽으면 미반영분은 사라진다(의도된 손실) |",
    "| **트렌딩** | Redis JSON 캐시 · 창 24h 서버 고정 · TTL 300s / 600s / 3600s | "
    "창을 매개변수로 두면 캐시 키가 값별로 분화한다(ADR 0004) |",
    "| **삭제** | soft-delete + 댓글·좋아요 캐스케이드 단일 트랜잭션 | 반쯤 지워진 글이 남지 않게 |",
)


# ════════════════════════════════ comments ════════════════════════════════
domain(
    "comments",
    "POST · GET /v1/posts/{post_id}/comments   GET /{id}/replies   PATCH · DELETE /{id}",
    "2단 트리 댓글(루트 + 대댓글). 아래는 **댓글 작성 1건** — 알림이 생기는 두 도메인 중 하나.",
)
plain("CLIENT           POST /v1/posts/{post_id}/comments  { content, parentId? }")
bx(CONN)
core("comments 도메인 코어 ─ 대표 경로: 댓글 작성 1건")
row()
row("   CommentService.create_comment()" + " " * 26 + "stack / 수치")
step(
    "① VISIBILITY        글이 보이는가 + 작성자는 누구인가",
    [
        "★한 쿼리로 둘 다 — 알림 수신자 판정에",
        " 작성자가 필요해서 따로 안 돈다",
        "차단 · 블라인드 · 삭제는 여기서 404",
    ],
    ["──▶ [ PostgreSQL writer ]", "posts", "_ensure_post_visible()"],
)
down()
step(
    "② INSERT            comments 한 줄",
    ["parentId 가 있으면 대댓글 (2단까지만)"],
    ["──▶ [ PostgreSQL writer ]", "comments"],
)
down()
step(
    "③ COUNT             게시글 comment_count 조정",
    ["같은 트랜잭션 — 카운트 소유는 posts 저장소"],
    ["──▶ [ PostgreSQL writer ]", "posts"],
)
down()
step(
    "④ NOTIFY            알림 기록도 같은 트랜잭션에서",
    ["NotificationService.record()", "★자기 글이면 안 만든다"],
    ["──▶ [ PostgreSQL writer ]", "notifications", "→ § 13 이 이어받는다"],
)
fbot(True, conn=True)
bx(CONN)
bx("   201 { comment }        발행(SSE · 푸시)은 커밋 이후 — § 13")
bx("")
side("목록 — 기본 인기순, 대댓글만 예외")
row()
row("   GET /v1/posts/{post_id}/comments")
step(
    "ROOTS               루트 한 페이지 + 전체 건수",
    [
        "offset 기반 (ADR 0016) — 인기순은 정렬 축이",
        "가변이라 keyset 커서가 성립하지 않는다",
        "★기본 인기순. 동점은 id DESC 로 갈려 최신순",
    ],
    ["──▶ [ PostgreSQL reader ]"],
)
down()
step(
    "PREVIEW             루트마다 대댓글 몇 건을 함께",
    [
        "개수는 서버 상수 — 클라이언트가 제어하면",
        "한 요청이 끌어오는 행 수의 상한이 사라진다",
        "★대댓글은 항상 최신순 — 좋아요 UI 가 루트에만",
        " 있어 인기순이 성립하지 않는다",
    ],
    ["", "트렌딩 window_hours 와", "같은 이유"],
    last=True,
)
fbot(False)
bx("")
side("나머지")
row()
step(
    "GET /{comment_id}/replies   대댓글 이어받기",
    [
        "keyset 페이지 — 목록의 preview 뒤를 잇는다",
    ],
    ["──▶ [ PostgreSQL reader ]"],
    last=True,
)
notes(
    "★ 루트가 이 글의 실제 루트인지 확인 — 대댓글 id 로 한 단계 더 파고드는 요청과 남의 글 id",
    "  조합을 함께 막는다",
    "★ 블라인드 루트도 막는다. 목록에선 서브트리가 통째로 사라지는데 여기만 열려 있으면",
    "  모더레이션이 무력해진다",
    "★ 삭제된 루트는 허용 — placeholder 로 남는다",
)
step("PATCH /{comment_id}         수정 — 본인만", [], ["──▶ [ PostgreSQL writer ]"], last=True)
step(
    "DELETE /{comment_id}        삭제 — soft",
    [
        "UPDATE 우선(행복 경로 1쿼리)",
        "★미존재 · 이미 삭제 · 방금 지운 블라인드 셋이",
        " 여기로 온다. 뒤의 둘은 멱등 204 로 수렴",
    ],
    ["──▶ [ PostgreSQL writer ]"],
    last=True,
)
fbot(False)
dia()
stack(
    "| **API** | FastAPI (async) · `/v1/posts/{id}/comments/*` 5개 | 2단 트리 |",
    "| **저장소** | PostgreSQL — 목록 reader / 변경 writer | `comments` |",
    "| **정렬** | 루트=인기순(offset · ADR 0016) / 대댓글=최신순(keyset) | 정렬 축이 가변이면 keyset 불가 |",
    "| **카운트** | `posts.comment_count` 를 같은 트랜잭션에서 조정 | 블라인드 전이 시 재계산 |",
    "| **알림** | `NotificationService.record()` — 트랜잭션 안에서 기록 | 발행은 커밋 이후(§ 13) |",
    "| **모더레이션** | 블라인드 시 서브트리가 통째로 사라진다 | `replies` 도 막아야 우회가 안 생긴다 |",
)


# ═════════════════════════════════ likes ══════════════════════════════════
domain(
    "likes",
    "POST · DELETE /v1/likes/posts/{post_id} · /v1/likes/comments/{comment_id}",
    "글 좋아요와 댓글 좋아요가 **같은 흐름의 타깃 치환**이다 — `_Target` 하나로 파라미터화돼",
    "있어 코드도 그림도 한 벌이다. 아래는 **좋아요 1건**.",
)
plain("CLIENT           POST /v1/likes/posts/{post_id}     (댓글이면 /comments/{id})")
bx(CONN)
core("likes 도메인 코어 ─ 대표 경로: 좋아요 1건 (타깃 = post | comment)")
row()
row("   LikeService._like()" + " " * 38 + "stack / 수치")
step(
    "① RESOLVE           가시성 + 작성자 조회를 1쿼리로",
    ["차단 술어 포함 — 목록 · 댓글과 같은 판정", "작성자는 알림 수신자로 쓰인다"],
    ["──▶ [ PostgreSQL writer ]", "posts | comments"],
)
down()
step(
    "② LINK              좋아요 행 INSERT",
    ["★ON CONFLICT DO NOTHING → inserted=False", " 중복 좋아요는 조용히 no-op"],
    ["──▶ [ PostgreSQL writer ]", "post_likes | comment_likes"],
)
down()
step(
    "③ COUNT             like_count 증가",
    ["카운터 소유는 타깃 저장소 (posts | comments)", "한 요청 = 한 트랜잭션이라 경쟁이 없다"],
    ["──▶ [ PostgreSQL writer ]"],
)
down()
step(
    "④ NOTIFY            작성자에게 알림",
    ["★자기 글이면 안 만든다", "커밋 이후 발행 — § 13"],
    ["──▶ [ PostgreSQL writer ]", "notifications"],
)
fbot(True, conn=True)
bx(CONN)
bx("   200 { likeCount }")
bx("")
side("회수 — 가시성 술어를 타지 않는다")
row()
row("   DELETE /v1/likes/posts/{post_id}   ·   /v1/likes/comments/{comment_id}")
step(
    "UNLIKE              삭제 후 like_count 반환",
    [
        "없던 좋아요 삭제는 no-op (현재 카운트만 반환)",
    ],
    ["──▶ [ PostgreSQL writer ]"],
    last=True,
)
notes(
    "★ 가드가 like 와 다르다 — post : 미삭제 · 미블라인드만 (차단 술어 없음) comment: 삭제만",
    "  검사 (블라인드 · 차단 무관)",
    "★ 차단하거나 블라인드된 뒤에도 자기 좋아요는 되돌릴 수 있어야 한다 — 고착 방지",
)
fbot(False)
dia()
stack(
    "| **API** | FastAPI (async) · `/v1/likes/*` 4개 | post · comment 가 같은 흐름의 타깃 치환 |",
    "| **저장소** | PostgreSQL writer | `post_likes` · `comment_likes` |",
    "| **중복** | `ON CONFLICT DO NOTHING` | 한 요청 = 한 트랜잭션, race 없음 |",
    "| **카운터** | 타깃 저장소가 소유(`posts` · `comments`) | 프로토콜로 파라미터화 |",
    "| **가시성** | like는 차단 술어 통과 필요 / unlike는 아님 | 권한 경계 — 회수는 고착되면 안 된다 |",
    "| **알림** | 자기 글이 아니면 커밋 후 작성자에게 | § 13 이 이어받는다 |",
)


# ═════════════════════════════════ media ══════════════════════════════════
domain(
    "media",
    "POST /v1/media/images/presign · confirm · signup/presign · signup/confirm   "
    "DELETE /v1/media/images/{id}",
    "서버가 파일을 받아 S3에 올리면 대역폭과 메모리를 파일 크기만큼 쓴다. 대신 일회용 서명",
    "URL을 발급하고 클라이언트가 S3와 직접 이야기하게 한다. 아래는 **업로드 1건**.",
    "",
    "> **불변식** — S3 객체는 항상 그것을 가리키는 `images` 행보다 **늦게 생기고 먼저 사라진다.**",
)
plain(
    "CLIENT           POST /v1/media/images/presign",
    "                 (비로그인 가입 경로는 /signup/presign — IP rate limit)",
)
bx(CONN)
core("media 도메인 코어 ─ 대표 경로: 업로드 1건 (presign → S3 → confirm)")
row()
row("   ① 서명 발급" + " " * 46 + "stack / 수치")
step(
    "POLICY → SIGN       확장자 · MIME · 크기 상한 검사",
    ["★어긋남 → 400. S3를 아직 안 건드려 남는 게 없다", "issue_presigned_upload()"],
    ["", "image_policy.py", "pending/{uuid7}-{원본명}", "TTL 15분"],
)
row()
row("      ★ 이 15분이 아래 모든 경쟁 조건의 원인이다")
row()
row("   ② 클라이언트가 S3로 직접 올린다")
row("      클라이언트 ══ 파일 본체 ══▶ [ S3 ] pending/…")
row("      서버는 이 바이트를 한 번도 손에 쥐지 않는다")
row("      ★여기서 이탈하면 pending/ 고아 객체 → 버킷 lifecycle 이 회수")
row("        (images 행이 없어 스위퍼는 원리적으로 못 본다)")
row()
row("   ③ 확정  POST /v1/media/images/confirm { key }")
step(
    "① HEAD              올라온 게 진짜 그건지",
    ["크기 · Content-Type 대조 + ETag 확보", "★어긋남 → 400. 아직 행도 객체도 없다"],
    ["──▶ [ S3 ] HEAD", "head_pending_object()"],
)
down()
step(
    "② RESERVE           deleted_at 찍은 예약 행 INSERT",
    ["★여기서 커밋한다 — 객체보다 행이 먼저", "목적지 키를 도메인이 미리 만든다"],
    ["──▶ [ PostgreSQL writer ]", "create_reserved_image()", "build_permanent_file_key()"],
)
down()
step(
    "③ LOCK              예약 행을 FOR UPDATE",
    [
        "확정까지 놓지 않는다",
        "★스위퍼가 아직 → SKIP LOCKED 로 건너뛴다",
        "★스위퍼가 먼저 → 기다렸다 행이 사라진 걸 보고",
        "  승격 전에 실패한다. 둘은 겹칠 수 없다",
    ],
    [
        "──▶ [ PostgreSQL writer ]",
        "lock_reserved_image()",
        "",
        "대가: 이 트랜잭션이",
        "S3 COPY 동안 열려 있다",
    ],
)
down()
step(
    "④ PROMOTE           pending/ → media/ 복사",
    [
        "CopySourceIfMatch = ① 에서 받은 ETag",
        "★그 사이 같은 URL 로 덮어썼다 → COPY 가 412",
        " 검증한 바로 그 객체만 옮겨진다",
    ],
    ["──▶ [ S3 ] COPY", "promote_pending_object()"],
)
down()
step(
    "⑤ CONFIRM           deleted_at 지움 + 실측 메타",
    ["★호출부의 마지막 쓰기여야 한다"],
    ["──▶ [ PostgreSQL writer ]", "confirm_reserved_image()"],
)
fbot(True, conn=True)
bx(CONN)
bx("   201 { imageId, url }")
bx("")
bx("   ★ 되감기(보상 삭제)가 한 곳도 없다. 어디서 끊기든 예약 행이 남고 스위퍼가 회수한다 —")
bx("     보상은 그 자체가 실패하면 끝이지만, 스위퍼는 다시 온다.")
bx("")
side("가입 경로만 다른 것")
row()
row("   POST /v1/media/images/signup/confirm 은 확정 뒤에 토큰 발급이 남는다.")
row("   확정이 마지막 쓰기가 아니게 되면, 토큰 발급 실패 시 확정을 되돌리는 쓰기가")
row("   필요해진다 — 방금 없앤 보상 쓰기가 S3 대신 DB 로 되살아난다.")
row()
row("      with _reserved_upload(...) as img:")
row("          토큰 발급              ← 블록 본문 = 확정 전에 할 일")
row("      # 블록을 나가면서 확정     ← 확정은 항상 마지막")
row()
row("   컨텍스트 매니저가 이 순서를 구조로 강제한다.")
fbot(False)
bx("")
side("삭제 — 요청 안에서 S3를 건드리지 않는다")
row()
row("   DELETE /v1/media/images/{image_id}")
step(
    "① MARK              UPDATE images SET deleted_at",
    [
        "소프트 삭제 · FOR NO KEY UPDATE 로 잠긴다",
        "★S3와 DB는 한 트랜잭션으로 못 묶는다. 옛 순서",
        " (S3 → 행)는 행 삭제가 실패하면 500 과 함께",
        " 사용자에게 재시도를 요구했다",
    ],
    ["──▶ [ PostgreSQL writer ]"],
)
down()
step(
    "② DETACH            참조 3종을 끊는다",
    [
        "users.profile_image_id",
        "dog_profiles.profile_image_id",
        "post_images 행",
    ],
    ["──▶ [ PostgreSQL writer ]", "같은 트랜잭션", "별도 문장"],
    last=True,
)
notes(
    "★ ① 과 문장을 나눈 이유 — 한 CTE 로 묶으면 부문장이 전부 문장 시작 시점 스냅샷을 써서,",
    "  잠금을 기다리는 동안 새로 붙은 참조를 못 본다",
)
row("      204 — 사용자에겐 여기서 끝. S3 객체는 아직 남아 있다")
fbot(False)
bx("")
side("첨부 검증이 삭제와 경쟁한다")
row()
row("   posts · users · dogs 가 이미지를 붙일 때 assert_images_attachable() 이")
row("   FOR SHARE 로 잠근다. 소프트 삭제의 UPDATE(FOR NO KEY UPDATE)와 충돌해")
row("   어느 쪽이 먼저든 뒤가 기다린다.")
row()
row("   안 잠그면:   검증 (deleted_at IS NULL 읽기)   ──┐")
row("               소프트 삭제 커밋 · 참조 해제        │  이 사이")
row("               참조 INSERT                     ◄──┘")
row()
row("   → 지운 이미지가 게시글에 붙고, 참조가 있으니 스위퍼가 영구히 수거 못 한다.")
row("     하드 삭제 시절엔 FK 가 막던 것이다.")
fbot(False)
bx("")
side("스위퍼 — 주기 잡 · POST /v1/admin/media/sweep 로도 수동 실행")
row()
row("   두 패스가 서로 다른 것을 본다. 한 쿼리로 합치면 created_at 에 인덱스가 없어")
row("   Postgres 가 BitmapOr 를 못 만들고 전체 스캔으로 떨어진다 (20만 행: 13.7ms → 0.4ms).")
row()
row("     수거 패스                             고아 패스")
row("     deleted_at IS NOT NULL                deleted_at IS NULL")
row("     ORDER BY id (keyset)                  AND created_at < cutoff")
row("     FOR UPDATE SKIP LOCKED                AND 아무도 참조하지 않음")
row("       삭제 요청 · 실패한 확정 둘 다          확정은 됐지만 아무 글에도")
row("       여기로 온다                            안 붙은 이미지")
row()
step(
    "PURGE               S3 삭제 → 성공한 것만 행 삭제",
    [
        "실패분은 커서를 넘겨 다음 회차에 재시도 —",
        "실패한 앞머리가 뒤쪽 정상 행을 굶기지 않게",
    ],
    ["──▶ [ S3 ] · [ PostgreSQL writer ]"],
    last=True,
)
notes(
    "★ 배치당 트랜잭션 하나. 잠근 채로 S3와 행을 함께 지운다 — 놓고 지우면 그 사이 확정 ·",
    "  첨부가 그 행을 바꿀 수 있다",
    "★ 예약 행은 유예(RESERVED_IMAGE_GRACE_SECONDS · 기본 60초)를 넘긴 것만 집는다. 잠금이",
    "  닫지 못하는 창은 예약 행 커밋~잠금 사이(마이크로초)뿐이고 유예가 그걸 덮는다",
)
fbot(False)
bx("")
side("어디서 죽으면 무엇이 남나")
row()
row("     어디서               무엇이 남나                누가 치운다")
row("     ──────────────────   ────────────────────────   ──────────────────")
row("     presign 후 이탈      pending/ 객체              버킷 lifecycle")
row("     HEAD 실패            pending/ 객체              버킷 lifecycle")
row("     예약 후 죽음         예약 행 + pending/ 객체    스위퍼 수거 패스")
row("     승격 후 죽음         예약 행 + media/ 객체      스위퍼 수거 패스")
row("     확정 성공            정상                       —")
row("     삭제 요청            행(deleted_at) + 객체      스위퍼 수거 패스")
row("     아무 글에도 안 붙음  행 + 객체                  스위퍼 고아 패스")
fbot(False)
dia()
stack(
    "| **API** | FastAPI (async) · `/v1/media/*` 5개 | presign · confirm × (일반 · 가입) + 삭제 |",
    "| **파일 본체** | S3 — presign PUT · HEAD · COPY | 외부 인프라 접점 — 서버는 바이트를 안 쥔다 |",
    "| **행** | PostgreSQL writer `images` | 예약(`deleted_at` 찍힘) → 승격 → 확정 |",
    "| **경쟁 차단** | `FOR UPDATE`(승격\\~확정) · `SKIP LOCKED`(스위퍼) · `FOR SHARE`(첨부 검증) | "
    "권한 경계 — 하드 삭제 시절 FK가 하던 일을 잠금이 대신한다 |",
    "| **덮어쓰기 차단** | `CopySourceIfMatch` = HEAD 때의 ETag | 서명 URL 15분 창의 재업로드를 412로 |",
    "| **회수** | 스위퍼 2패스(수거 · 고아) + 버킷 lifecycle | 보상 삭제 0곳 — 실패해도 다음 회차가 온다 |",
    "| **삭제** | 소프트 삭제 + 참조 3종 해제(별도 문장) | 요청 경로에서 S3를 안 건드린다 |",
)


# ══════════════════════════════════ chat ══════════════════════════════════
domain(
    "chat",
    "WS /v1/ws/chat   POST /v1/chat/rooms/direct/{peer}   GET /rooms · /rooms/{id}   "
    "GET /rooms/{id}/messages   POST /rooms/{id}/read",
    "소켓은 인스턴스 하나에 물리적으로 묶인다. 1번은 2번에 붙은 사람의 소켓을 쥐고 있지 않다.",
    "Redis Pub/Sub이 그 사이의 전달선이다. 아래는 **DM 전송 1건**.",
)
plain("A · 인스턴스 1에 붙어 있다        WS 프레임 { peerId, content }")
bx(CONN)
core("chat 도메인 코어 ─ 대표 경로: DM 전송 1건")
row()
row("   ChatService.send_message()" + " " * 31 + "stack / 수치")
step(
    "① GUARD             차단 · 탈퇴 여부",
    [
        "★캐시 적중과 무관하게 매 전송마다 돈다",
        " hit/miss 가 서로 다른 검사 경로를 타면",
        " 나중에 추가되는 가드가 두 번째 메시지부터",
        " 조용히 빠진다",
    ],
    ["──▶ [ PostgreSQL writer ]", "assert_can_dm()"],
)
down()
step(
    "② ROOM              peer_id → room_id",
    [
        "★소켓 세션 캐시. 미스일 때만 upsert",
        "방 id 는 유저 쌍당 불변이라 캐시해도 안전",
        "상한 64개 — 상대를 바꿔가며 보내면",
        "소켓 하나가 하루 수만 엔트리를 쌓는다",
    ],
    ["──▶ [ PostgreSQL writer ]", "chat_rooms", "메시지당 왕복 3 → 2", "그중 쓰기 2 → 1"],
)
down()
step(
    "③ STORE             chat_messages INSERT → 커밋",
    ["여기까지가 진실. 아래는 전부 부가 기능"],
    ["──▶ [ PostgreSQL writer ]"],
)
down()
step(
    "④ CACHE             room_id 기록",
    [
        "★커밋 성공 뒤에만. 트랜잭션 안에서 기록하면",
        " 롤백된 room_id 가 소켓 수명 내내 남아 이후",
        " 전송이 전부 FK 위반이 된다 — 수신 루프는",
        " 예외를 삼키므로 재연결 전까지 자가 치유 없음",
    ],
    ["", "프로세스 메모리", "인스턴스 한정"],
)
down()
step(
    "⑤ FANOUT            두 갈래가 동시에 (asyncio.gather)",
    [
        "├ 이 인스턴스 소켓에 직접 → A의 다른 탭",
        "└ publish {origin, targets:[B,A], payload}",
        "★정체 소켓 하나가 publish 를 늦추지 않게 —",
        " 지연이 합이 아니라 max",
    ],
    ["──▶ [ Redis PUBLISH ]", "publish_user_envelope()", "CHAT_DM_FANOUT_CHANNEL"],
)
fbot(True, conn=True)
bx(CONN)
bx("   [ Redis 채널 user-fanout ] — 모든 인스턴스가 받는다")
bx("      ├─ 인스턴스 1 리스너 → origin이 나다 → 버린다 (로컬 전달로 이미 갔다)")
bx("      └─ 인스턴스 2 리스너 → targets 중 여기 붙은 소켓으로 → B 수신")
bx("           유저당 소켓 5개 상한 · 전송 5초 초과 시 소켓을 실제로 닫는다")
bx("")
bx("   ★ at-most-once. publish 가 실패하면 다른 인스턴스 수신자는 못 받는다 — DB엔 있다.")
bx("     재전송 큐는 만들지 않았다. 진실은 DB에 있고 복구는 재조회다.")
bx("")
side("리스너 — 프로세스당 연결 1개")
row()
step(
    "SUBSCRIBE           run_user_fanout_listener()",
    [
        "끊기면 0.5초 → 30초 백오프 재연결",
        "5초 이상 살아남아야 백오프를 초기화한다",
    ],
    ["──▶ [ Redis SUBSCRIBE ]", "", "", "", "", "", "앱 수준 워치독"],
    last=True,
)
notes(
    "★ 연결마다 구독을 열면 동시 접속자 수만큼 Redis 연결이 필요해 풀이 마른다 → rate limit ·",
    "  인증 캐시 · 조회수 버퍼가 연쇄적으로 fail-open",
    "★ 유휴 10초면 PING, pong 5초 없으면 끊고 재연결 — get_message(timeout=) 이 소켓",
    "  타임아웃을 덮어써 먹통 서버에서 예외가 영영 안 뜬다",
)
fbot(False)
bx("")
side("재동기 — 끊긴 사이를 메운다")
row()
row("   GET /v1/chat/rooms/{room_id}/messages?cursor={마지막 받은 id}&direction=after")
step(
    "RESYNC              커서 이후를 인접한 쪽부터",
    [
        "items 는 방향과 무관하게 항상 최신순 —",
        "다음 커서만 방향마다 다르다 (before=마지막,",
        "after=첫 번째)",
    ],
    ["──▶ [ PostgreSQL writer ]", "", "", "", "무한 스크롤(before)은", "과거 조회라 reader 유지"],
    last=True,
)
notes(
    "★ direction=before 만으로는 안 된다 — 최신 N건 재조회는 끊긴 사이 N건이 넘게 쌓였을 때",
    "  앞뒤만 맞고 중간이 빈다",
    "★ 이 경로만 master 를 읽는다. reader 복제 지연이 has_more=false 를 거짓말로 만들면",
    "  메우려던 구멍이 남은 채 양쪽 다 이어진 줄 안다",
    "★ cursor 없는 after 는 거부. 단 멤버십 가드 뒤에 — 앞세우면 비멤버가 403 대신 400 을",
    "  받는다",
)
fbot(False)
bx("")
side("종료 코드 · 나머지 REST")
row()
row("     4001   유저당 동시 연결 상한 초과   재연결해도 같은 결과 — 다른 탭을 닫아야")
row("     4002   레이트리밋 초과              백오프 후 재연결하면 회복된다")
row("     1011   서버 내부 오류               재연결")
row()
row("   ★등록만 지우고 소켓을 안 닫으면 클라이언트는 살아 있는 줄 알고 계속 보내면서")
row("     수신만 조용히 잃는다. 재연결 로직도 안 뜬다. 그래서 반드시 닫는다.")
row()
step(
    "POST /rooms/direct/{peer}   DM 방 생성 · 조회",
    ["uq_chat_rooms_user_pair — 유저 쌍당 하나"],
    ["──▶ [ PostgreSQL writer ]"],
    last=True,
)
step(
    "GET  /rooms                 방 목록",
    [],
    ["──▶ [ PostgreSQL reader ]", "list_recent_rooms()"],
    last=True,
)
notes(
    "★ 정렬은 ChatRoom.updated_at 이 아니라 최근 메시지 시각. 그래서 방 캐시 히트 경로에서",
    "  updated_at 이 안 갱신돼도 목록이 안 틀어진다 — 모델에 그 뜻을 못 박아 활동 지표로",
    "  읽는 조회가 붙지 않게 했다",
)
step("GET  /rooms/{id}            방 1건", [], ["──▶ [ PostgreSQL reader ]"], last=True)
step(
    "POST /rooms/{id}/read       읽음 처리",
    ["멤버십 가드 뒤"],
    ["──▶ [ PostgreSQL writer ]"],
    last=True,
)
fbot(False)
dia()
stack(
    "| **API** | FastAPI · WS 1 + REST 5 | `/v1/ws/chat` · `/v1/chat/*` |",
    "| **저장소** | PostgreSQL — 재동기만 master, 무한 스크롤은 reader | `chat_rooms` · `chat_messages` |",
    "| **인스턴스 간** | Redis Pub/Sub `user-fanout` · envelope에 `origin` | "
    "구독은 프로세스당 1개 — 연결당 구독은 풀을 말린다 |",
    "| **소켓 목록** | 프로세스 메모리 · 유저당 5개 상한 · 전송 5초 타임아웃 | 인스턴스 한정, 진실로 안 씀 |",
    "| **방 캐시** | 소켓 세션 로컬 · 64개 상한 · 커밋 성공 뒤에만 기록 | 메시지당 쓰기 2 → 1 |",
    "| **전달 보장** | at-most-once. 복구는 `direction=after` 재조회 | 재전송 큐 없음 — 진실은 DB |",
    "| **복원력** | 리스너 0.5→30초 백오프 + PING 워치독(10s/5s) | 소켓 타임아웃만으로는 안 뜬다 |",
)


# ═════════════════════════════ notifications ══════════════════════════════
domain(
    "notifications",
    "GET /v1/notifications · /stream   PATCH /v1/notifications/read",
    "알림은 스스로 생기지 않는다. **comments · likes 트랜잭션 안에서** 행이 만들어지고",
    "(§ 9 · § 10), 이 도메인은 그 뒤를 맡는다. 아래는 **알림 1건이 나가는 경로**.",
)
plain("comments · likes 요청  ──▶  NotificationService.record()")
bx(CONN)
core("notifications 도메인 코어 ─ 대표 경로: 알림 1건 발행")
row()
step(
    "① RECORD            notifications 행 (호출자 트랜잭션)",
    ["★여기가 진실. 세 갈래가 다 실패해도", " 목록을 열면 남아 있다"],
    ["──▶ [ PostgreSQL writer ]", "NotificationService.record()"],
)
down()
row("  ━━ 커밋 ━━━ 이 선을 넘긴 뒤에만 바깥으로 나간다 ━━━━━━━━━━━━━━━━━━━━━━")
row("     ★커밋 전에 쏘면 롤백 시 댓글은 없는데 알림만 남는다 — 열어도 대상이 없다")
down()
step(
    "② SSE (로컬)        같은 인스턴스 스트림에 직접",
    ["★메모리 큐 100건 — 넘치면 새 이벤트를 버린다", " 느린 클라이언트가 팬아웃을 막지 않게"],
    ["", "프로세스 메모리", "ensure_sse_capacity()"],
)
down()
step(
    "③ PUBLISH           다른 인스턴스용 발행",
    ["채팅과 같은 봉투 규약 {origin, targets, payload}"],
    ["──▶ [ Redis PUBLISH ]", "notif:sse"],
)
down()
step(
    "④ ENQUEUE           푸시를 요청 밖으로",
    [
        "워커가 꺼내 SNS 발송 · 3회까지 재시도",
        "같은 알림은 멱등 키로 한 번만 배송",
    ],
    ["──▶ [ Celery / Redis ]", "큐 high_priority", "→ [ AWS SNS ]", "", "task_ignore_result=True"],
    last=True,
)
notes(
    "★ 브로커 장애 → 앱이 인라인 발송(같은 멱등 키)",
    "★ .delay() 가 요청 경로라 소켓 타임아웃 필수 — 죽은 브로커에 매달리면 요청이 통째로",
    "  멈춘다",
    "★ 결과 백엔드를 껐다. ignore_result 가 아니면 .delay() 가 pubsub.subscribe 를",
    "  여는데(Celery 기본 socket 120s · connect 무제한) 결과를 읽는 곳이 없다",
)
fbot(True)
bx("")
bx("   ★ ②③④ 는 순서가 아니라 동시다. 셋 다 실패해도 되돌리지 않는다.")
bx("   ★ 종료 시 인라인 태스크를 드레인한다 — 발행과 멱등 마킹 사이에서 끊기면")
bx("     워커 재시도 때 이중 배송 창이 다시 열린다 (drain_sns_inline_tasks · 5초)")
bx("")
side("읽는 쪽")
row()
step(
    "GET  /v1/notifications/stream   SSE 구독",
    [
        "구독은 이 인스턴스 큐 + Redis 채널 두 갈래",
        "★연결 수도 WS 와 같은 유저당 상한(5)을 공유한다",
    ],
    ["", "sse_subscribe()", "REALTIME_MAX_", "CONNECTIONS_PER_USER"],
    last=True,
)
notes(
    "★ 큐는 bounded 여도 연결 수가 무제한이면 유저 한 명이 인스턴스 로컬 상태를 무한히 늘린다",
    "★ Caddy 에 flush_interval -1 이 없으면 SSE 가 프록시 버퍼에 갇혀 실시간이 아니게 된다",
)
step(
    "GET  /v1/notifications          목록",
    ["cursor 페이지네이션 · reader"],
    ["──▶ [ PostgreSQL reader ]"],
    last=True,
)
step(
    "PATCH /v1/notifications/read    읽음 처리",
    ["오래된 알림은 주기 잡이 정리(purge_old)"],
    ["──▶ [ PostgreSQL writer ]"],
    last=True,
)
fbot(False)
dia()
stack(
    "| **API** | FastAPI · `/v1/notifications/*` 3개 (SSE 1) | 생성 지점은 comments · likes 뿐 |",
    "| **저장소** | PostgreSQL writer / 목록은 reader | `notifications` — 여기가 진실 |",
    "| **로컬 전달** | 프로세스 메모리 큐 100건 · 넘치면 버린다 | 느린 클라이언트 격리 |",
    "| **인스턴스 간** | Redis Pub/Sub `notif:sse` | 채팅과 같은 봉투 규약 |",
    "| **푸시** | Celery `high_priority` → AWS SNS · 3회 재시도 | "
    "브로커 장애 시 인라인 폴백(같은 멱등 키) · `task_ignore_result=True` |",
    "| **순서** | 기록은 트랜잭션 안, 발행은 **반드시 커밋 이후** | 롤백 시 실체 없는 알림이 남지 않게 |",
    "| **프록시** | Caddy `flush_interval -1` | 없으면 SSE가 버퍼에 갇힌다 |",
)


# ══════════════════════════════════ dogs ══════════════════════════════════
domain(
    "dogs",
    "PATCH /v1/users/me/dogs/representative",
    "반려견 프로필. 엔드포인트는 대표견 지정 **하나뿐**이고, 프로필 생성·수정·삭제는",
    "`users`의 `PATCH /v1/users/me`가 이 도메인 서비스를 부른다(§ 7 ③). 아래는 **대표견 지정 1건**.",
)
plain("CLIENT           PATCH /v1/users/me/dogs/representative  { dogId }")
bx(CONN)
core("dogs 도메인 코어 ─ 대표 경로: 대표견 지정 1건")
row()
row("   DogService.set_representative_dog()" + " " * 28 + "stack / 수치")
step(
    "① OWN               내 강아지가 맞는가",
    ["★남의 dog_id 면 404 — owner_id 와 함께 조회한다"],
    ["──▶ [ PostgreSQL writer ]", "dog_profiles"],
)
down()
step(
    "② SWITCH            전체 False → 대상 1개만 True",
    ["부분 유니크 인덱스가 소유자당 1개를 강제한다"],
    ["──▶ [ PostgreSQL writer ]", "set_representative()"],
)
down()
step(
    "③ RETURN            갱신된 사용자 프로필을 통째로",
    ["클라가 재조회할 필요가 없다"],
    ["──▶ [ PostgreSQL writer ]", "get_user_by_id_with_dogs()"],
    last=True,
)
fbot(True)
bx("")
bx("   200 { user }        셋 다 한 트랜잭션 — 대표가 둘인 순간이 없다")
bx("")
side("프로필 upsert — 입구는 users, 로직은 여기")
row()
row("   PATCH /v1/users/me  { dogs: [...] }  ──▶  DogService.upsert_dog_profile()")
step(
    "REPLACE             목록 전체 교체 (생성·수정·삭제)",
    [
        "붙는 이미지는 assert_images_attachable() 로 검증",
        "create·update 행은 is_representative 를 항상 False",
        "대표 배정은 마지막 set_representative 에 일임",
    ],
    ["──▶ [ PostgreSQL writer ]", "dog_profiles · images"],
    last=True,
)
notes(
    "★ 인라인으로 True 를 여러 행에 넣으면 부분 유니크 인덱스가 한 트랜잭션 안에서도",
    "  소유자당 1개를 강제하므로 statement 시점에 일시적 중복으로 거부된다",
    "★ 예전엔 payload 의 이미지 id 를 그대로 bulk upsert 에 넘겨 검증 자체가 없었다 —",
    "  소프트 삭제 전환 때 users · posts 와 같은 문(FOR SHARE)으로 모았다",
)
fbot(False)
dia()
stack(
    "| **API** | FastAPI (async) · `/v1/users/me/dogs/representative` 1개 | upsert 입구는 `users`의 `PATCH /me` |",
    "| **저장소** | PostgreSQL writer | `dog_profiles` |",
    "| **이미지** | `assert_images_attachable()` · `FOR SHARE` | users · posts 와 같은 문 |",
    "| **불변식** | 대표견은 소유자당 1마리 — 부분 유니크 인덱스 | 전환은 전체 False → 1개 True 순서 |",
    "| **소유권** | `owner_id` 와 함께 조회 — 남의 `dog_id` 는 404 | 권한 경계 |",
)


# ═════════════════════════════════ reports ════════════════════════════════
domain(
    "reports",
    "POST /v1/reports",
    "신고 접수. 임계값에 닿으면 **자동 블라인드**까지 한 트랜잭션에서 끝난다.",
    "타깃(post · comment) 분기는 `reports/targets.py` 의 모더레이션 배선이 흡수한다.",
)
plain("CLIENT           POST /v1/reports  { targetType, targetId, reason }")
bx(CONN)
core("reports 도메인 코어 ─ 대표 경로: 신고 1건 (타깃 = post | comment)")
row()
row("   ReportService.submit_report()" + " " * 28 + "stack / 수치")
step(
    "① INCREMENT         report_count += 1",
    [
        "★RETURNING 이 존재 확인을 겸한다 —",
        " None = 미존재 · 삭제 · 저자 없는 글",
        " 별도 사전 조회 쿼리가 없다",
    ],
    ["──▶ [ PostgreSQL writer ]", "posts | comments", "moderation_target(type)"],
)
down()
step("② RECORD            reports 한 줄", [], ["──▶ [ PostgreSQL writer ]", "reports"])
down()
step(
    "③ AUTO-BLIND        임계값 도달 시 블라인드",
    [
        "new_count >= REPORT_BLIND_THRESHOLD",
        "블라인드 전이는 게시글 comment_count 까지 조율",
        "(댓글이면 CommentModeration 이 재계산)",
    ],
    ["──▶ [ PostgreSQL writer ]", "settings.REPORT_BLIND_THRESHOLD"],
    last=True,
)
fbot(True)
bx("")
bx("   201 { reported: true, blinded: true|false }        전부 단일 트랜잭션")
dia()
stack(
    "| **API** | FastAPI (async) · `/v1/reports` 1개 | post · comment 가 타깃 치환 |",
    "| **저장소** | PostgreSQL writer | `reports` + 타깃의 `report_count` |",
    "| **분기 흡수** | `reports/targets.py` 모더레이션 배선 | admin 과 같은 배선을 공유 |",
    "| **자동 조치** | `REPORT_BLIND_THRESHOLD` 도달 시 블라인드 | 접수와 같은 트랜잭션 |",
    "| **존재 확인** | `increment` 의 `RETURNING` 이 겸한다 | 사전 조회 쿼리 없음 |",
)


# ══════════════════════════════════ admin ═════════════════════════════════
domain(
    "admin",
    "GET /v1/admin/reported-posts   PATCH blind · unblind · reset-reports (post · comment)   "
    "PATCH users/{id}/suspend · activate   DELETE posts/{id} · comments/{id}   "
    "POST media/sweep",
    "12개지만 **모양이 다섯 가지**다 — 블라인드류 · 카운트 초기화 · 삭제 · 유저 상태 · 스윕.",
    "타깃(post ↔ comment) 치환은 reports 와 같은 배선이 흡수한다.",
    "",
    "> admin은 **판정을 재구현하지 않는다.** 읽기와 상태 전이만 하고 규칙은 각 도메인이 소유한다 —",
    "> 재구현하면 규칙이 두 벌이 되어 어긋난다.",
)
plain("ADMIN            PATCH /v1/admin/posts/{id}/blind    (댓글이면 /comments/{id}/blind)")
bx(CONN)
core("admin 도메인 코어 ─ 대표 경로: 블라인드 1건")
row()
step(
    "① TARGET            moderation_target(type)",
    ["post ↔ comment 분기를 배선이 흡수한다", "reports 와 같은 배선"],
    ["", "reports/targets.py"],
)
down()
step(
    "② TRANSITION        블라인드 전이",
    ["★전이가 없었다 → 이미 블라인드(멱등 성공)인지", " 미존재 · 삭제(404)인지 가른다"],
    ["──▶ [ PostgreSQL writer ]", "set_blinded()"],
)
down()
step(
    "③ RESYNC            게시글 comment_count 재계산",
    ["★댓글 블라인드는 카운트를 바꾼다 —", " 다시 세어 넣는다(증감이 아니라 재계산)"],
    ["──▶ [ PostgreSQL writer ]", "CommentModeration"],
    last=True,
)
fbot(True)
bx("")
side("나머지 넷 — 같은 모양끼리 묶었다")
row()
step(
    "PATCH …/unblind          해제  (post · comment)",
    ["블라인드의 역전이 · 카운트 재계산도 대칭"],
    ["──▶ [ PostgreSQL writer ]"],
    last=True,
)
step(
    "PATCH …/reset-reports    신고 카운트 초기화",
    ["post · comment 둘 다", "오탐 신고가 쌓여 자동 블라인드된 것을 되돌린다"],
    ["──▶ [ PostgreSQL writer ]"],
    last=True,
)
step(
    "DELETE …/posts · comments   삭제",
    [
        "★캐스케이드 · 멱등 시맨틱은 각 도메인 서비스가",
        " 소유한다. admin 은 그 서비스를 부를 뿐 —",
        " 글 삭제는 § 8, 댓글 삭제는 § 9 와 같은 경로",
    ],
    ["──▶ PostService · CommentService"],
    last=True,
)
step(
    "PATCH users/{id}/suspend · activate   유저 상태",
    ["★정지는 인증 스냅샷에 실려 다음 요청부터", " DB 없이 즉시 튕긴다 (§ 6 ②)"],
    ["──▶ [ PostgreSQL ] · [ Redis ]"],
    last=True,
)
step(
    "POST media/sweep         스위퍼 수동 실행",
    [
        "★주기 잡과 같은 잠금 키를 쓴다 — 키가 갈리면",
        " 두 경로가 서로를 배제하지 못해 동시에 같은",
        " 객체를 지우려 든다 (§ 11)",
    ],
    ["──▶ MediaService · [ Redis ] 잠금"],
    last=True,
)
step(
    "GET  reported-posts      신고 누적 목록",
    ["본문 미리보기만 잘라 반환"],
    ["──▶ [ PostgreSQL reader ]"],
    last=True,
)
fbot(False)
dia()
stack(
    "| **API** | FastAPI (async) · `/v1/admin/*` 12개 | 모양은 다섯 가지 |",
    "| **저장소** | PostgreSQL — 목록 reader / 전이 writer | 타깃 테이블의 상태 컬럼 |",
    "| **분기 흡수** | `reports/targets.py` 모더레이션 배선 | post ↔ comment 치환 |",
    "| **위임** | 삭제 · 캐스케이드는 각 도메인 서비스 호출 | 권한 경계 — 규칙이 두 벌이 되지 않게 |",
    "| **유저 정지** | `users` 상태 + 인증 스냅샷 | 다음 요청부터 DB 없이 즉시 차단 |",
    "| **스윕** | 주기 잡과 **같은 잠금 키** | 키가 갈리면 동시에 같은 객체를 지운다 |",
)

# ══════════════════════════════════════════════════════════════════════════
check()
open("docs/pipeline.md", "w").write("\n".join(MD).rstrip() + "\n")
