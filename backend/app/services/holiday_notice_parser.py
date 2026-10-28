"""Deterministic, offline parsing of annual State Council holiday notices.

Notice text is data, never instructions. Only numbered holiday arrangements are
parsed; recommendations and closing policy prose cannot change the calendar.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

MAX_NOTICE_CHARS = 20_000
FESTIVALS = frozenset({"元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节"})
_TITLE = re.compile(r"(?:国务院办公厅)?关于(\d{4})年部分节假日安排的通知")
_HEADING = re.compile(r"([一二三四五六七八九十]+)[、.](.{1,24}?)[：:]")
_DATE = re.compile(
    r"(?:(?P<year>\d{4})年)?(?:(?P<month>\d{1,2})月)?(?P<day>\d{1,2})日"
    r"(?:\((?P<annotation>[^()]*)\))?"
)
_WEEK = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_ORDINAL = {value: index for index, value in enumerate("一二三四五六七八九十", 1)}
_LUNAR_DATE = r"(?:除夕|[〇零一二三四五六七八九十廿卅正冬腊初年月日\d]+)"
_ANNOTATION = rf"(?:(?:星期|周)[一二三四五六日天]|农历{_LUNAR_DATE})"


@dataclass(frozen=True)
class NoticeDay:
    date: date
    working: bool
    name: str

    def as_dict(self) -> dict:
        return {"date": self.date.isoformat(), "working": self.working, "name": self.name}


@dataclass
class ParsedNotice:
    year: int | None = None
    title: str = ""
    days: list[NoticeDay] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    source_format: str = "notice"

    @property
    def can_save(self) -> bool:
        return self.year is not None and bool(self.days) and not self.errors

    def issue(self, message: str, source: str = "", *, warning: bool = False) -> None:
        target = self.warnings if warning else self.errors
        if len(target) < 30:
            target.append({"message": message, "source": source[:600]})


def _date_tokens(clause: str, year: int) -> list[tuple[date, re.Match]]:
    tokens: list[tuple[date, re.Match]] = []
    current_year, current_month = year, None
    for match in _DATE.finditer(clause):
        current_year = int(match["year"]) if match["year"] else current_year
        current_month = int(match["month"]) if match["month"] else current_month
        if current_month is None:
            raise ValueError("日期缺少月份，不能确定安排")
        try:
            day = date(current_year, current_month, int(match["day"]))
        except ValueError:
            raise ValueError("存在无效的公历日期") from None
        if abs(day.year - year) > 1:
            raise ValueError("日期与通知适用年份相差超过一年")
        annotation = match["annotation"] or ""
        if annotation and not re.fullmatch(
            rf"{_ANNOTATION}(?:[、,]{_ANNOTATION})*",
            annotation,
        ):
            raise ValueError("日期括注含有未支持的内容，请核对原文")
        weekdays = re.findall(r"(?:星期|周)([一二三四五六日天])", annotation)
        if any(day.weekday() != _WEEK[weekday] for weekday in weekdays):
            raise ValueError(f"{day.isoformat()} 与括号中的星期不一致")
        tokens.append((day, match))
    remaining = _DATE.sub("", clause)
    if not tokens or re.search(r"\d+(?:年|月|日|号)", remaining):
        raise ValueError("有日期未被完整识别，请核对原文")
    return tokens


def _clause_days(clause: str, year: int) -> set[date]:
    tokens = _date_tokens(clause, year)
    if clause[: tokens[0][1].start()]:
        raise ValueError("日期前有未支持的限定表述，请核对原文")
    suffix = clause[tokens[-1][1].end() :]
    if not re.fullmatch(
        r"(?:(?:放假(?:调休)?|补休|补假)(?:\d+天)?(?:[,，](?:共\d+天|不调休|与周末连休))*|(?:正常)?上班|补班)",
        suffix,
    ):
        raise ValueError("日期安排含有未支持的表述，请核对原文")
    values: list[date] = []
    index = 0
    while index < len(tokens):
        first, match = tokens[index]
        if index and clause[tokens[index - 1][1].end() : match.start()] not in {
            "、",
            ",",
            "和",
            "及",
        }:
            raise ValueError("多个日期或区间之间的关系未识别，请核对原文")
        if index + 1 < len(tokens):
            last, next_match = tokens[index + 1]
            separator = clause[match.end() : next_match.start()]
            if re.fullmatch(r"(?:至|到|[-—–~])", separator):
                if last < first:
                    raise ValueError("日期范围倒序；跨年范围需明确写出年份")
                if (last - first).days > 31:
                    raise ValueError("单段安排超过 32 天，需要人工核对")
                values.extend(
                    date.fromordinal(day) for day in range(first.toordinal(), last.toordinal() + 1)
                )
                index += 2
                continue
            if separator not in {"、", ",", "和", "及"}:
                raise ValueError("多个日期之间的关系未识别，请核对原文")
        values.append(first)
        index += 1
    result = set(values)
    if len(result) != len(values):
        raise ValueError("同一句中出现重复日期或重叠区间")
    counts = re.findall(r"(?:共|放假|补休|补假)(\d+)天", clause)
    if counts:
        counted = set(result)
        if "与周末连休" in clause:
            for value in list(result):
                for delta in (-1, 1):
                    day = value
                    while 1 <= day.toordinal() + delta <= date.max.toordinal():
                        day += timedelta(days=delta)
                        if day.weekday() < 5:
                            break
                        counted.add(day)
        if any(len(counted) != int(count) for count in counts):
            raise ValueError("日期范围与原文声明的天数不一致")
    return result


def parse_holiday_notice(text: str) -> ParsedNotice:
    result = ParsedNotice()
    if len(text) > MAX_NOTICE_CHARS or len(text.encode("utf-8")) > 64 * 1024:
        result.issue("通知过长，请仅粘贴通知标题和完整正文（最多 20000 字）")
        return result
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    if re.search(r"关于(?:调整\d{4}年|延长|临时).*?假期", normalized):
        result.issue("这是补充调整或临时通知，暂不支持作为年度通知录入，请使用完整年度通知")
        return result
    titles = list(_TITLE.finditer(normalized))
    if not titles:
        result.issue("未找到年度通知标题，请一并粘贴“关于××××年部分节假日安排的通知”")
        return result
    title_years = {int(match[1]) for match in titles}
    if len(title_years) != 1:
        result.issue("发现多个年份的通知，请每次只粘贴一份完整年度通知")
        return result
    result.year = title_years.pop()
    if not 1 <= result.year <= 9999:
        result.issue("通知适用年份无效")
        return result
    result.title = f"国务院办公厅关于{result.year}年部分节假日安排的通知"
    body = normalized[titles[-1].end() :]
    headings = list(_HEADING.finditer(body))
    if not headings:
        result.issue("未找到按节日分项列出的日期安排，请粘贴完整通知正文")
        return result
    known_festivals: set[str] = set()
    output: dict[date, NoticeDay] = {}
    for index, heading in enumerate(headings):
        name = heading[2]
        content = body[
            heading.end() : headings[index + 1].start() if index + 1 < len(headings) else len(body)
        ]
        content = re.split(r"节假日期间|国务院办公厅|责任编辑[:：]|相关阅读", content, maxsplit=1)[
            0
        ]
        names = set(re.split(r"[、,和及与]", name))
        source = f"{heading[1]}、{name}：{content}"
        if _ORDINAL.get(heading[1]) != index + 1:
            result.issue("节日条目的编号缺失、重复或顺序异常，请核对是否完整粘贴", source)
        if not names or not names.issubset(FESTIVALS):
            result.issue("有未支持的节日条目，不能直接保存", source)
            continue
        if known_festivals.intersection(names):
            result.issue("节日条目重复，请确认没有重复粘贴正文", source)
        known_festivals.update(names)
        rest: set[date] = set()
        work: set[date] = set()
        # Older notices can give two dated actions in one comma-separated sentence:
        # "1月1日放假，1月2日（星期一）补休". Do not split date lists or count prose.
        clauses = re.sub(r"((?:放假|补休|补假|上班|补班)(?:\d+天)?)[,，](?=\d)", r"\1;", content)
        for clause in filter(None, re.split(r"[。;！？!?]", clauses)):
            if re.search(r"鼓励|建议|倡导|自行|带薪年休假", clause):
                if re.search(r"\d+(?:月|日)", clause):
                    result.issue("建议性休息未自动计入放假安排", clause, warning=True)
                continue
            has_rest = any(word in clause for word in ("放假", "补休", "补假"))
            has_work = "上班" in clause or "补班" in clause
            if has_rest == has_work:
                if has_rest or re.search(r"\d+(?:年|月|日|号)", clause):
                    result.issue("有日期安排无法明确区分放假或补班", clause)
                continue
            if re.search(r"不放假|不补休|不补假|不上班|另行|待定|取消|调整为", clause):
                result.issue("发现否定、待定或变更表述，需要核对后重新粘贴", clause)
                continue
            try:
                dates = _clause_days(clause, result.year)
            except ValueError as error:
                result.issue(str(error), clause)
                continue
            target = work if has_work else rest
            if target.intersection(dates):
                result.issue("同一节日存在重复安排日期", clause)
            target.update(dates)
        if not rest:
            result.issue(f"{name}缺少可识别的放假日期", source)
        for working, dates in ((False, rest), (True, work)):
            for day in dates:
                if day in output:
                    result.issue(f"{day.isoformat()}存在重复或冲突安排", source)
                output[day] = NoticeDay(day, working, name)
        result.sections.append(
            {
                "name": name,
                "source": source[:2000],
                "restDates": sorted(day.isoformat() for day in rest),
                "workDates": sorted(day.isoformat() for day in work),
            }
        )
    missing = FESTIVALS - known_festivals
    if missing:
        result.issue("缺少节日条目：" + "、".join(sorted(missing)) + "；请粘贴完整年度通知")
    result.days = sorted(output.values(), key=lambda day: day.date)
    if not any(day.date.year == result.year for day in result.days):
        result.issue("未识别出适用年份的日期安排")
    return result
