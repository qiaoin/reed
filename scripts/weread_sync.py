#!/usr/bin/env python3
"""从微信读书 Mac 本地库同步划线（= 查词）数据，写入 reading-log.csv。

原理：微信读书 Mac 版把划线存在
  ~/Library/Containers/com.tencent.weread/Data/Documents/WeReadDB_*.db
表 WRBookmark 里每一条就是你划的词，带章节、字符位置、时间戳。
脚本做快照（不碰运行中的库），按「章节字数 + 划线密度」还原每天读了多少词。

用法:
    python3 scripts/weread_sync.py              # 最近 30 天
    python3 scripts/weread_sync.py --days 7
    python3 scripts/weread_sync.py --all
    python3 scripts/weread_sync.py --dry-run    # 只看，不写文件
"""

from __future__ import annotations

import argparse
import csv
import difflib
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOKS_CSV = ROOT / "books.csv"
LOG_CSV = ROOT / "log" / "reading-log.csv"
MARKS_CSV = ROOT / "log" / "weread-marks.csv"
OPEN_MD = ROOT / "log" / "open-words.md"
SESSIONS_CSV = ROOT / "log" / "weread-sessions.csv"
OVERRIDES_CSV = ROOT / "log" / "date-overrides.csv"
CONTAINER = Path.home() / "Library/Containers/com.tencent.weread/Data/Documents"
SESSION_GAP_MIN = 20
DEFERRED_COLOR = 4  # 绿色 = 不认识但没查；其他任何颜色都算已处理
LOG_FIELDS = [
    "date", "book", "from_pct", "to_pct", "minutes", "lookups", "marks",
    "stuck", "rereads", "immersion", "perfectionism", "highlight", "source", "slot",
]


