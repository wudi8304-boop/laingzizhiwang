#!/usr/bin/env bash
# 备份数据目录；SQLite 使用在线 backup API，并对副本执行 integrity_check。
set -Eeuo pipefail
umask 077

APP_DIR="${APP_DIR:-/www/laingzizhiwang}"
DATA_DIR="${DATA_DIR:-/www/laingzizhiwang-data}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/lzz-config}"
ENV_FILE="${ENV_FILE:-/etc/lzz-config.env}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
mode="${1:---manual}"

[[ -r "${ENV_FILE}" ]] && set -a && source "${ENV_FILE}" && set +a
mkdir -p "${BACKUP_DIR}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="${stamp}-${mode#--}"
work_dir="$(mktemp -d "${BACKUP_DIR}/.${name}.XXXXXX")"
trap 'rm -rf "${work_dir}"' EXIT
mkdir -p "${work_dir}/sqlite"

if [[ -d "${DATA_DIR}" ]]; then
    # SQLite 文件由下方在线 API 单独备份，避免直接复制活跃数据库。
    tar --exclude='*.db' --exclude='*.db-*' \
        --exclude='*.sqlite' --exclude='*.sqlite-*' \
        --exclude='*.sqlite3' --exclude='*.sqlite3-*' \
        -C "${DATA_DIR}" -czf "${work_dir}/data-files.tar.gz" .
fi

DATA_DIR="${DATA_DIR}" SQLITE_BACKUP_DIR="${work_dir}/sqlite" /usr/bin/python3 - <<'PY'
import os
import pathlib
import sqlite3
import subprocess
import sys

source_dir = pathlib.Path(os.environ["DATA_DIR"])
target_dir = pathlib.Path(os.environ["SQLITE_BACKUP_DIR"])
patterns = ("*.db", "*.sqlite", "*.sqlite3")
databases = sorted({p for pattern in patterns for p in source_dir.rglob(pattern)}) if source_dir.exists() else []
for source in databases:
    relative = source.relative_to(source_dir)
    target = target_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if hasattr(sqlite3.Connection, "backup"):
            with sqlite3.connect("file:%s?mode=ro" % source.as_posix(), uri=True) as src:
                with sqlite3.connect(str(target)) as dst:
                    src.backup(dst)
        else:
            subprocess.check_call(["sqlite3", str(source), ".backup %s" % str(target)])
        with sqlite3.connect("file:%s?mode=ro" % target.as_posix(), uri=True) as check:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError("integrity_check: %s" % result)
        print("SQLite 已验证: %s" % relative)
    except Exception as exc:
        print("SQLite 备份失败 %s: %s" % (source, exc), file=sys.stderr)
        sys.exit(1)
PY

archive="${BACKUP_DIR}/${name}.tar.gz"
tar -C "${work_dir}" -czf "${archive}" .
sha256sum "${archive}" >"${archive}.sha256"
echo "本地备份完成：${archive}"

if [[ "${OSS_ENABLED:-false}" == "true" ]]; then
    : "${OSS_DEST:?OSS_ENABLED=true 时必须填写 OSS_DEST}"
    : "${OSS_ENDPOINT:?OSS_ENABLED=true 时必须填写 OSS_ENDPOINT}"
    command -v ossutil >/dev/null || {
        echo "错误：未安装 ossutil，无法上传 OSS。" >&2
        exit 1
    }
    oss_args=(-e "${OSS_ENDPOINT}")
    if [[ "${OSS_AUTH_MODE:-AK}" == "EcsRamRole" ]]; then
        oss_args+=(--mode EcsRamRole)
        [[ -n "${OSS_ECS_ROLE_NAME:-}" ]] && oss_args+=(--ecs-role-name "${OSS_ECS_ROLE_NAME}")
    fi
    ossutil "${oss_args[@]}" cp -f "${archive}" "${OSS_DEST%/}/$(basename "${archive}")"
    ossutil "${oss_args[@]}" cp -f "${archive}.sha256" "${OSS_DEST%/}/$(basename "${archive}.sha256")"
    echo "OSS 上传完成。"
fi

find "${BACKUP_DIR}" -maxdepth 1 -type f \
    \( -name '*.tar.gz' -o -name '*.tar.gz.sha256' \) \
    -mtime "+${BACKUP_RETENTION_DAYS}" -delete
