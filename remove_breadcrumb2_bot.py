#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت لإزالة قالب:ممر تصفح وكل تحويلاته (مثل {{ممر}})
من صفحات النطاق الرئيسي (المقالات)، بما في ذلك الاستدعاءات التي تحتوي
على معلمات، مثل: {{ممر|كونان1|كونان2}}

المصدر: قالب:ممر تصفح
https://ar.wikipedia.org/wiki/قالب:ممر_تصفح

الحجج المدعومة:

-always           لا تطلب تأكيداً قبل الحفظ.
-dry              وضع تجريبي: يعرض التغييرات دون حفظ فعلي.
-limit:N          معالجة أول N صفحة فقط (افتراضي: بلا حد).
-delay:N          التأخير بين التعديلات بالثواني (افتراضي: 5).
-checkinterval:N  التحقق من صفحة التوقف كل N صفحة (افتراضي: 10).

&params;
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pywikibot
from pywikibot.bot import ExistingPageBot, FollowRedirectPageBot, SingleSiteBot
from pywikibot.pagegenerators import GeneratorFactory, parameterHelp

docuReplacements = {"&params;": parameterHelp}  # noqa: N816

# ============================================================
# الثوابت
# ============================================================

# أسماء القوالب الأولية (سيتتبع البوت كل تحويلاتها تلقائياً)
# "ممر تصفح" هو الاسم الرئيسي للقالب
SEED_TEMPLATE_NAMES: list[str] = ['ممر تصفح', 'ممر']

# التأخير الافتراضي بين التعديلات بالثواني
DEFAULT_DELAY_SECONDS: int = 5

# التحقق من صفحة التوقف كل N صفحة
DEFAULT_CHECK_INTERVAL: int = 10

# ملف السجل
LOG_FILE = Path("remove_breadcrumb2.log")

# ============================================================
# إعداد التسجيل
# ============================================================


def setup_logger() -> logging.Logger:
    """يُعدّ مسجّلاً يكتب في ملف وعلى الشاشة معاً."""
    _logger = logging.getLogger("breadcrumb2_bot")
    _logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    _logger.addHandler(file_handler)
    _logger.addHandler(console_handler)
    return _logger


logger = setup_logger()

# ============================================================
# دوال مساعدة مستقلة
# ============================================================


def normalize_name(name: str) -> str:
    """يوحّد اسم القالب للمقارنة."""
    name = name.strip().replace('_', ' ')
    name = re.sub(r'\s+', ' ', name)
    if name:
        name = name[0].upper() + name[1:]
    return name


def resolve_template_group(
    site: pywikibot.Site,
    seed_titles: list[str],
) -> dict[str, pywikibot.Page]:
    """
    يبني قائمة كاملة بكل صفحات القوالب المترابطة عبر التحويلات.
    يتتبع الشبكة في الاتجاهين دون الاعتماد على مكتبات خارجية.
    """
    found_pages: dict[str, pywikibot.Page] = {}
    to_process = list(seed_titles)
    processed_norm: set[str] = set()

    while to_process:
        title = to_process.pop()
        norm = normalize_name(title)
        if norm in processed_norm:
            continue
        processed_norm.add(norm)

        page = pywikibot.Page(site, 'قالب:' + title)
        found_pages[norm] = page

        if not page.exists():
            logger.warning("القالب غير موجود: %s", page.title())
            continue

        # تتبع هدف التحويل إن وُجد
        if page.isRedirectPage():
            try:
                target = page.getRedirectTarget()
                target_title = target.title(with_ns=False)
                if normalize_name(target_title) not in processed_norm:
                    to_process.append(target_title)
            except Exception:
                logger.exception("تعذّر تتبع هدف تحويل: %s", page.title())

        # جمع كل التحويلات التي تشير لهذه الصفحة
        for redirect in page.backlinks(filter_redirects=True, namespaces=[10]):
            rtitle = redirect.title(with_ns=False)
            if normalize_name(rtitle) not in processed_norm:
                to_process.append(rtitle)

    return found_pages