def find_db() -> Path:
    candidates = sorted(CONTAINER.glob("WeReadDB_*.db"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit(f"没找到微信读书数据库：{CONTAINER}/WeReadDB_*.db")
    return candidates[-1]


def snapshot(db: Path) -> Path:
    """把 db + wal + shm 拷到临时目录，避免锁库（App 开着也能读）。"""
    tmp = Path(tempfile.mkdtemp(prefix="weread-sync-"))
    for suffix in ("", "-wal", "-shm"):
        src = db.with_name(db.name + suffix)
        if src.exists():
            shutil.copy2(src, tmp / src.name)
    return tmp


def load_books() -> list[dict]:
    with BOOKS_CSV.open(encoding="utf-8-sig", newline="") as fh:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def match_book(wx_title: str, wx_id: str, books: list[dict]) -> dict | None:
    for row in books:
        if row.get("weread_id") and row["weread_id"] == wx_id:
            return row
    lowered = wx_title.casefold()
    hits = [r for r in books if r["title"].casefold() in lowered]
    if hits:
        return max(hits, key=lambda r: len(r["title"]))
    names = [r["title"] for r in books]
    close = difflib.get_close_matches(wx_title, names, n=1, cutoff=0.9)
    return next((r for r in books if r["title"] == close[0]), None) if close else None


def load_date_overrides() -> dict[tuple[str, date], date]:
    """用户自己记的「哪一晚读的」和微信读书时间戳不一致时，在这里改日期。

    log/date-overrides.csv: book,from_date,to_date,note
    只挪日期，不动钟点；同步时优先于划线时间戳。
    """
    if not OVERRIDES_CSV.exists():
        return {}
    overrides: dict[tuple[str, date], date] = {}
    with OVERRIDES_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            book = (row.get("book") or "").strip()
            src = (row.get("from_date") or "").strip()
            dst = (row.get("to_date") or "").strip()
            if not (book and src and dst):
                continue
            try:
                overrides[(book, date.fromisoformat(src))] = date.fromisoformat(dst)
            except ValueError:
                continue
    return overrides


def apply_date_overrides(marks: list[dict], title: str, overrides: dict[tuple[str, date], date]) -> None:
    for mark in marks:
        target = overrides.get((title, mark["time"].date()))
        if target:
            mark["time"] = datetime.combine(target, mark["time"].time())


def ts_to_dt(value: float) -> datetime:
    if value > 1e12:  # 毫秒
        value /= 1000
    return datetime.fromtimestamp(value)


def read_marks(conn: sqlite3.Connection, book_id: str) -> list[dict]:
    rows = conn.execute(
        "select chapterIdx, chapterTitle, markText, markRange, updateTime, colorStyle "
        "from WRBookmark where bookId=? order by updateTime",
        (book_id,),
    ).fetchall()
    marks = []
    for chapter, chapter_title, text, rng, stamp, color in rows:
        marks.append({
            "chapter": int(chapter or 0),
            "chapter_title": chapter_title or "",
            "text": (text or "").strip(),
            "range": rng or "",
            "time": ts_to_dt(stamp or 0),
            "color": int(color or 1),
        })
    return [m for m in marks if m["text"]]


def read_chapters(conn: sqlite3.Connection, book_id: str) -> dict[int, dict]:
    rows = conn.execute(
        "select chapterIdx, title, wordCount from WRChapter where bookId=? order by chapterIdx",
        (book_id,),
    ).fetchall()
    return {
        int(idx): {"title": title or "", "chars": int(chars or 0)}
        for idx, title, chars in rows
    }


def allocate(marks: list[dict], chapters: dict[int, dict], book_words: int) -> dict[str, float]:
    """按「章节字数 × 该章划线占比」把词数分摊到每一天。

    同章的总划线数取自全库（包含之后的划线），所以读到一半的章节
    只会分到已读比例，而不是整章。
    """
    per_chapter = Counter(m["chapter"] for m in marks)
    total_chars = sum(c["chars"] for c in chapters.values())
    if per_chapter and total_chars:
        scale = book_words / total_chars
        chapter_words = {idx: c["chars"] * scale for idx, c in chapters.items()}
    else:
        # 没有章节表时退化为：全书词数按划线比例分
        chapter_words = {idx: book_words * n / len(marks) for idx, n in per_chapter.items()}

    seen: Counter = Counter()
    words_by_day: dict[date, float] = defaultdict(float)
    cum_by_day: dict[date, float] = {}
    for mark in marks:
        chapter = mark["chapter"]
        total = per_chapter[chapter] or 1
        share = chapter_words.get(chapter, 0.0) / total
        seen[chapter] += 1
        day = mark["time"].date()
        words_by_day[day] += share
        cum_by_day[day] = sum(
            (seen[c] / (per_chapter[c] or 1)) * chapter_words.get(c, 0.0)
            for c in seen
        )
    return {"words_by_day": words_by_day, "cum_by_day": cum_by_day}


def sessions_of(marks: list[dict]) -> list[list[dict]]:
    if not marks:
        return []
    groups = [[marks[0]]]
    for prev, cur in zip(marks, marks[1:]):
        if (cur["time"] - prev["time"]).total_seconds() > SESSION_GAP_MIN * 60:
            groups.append([cur])
        else:
            groups[-1].append(cur)
    return groups


def read_log() -> list[dict]:
    if not LOG_CSV.exists():
        return []
    with LOG_CSV.open(encoding="utf-8-sig", newline="") as fh:
        return [{k: (v or "") for k, v in row.items()} for row in csv.DictReader(fh)]


def write_log(rows: list[dict]) -> None:
    rows.sort(key=lambda r: (r["date"], r["book"]))
    with LOG_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="同步微信读书划线数据")
    parser.add_argument("--days", type=int, default=30, help="同步最近 N 天（默认 30）")
    parser.add_argument("--all", action="store_true", help="同步全部")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    parser.add_argument("--db", help="指定数据库路径")
    args = parser.parse_args()

    db = Path(args.db).expanduser() if args.db else find_db()
    tmp = snapshot(db)
    conn = sqlite3.connect(str(tmp / db.name))
    books = load_books()
    overrides = load_date_overrides()

    wx_books = conn.execute(
        "select bookId, title from WRBook "
        "where bookId in (select distinct bookId from WRBookmark)"
    ).fetchall()

    today = date.today()
    since = date.min if args.all else today - timedelta(days=args.days - 1)

    new_rows: list[dict] = []
    marks_out: list[dict] = []
    sessions_out: list[dict] = []
    repeated: Counter = Counter()
    open_by_chapter: dict[tuple[str, str], list[str]] = defaultdict(list)
    unknown: list[str] = []
    total_marks = 0
    total_lookups = 0
    total_words = 0.0

    for book_id, wx_title in wx_books:
        row = match_book(wx_title or "", str(book_id), books)
        marks = read_marks(conn, str(book_id))
        if not marks:
            continue
        if row is None:
            unknown.append(f"{wx_title}  (bookId={book_id}, {len(marks)} 条划线)")
            continue
        title = row["title"]
        apply_date_overrides(marks, title, overrides)
        slot = (row.get("slot") or "main").strip() or "main"
        book_words = int(float(row["words"] or 60000))
        allocated = allocate(marks, read_chapters(conn, str(book_id)), book_words)
        words_by_day = allocated["words_by_day"]
        cum_by_day = allocated["cum_by_day"]

        # 划线只能推出「读过哪些章」，推不出结尾：书读完时用微信读书的
        # finishReading 把最后一天校准到 100%，按比例回填其余各天。
        finished = conn.execute(
            "select finishReading from WRBook where bookId=?", (str(book_id),)
        ).fetchone()
        if finished and finished[0] and cum_by_day:
            last_day = max(cum_by_day)
            last_pct = cum_by_day[last_day] / book_words
            if 0 < last_pct < 1:
                k = min(1.0 / last_pct, 1.6)
                words_by_day = {d: w * k for d, w in words_by_day.items()}
                cum_by_day = {d: c * k for d, c in cum_by_day.items()}

        prev_cum = 0.0
        by_day: dict[date, list[dict]] = defaultdict(list)
        for mark in marks:
            by_day[mark["time"].date()].append(mark)
            if slot != "main":
                # 睡前轨只记账（本数 / 时长 / 进度），不进主线生词表
                continue
            repeated[mark["text"].casefold()] += 1
            total_marks += 1
            if mark["color"] != DEFERRED_COLOR:
                total_lookups += 1
            status = "留" if mark["color"] == DEFERRED_COLOR else "查"
            marks_out.append({
                "datetime": mark["time"].strftime("%Y-%m-%d %H:%M:%S"),
                "book": title,
                "chapter": mark["chapter_title"],
                "text": mark["text"],
                "status": status,
            })
            if status == "留":
                open_by_chapter[(title, mark["chapter_title"])].append(mark["text"])
        for day in sorted(by_day):
            day_marks = by_day[day]
            words = words_by_day[day]
            cum = cum_by_day[day]
            deferred = sum(1 for m in day_marks if m["color"] == DEFERRED_COLOR)
            lookups = len(day_marks) - deferred
            if day < since:
                prev_cum = cum
                continue
            if slot == "main":
                total_words += words
            groups = sessions_of(day_marks)
            minutes = 0
            for group in groups:
                span = (group[-1]["time"] - group[0]["time"]).total_seconds() / 60
                minutes += max(1, min(round(span), 120))
                sessions_out.append({
                    "date": day.isoformat(),
                    "start": group[0]["time"].strftime("%H:%M"),
                    "end": group[-1]["time"].strftime("%H:%M"),
                    "minutes": max(1, min(round(span), 120)),
                    "marks": len(group),
                    "book": title,
                })
            new_rows.append({
                "date": day.isoformat(),
                "book": title,
                "from_pct": f"{prev_cum / book_words * 100:.2f}",
                "to_pct": f"{cum / book_words * 100:.2f}",
                "minutes": str(minutes),
                "lookups": str(lookups),
                "marks": str(deferred),
                "stuck": "",
                "rereads": "",
                "immersion": "",
                "perfectionism": "",
                "highlight": "",
                "source": "weread",
                "slot": slot,
            })
            prev_cum = cum

    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)

    if not new_rows:
        print(f"最近没有可同步的划线（窗口起点 {since}）。")
        return

    existing = read_log()
    manual_keys = {(r["date"], r["book"]) for r in existing if r.get("source") == "manual"}
    new_rows = [r for r in new_rows if (r["date"], r["book"]) not in manual_keys]
    preserved = {
        (r["date"], r["book"]): r
        for r in existing
        if r.get("source") == "weread"
    }
    kept = [r for r in existing if r.get("source") != "weread"]
    for row in new_rows:
        old = preserved.get((row["date"], row["book"]))
        if old:
            for field in ("stuck", "rereads", "immersion", "perfectionism", "highlight"):
                if old.get(field):
                    row[field] = old[field]

    merged = kept + new_rows

    print(f"## 微信读书同步 · {new_rows[0]['date']} → {new_rows[-1]['date']}\n")
    print("| 日期 | 书名 | 槽 | 词数 | 查词 | 未查 | 中断率 | 分钟 | wpm | 时段数 |")
    print("|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for row in new_rows:
        words = (float(row["to_pct"]) - float(row["from_pct"])) / 100 * next(
            int(float(b["words"])) for b in books if b["title"] == row["book"]
        )
        density = int(row["lookups"]) / words * 1000 if words else 0
        wpm = words / int(row["minutes"]) if int(row["minutes"]) else 0
        n_sessions = sum(1 for s in sessions_out if s["date"] == row["date"] and s["book"] == row["book"])
        print(f"| {row['date']} | {row['book']} | {row['slot']} | {words:,.0f} | {row['lookups']} | "
              f"{row['marks']} | {density:.1f} | {row['minutes']} | {wpm:.0f} | {n_sessions} |")

    if total_words:
        unknown_total = total_marks
        print(f"\n**合计** {total_words:,.0f} 词 · 查词 {total_lookups} 次 · 未查 {unknown_total - total_lookups} 个")
        print(f"**中断率** {total_lookups / total_words * 1000:.1f} /千词（每 "
              f"{total_words / max(total_lookups, 1):.0f} 词停一次）· "
              f"**生词率** {unknown_total / total_words * 1000:.1f} /千词")
    if repeated:
        top = [(w, n) for w, n in repeated.most_common(6) if n > 1]
        if top:
            print("**重复划的词**：" + "、".join(f"{w} ×{n}" for w, n in top))

    if unknown:
        print("\n**books.csv 里没有这些书（未计入）**：")
        for item in unknown:
            print(f"- {item}")

    if args.dry_run:
        print("\n(dry-run：没有写入文件)")
        return

    write_log(merged)
    write_csv(MARKS_CSV, marks_out, ["datetime", "book", "chapter", "text", "status"])

    open_total = sum(len(v) for v in open_by_chapter.values())
    lines = [
        "# 未查生词（绿色）",
        "",
        "> 由 `weread_sync.py` 自动生成。查到就改成黄色，下次同步会从这里消失。",
        f"> 当前 {open_total} 个。",
        "",
    ]
    for (book, chapter), words in sorted(open_by_chapter.items()):
        lines += [f"## {book} · {chapter}", ""]
        lines += [f"- {w}" for w in words]
        lines.append("")
    OPEN_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"未查清单：{OPEN_MD.relative_to(ROOT)}（{open_total} 个）")
    write_csv(SESSIONS_CSV, sessions_out, ["date", "start", "end", "minutes", "marks", "book"])
    print(f"\n已写入 {LOG_CSV.relative_to(ROOT)}、{MARKS_CSV.relative_to(ROOT)}、{SESSIONS_CSV.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
