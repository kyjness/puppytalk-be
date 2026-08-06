#!/usr/bin/env bash
# 데모 서버의 DB 일일 백업 — pg_dump → gzip → S3(backup/ 프리픽스, 14일 후 자동 만료).
#
# 왜 필요한가: PostgreSQL이 관리형(RDS)이 아니라 컨테이너 볼륨이라 자동 백업·PITR이 없다.
# 인스턴스를 날리면 데이터도 함께 사라지므로, 최소한 하루 단위 사본은 밖에 둔다.
#
# 설치: 서버의 /opt/puppytalk/backup_db.sh 로 올리면 인스턴스 부팅 시 등록된 cron
# (/etc/cron.d/puppytalk-backup, 매일 19:00 UTC = 04:00 KST)이 실행한다.
# 수동 실행: cd /opt/puppytalk && ./backup_db.sh

set -euo pipefail

cd "$(dirname "$0")"

# .env.prod 에서 DB 접속 정보와 S3 자격을 읽는다(앱과 같은 값 — 별도 시크릿을 늘리지 않는다).
set -a
# shellcheck disable=SC1091
. ./.env.prod
set +a

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="/tmp/puppytalk-${STAMP}.sql.gz"

compose() { docker compose --env-file .env.prod -f compose.prod.yml "$@"; }

echo "[$(date -u +%FT%TZ)] 백업 시작"

# -T: cron에는 TTY가 없다. 압축은 호스트에서 — db 컨테이너에 gzip이 없을 수 있다.
compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip -9 >"$DUMP"

# aws CLI를 호스트에 설치하지 않고 컨테이너로 한 번만 띄운다(디스크·업데이트 부담 없음).
docker run --rm \
  -v /tmp:/backup:ro \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_DEFAULT_REGION="${AWS_REGION}" \
  amazon/aws-cli:latest \
  s3 cp "/backup/$(basename "$DUMP")" "s3://${S3_BUCKET_NAME}/backup/$(basename "$DUMP")"

rm -f "$DUMP"
echo "[$(date -u +%FT%TZ)] 백업 완료: s3://${S3_BUCKET_NAME}/backup/$(basename "$DUMP")"