def remove_templates(
    text: str,
    names_normalized: set[str],
) -> tuple[str, bool]:
    """
    يمسح كل استدعاءات {{...}} التي يطابق اسمها أحد الأسماء المعطاة،
    مع دعم المعلمات عبر مطابقة أقواس متوازنة.
    إذا كان الاستدعاء يشغل سطراً كاملاً يُحذف السطر بأكمله.
    يعيد (النص الجديد، هل حدث تغيير).
    """
    result: list[str] = []
    i = 0
    n = len(text)
    changed = False

    while i < n:
        if text[i:i + 2] == '{{':
            # تتبع الأقواس المتوازنة
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if text[j:j + 2] == '{{':
                    depth += 1
                    j += 2
                elif text[j:j + 2] == '}}':
                    depth -= 1
                    j += 2
                else:
                    j += 1

            block = text[i:j]
            inner = text[i + 2:max(j - 2, i + 2)]
            name_part = inner.split('|', 1)[0].split('\n', 1)[0].strip()
            norm = normalize_name(name_part)

            if norm in names_normalized:
                changed = True

                # هل يشغل الاستدعاء سطره بالكامل؟
                line_start = text.rfind('\n', 0, i) + 1
                prefix = text[line_start:i]
                only_ws_prefix = (prefix.strip(' \t') == '')

                line_end = text.find('\n', j)
                if line_end == -1:
                    suffix = text[j:]
                    line_end_pos = n
                else:
                    suffix = text[j:line_end]
                    line_end_pos = line_end + 1

                only_ws_suffix = (suffix.strip(' \t') == '')

                if only_ws_prefix and only_ws_suffix:
                    # احذف السطر كاملاً
                    for _ in range(len(prefix)):
                        if result:
                            result.pop()
                    i = line_end_pos
                else:
                    # احذف الاستدعاء فقط
                    i = j
            else:
                result.append(block)
                i = j
        else:
            result.append(text[i])
            i += 1

    return ''.join(result), changed


# ============================================================
# البوت الرئيسي
# ============================================================


class RemoveBreadcrumb2Bot(SingleSiteBot, FollowRedirectPageBot, ExistingPageBot):
    """بوت لإزالة قالب ممر تصفح وكل تحويلاته من المقالات."""

    update_options = {
        "always": False,
        "dry": False,
        "limit": None,
        "delay": DEFAULT_DELAY_SECONDS,
        "checkinterval": DEFAULT_CHECK_INTERVAL,
    }

    def __init__(
        self,
        template_group: dict[str, pywikibot.Page] | None = None,
        **kwargs: Any,
    ) -> None:
        """تهيئة البوت وجلب شبكة القوالب (أو استخدام واحدة جاهزة إن أُعطيت)."""
        super().__init__(**kwargs)

        # بناء شبكة القوالب وتحويلاتها، أو إعادة استخدام واحدة محسوبة مسبقًا
        self._template_group = template_group or resolve_template_group(
            self.site, SEED_TEMPLATE_NAMES
        )
        self._all_names: set[str] = set(self._template_group.keys())

        display_names = [
            p.title(with_ns=False)
            for p in self._template_group.values()
            if p.exists()
        ]
        logger.info("الأسماء المستهدفة: %s", "، ".join(sorted(display_names)))

        # عدّادات الإحصائيات
        self._count_changed = 0
        self._count_skipped = 0
        self._count_errors = 0
        self._count_processed = 0
        self._pages_since_check = 0
        self._start_time = datetime.now(tz=timezone.utc)

    # ── دورة الحياة ─────────────────────────────────────────

    def teardown(self) -> None:
        """طباعة الإحصائيات النهائية عند انتهاء العمل."""
        elapsed = datetime.now(tz=timezone.utc) - self._start_time
        summary = (
            f"\n{'=' * 50}\n"
            f"انتهى العمل — المدة: {elapsed}\n"
            f"  مُعالَجة  : {self._count_processed}\n"
            f"  مُعدَّلة  : {self._count_changed}\n"
            f"  مُتخطاة   : {self._count_skipped}\n"
            f"  أخطاء     : {self._count_errors}\n"
            f"{'=' * 50}"
        )
        pywikibot.output(summary)
        logger.info(summary)

    # ── التصفية ─────────────────────────────────────────────

    def skip_page(self, page: pywikibot.Page) -> bool:
        """تخطي الصفحات خارج النطاق الرئيسي وصفحات التحويل."""
        if page.namespace().id != 0:
            logger.debug("تخطي (خارج النطاق الرئيسي): %s", page.title())
            self._count_skipped += 1
            return True

        if page.isRedirectPage():
            logger.debug("تخطي (صفحة تحويل): %s", page.title())
            self._count_skipped += 1
            return True

        # التحقق من حد الصفحات — التوقف الفوري دون معالجة صفحة زائدة
        if self.opt.limit is not None and self._count_processed >= self.opt.limit:
            logger.info("بلغ الحد المحدد (%d صفحة)، إيقاف.", self.opt.limit)
            self.quit()
            return True

        return super().skip_page(page)

    # ── صفحة التوقف ─────────────────────────────────────────

    def check_disabled(self) -> bool:
        """
        يتحقق من صفحة التوقف كل N صفحة.
        يعيد True إذا وجب إيقاف المعالجة فورًا (صفحة التوقف مفعّلة).
        """
        self._pages_since_check += 1
        if self._pages_since_check < self.opt.checkinterval:
            return False
        self._pages_since_check = 0

        # إعادة تسجيل الدخول عند انتهاء الجلسة فقط
        if not self.site.logged_in():
            logger.warning("انتهت الجلسة، إعادة تسجيل الدخول...")
            self.site.login()

        shutoff_page = pywikibot.Page(
            self.site,
            f"مستخدم:{self.site.user()}/shutoff/{self.__class__.__name__}.json",
        )
        if shutoff_page.exists():
            content = shutoff_page.get(force=True).strip()
            if content:
                msg = f"البوت موقوف:\n{content}"
                logger.error(msg)
                pywikibot.error(msg)
                self.quit()
                return True

        return False

    # ── معالجة الصفحة ────────────────────────────────────────

    def treat_page(self) -> None:
        """معالجة صفحة واحدة."""
        if self.check_disabled():
            return  # صفحة التوقف مفعّلة: لا تعالج هذه الصفحة ولا ما بعدها

        page = self.current_page
        page_title = page.title()
        self._count_processed += 1

        logger.info("معالجة: %s", page_title)

        original_text = page.text
        new_text, changed = remove_templates(original_text, self._all_names)

        if not changed:
            logger.debug("لا تغيير في: %s", page_title)
            return

        self._count_changed += 1
        pywikibot.showDiff(original_text, new_text)

        if self.opt.dry:
            logger.info("وضع تجريبي: لم يُحفظ — %s", page_title)
            pywikibot.output("** وضع تجريبي: لم يتم الحفظ فعلياً **")
            return

        try:
            summary = 'بوت: إزالة {{ممر تصفح}} وتحويلاته'
            self.put_current(new_text, summary=summary, minor=False)
            logger.info("✔ تم حفظ: %s", page_title)
        except Exception:
            logger.exception("✘ فشل حفظ: %s", page_title)
            self._count_errors += 1
            return

        # تأخير محترم بين التعديلات
        time.sleep(self.opt.delay)


