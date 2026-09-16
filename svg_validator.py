"""
التحقق من صحة ملفات SVG باستخدام W3C Nu Validator.

الحجج المدعومة:

-always           لا تطلب تأكيداً قبل الحفظ.
-maxsize:N        تخطي الملفات الأكبر من N كيلوبايت (افتراضي: 2048).
-checkinterval:N  التحقق من صفحة التوقف كل N صفحة (افتراضي: 10).
-delay:N          التأخير بين طلبات W3C بالثواني (افتراضي: 3).

&params;
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mwparserfromhell
import pywikibot
import requests
from mwparserfromhell.nodes import Template
from pywikibot.bot import ExistingPageBot, FollowRedirectPageBot, SingleSiteBot
from pywikibot.comms.http import user_agent
from pywikibot.exceptions import InvalidTitleError
from pywikibot.pagegenerators import GeneratorFactory, parameterHelp
from pywikibot.textlib import removeDisabledParts
from requests.exceptions import RequestException, Timeout

docuReplacements = {"&params;": parameterHelp}  # noqa: N816

# ============================================================
# الثوابت
# ============================================================

# الحد الأقصى الافتراضي لحجم الملف بالكيلوبايت
DEFAULT_MAX_SIZE_KB = 2048

# التأخير الافتراضي بين طلبات W3C بالثواني
DEFAULT_DELAY_SECONDS = 3

# التحقق من صفحة التوقف كل N صفحة
DEFAULT_CHECK_INTERVAL = 10

# أسماء القوالب المستخدمة في ويكيبيديا العربية
INVALID_SVG_TEMPLATE = "SVG غير سليم"
VALID_SVG_TEMPLATE = "SVG سليم"

# ملف السجل
LOG_FILE = Path("svg_validator.log")

# ============================================================
# إعداد التسجيل
# ============================================================


def setup_logger() -> logging.Logger:
    """يُعدّ مسجّلاً يكتب في ملف وعلى الشاشة معاً."""
    logger = logging.getLogger("svg_validator")
    logger.setLevel(logging.DEBUG)

    # تنسيق موحّد مع التوقيت
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # معالج الملف
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # معالج الشاشة (تحذيرات فما فوق)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = setup_logger()


# ============================================================
# دوال مساعدة
# ============================================================


def get_template_redirects(site: pywikibot.Site, *names: str) -> frozenset[pywikibot.Page]:
    """
    يجمع صفحات القوالب وكل تحويلاتها دون الاعتماد على مكتبات خارجية.

    :param site: موقع ويكيبيديا.
    :param names: أسماء القوالب الأصلية (بدون بادئة قالب:).
    :returns: مجموعة ثابتة من كائنات الصفحة تشمل الأصول والتحويلات.
    """
    result: set[pywikibot.Page] = set()
    to_process = list(names)
    processed: set[str] = set()

    while to_process:
        name = to_process.pop()
        if name in processed:
            continue
        processed.add(name)

        page = pywikibot.Page(site, name, ns=10)
        if not page.exists():
            logger.warning("القالب غير موجود: %s", page.title())
            continue

        result.add(page)

        # تتبع التحويلات التي تشير لهذه الصفحة
        for redirect in page.backlinks(filter_redirects=True, namespaces=[10]):
            rname = redirect.title(with_ns=False)
            if rname not in processed:
                to_process.append(rname)

    return frozenset(result)


# ============================================================
# البوت الرئيسي
# ============================================================


class SVGValidatorBot(SingleSiteBot, FollowRedirectPageBot, ExistingPageBot):
    """بوت للتحقق من صحة ملفات SVG باستخدام W3C Nu Validator."""

    update_options = {
        "always": False,
        "maxsize": DEFAULT_MAX_SIZE_KB,
        "delay": DEFAULT_DELAY_SECONDS,
        "checkinterval": DEFAULT_CHECK_INTERVAL,
    }

    def __init__(self, **kwargs: Any) -> None:
        """تهيئة البوت."""
        super().__init__(**kwargs)

        # جلسة HTTP مع ترويسة user-agent مناسبة
        self.nu_session = requests.Session()
        self.nu_session.headers["User-Agent"] = user_agent(
            self.site,
            "{script} ({script_comments}) {python} {http_backend}",
        )
        self.nu_session.params = {"level": "error", "out": "json"}

        # جلب القوالب وتحويلاتها بدون مكتبات خارجية
        self.templates = get_template_redirects(
            self.site, INVALID_SVG_TEMPLATE, VALID_SVG_TEMPLATE
        )
        logger.info(
            "القوالب المستهدفة: %s",
            ", ".join(p.title() for p in self.templates),
        )

        # عدّادات الإحصائيات
        self._count_valid = 0
        self._count_invalid = 0
        self._count_skipped = 0
        self._count_errors = 0
        self._pages_since_check = 0
        self._start_time = datetime.now(tz=timezone.utc)

    # ── دورة الحياة ─────────────────────────────────────────

    def teardown(self) -> None:
        """إغلاق الجلسة وطباعة الإحصائيات النهائية."""
        self.nu_session.close()
        elapsed = datetime.now(tz=timezone.utc) - self._start_time
        summary = (
            f"\n{'=' * 50}\n"
            f"انتهى العمل — المدة: {elapsed}\n"
            f"  صالحة    : {self._count_valid}\n"
            f"  غير صالحة: {self._count_invalid}\n"
            f"  متخطاة   : {self._count_skipped}\n"
            f"  أخطاء    : {self._count_errors}\n"
            f"{'=' * 50}"
        )
        pywikibot.output(summary)
        logger.info(summary)

    def init_page(self, item: Any) -> pywikibot.Page:
        """تحويل الصفحة إلى FilePage."""
        page = super().init_page(item)
        try:
            return pywikibot.FilePage(page)
        except ValueError:
            return page

    # ── التصفية ─────────────────────────────────────────────

    def skip_page(self, page: pywikibot.Page) -> bool:
        """تخطي الصفحة إذا لم تكن ملف SVG أو تجاوزت الحجم الأقصى."""
        if not isinstance(page, pywikibot.FilePage):
            logger.debug("تخطي (ليست ملفاً): %s", page.title())
            self._count_skipped += 1
            return True

        if not page.title(with_ns=False).lower().endswith(".svg"):
            logger.debug("تخطي (ليست SVG): %s", page.title())
            self._count_skipped += 1
            return True

        # التحقق من حجم الملف قبل الإرسال لـ W3C
        try:
            file_info = page.latest_file_info
            size_kb = file_info.size / 1024
            max_kb = self.opt.maxsize
            if size_kb > max_kb:
                logger.warning(
                    "تخطي (حجم كبير: %.1f كب > %d كب): %s",
                    size_kb, max_kb, page.title(),
                )
                self._count_skipped += 1
                return True
        except Exception:
            logger.exception("تعذّر التحقق من حجم: %s", page.title())

        return super().skip_page(page)

    # ── صفحة التوقف ─────────────────────────────────────────

    def check_disabled(self) -> None:
        """
        التحقق من صفحة التوقف كل N صفحة بدل كل صفحة،
        مع إعادة تسجيل الدخول تلقائياً عند انتهاء الجلسة فقط.
        """
        self._pages_since_check += 1
        if self._pages_since_check < self.opt.checkinterval:
            return
        self._pages_since_check = 0

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

    # ── التحقق من SVG ────────────────────────────────────────

    def validate_svg(self) -> tuple[list[str], list[str]]:
        """
        التحقق من ملف SVG عبر W3C Nu Validator.

        :returns: (قائمة الأخطاء، قائمة التحذيرات)
        :raises RuntimeError: إذا كانت نتيجة التحقق غير محددة.
        :raises ValueError: إذا كانت الاستجابة بتنسيق غير متوقع.
        :raises RequestException: عند فشل الطلب الشبكي.
        """
        url = self.current_page.get_file_url()
        _logger = "w3c-nu"
        retries = 0
        retry_wait = pywikibot.config.retry_wait

        while True:
            try:
                response = self.nu_session.get(
                    url="https://validator.w3.org/nu/",
                    params={"doc": url},
                    timeout=pywikibot.config.socket_timeout,
                )
                response.raise_for_status()
                break
            except Timeout:
                if (
                    retry_wait > pywikibot.config.retry_max
                    or retries >= pywikibot.config.max_retries
                ):
                    logger.error("انتهت مهلة الانتظار بعد %d محاولة.", retries)
                    raise
                logger.warning("مهلة انتهت، إعادة المحاولة بعد %ds...", retry_wait)
                pywikibot.sleep(retry_wait)
                retries += 1
                retry_wait = min(retry_wait * 2, pywikibot.config.retry_max)
            except RequestException:
                logger.exception("خطأ شبكي عند التحقق من: %s", url)
                raise

        pywikibot.debug(response.text, layer=_logger)
        data = response.json()

        # التحقق من صحة الاستجابة بدل assert
        if "messages" not in data or not isinstance(data["messages"], list):
            raise ValueError(
                f"الاستجابة لا تحتوي على مفتاح 'messages' صالح: {data}"
            )
        if "url" in data and data["url"] != url:
            raise ValueError(
                f"طلبنا التحقق من {url} لكن الاستجابة تخص {data['url']}"
            )

        errors: list[str] = []
        warnings: list[str] = []

        for message in data["messages"]:
            if not isinstance(message, dict):
                logger.error("رسالة غير صالحة (ليست كائناً): %s", message)
                continue

            msg_type = message.get("type")
            if not msg_type:
                logger.error("رسالة بدون نوع: %s", message)
                continue

            if msg_type not in ("non-document-error", "error", "info"):
                logger.error("نوع رسالة غير معروف: %s", msg_type)
                continue

            msg_text = message.get("message", "")
            sub_type = message.get("subType", "none")

            if msg_type == "non-document-error":
                raise RuntimeError(
                    f"نتيجة التحقق غير محددة. {msg_type}/{sub_type}: {msg_text}"
                )
            if msg_type == "error":
                errors.append(msg_text)
                logger.debug("خطأ SVG: %s", msg_text)
            elif sub_type == "warning":
                warnings.append(msg_text)
                logger.debug("تحذير SVG: %s", msg_text)

        return errors, warnings

    # ── معالجة الصفحة ────────────────────────────────────────

    def treat_page(self) -> None:
        """معالجة ملف SVG واحد."""
        self.check_disabled()

        page_title = self.current_page.title()
        logger.info("معالجة: %s", page_title)

        # التحقق من W3C
        try:
            errors, warnings = self.validate_svg()
        except (ValueError, RequestException, RuntimeError):
            logger.exception("فشل التحقق من: %s", page_title)
            self._count_errors += 1
            return
        finally:
            # تأخير محترم بين الطلبات بغض النظر عن النتيجة
            time.sleep(self.opt.delay)

        # تسجيل التحذيرات
        if warnings:
            logger.warning(
                "%s: %d تحذير(ات): %s",
                page_title, len(warnings), " | ".join(warnings),
            )

        # إعداد القالب والملخص
        if errors:
            n_errors = len(errors)
            new_tpl = Template(INVALID_SVG_TEMPLATE)
            new_tpl.add("1", str(n_errors))
            summary = f"بوت: SVG غير سليم وفق W3C — {n_errors} خطأ"
            logger.info("غير سليم (%d خطأ): %s", n_errors, page_title)
            self._count_invalid += 1
        else:
            new_tpl = Template(VALID_SVG_TEMPLATE)
            summary = "بوت: SVG سليم وفق W3C"
            logger.info("صالح: %s", page_title)
            self._count_valid += 1

        # تعديل الويكي-نص
        wikicode = mwparserfromhell.parse(
            self.current_page.text, skip_style_tags=True
        )

        replaced = False
        for tpl in wikicode.ifilter_templates():
            try:
                tpl_name = removeDisabledParts(str(tpl.name), site=self.site).strip()
                template_page = pywikibot.Page(self.site, tpl_name, ns=10)
                template_page.title()  # للتحقق من صحة العنوان
            except InvalidTitleError:
                continue

            if template_page in self.templates:
                wikicode.replace(tpl, new_tpl)
                replaced = True
                break

        if not replaced:
            wikicode.insert(0, "\n")
            wikicode.insert(0, new_tpl)

        self.put_current(
            str(wikicode),
            summary=summary,
            minor=not errors,
            asynchronous=False,
        )


# ============================================================
# نقطة الدخول
# ============================================================


def main(*args: str) -> int:
    """معالجة حجج سطر الأوامر وتشغيل البوت."""
    options: dict[str, Any] = {}
    local_args = pywikibot.handle_args(args)
    site = pywikibot.Site()
    site.login()

    gen_factory = GeneratorFactory(site)
    script_args = gen_factory.handle_args(local_args)

    for arg in script_args:
        opt, _, value = arg[1:].partition(":")
        if opt in ("maxsize", "delay", "checkinterval"):
            try:
                options[opt] = int(value)
            except ValueError:
                pywikibot.error(f"قيمة غير صالحة للخيار -{opt}: {value!r}")
                return 1
        else:
            options[opt] = True

    gen = gen_factory.getCombinedGenerator(preload=True)
    if gen is None:
        pywikibot.error("لم يُحدَّد مولّد صفحات. استخدم -cat أو -ns أو غيرهما.")
        return 1

    logger.info("=" * 50)
    logger.info("بدء تشغيل SVGValidatorBot — %s", datetime.now(tz=timezone.utc))
    logger.info("=" * 50)

    SVGValidatorBot(generator=gen, site=site, **options).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
