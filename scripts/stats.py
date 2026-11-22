#!/usr/bin/env python3
"""reed —— 阅读数据统计与周报生成。

用法:
    python3 scripts/stats.py              # 最近 7 天
    python3 scripts/stats.py --days 30    # 最近 30 天
    python3 scripts/stats.py --all        # 全程

数据源: books.csv + log/reading-log.csv（唯一真相）。
"""

from __future__ import annotations

import argparse
import csv
import difflib
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOKS_CSV = ROOT / "books.csv"
LOG_CSV = ROOT / "log" / "reading-log.csv"
TARGET_WORDS = 1_780_000
PROJECT_START = date(2026, 11, 23)


@dataclass(frozen=True)
class Phase:
    code: str
    start: date
    end: date
    cap: int  # 每日查词上限
    density: float  # 每千词查词上限
    plan: int  # 阶段计划词数
    name: str


PHASES = (
    Phase("P0", date(2026, 11, 23), date(2026, 11, 30), 999, 99.0, 8_000, "基线周"),
    Phase("P1", date(2026, 12, 1), date(2027, 5, 31), 6, 4.0, 402_000, "去敏期"),
    Phase("P2", date(2027, 6, 1), date(2027, 11, 30), 5, 2.5, 402_000, "建图期"),
    Phase("P3", date(2027, 12, 1), date(2028, 5, 31), 4, 1.5, 440_000, "风格期"),
    Phase("P4", date(2028, 6, 1), date(2028, 11, 30), 2, 1.0, 540_000, "沉浸期"),
)


@dataclass
class Entry:
    day: date
    book: str
    start: float
    end: float
    minutes: int
    lookups: int
    marks: int
    stuck: int
    rereads: int
    immersion: float | None
    perfectionism: bool
    highlight: str
    words: int
    slot: str = "main"


def num(raw: str | None, default: float = 0.0) -> float:
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def phase_for(day: date) -> Phase:
    for phase in PHASES:
        if phase.start <= day <= phase.end:
            return phase
    return PHASES[0] if day < PHASES[0].start else PHASES[-1]


def load_books() -> dict[str, int]:
    books: dict[str, int] = {}
    with BOOKS_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            title = (row.get("title") or "").strip()
            if title:
                books[title] = int(num(row.get("words"), 60_000))
    return books


def resolve(title: str, books: dict[str, int]) -> str | None:
    if title in books:
        return title
    folded = {name.casefold(): name for name in books}
    if title.casefold() in folded:
        return folded[title.casefold()]
    match = difflib.get_close_matches(title, list(books), n=1, cutoff=0.6)
    return match[0] if match else None


def load_entries(books: dict[str, int]) -> list[Entry]:
    if not LOG_CSV.exists():
        return []
    entries: list[Entry] = []
    last_pct: dict[str, float] = {}
    with LOG_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            raw_date = (row.get("date") or "").strip()
            raw_book = (row.get("book") or "").strip()
            if not raw_date or not raw_book:
                continue
            try:
                day = date.fromisoformat(raw_date)
            except ValueError:
                print(f"! 第 {lineno} 行日期无法解析: {raw_date}", file=sys.stderr)
                continue
            book = resolve(raw_book, books)
            if book is None:
                print(f"! 第 {lineno} 行书名不在 books.csv: {raw_book}（已跳过）", file=sys.stderr)
                continue
            start = num(row.get("from_pct"), last_pct.get(book, 0.0))
            end = num(row.get("to_pct"), start)
            if end < start:
                start, end = end, start
            last_pct[book] = end
            words = round((end - start) / 100 * books[book])
            entries.append(
                Entry(
                    day=day,
                    book=book,
                    start=start,
                    end=end,
                    minutes=int(num(row.get("minutes"))),
                    lookups=int(num(row.get("lookups"))),
                    marks=int(num(row.get("marks"))),
                    stuck=int(num(row.get("stuck"))),
                    rereads=int(num(row.get("rereads"))),
                    immersion=(num(row["immersion"]) if (row.get("immersion") or "").strip() else None),
                    perfectionism=(row.get("perfectionism") or "").strip().upper().startswith("Y"),
                    highlight=(row.get("highlight") or "").strip(),
                    words=words,
                    slot=(row.get("slot") or "main").strip() or "main",
                )
            )
    entries.sort(key=lambda e: e.day)
    return entries