# ============================================================
# نقطة الدخول
# ============================================================


def main(*args: str) -> int:
    """معالجة حجج سطر الأوامر وتشغيل البوت."""
    options: dict[str, Any] = {}
    local_args = pywikibot.handle_args(args)
    site = pywikibot.Site('ar', 'wikipedia')
    site.login()

    gen_factory = GeneratorFactory(site)
    script_args = gen_factory.handle_args(local_args)

    for arg in script_args:
        opt, _, value = arg[1:].partition(":")
        if opt in ("delay", "checkinterval"):
            try:
                options[opt] = int(value)
            except ValueError:
                pywikibot.error(f"قيمة غير صالحة للخيار -{opt}: {value!r}")
                return 1
        elif opt == "limit":
            try:
                options[opt] = int(value) if value else None
            except ValueError:
                pywikibot.error(f"قيمة غير صالحة للخيار -limit: {value!r}")
                return 1
        else:
            options[opt] = True

    # نحسب شبكة القوالب مرة واحدة فقط هنا، ونمررها للبوت لاحقًا لتفادي إعادة حسابها
    template_group = resolve_template_group(site, SEED_TEMPLATE_NAMES)

    # إذا لم يُحدَّد مولّد، نجلب الصفحات من مراجع القوالب تلقائياً
    gen = gen_factory.getCombinedGenerator(preload=True)
    if gen is None:
        seen: set[str] = set()
        pages = []
        for tmpl in template_group.values():
            if not tmpl.exists():
                continue
            for page in tmpl.getReferences(
                only_template_inclusion=True,
                namespaces=[0],
                follow_redirects=False,
            ):
                if page.title() not in seen:
                    seen.add(page.title())
                    pages.append(page)
        gen = iter(pages)
        logger.info("جلب %d صفحة من مراجع القوالب.", len(pages))

    logger.info("=" * 50)
    logger.info("بدء تشغيل RemoveBreadcrumb2Bot — %s", datetime.now(tz=timezone.utc))
    logger.info("=" * 50)

    RemoveBreadcrumb2Bot(
        generator=gen, site=site, template_group=template_group, **options
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
