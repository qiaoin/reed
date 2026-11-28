#!/usr/bin/env python3
"""reed —— 阅读数据统计与周报生成。

用法:
    python3 scripts/stats.py              # 最近 7 天
    python3 scripts/stats.py --days 30    # 最近 30 天
    python3 scripts/stats.py --all        # 全程
    python3 scripts/stats.py --review     # 书末 10 分钟复现词清算

数据源: books.csv + log/reading-log.csv（唯一真相）+ log/weread-marks.csv。
"""

from __future__ import annotations

import argparse
import csv
import difflib
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOKS_CSV = ROOT / "books.csv"
LOG_CSV = ROOT / "log" / "reading-log.csv"
MARKS_CSV = ROOT / "log" / "weread-marks.csv"
PROJECT_START = date(2026, 11, 20)


def _phase_plans() -> dict[str, int]:
    """从 books.csv 现算 P1–P4 的计划词数，避免改书单后目标数字漂移。"""
    plans: dict[str, int] = {}
    if not BOOKS_CSV.exists():
        return plans
    with BOOKS_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("phase") or "").strip().upper()
            if code not in ("P1", "P2", "P3", "P4"):
                continue
            if (row.get("slot") or "main").strip() not in ("", "main"):
                continue  # 睡前 / 其他轨道不占主线词数
            try:
                plans[code] = plans.get(code, 0) + int(float((row.get("words") or "0").strip() or 0))
            except ValueError:
                continue
    return plans


PLANS = _phase_plans()
TARGET_WORDS = sum(PLANS.values()) or 1_601_000


@dataclass(frozen=True)
class Phase:
    code: str
    start: date
    end: date
    lookup_ref: int  # 每日查词参考
    density_ref: float  # 每千词查词参考
    plan: int  # 阶段计划词数
    name: str