def streak(days: set[date], until: date) -> int:
    cursor = until if until in days else until - timedelta(days=1)
    count = 0
    while cursor in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def fmt_int(value: float) -> str:
    return f"{int(round(value)):,}"


def report(entries: list[Entry], since: date, until: date) -> str:
    window = [e for e in entries if since <= e.day <= until]
    bed_all = [e for e in entries if e.slot != "main"]
    bed_window = [e for e in window if e.slot != "main"]
    entries = [e for e in entries if e.slot == "main"]
    window = [e for e in window if e.slot == "main"]
    all_words = sum(e.words for e in entries)
    if not entries and not bed_all:
        return (
            "## 还没有任何记录\n\n"
            "先在 `log/reading-log.csv` 里加一行（或做完 W0 基线）：\n\n"
            "```\n"
            "date,book,from_pct,to_pct,minutes,lookups,marks,stuck,rereads,immersion,perfectionism,highlight\n"
            "2026-11-23,And Then There Were None,0,8,31,9,14,2,1,3,Y,法官在留声机旁停了两秒\n"
            "```\n"
        )

    words = sum(e.words for e in window)
    minutes = sum(e.minutes for e in window)
    lookups = sum(e.lookups for e in window)
    marks = sum(e.marks for e in window)
    unknown = lookups + marks
    days = {e.day for e in window}
    density = lookups / words * 1000 if words else 0.0
    unknown_density = unknown / words * 1000 if words else 0.0
    wpm = words / minutes if minutes else 0.0
    immersions = [e.immersion for e in window if e.immersion is not None]
    immersion = sum(immersions) / len(immersions) if immersions else 0.0

    all_days = {e.day for e in entries}
    all_lookups = sum(e.lookups for e in entries)
    all_minutes = sum(e.minutes for e in entries)
    all_density = all_lookups / all_words * 1000 if all_words else 0.0
    all_unknown = all_lookups + sum(e.marks for e in entries)
    all_unknown_density = all_unknown / all_words * 1000 if all_words else 0.0
    all_wpm = all_words / all_minutes if all_minutes else 0.0
    all_imm = [e.immersion for e in entries if e.immersion is not None]
    all_immersion = sum(all_imm) / len(all_imm) if all_imm else 0.0

    per_day: dict[date, int] = {}
    for e in window:
        per_day[e.day] = per_day.get(e.day, 0) + e.lookups
    over = [d for d, n in per_day.items() if n > phase_for(d).cap]
    perfect_days = {e.day for e in window if e.perfectionism}
    phase = phase_for(until)
    wpm_text = f"{wpm:.0f} wpm" if minutes else "—"
    all_wpm_text = f"{all_wpm:.0f} wpm" if all_minutes else "—"
    imm_text = f"{immersion:.1f}" if immersions else "—"
    all_imm_text = f"{all_immersion:.1f}" if all_imm else "—"

    lines = [
        f"## 阅读报告 · {since.isoformat()} → {until.isoformat()}（{phase.code} {phase.name}）",
        "",
        "| 指标 | 本期 | 累计 |",
        "|---|---:|---:|",
        f"| 阅读天数 | {len(days)} | {len(all_days)} |",
        f"| 词数 | {fmt_int(words)} | {fmt_int(all_words)} |",
        f"| 有效时长 | {fmt_int(minutes)} min | {fmt_int(all_minutes)} min |",
        f"| 速度 | {wpm_text} | {all_wpm_text} |",
        f"| 查词 / 未查 | {lookups} / {marks} | {all_lookups} / {sum(e.marks for e in entries)} |",
        f"| 中断率 | {density:.1f} /千词 | {all_density:.1f} /千词 |",
        f"| 生词率 | {unknown_density:.1f} /千词 | {all_unknown_density:.1f} /千词 |",
        f"| 超配额天数 | {len(over)} | — |",
        f"| 沉浸度均值 | {imm_text} | {all_imm_text} |",
        f"| 完美主义发作 | {len(perfect_days)} 天 | — |",
        f"| 连续天数 | {streak(all_days, until)} | — |",
        "",
    ]

    elapsed_phase = (until - phase.start).days + 1
    phase_days = (phase.end - phase.start).days + 1
    phase_words = sum(e.words for e in entries if phase.start <= e.day <= phase.end)
    phase_pct = phase_words / phase.plan * 100 if phase.plan else 0.0
    lines += [
        f"**阶段** {phase.code} {phase.name} · 第 {max(1, min(elapsed_phase, phase_days))}/{phase_days} 天 · "
        f"{fmt_int(phase_words)} / {fmt_int(phase.plan)} 词（{phase_pct:.0f}%）",
        f"**总进度** {fmt_int(all_words)} / {fmt_int(TARGET_WORDS)} 词（{all_words / TARGET_WORDS * 100:.1f}%）",
    ]

    span = max(1, (until - PROJECT_START).days + 1)
    rate = all_words / span
    if rate > 0 and all_words < TARGET_WORDS and span >= 14:
        eta = until + timedelta(days=round((TARGET_WORDS - all_words) / rate))
        lines.append(f"**预测** 按当前均速（{fmt_int(rate)} 词/天），约 {eta.strftime('%Y-%m')} 读完 1.78M 词")
    elif all_words < TARGET_WORDS:
        lines.append("**预测** 数据不足 14 天，暂不外推。")

    verdicts: list[str] = []
    if lookups == 0 and marks:
        verdicts.append(
            f"零中断：{marks} 个生词只标未查，生词率 {unknown_density:.1f}/千词"
            f"（覆盖率 {100 - unknown_density / 10:.1f}%）。"
        )
    elif over:
        verdicts.append(
            f"{len(over)} 天超配额（上限 {phase.cap} 个/天）：明天零查词日，只标记不查。"
        )
    elif words and density > phase.density:
        verdicts.append(
            f"密度 {density:.1f} 超过阶段目标 {phase.density:.1f}：下次把配额压到 {phase.cap - 2} 个，"
            "超出的只标记不查。"
        )
    elif lookups and density < phase.density * 0.6 and phase.density < 50:
        verdicts.append(
            f"密度 {density:.1f} 远低于目标 {phase.density:.1f}：可能在忍，也可能在漂。"
            "抽一页自测：能不能说出谁干了什么？"
        )
    if wpm and wpm < 90:
        verdicts.append(f"速度 {wpm:.0f} wpm：先别追速度，查一下回读次数是不是偏高。")
    if len(perfect_days) >= 3:
        if over:
            verdicts.append(
                f"本期 {len(perfect_days)} 天完美主义发作：配额只是症状。明天加一条——想查时先数 10 秒。"
            )
        else:
            verdicts.append(f"本期 {len(perfect_days)} 天完美主义发作：触发黄牌，明天零查词日。")
    if streak(all_days, until) >= 7:
        verdicts.append(f"连续 {streak(all_days, until)} 天：这是本周唯一值得夸的数。")
    if not verdicts:
        verdicts.append("数据正常。不调整，继续读。")

    lines += ["", "**教练判读**", ""] + [f"- {v}" for v in verdicts[:3]]
    if bed_all:
        bbooks = len({e.book for e in bed_window})
        bwords = sum(e.words for e in bed_window)
        bmins = sum(e.minutes for e in bed_window)
        extra = f" · {bwords / bmins:.0f} wpm" if bmins else ""
        lines += ["", f"**睡前** {bbooks} 本 · {fmt_int(bwords)} 词{extra}（不计入主线指标）"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="reed 阅读统计")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=7, help="统计最近 N 天（默认 7）")
    group.add_argument("--all", action="store_true", help="统计全部记录")
    args = parser.parse_args()

    books = load_books()
    entries = load_entries(books)
    until = max([date.today()] + [e.day for e in entries])
    since = PROJECT_START if args.all else until - timedelta(days=args.days - 1)
    if args.all and entries:
        since = min(e.day for e in entries)
    print(report(entries, since, until))


if __name__ == "__main__":
    main()
