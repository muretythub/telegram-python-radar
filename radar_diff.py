#!/usr/bin/env python3
"""Fetch Telegram primary sources, diff them against a snapshot, suppress repeats.

Stdout is JSON. Logs go to stderr. Does not send a message unless `run`
is used and TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set.
Does not mark findings seen until `accept` (or a successful send inside `run`).
A source that fails to load is an error, not "nothing changed".
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

TOPIC = "Что делать Python (telegram) разработчику уже сейчас?"

CHANGELOG_URL = "https://core.telegram.org/bots/api-changelog"
WEBAPPS_URL = "https://core.telegram.org/bots/webapps"
BOT_API_URL = "https://core.telegram.org/bots/api"
BOTNEWS_URL = "https://t.me/s/BotNews"

CLIENT_FEEDS = (
    (
        "tdesktop",
        "https://github.com/telegramdesktop/tdesktop/commits/dev.atom",
        "https://github.com/telegramdesktop/tdesktop/commit/",
    ),
    (
        "android",
        "https://github.com/DrKLO/Telegram/commits/master.atom",
        "https://github.com/DrKLO/Telegram/commit/",
    ),
    (
        "ios",
        "https://github.com/TelegramMessenger/Telegram-iOS/commits/master.atom",
        "https://github.com/TelegramMessenger/Telegram-iOS/commit/",
    ),
    (
        "tdlib",
        "https://github.com/tdlib/td/commits/master.atom",
        "https://github.com/tdlib/td/commit/",
    ),
)

REQUIRED_KEYS = ("bot-api-changelog", "mini-apps", "botnews")
SIGNAL_RE = re.compile(
    r"web[ _-]?app|webview|web[ _-]?view|mini[ _-]?app|bot[ _-]?api|bots/api|attach[ _-]?menu",
    re.I,
)
DATE_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})$"
)
MONTHS = {
    name: index
    for index, name in enumerate(
        "January February March April May June July August September October November December".split(),
        1,
    )
}
VERSION_RE = re.compile(r"Bot API (\d+\.\d+)")
HEADING_RE = re.compile(r"<h([1-4])([^>]*)>(.*?)</h\1>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
MYTH_RES = (
    (re.compile(r"htmlbubbles", re.I), "htmlbubbles"),
    (re.compile(r"90\s*%\s*(кода|code)", re.I), "90% кода"),
    (re.compile(r"займ\w*\s+рынок|занять\s+рынок|перв\w.{0,40}рын|рын\w.{0,40}перв", re.I), "занять рынок"),
)
GUIDELINE_OK = ("рабочий ориентир", "не требование", "не установлен")
TEXT_CAP = 5000
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

STATUS_RU = {
    "official": "официально",
    "code_experiment": "эксперимент в коде",
    "unconfirmed": "неподтверждённая публикация",
}

LLM_SYSTEM = """Ты классификатор находки для Python-разработчика Telegram.
Используй ТОЛЬКО JSON с материалами во входном сообщении.
Если факта нет в материалах, напиши «не найдено в источниках».
Не добавляй новости, версии API, методы и параметры из памяти.
Находка со статусом code_experiment — сигнал в коде клиента, не доказательство публичного API.
Не утверждай перенос 90% кода, параметры HTMLBubbles, что нужно занять рынок первыми.
320 px, 200 kB gzip и TTI < 1 с — не требования Telegram; если упоминаешь, подпиши «рабочий ориентир, не требование».
Каждое утверждение сопровождай URL из материалов.
Пиши по-русски строго в шаблоне уведомления, который дан во входном сообщении.
"""


def log(message: str) -> None:
    print(message, file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def empty_state() -> dict:
    return {
        "topic": TOPIC,
        "request_saved": True,
        "automation": None,
        "destination": None,
        "notes": "Пока автоматизация не запущена — сохранены только тема и запрос.",
        "meta": {
            "banner_version": None,
            "changelog_version": None,
            "divergence": False,
            "last_digest_at": None,
            "last_checked_at": None,
        },
        "seen": {},
    }


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return empty_state()
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    base = empty_state()
    base.update({key: data.get(key, base[key]) for key in base})
    base["meta"].update(data.get("meta") or {})
    base["seen"] = data.get("seen") or {}
    return base


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def fetch_url(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "telegram-python-now/1.0"})
    with urllib.request.urlopen(request, timeout=40) as response:
        raw = response.read(3_000_000)
    return raw.decode("utf-8", "replace")


def normalize(text: str) -> str:
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_html(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</(p|li|div|h[1-6]|tr)>", "\n", fragment, flags=re.I)
    fragment = TAG_RE.sub("", fragment)
    return normalize(fragment)


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def cap_text(text: str) -> tuple[str, bool]:
    if len(text) <= TEXT_CAP:
        return text, False
    return text[:TEXT_CAP].rstrip() + "\n…", True


def parse_date_title(title: str) -> str | None:
    match = DATE_RE.match(title.strip())
    if not match:
        return None
    month, day, year = match.group(1), int(match.group(2)), int(match.group(3))
    return f"{year:04d}-{MONTHS[month]:02d}-{day:02d}"


def content_region(page_html: str) -> str:
    marker = page_html.find('id="dev_page_content"')
    if marker == -1:
        raise ValueError("dev_page_content not found")
    start = page_html.find(">", marker)
    region = page_html[start + 1 :]
    cut = len(region)
    for marker_name in ("tl_footer_wrap", "dev_page_footer", "<footer", 'class="footer"'):
        position = region.find(marker_name)
        if position != -1:
            cut = min(cut, position)
    return region[:cut]


def headings(region: str) -> list[dict]:
    found = []
    for match in HEADING_RE.finditer(region):
        raw_title = strip_html(match.group(3))
        anchor_match = re.search(r'name="([^"]+)"', match.group(2) + match.group(3))
        found.append(
            {
                "level": int(match.group(1)),
                "title": raw_title,
                "anchor": anchor_match.group(1) if anchor_match else "",
                "body_start": match.end(),
            }
        )
    return found


def section_body(region: str, heads: list[dict], index: int) -> str:
    start = heads[index]["body_start"]
    end = heads[index + 1]["body_start"] if index + 1 < len(heads) else len(region)
    # Stop at the next heading tag, which begins before body_start of the next head.
    if index + 1 < len(heads):
        next_heading = region.rfind("<h", 0, heads[index + 1]["body_start"])
        if next_heading != -1 and next_heading > start:
            end = next_heading
    return strip_html(region[start:end])


def make_block(
    *,
    source_key: str,
    block_id: str,
    title: str,
    url: str,
    source_date: str | None,
    text: str,
    status: str,
    versions: list[str] | None = None,
) -> dict:
    full = normalize(text)
    shown, truncated = cap_text(full)
    found_versions = versions if versions is not None else VERSION_RE.findall(full)
    return {
        "id": f"{source_key}:{block_id}",
        "source_key": source_key,
        "title": title,
        "url": url,
        "date": source_date,
        "text": shown,
        "text_hash": text_hash(full),
        "truncated": truncated,
        "status": status,
        "versions": list(dict.fromkeys(found_versions)),
    }


def dated_blocks(page_html: str, source_key: str, page_url: str, stop_title: str | None) -> list[dict]:
    region = content_region(page_html)
    heads = headings(region)
    blocks = []
    for index, head in enumerate(heads):
        if stop_title and head["level"] <= 3 and head["title"] == stop_title:
            break
        if head["level"] != 4:
            continue
        source_date = parse_date_title(head["title"])
        if not source_date:
            continue
        anchor = head["anchor"] or source_date
        body = section_body(region, heads, index)
        blocks.append(
            make_block(
                source_key=source_key,
                block_id=anchor,
                title=head["title"],
                url=f"{page_url}#{anchor}",
                source_date=source_date,
                text=body,
                status="official",
            )
        )
    return blocks


def named_section(page_html: str, source_key: str, page_url: str, title: str) -> dict | None:
    region = content_region(page_html)
    heads = headings(region)
    for index, head in enumerate(heads):
        if head["title"] != title:
            continue
        anchor = head["anchor"] or title.lower().replace(" ", "-")
        return make_block(
            source_key=source_key,
            block_id=anchor or "section",
            title=title,
            url=f"{page_url}#{anchor}",
            source_date=None,
            text=section_body(region, heads, index),
            status="official",
        )
    return None


def newest_version(blocks: list[dict]) -> str | None:
    dated = [block for block in blocks if block.get("date")]
    dated.sort(key=lambda block: block["date"], reverse=True)
    for block in dated:
        if block["versions"]:
            return block["versions"][0]
    return None


def banner_version(page_html: str) -> str | None:
    region = content_region(page_html)
    match = VERSION_RE.search(strip_html(region[:4000]))
    return match.group(1) if match else None


def botnews_blocks(page_html: str) -> tuple[list[dict], list[str]]:
    chunks = re.split(r'data-post="(BotNews/\d+)"', page_html)
    blocks = []
    warnings = []
    # split keeps the delimiter: [pre, id, body, id, body...]
    ids = []
    for index in range(1, len(chunks), 2):
        post = chunks[index]
        body = chunks[index + 1] if index + 1 < len(chunks) else ""
        ids.append(int(post.split("/")[1]))
        text_match = re.search(
            r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            body,
            re.S,
        )
        time_match = re.search(r'<time[^>]+datetime="([^"]+)"', body)
        if not text_match or not time_match:
            warnings.append(f"botnews post {post} has no text or date")
            continue
        source_date = time_match.group(1)[:10]
        post_id = post.split("/")[1]
        blocks.append(
            make_block(
                source_key="botnews",
                block_id=post_id,
                title=f"@BotNews/{post_id}",
                url=f"https://t.me/BotNews/{post_id}",
                source_date=source_date,
                text=strip_html(text_match.group(1)),
                status="official",
            )
        )
    gaps = []
    if ids:
        ids_sorted = sorted(set(ids))
        for left, right in zip(ids_sorted, ids_sorted[1:]):
            if right - left > 1:
                missing = ",".join(str(number) for number in range(left + 1, right))
                gaps.append(
                    f"В публичной ленте @BotNews дыра между {left} и {right}: нет постов {missing}. Не выдумывай их содержимое."
                )
    return blocks, warnings + gaps


def atom_blocks(feed_xml: str, source_key: str, commit_prefix: str) -> list[dict]:
    root = ElementTree.fromstring(feed_xml)
    blocks = []
    for entry in root.findall("a:entry", ATOM_NS):
        entry_id = entry.findtext("a:id", default="", namespaces=ATOM_NS)
        sha = entry_id.rstrip("/").split("/")[-1]
        title = normalize(entry.findtext("a:title", default="", namespaces=ATOM_NS))
        content = strip_html(entry.findtext("a:content", default="", namespaces=ATOM_NS))
        updated = entry.findtext("a:updated", default="", namespaces=ATOM_NS)[:10] or None
        blob = f"{title}\n{content}"
        if not SIGNAL_RE.search(blob):
            continue
        blocks.append(
            make_block(
                source_key=source_key,
                block_id=sha,
                title=title or sha[:12],
                url=commit_prefix + sha,
                source_date=updated,
                text=blob,
                status="code_experiment",
                versions=[],
            )
        )
    return blocks


def collect_from_pages(
    *,
    changelog_html: str,
    webapps_html: str,
    bot_api_html: str,
    botnews_html: str,
    client_feeds: list[tuple[str, str, str]] | None = None,
) -> dict:
    errors = []
    warnings = []
    blocks: list[dict] = []
    try:
        changelog = dated_blocks(changelog_html, "bot-api-changelog", CHANGELOG_URL, None)
        blocks.extend(changelog)
    except (ValueError, re.error) as exc:
        changelog = []
        errors.append(f"bot-api-changelog: {exc}")
    try:
        blocks.extend(dated_blocks(webapps_html, "mini-apps", WEBAPPS_URL, "Designing Mini Apps"))
        design = named_section(webapps_html, "mini-apps", WEBAPPS_URL, "Design Guidelines")
        if design:
            blocks.append(design)
        else:
            warnings.append("На странице Mini Apps нет раздела Design Guidelines — не утверждай, что требования к вёрстке не менялись.")
    except (ValueError, re.error) as exc:
        errors.append(f"mini-apps: {exc}")
    try:
        news, news_warnings = botnews_blocks(botnews_html)
        blocks.extend(news)
        warnings.extend(news_warnings)
    except re.error as exc:
        errors.append(f"botnews: {exc}")
    banner = None
    changelog_version = newest_version([block for block in blocks if block["source_key"] == "bot-api-changelog"])
    try:
        banner = banner_version(bot_api_html)
    except ValueError as exc:
        errors.append(f"bot-api: {exc}")
    if client_feeds:
        for source_key, xml_text, prefix in client_feeds:
            try:
                blocks.extend(atom_blocks(xml_text, source_key, prefix))
            except ElementTree.ParseError as exc:
                warnings.append(f"{source_key}: не разобран atom ({exc}). Клиенты не проверены, это не «тишина».")
    return {
        "blocks": blocks,
        "errors": errors,
        "warnings": warnings,
        "banner_version": banner,
        "changelog_version": changelog_version,
    }


def fetch_all_live() -> dict:
    """Network fetch. Required doc failures stay errors and block a quiet report."""
    errors = []
    pages = {}
    for key, url in {
        "changelog": CHANGELOG_URL,
        "webapps": WEBAPPS_URL,
        "bot_api": BOT_API_URL,
        "botnews": BOTNEWS_URL,
    }.items():
        try:
            pages[key] = fetch_url(url)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{key}: {exc}")
            pages[key] = ""
    client_feeds = []
    for source_key, url, prefix in CLIENT_FEEDS:
        try:
            client_feeds.append((source_key, fetch_url(url), prefix))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{source_key}: {exc}. Репозиторий не проверен, это не отсутствие изменений.")
    if any(not pages[name] for name in ("changelog", "webapps", "botnews")):
        return {
            "blocks": [],
            "errors": errors + ["Обязательный источник пуст. Не отправляй «изменений нет»."],
            "warnings": [],
            "banner_version": None,
            "changelog_version": None,
        }
    parsed = collect_from_pages(
        changelog_html=pages["changelog"],
        webapps_html=pages["webapps"],
        bot_api_html=pages["bot_api"] or '<div id="dev_page_content"></div>',
        botnews_html=pages["botnews"],
        client_feeds=client_feeds,
    )
    parsed["errors"] = errors + parsed["errors"]
    return parsed


def seen_record(block: dict, notified: bool) -> dict:
    return {
        "hash": block["text_hash"],
        "title": block["title"],
        "url": block["url"],
        "date": block["date"],
        "status": block["status"],
        "versions": block.get("versions") or [],
        "notified": notified,
    }


def known_versions(state: dict) -> set[str]:
    found = set()
    for record in state["seen"].values():
        if record.get("status") == "official":
            found.update(record.get("versions") or [])
    return found


def divergence_block(banner: str | None, changelog_version: str | None) -> dict | None:
    if not banner or not changelog_version or banner == changelog_version:
        return None
    text = (
        f"Страница {BOT_API_URL} показывает Bot API {banner}, "
        f"а свежий датированный блок {CHANGELOG_URL} показывает Bot API {changelog_version}. "
        "Не выбирай версию по памяти."
    )
    return make_block(
        source_key="bot-api",
        block_id="version-divergence",
        title="Расхождение версии Bot API между справочником и changelog",
        url=BOT_API_URL,
        source_date=None,
        text=text,
        status="official",
        versions=[banner, changelog_version],
    )


def classify_changes(parsed: dict, state: dict) -> dict:
    """Pure diff. Does not write state."""
    if parsed["errors"]:
        return {
            "status": "incomplete",
            "significant": False,
            "changes": [],
            "repeats_suppressed": [],
            "warnings": parsed["warnings"],
            "errors": parsed["errors"],
            "materials": [],
        }
    changes = []
    repeats = []
    versions_already = known_versions(state)
    fresh_versions: set[str] = set()
    for block in parsed["blocks"]:
        if block["source_key"] != "bot-api-changelog":
            continue
        previous = state["seen"].get(block["id"])
        if previous and previous.get("hash") == block["text_hash"]:
            continue
        fresh_versions.update(block["versions"])
    for block in parsed["blocks"]:
        previous = state["seen"].get(block["id"])
        if previous and previous.get("hash") == block["text_hash"]:
            continue
        change_type = "updated" if previous else "added"
        if block["source_key"] == "botnews" and block["versions"]:
            versions = set(block["versions"])
            if versions.issubset(fresh_versions):
                for change in changes:
                    if change["source_key"] == "bot-api-changelog" and versions.intersection(change.get("versions") or []):
                        change.setdefault("extra_urls", []).append(block["url"])
                        break
                repeats.append(
                    {
                        **block,
                        "change_type": "folded",
                        "reason": "пост @BotNews про ту же версию, что и changelog в этом запуске",
                    }
                )
                continue
            if versions.issubset(versions_already):
                repeats.append(
                    {
                        **block,
                        "change_type": "repeat_suppressed",
                        "reason": "пост @BotNews повторяет уже принятую версию changelog",
                    }
                )
                continue
        changes.append({**block, "change_type": change_type})
    extra = divergence_block(parsed["banner_version"], parsed["changelog_version"])
    if extra:
        previous = state["seen"].get(extra["id"])
        if not previous or previous.get("hash") != extra["text_hash"]:
            changes.append({**extra, "change_type": "updated" if previous else "added"})
    elif state["seen"].get("bot-api:version-divergence"):
        resolved = make_block(
            source_key="bot-api",
            block_id="version-divergence-resolved",
            title="Версии Bot API на справочнике и в changelog снова совпали",
            url=CHANGELOG_URL,
            source_date=None,
            text=(
                f"Обе официальные страницы показывают Bot API {parsed['changelog_version']}. "
                "Предыдущее расхождение больше не воспроизводится этим запуском."
            ),
            status="official",
            versions=[parsed["changelog_version"]] if parsed["changelog_version"] else [],
        )
        previous = state["seen"].get(resolved["id"])
        if not previous or previous.get("hash") != resolved["text_hash"]:
            changes.append({**resolved, "change_type": "updated" if previous else "added"})
    return {
        "status": "changes" if changes else "quiet",
        "significant": bool(changes),
        "changes": changes,
        "repeats_suppressed": repeats,
        "warnings": parsed["warnings"],
        "errors": [],
        "banner_version": parsed["banner_version"],
        "changelog_version": parsed["changelog_version"],
    }


def remember(state: dict, blocks: list[dict], notified: bool) -> None:
    for block in blocks:
        state["seen"][block["id"]] = seen_record(block, notified)


def apply_meta(state: dict, parsed: dict) -> None:
    state["meta"]["banner_version"] = parsed.get("banner_version")
    state["meta"]["changelog_version"] = parsed.get("changelog_version")
    state["meta"]["divergence"] = bool(
        parsed.get("banner_version")
        and parsed.get("changelog_version")
        and parsed["banner_version"] != parsed["changelog_version"]
    )
    state["meta"]["last_checked_at"] = now_iso()


def baseline_state(parsed: dict, state: dict | None = None) -> dict:
    state = state or empty_state()
    if parsed["errors"]:
        raise RuntimeError("; ".join(parsed["errors"]))
    remember(state, parsed["blocks"], notified=False)
    extra = divergence_block(parsed["banner_version"], parsed["changelog_version"])
    if extra:
        remember(state, [extra], notified=False)
    apply_meta(state, parsed)
    return state


def digest_materials(parsed: dict, since_days: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).date().isoformat()
    undated = []
    dated = []
    for block in parsed["blocks"]:
        if block["status"] != "official":
            continue
        if not block.get("date"):
            undated.append(block)
        elif block["date"] >= cutoff:
            dated.append(block)
    dated.sort(key=lambda block: (block["date"], block["id"]), reverse=True)
    return undated + dated


def accept_blocks(state: dict, blocks: list[dict], notified: bool) -> dict:
    remember(state, blocks, notified=notified)
    return state


def lint_report(report: str, materials: str) -> list[str]:
    prose_lines = []
    for line in report.splitlines():
        if line.startswith("  "):
            continue
        if "Цитата:" in line:
            line = line.split("Цитата:", 1)[0]
        prose_lines.append(line)
    prose = "\n".join(prose_lines)
    errors = []
    for pattern, label in MYTH_RES:
        if pattern.search(prose) and not pattern.search(materials):
            errors.append(f"формулировка не из материалов и запрещена чек-листом: {label}")
    for paragraph in re.split(r"\n\s*\n", prose):
        lowered = paragraph.lower()
        has_number = bool(
            re.search(r"320\s*px", lowered)
            or re.search(r"200\s*(kb|кб)", lowered)
            or re.search(r"\btti\b", lowered)
        )
        if has_number and not any(marker in lowered for marker in GUIDELINE_OK):
            errors.append(
                "320 px, 200 kB gzip или TTI упомянуты без пометки «рабочий ориентир» / «не требование»"
            )
    if re.search(r"Источник", prose) and "http" not in prose:
        errors.append("в отчёте есть поле источника, но нет URL")
    return errors


def render_change(change: dict) -> str:
    status = STATUS_RU.get(change.get("status"), change.get("status", ""))
    quote = change.get("text") or ""
    if len(quote) > 700:
        quote = quote[:700].rstrip() + "…"
    unknown = "Публичная доступность не следует из этого фрагмента."
    action = "Открой ссылку и делай только то, что буквально названо в источнике. Не дополняй методами из памяти."
    if change.get("status") == "code_experiment":
        unknown = "Это сигнал в коде клиента, не доказательство, что метод уже есть в публичном Bot API или Mini Apps."
        action = "Не выпускай фичу и не обещай её пользователям. Сверься с changelog и @BotNews; пока метода нет там — не реализуй."
    pillar = "не классифицировано автоматически — реши по цитате, не по памяти"
    lines = [
        f"Что изменилось: {change.get('title')}. Цитата: {quote}",
        f"Источник и дата: {change.get('url')} ({change.get('date') or 'дата в источнике не указана'})",
        f"Статус: {status}.",
        f"Какой из пяти пунктов затронут: {pillar}.",
        f"Что сделать разработчику: {action}",
        f"Что пока неизвестно: {unknown}",
    ]
    if change.get("extra_urls"):
        lines.append("Тот же релиз также опубликован: " + ", ".join(change["extra_urls"]))
    if change.get("truncated"):
        lines.append("Что пока неизвестно: цитата обрезана, полный текст только по ссылке.")
    return "\n".join(lines)


def render_digest(materials: list[dict], since_days: int) -> str:
    lines = [
        f"Вопрос: {TOPIC}",
        f"Окно: {since_days} дней. Дата блока — дата источника, не дата «вышло сегодня».",
        "Пять пунктов по цитатам не разложены: без модели скрипт не угадывает, к какому вопросу относится абзац.",
        "1. Что разрабатывать сейчас: не найдено автоматической раскладкой — смотри цитаты.",
        "2. Как адаптировать интерфейс: не найдено автоматической раскладкой — смотри цитаты.",
        "3. Как оптимизировать: не найдено автоматической раскладкой — смотри цитаты.",
        "4. За чем следить: ссылки на материалы ниже.",
        "5. Какие демо готовить: не найдено автоматической раскладкой — не предлагай демо метода, которого нет в цитате.",
        "Материалы:",
    ]
    undated = [block for block in materials if not block.get("date")]
    dated = [block for block in materials if block.get("date")][:8]
    for block in undated + dated:
        quote = (block.get("text") or "").replace("\n", " ")
        if len(quote) > 400:
            quote = quote[:400].rstrip() + "…"
        lines.append(f"- {block.get('date') or 'без даты'} | {block['title']} | {block['url']}")
        lines.append(f"  {quote}")
    lines.append("Что пока неизвестно: какой из пунктов 1–3 и 5 закрывает каждая цитата.")
    return "\n".join(lines)


def build_llm_messages(changes: list[dict], template: str) -> list[dict]:
    payload = {
        "materials": [
            {
                "title": change.get("title"),
                "url": change.get("url"),
                "date": change.get("date"),
                "status": change.get("status"),
                "text": change.get("text"),
            }
            for change in changes
        ]
    }
    user = (
        "Материалы JSON:\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n\nШаблон:\n"
        + template
        + "\nЗакрой каждый ответ ссылкой из материалов. Не используй другие источники."
    )
    return [
        {"role": "system", "content": LLM_SYSTEM},
        {"role": "user", "content": user},
    ]


def classify_with_llm(changes: list[dict], template: str) -> str | None:
    base = os.environ.get("RADAR_LLM_API_BASE", "").rstrip("/")
    key = os.environ.get("RADAR_LLM_API_KEY", "")
    model = os.environ.get("RADAR_LLM_MODEL", "")
    if not (base and key and model):
        return None
    url = base if base.endswith("/chat/completions") else base + "/v1/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "messages": build_llm_messages(changes, template),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps(
        {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": False}
    ).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError("Telegram API rejected the message")


def dump(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def command_diff(state: dict, parsed: dict) -> dict:
    result = classify_changes(parsed, state)
    result["topic"] = state["topic"]
    result["automation"] = state.get("automation")
    result["destination"] = state.get("destination")
    return result


def within_days(date_text: str | None, since_days: int) -> bool:
    if not date_text:
        return True
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).date().isoformat()
    return date_text >= cutoff


def command_digest(parsed: dict, since_days: int) -> dict:
    if parsed["errors"]:
        return {"status": "incomplete", "errors": parsed["errors"], "materials": [], "warnings": parsed["warnings"]}
    materials = [block for block in parsed["blocks"] if block["status"] == "official" and within_days(block.get("date"), since_days)]
    return {
        "status": "digest",
        "since_days": since_days,
        "materials": materials,
        "warnings": parsed["warnings"],
        "errors": [],
        "instruction": "Ответь на пять пунктов только по materials. Даты сохрани. Не помечай старый блок как изменение этой недели.",
    }


def finish_run(state_path: str, state: dict, parsed: dict, mode: str) -> int:
    if parsed["errors"]:
        dump({"status": "incomplete", "errors": parsed["errors"], "warnings": parsed.get("warnings", [])})
        return 5
    if not state["seen"]:
        # Первый запуск (нет файла или в нём пустой seen): только baseline, без отправки.
        # Иначе вся история changelog ушла бы в чат как «новое».
        baseline_state(parsed, state)
        save_state(state_path, state)
        dump(
            {
                "status": "baseline",
                "mode": mode,
                "seen": len(state["seen"]),
                "notified": False,
                "warnings": parsed.get("warnings", []),
                "note": "Первый снимок сохранён без уведомления. Дайджест придёт в следующий понедельник UTC.",
            }
        )
        return 0
    result = classify_changes(parsed, state)
    since_days = int(os.environ.get("RADAR_SINCE_DAYS") or "180")
    messages: list[str] = []
    if mode == "digest":
        materials = digest_materials(parsed, since_days)
        blob = json.dumps(materials, ensure_ascii=False)
        draft = None
        try:
            draft = classify_with_llm(materials, DIGEST_TEMPLATE)
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as exc:
            log(f"llm skipped: {exc}")
        if draft and not lint_report(draft, blob):
            messages.append(draft)
        else:
            if draft:
                log("llm digest rejected by lint")
            messages.append(render_digest(materials, since_days))
    if result["changes"]:
        blob = json.dumps(result["changes"], ensure_ascii=False)
        draft = None
        if mode != "digest":
            try:
                draft = classify_with_llm(result["changes"], NOTIFY_TEMPLATE)
            except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as exc:
                log(f"llm skipped: {exc}")
        if draft and not lint_report(draft, blob):
            messages.append(draft)
        else:
            messages.extend(render_change(change) for change in result["changes"])
    should_send = mode == "digest" or bool(result["changes"])
    token_ready = bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))
    if should_send and messages and token_ready:
        try:
            for message in messages:
                problems = lint_report(message, json.dumps(result["changes"], ensure_ascii=False))
                if problems:
                    raise RuntimeError("; ".join(problems))
                for start in range(0, len(message), 3500):
                    send_telegram(message[start : start + 3500])
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
            dump({"status": "send_failed", "error": str(exc), "changes": result["changes"]})
            return 6
        remember(state, result["repeats_suppressed"], notified=False)
        remember(state, result["changes"], notified=True)
        apply_meta(state, parsed)
        if mode == "digest":
            state["meta"]["last_digest_at"] = now_iso()
        save_state(state_path, state)
        dump({"status": "notified", "sent": len(messages), "change_ids": [item["id"] for item in result["changes"]]})
        return 0
    if result["repeats_suppressed"]:
        remember(state, result["repeats_suppressed"], notified=False)
        save_state(state_path, state)
    dump(
        {
            "status": "pending_delivery" if (result["changes"] or mode == "digest") else "quiet",
            "significant": result["significant"],
            "changes": result["changes"],
            "repeats_suppressed": [item["id"] for item in result["repeats_suppressed"]],
            "warnings": result["warnings"],
            "automation": state.get("automation"),
            "destination": state.get("destination"),
            "digest": render_digest(digest_materials(parsed, since_days), since_days) if mode == "digest" else None,
        }
    )
    return 0


NOTIFY_TEMPLATE = """Что изменилось: …
Источник и дата: …
Статус: официально / эксперимент в коде / неподтверждённая публикация.
Какой из пяти пунктов затронут: …
Что сделать разработчику: …
Что пока неизвестно: …
"""

DIGEST_TEMPLATE = """Вопрос: Что делать Python (telegram) разработчику уже сейчас?
Для каждого из пяти пунктов: ответ только из материалов, источник и дата, статус, что сделать, что неизвестно.
Пункты: что разрабатывать; как адаптировать интерфейс; как оптимизировать; за чем следить; какие демо готовить.
Если пункт материалами не закрыт, напиши «не найдено в источниках». Не называй старую дату изменением этой недели.
"""


def self_test() -> int:
    changelog = """
    <div id="dev_page_content">
      <h4><a name="august-24-2026"></a>August 24, 2026</h4>
      <p><strong>Bot API 10.3</strong></p>
      <ul><li>Added the class ExampleAnchor for tests only.</li></ul>
      <h4><a name="july-14-2026"></a>July 14, 2026</h4>
      <p><strong>Bot API 9.9</strong></p>
      <ul><li>Older test entry.</li></ul>
    </div>
    """
    webapps = """
    <div id="dev_page_content">
      <h3>Recent changes</h3>
      <h4><a name="june-11-2026"></a>June 11, 2026</h4>
      <p>Mini App test note.</p>
      <h3>Designing Mini Apps</h3>
      <h4><a name="design-guidelines"></a>Design Guidelines</h4>
      <p>No pixel minimum is stated in this fixture.</p>
      <h3>Implementing Mini Apps</h3>
    </div>
    """
    bot_api = '<div id="dev_page_content"><p>Recent changes</p><p>Bot API 10.3</p></div>'
    news = """
    <div data-post="BotNews/121">
      <div class="tgme_widget_message_text">Bot API 10.3<br/>same story</div>
      <time datetime="2026-08-24T16:04:08+00:00"></time>
    </div>
    """
    feed = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>tag:github.com,2008:Commit/abc123</id>
        <title>fix webview inset</title>
        <updated>2026-10-01T00:00:00Z</updated>
        <content>touch web_app safe area</content>
      </entry>
      <entry>
        <id>tag:github.com,2008:Commit/def456</id>
        <title>fix memory leak in photos</title>
        <updated>2026-10-02T00:00:00Z</updated>
        <content>unrelated</content>
      </entry>
    </feed>
    """
    parsed = collect_from_pages(
        changelog_html=changelog,
        webapps_html=webapps,
        bot_api_html=bot_api,
        botnews_html=news,
        client_feeds=[("tdesktop", feed, "https://github.com/telegramdesktop/tdesktop/commit/")],
    )
    assert not parsed["errors"], parsed["errors"]
    ids = {block["id"] for block in parsed["blocks"]}
    assert "bot-api-changelog:august-24-2026" in ids
    assert "mini-apps:design-guidelines" in ids
    assert "tdesktop:abc123" in ids
    assert "tdesktop:def456" not in ids, "unrelated client commit must not notify"
    state = baseline_state(parsed)
    second = classify_changes(parsed, state)
    assert second["status"] == "quiet", second
    assert second["significant"] is False

    updated = changelog.replace("ExampleAnchor", "ExampleAnchorEdited")
    parsed2 = collect_from_pages(
        changelog_html=updated,
        webapps_html=webapps,
        bot_api_html=bot_api,
        botnews_html=news,
        client_feeds=[("tdesktop", feed, "https://github.com/telegramdesktop/tdesktop/commit/")],
    )
    diff = classify_changes(parsed2, state)
    assert [item["id"] for item in diff["changes"]] == ["bot-api-changelog:august-24-2026"]
    assert diff["changes"][0]["change_type"] == "updated"
    again = classify_changes(parsed2, state)
    assert again["changes"][0]["id"] == diff["changes"][0]["id"], "without accept the finding must stay new"

    accept_blocks(state, diff["changes"], notified=True)
    quiet = classify_changes(parsed2, state)
    assert quiet["significant"] is False

    late_news = news.replace("BotNews/121", "BotNews/130").replace("same story", "Bot API 10.3 recap only")
    # keep 121 so the public page has no gap, plus a recap post
    both_news = news + late_news.replace('datetime="2026-08-24T16:04:08+00:00"', 'datetime="2026-08-25T16:04:08+00:00"')
    parsed3 = collect_from_pages(
        changelog_html=updated,
        webapps_html=webapps,
        bot_api_html=bot_api,
        botnews_html=both_news,
        client_feeds=[("tdesktop", feed, "https://github.com/telegramdesktop/tdesktop/commit/")],
    )
    # 121 is already seen; 130 only restates 10.3 which is in the accepted changelog block
    state["seen"]["bot-api-changelog:august-24-2026"]["versions"] = ["10.3"]
    recap = classify_changes(parsed3, state)
    assert recap["significant"] is False, recap
    assert any(item["id"] == "botnews:130" for item in recap["repeats_suppressed"])

    folded_log = updated.replace(
        "</div>",
        "<h4><a name=\"september-1-2026\"></a>September 1, 2026</h4>"
        "<p><strong>Bot API 10.4</strong> Added FoldedMethod.</p></div>",
    )
    folded_news = news + """
    <div data-post="BotNews/140">
      <div class="tgme_widget_message_text">Bot API 10.4<br/>same release</div>
      <time datetime="2026-09-01T00:00:00+00:00"></time>
    </div>
    """
    parsed_fold = collect_from_pages(
        changelog_html=folded_log,
        webapps_html=webapps,
        bot_api_html=bot_api,
        botnews_html=folded_news,
        client_feeds=[],
    )
    folded = classify_changes(parsed_fold, state)
    folded_ids = [item["id"] for item in folded["changes"]]
    assert "bot-api-changelog:september-1-2026" in folded_ids
    assert "botnews:140" not in folded_ids
    august = next(item for item in folded["changes"] if item["id"].endswith("september-1-2026"))
    assert "https://t.me/BotNews/140" in august.get("extra_urls", [])

    gap_news = both_news.replace("BotNews/130", "BotNews/133")
    parsed_gap = collect_from_pages(
        changelog_html=updated,
        webapps_html=webapps,
        bot_api_html=bot_api,
        botnews_html=gap_news,
        client_feeds=[],
    )
    assert any("дыра" in warning for warning in parsed_gap["warnings"]), parsed_gap["warnings"]

    diverged = collect_from_pages(
        changelog_html=updated,
        webapps_html=webapps,
        bot_api_html='<div id="dev_page_content"><p>Bot API 10.4</p></div>',
        botnews_html=news,
        client_feeds=[],
    )
    divergence = classify_changes(diverged, state)
    assert any(item["id"] == "bot-api:version-divergence" for item in divergence["changes"])

    bad = "Что изменилось: перенеси 90% кода и займи рынок.\nИсточник и дата: нет\nИнтерфейс 320 px обязателен."
    problems = lint_report(bad, materials="Added the class ExampleAnchor")
    assert any("90% кода" in item for item in problems)
    assert any("занять рынок" in item for item in problems)
    assert any("320 px" in item for item in problems)
    assert any("нет URL" in item for item in problems)
    allowed = "Рабочий ориентир, не требование Telegram: 320 px и TTI. Источник: https://example.test/a"
    assert lint_report(allowed, materials="") == []

    messages = build_llm_messages(
        [{"title": "t", "url": "https://core.telegram.org/bots/api-changelog", "date": "2026-08-24", "status": "official", "text": "Added ExampleAnchor"}],
        NOTIFY_TEMPLATE,
    )
    blob = json.dumps(messages, ensure_ascii=False)
    assert "ExampleAnchor" in blob
    assert "10.3" not in messages[0]["content"]
    assert "RichMessage" not in messages[0]["content"]
    rendered = render_change(
        {
            "title": "fix webview",
            "text": "touch web_app",
            "url": "https://github.com/telegramdesktop/tdesktop/commit/abc123",
            "date": "2026-10-01",
            "status": "code_experiment",
        }
    )
    assert "эксперимент в коде" in rendered
    assert "не доказательство" in rendered
    assert lint_report(rendered, materials=rendered) == []

    incomplete = classify_changes({"blocks": [], "errors": ["changelog down"], "warnings": [], "banner_version": None, "changelog_version": None}, state)
    assert incomplete["status"] == "incomplete"

    import contextlib
    import io
    import tempfile

    sent: list[str] = []
    original_send = globals()["send_telegram"]
    globals()["send_telegram"] = sent.append
    saved_env = {key: os.environ.get(key) for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
    os.environ["TELEGRAM_CHAT_ID"] = "test-chat"
    try:
        for run_mode in ("daily", "digest"):
            for variant in ("no-file", "template-empty-seen"):
                path = os.path.join(tempfile.mkdtemp(), "state.json")
                if variant == "template-empty-seen":
                    save_state(path, empty_state())
                sent.clear()
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = finish_run(path, load_state(path), parsed, run_mode)
                first = json.loads(out.getvalue())
                assert code == 0 and first["status"] == "baseline", (run_mode, variant, first)
                assert not sent, f"first run must not send ({run_mode}, {variant})"
                assert load_state(path)["seen"], "baseline must be saved"
        # следующий тихий день: ничего не шлёт и файл не трогает
        before = open(path, encoding="utf-8").read()
        sent.clear()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = finish_run(path, load_state(path), parsed, "daily")
        assert code == 0 and json.loads(out.getvalue())["status"] == "quiet"
        assert not sent and open(path, encoding="utf-8").read() == before
        # понедельничный дайджест после baseline уходит
        with contextlib.redirect_stdout(io.StringIO()):
            code = finish_run(path, load_state(path), parsed, "digest")
        assert code == 0 and sent, "digest after baseline must be sent"
    finally:
        globals()["send_telegram"] = original_send
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("self-test ok")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diff Telegram developer primary sources")
    parser.add_argument(
        "command",
        choices=("self-test", "baseline", "diff", "digest", "accept", "lint", "run"),
    )
    parser.add_argument("--state", default="state/telegram-radar.json")
    parser.add_argument("--since-days", type=int, default=180)
    parser.add_argument("--changes-file")
    parser.add_argument("--report-file")
    parser.add_argument("--materials-file")
    parser.add_argument("--mode", choices=("auto", "daily", "digest"), default="auto")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv or sys.argv[1:])
    if args.command == "self-test":
        return self_test()
    if args.command == "lint":
        report = open(args.report_file, encoding="utf-8").read()
        materials = open(args.materials_file, encoding="utf-8").read() if args.materials_file else ""
        problems = lint_report(report, materials)
        dump({"ok": not problems, "errors": problems})
        return 1 if problems else 0
    if args.command == "accept":
        state = load_state(args.state)
        payload = json.load(open(args.changes_file, encoding="utf-8"))
        blocks = payload.get("changes") or payload.get("blocks") or []
        accept_blocks(state, blocks, notified=True)
        save_state(args.state, state)
        dump({"status": "accepted", "count": len(blocks)})
        return 0

    parsed = fetch_all_live()
    state = load_state(args.state)
    if args.command == "baseline":
        if parsed["errors"]:
            dump({"status": "incomplete", "errors": parsed["errors"]})
            return 5
        baseline_state(parsed, state)
        save_state(args.state, state)
        dump(
            {
                "status": "baseline",
                "seen": len(state["seen"]),
                "notified": False,
                "automation": state["automation"],
                "destination": state["destination"],
                "warnings": parsed["warnings"],
            }
        )
        return 0
    if args.command == "diff":
        if parsed["errors"]:
            dump({"status": "incomplete", "errors": parsed["errors"], "warnings": parsed["warnings"]})
            return 5
        if not state["seen"]:
            baseline_state(parsed, state)
            save_state(args.state, state)
            dump(
                {
                    "status": "baseline",
                    "significant": False,
                    "seen": len(state["seen"]),
                    "notified": False,
                    "note": "Первый снимок сохранён без уведомления. Для ответа на вопрос запусти digest.",
                }
            )
            return 3
        result = command_diff(state, parsed)
        dump(result)
        return 0 if result["significant"] else 3
    if args.command == "digest":
        if parsed["errors"]:
            dump({"status": "incomplete", "errors": parsed["errors"], "warnings": parsed["warnings"]})
            return 5
        just_baselined = False
        if not state["seen"]:
            baseline_state(parsed, state)
            save_state(args.state, state)
            just_baselined = True
        result = command_digest(parsed, args.since_days)
        result["changes"] = [] if just_baselined else classify_changes(parsed, state).get("changes", [])
        result["baselined"] = just_baselined
        result["note"] = (
            "Снимок создан, история не является дельтой. Ответь по materials."
            if just_baselined
            else "changes — только отличие от снимка. Остальные materials не называй событием этой недели."
        )
        dump(result)
        return 0
    mode = args.mode
    if mode == "auto":
        mode = "digest" if datetime.now(timezone.utc).weekday() == 0 else "daily"
    return finish_run(args.state, state, parsed, "digest" if mode == "digest" else "daily")


if __name__ == "__main__":
    sys.exit(main())
