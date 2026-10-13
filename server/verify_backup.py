#!/usr/bin/env python3
"""验证最近一次备份归档可读取，且其中 SQLite 副本完整。"""
import argparse
import glob
import os
import sqlite3
import tarfile
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-dir", default=os.environ.get("BACKUP_DIR", "/var/backups/lzz-config"))
    args = parser.parse_args()
    archives = sorted(
        glob.glob(os.path.join(args.backup_dir, "*.tar.gz")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not archives:
        raise SystemExit("没有可验证的备份归档")
    archive = archives[0]
    checked = 0
    with tempfile.TemporaryDirectory(prefix="lzz-backup-check-") as target:
        with tarfile.open(archive, "r:gz") as handle:
            members = [
                member for member in handle.getmembers()
                if member.isfile() and member.name.startswith("./sqlite/")
                and member.name.lower().endswith((".db", ".sqlite", ".sqlite3"))
            ]
            for member in members:
                normalized = os.path.abspath(os.path.join(target, member.name))
                if not normalized.startswith(os.path.abspath(target) + os.sep):
                    raise SystemExit("备份包含不安全路径")
                handle.extract(member, target)
                with sqlite3.connect("file:%s?mode=ro" % normalized, uri=True) as conn:
                    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise SystemExit("SQLite 完整性失败：%s" % member.name)
                checked += 1
    if not checked:
        raise SystemExit("备份中没有 SQLite 数据库")
    print("备份恢复验证通过：%s，数据库 %s 个" % (archive, checked))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