PHASES = (
    Phase("P0", date(2026, 11, 20), date(2026, 11, 30), 999, 99.0, 8_000, "基线期"),
    Phase("P1", date(2026, 12, 1), date(2027, 5, 31), 6, 4.0, PLANS.get("P1", 275_000), "去敏期"),
    Phase("P2", date(2027, 6, 1), date(2027, 11, 30), 5, 2.5, PLANS.get("P2", 402_000), "建图期"),
    Phase("P3", date(2027, 12, 1), date(2028, 5, 31), 4, 1.5, PLANS.get("P3", 524_000), "风格期"),
    Phase("P4", date(2028, 6, 1), date(2028, 11, 30), 2, 1.0, PLANS.get("P4", 400_000), "沉浸期"),
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


def _day_stats(entries: list[Entry]) -> dict[date, dict[str, int]]:
    """按天汇总主线记录（同一天可能有多行）。"""
    out: dict[date, dict[str, int]] = {}
    for e in entries:
        row = out.setdefault(e.day, {"words": 0, "minutes": 0, "lookups": 0, "marks": 0})
        row["words"] += e.words
        row["minutes"] += e.minutes
        row["lookups"] += e.lookups
        row["marks"] += e.marks
    return out


def behavioral_flags(entries: list[Entry]) -> dict[date, list[str]]:
    """标记相对近期阅读速度的变化，仅作观察，不推断心理状态。

    单日速度可能受文本难度、疲劳、阅读方式或记录误差影响。
    查词数和阶段参考值单独展示，不在这里触发行为判定。
    """
    stats = _day_stats(entries)
    days = sorted(stats)
    flags: dict[date, list[str]] = {}
    for day in days:
        row = stats[day]
        if row["minutes"] < 20:
            continue
        reasons: list[str] = []
        pool = [
            stats[d]["words"] / stats[d]["minutes"]
            for d in days
            if d < day and stats[d]["minutes"] >= 20 and (day - d).days <= 28
        ]
        if len(pool) >= 3:
            base = sorted(pool)[len(pool) // 2]
            wpm = row["words"] / row["minutes"]
            if base >= 40 and wpm < base * 0.65:
                reasons.append(f"速度 {wpm:.0f} wpm，只有基线 {base:.0f} 的 {wpm / base * 100:.0f}%")
        if reasons:
            flags[day] = reasons
    return flags


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
    over = [d for d, n in per_day.items() if n > phase_for(d).lookup_ref]
    perfect_days = {e.day for e in window if e.perfectionism}
    flag_map = {d: r for d, r in behavioral_flags(entries).items() if since <= d <= until}
    flag_days = set(flag_map)
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
        f"| 查词密度 | {density:.1f} /千词 | {all_density:.1f} /千词 |",
        f"| 标记密度（查词+未查） | {unknown_density:.1f} /千词 | {all_unknown_density:.1f} /千词 |",
        f"| 查词高于参考值天数 | {len(over)} | — |",
        f"| 沉浸度均值 | {imm_text} | {all_imm_text} |",
        f"| 完美主义（自报） | {len(perfect_days)} 天 | {len({e.day for e in entries if e.perfectionism})} 天 |",
        f"| 速度变化提示 | {len(flag_days)} 天 | — |",
        f"| 连续天数 | {streak(all_days, until)} | — |",
        "",
    ]

    elapsed_phase = (until - phase.start).days + 1
    phase_days = (phase.end - phase.start).days + 1
    phase_words = sum(e.words for e in entries if phase.start <= e.day <= phase.end)
    plan_note = (
        "基线期，不作词数规划"
        if phase.code == "P0"
        else f"当前阶段书单合计 {fmt_int(phase.plan)} 词（参考）"
    )
    lines += [
        f"**阶段观察** {phase.code} {phase.name} · 第 {max(1, min(elapsed_phase, phase_days))}/{phase_days} 天 · "
        f"本阶段记录 {fmt_int(phase_words)} 词；{plan_note}",
        f"**累计记录** {fmt_int(all_words)} 词；当前 P1–P4 书单合计 {fmt_int(TARGET_WORDS)} 词（参考，不是配额）",
    ]

    verdicts: list[str] = []
    if lookups == 0 and marks:
        verdicts.append(
            f"本期有 {marks} 个未查标记；标记密度只代表被标记的词，不能估算全文覆盖率。"
        )
    if over:
        verdicts.append(
            f"有 {len(over)} 天查词量高于阶段参考值；只记录趋势，不自动要求调整。"
        )
    elif words and density > phase.density_ref:
        verdicts.append(
            f"查词密度 {density:.1f}/千词，高于阶段参考 {phase.density_ref:.1f}；单看数量不判定阅读状态。"
        )
    if flag_days:
        detail = "；".join(
            f"{d.strftime('%m-%d')} {flag_map[d][0]}" for d in sorted(flag_days)[-2:]
        )
        verdicts.append(f"速度变化提示 {len(flag_days)} 天（{detail}）：原因需要结合文本和当天状态判断。")
    if streak(all_days, until) >= 7:
        verdicts.append(f"连续 {streak(all_days, until)} 天有记录；连续性只作观察。")
    if not verdicts:
        verdicts.append("本期没有额外观察提示。")

    lines += ["", "**教练判读**", ""] + [f"- {v}" for v in verdicts[:3]]
    if bed_all:
        done = {e.book for e in bed_window if e.end >= 95}
        reading = {e.book for e in bed_window} - done
        bwords = sum(e.words for e in bed_window)
        bmins = sum(e.minutes for e in bed_window)
        extra = f" · {bwords / bmins:.0f} wpm" if bmins else ""
        label = f"{len(done)} 本读完"
        if reading:
            label += " + 在读 " + "、".join(sorted(reading))
        lines += ["", f"**睡前** {label} · {fmt_int(bwords)} 词{extra}（不计入主线指标）"]
    return "\n".join(lines) + "\n"


def _candidate_stems(word: str) -> list[str]:
    """生成英文单词的基础词形候选（无外部依赖），用于跨章节/跨书复现归并。"""
    cands = [word]
    n = len(word)
    if n <= 3:
        return cands
    if word.endswith("ies") and n > 4:
        cands.append(word[:-3] + "y")
    elif word.endswith("es") and n > 4 and word[-4:-2] in ("ch", "sh"):
        cands.append(word[:-2])
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        cands.append(word[:-1])

    if word.endswith("ied") and n > 4:
        cands.append(word[:-3] + "y")
    elif word.endswith("ed") and n > 4:
        stem = word[:-2]
        cands.append(stem)
        cands.append(word[:-1])
        if len(stem) >= 4 and stem[-1] == stem[-2]:
            cands.append(stem[:-1])

    if word.endswith("ing") and n > 5:
        stem = word[:-3]
        cands.append(stem)
        cands.append(stem + "e")
        if len(stem) >= 4 and stem[-1] == stem[-2]:
            cands.append(stem[:-1])

    if word.endswith("ily") and n > 5:
        cands.append(word[:-3])
        cands.append(word[:-3] + "y")
    elif word.endswith("ly") and n > 5:
        cands.append(word[:-2])
        if word.endswith("bly"):
            cands.append(word[:-2] + "le")
    return [c for c in dict.fromkeys(cands) if len(c) >= 3]


def review_report(min_count: int = 2, slot: str = "main") -> str:
    """书末 10 分钟复现清算：从 weread-marks.csv 筛出累计复现 >= min_count 次的词。"""
    if not MARKS_CSV.exists():
        return "## 复现生词清算\n\n还没有 `log/weread-marks.csv`，先跑一次 `python3 scripts/weread_sync.py`。\n"

    records: list[dict] = []
    with MARKS_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            row_slot = (row.get("slot") or "main").strip() or "main"
            if slot != "all" and row_slot != slot:
                continue
            raw = (row.get("text") or "").strip()
            cleaned = re.sub(r"^[^a-zA-Z]+|[^a-zA-Z]+$", "", raw).lower()
            cleaned = re.sub(r"\s+", " ", cleaned)
            if not cleaned or len(cleaned.split()) > 3:
                continue
            records.append({
                "norm": cleaned,
                "raw": raw,
                "book": (row.get("book") or "").strip(),
                "chapter": (row.get("chapter") or "").strip(),
                "status": (row.get("status") or "").strip(),
                "datetime": (row.get("datetime") or "").strip(),
            })

    if not records:
        return "## 复现生词清算\n\n暂无有效生词记录。\n"

    # 语料内词形归并：只有当候选词干在语料中实际出现、或被 >=2 个不同词形共享时才合并
    single_words = {r["norm"] for r in records if " " not in r["norm"]}
    stem_owners: dict[str, set[str]] = defaultdict(set)
    for w in single_words:
        for stem in _candidate_stems(w):
            stem_owners[stem].add(w)

    lemma_map: dict[str, str] = {}
    for w in single_words:
        cands = _candidate_stems(w)
        shared = [c for c in cands[1:] if c in single_words or len(stem_owners[c]) >= 2]
        lemma_map[w] = min(shared, key=len) if shared else w

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        key = lemma_map.get(r["norm"], r["norm"])
        groups[key].append(r)

    green_only: list[tuple[str, list[dict]]] = []
    looked_again: list[tuple[str, list[dict]]] = []
    for key, items in groups.items():
        if len(items) < min_count:
            continue
        if any(it["status"] == "查" for it in items):
            looked_again.append((key, items))
        else:
            green_only.append((key, items))

    green_only.sort(key=lambda x: (-len(x[1]), x[0]))
    looked_again.sort(key=lambda x: (-len(x[1]), x[0]))
    discarded = sum(1 for items in groups.values() if len(items) < min_count)

    def _fmt_table(rows: list[tuple[str, list[dict]]]) -> list[str]:
        if not rows:
            return ["（无）", ""]
        out = [
            "| 词条 | 出现形式 | 次数 | 状态 | 位置 |",
            "|---|---|---:|---|---|",
        ]
        for key, items in rows:
            forms = " / ".join(dict.fromkeys(it["norm"] for it in items))
            n_lk = sum(1 for it in items if it["status"] == "查")
            n_mk = len(items) - n_lk
            st = " ".join(p for p in (f"查{n_lk}" if n_lk else "", f"留{n_mk}" if n_mk else "") if p)
            locs = list(dict.fromkeys(
                f"{it['book'][:24]}:{it['chapter'][:12] or '?'}"
                if len({x['book'] for x in items}) > 1
                else (it["chapter"][:22] or it["book"][:22])
                for it in items
            ))
            out.append(f"| **{key}** | {forms} | {len(items)} | {st} | {'；'.join(locs[:3])} |")
        out.append("")
        return out

    slot_label = "全部" if slot == "all" else ("主线" if slot == "main" else slot)
    lines = [
        f"## 书末复现词清算（{slot_label}，阈值 ≥ {min_count} 次）",
        "",
        f"扫描 {len(records)} 条标记（归并后 {len(groups)} 个词条）："
        f"**捞出 {len(green_only) + len(looked_again)} 个复现词**，自动放生 {discarded} 个单次偶遇词。",
        "",
        f"### 1. 复现绿标词（未查但反复遇到，{len(green_only)} 个）",
        "",
        *_fmt_table(green_only),
        f"### 2. 查过又忘 / 反复查（{len(looked_again)} 个）",
        "",
        *_fmt_table(looked_again),
        "> 动作：花 10 分钟集中扫一遍核心义即可，不背诵、不进 Anki，下本书在语境里自然固化。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="reed 阅读统计")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=7, help="统计最近 N 天（默认 7）")
    group.add_argument("--all", action="store_true", help="统计全部记录")
    group.add_argument("--review", action="store_true", help="书末 10 分钟复现词清算")
    parser.add_argument("--min-count", type=int, default=2, help="--review 最小复现次数（默认 2）")
    parser.add_argument("--slot", default="main", help="--review 的轨道：main（默认）/ bedtime / all")
    args = parser.parse_args()

    if args.review:
        print(review_report(args.min_count, args.slot))
        return

    books = load_books()
    entries = load_entries(books)
    until = max([date.today()] + [e.day for e in entries])
    since = PROJECT_START if args.all else until - timedelta(days=args.days - 1)
    if args.all and entries:
        since = min(e.day for e in entries)
    print(report(entries, since, until))


if __name__ == "__main__":
    main()
