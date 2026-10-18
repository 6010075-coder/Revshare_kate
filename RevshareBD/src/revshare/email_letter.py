from __future__ import annotations

from html import escape

from revshare.notifications import format_notice

SUBJECTS = {
    "en": {
        "invite": "{name}: report for {period}",
        "reminder": "{name}: report for {period} is still missing",
        "last": "{name}: today is the deadline for {period}",
        "overdue": "{name}: report for {period} is overdue",
        "digest": "Revshare status for {period}",
    },
    "ru": {
        "invite": "{name}: отчёт за {period}",
        "reminder": "{name}: отчёт за {period} ещё не получен",
        "last": "{name}: сегодня последний день сдачи за {period}",
        "overdue": "{name}: отчёт за {period} просрочен",
        "digest": "Статус revshare за {period}",
    },
    "uk": {
        "invite": "{name}: звіт за {period}",
        "reminder": "{name}: звіт за {period} ще не отримано",
        "last": "{name}: сьогодні останній день подання за {period}",
        "overdue": "{name}: звіт за {period} прострочено",
        "digest": "Статус revshare за {period}",
    },
    "pl": {
        "invite": "{name}: raport za {period}",
        "reminder": "{name}: raport za {period} jeszcze nie wpłynął",
        "last": "{name}: dziś mija termin za {period}",
        "overdue": "{name}: raport za {period} jest po terminie",
        "digest": "Status revshare za {period}",
    },
    "hu": {
        "invite": "{name}: jelentés {period}",
        "reminder": "{name}: a {period} jelentés még hiányzik",
        "last": "{name}: ma van a {period} határideje",
        "overdue": "{name}: a {period} jelentés késik",
        "digest": "Revshare státusz {period}",
    },
    "pt": {
        "invite": "{name}: relatório de {period}",
        "reminder": "{name}: o relatório de {period} ainda não chegou",
        "last": "{name}: hoje é o prazo de {period}",
        "overdue": "{name}: o relatório de {period} está em atraso",
        "digest": "Estado do revshare de {period}",
    },
}

LETTER_TEXTS = {
    "en": {
        "brand": "Altegio Integration Hub",
        "titles": {
            "invite": "Report for {period}",
            "reminder": "Report for {period} is still missing",
            "last": "Today is the deadline for {period}",
            "overdue": "Report for {period} is overdue",
        },
        "button": "Open the report form",
        "link_hint": "If the button does not open, use this link:",
        "deadline": "Deadline: {deadline} inclusive.",
        "prepare_title": "What to prepare",
        "prepare": [
            "Total turnover for the month",
            "A report file with every client's Altegio Location ID and payment amount: Excel, CSV or PDF.",
        ],
        "note": (
            "The form asks for a short comment only about clients that were active "
            "during the month and are missing from your file."
        ),
        "signature": "Partnerships, Altegio",
        "footer": (
            "Data is used only to calculate partner revenue share and reconcile it "
            "with Altegio."
        ),
    },
    "ru": {
        "brand": "Хаб Интеграций Altegio",
        "titles": {
            "invite": "Отчёт за {period}",
            "reminder": "Отчёт за {period} ещё не получен",
            "last": "Сегодня последний день сдачи за {period}",
            "overdue": "Отчёт за {period} просрочен",
        },
        "button": "Открыть форму отчёта",
        "link_hint": "Если кнопка не открывается, перейдите по ссылке:",
        "deadline": "Срок сдачи: {deadline} включительно.",
        "prepare_title": "Что подготовить",
        "prepare": [
            "Итоговый оборот за месяц",
            "Файл отчёта с Location ID каждого клиента Altegio и суммой оплаты: Excel, CSV или PDF.",
        ],
        "note": (
            "Форма попросит короткий комментарий только по клиентам, которые были "
            "активны в течение месяца и отсутствуют в вашем файле."
        ),
        "signature": "Partnerships, Altegio",
        "footer": (
            "Данные используются только для расчёта партнёрского revshare и сверки "
            "с Altegio."
        ),
    },
    "uk": {
        "brand": "Хаб Інтеграцій Altegio",
        "titles": {
            "invite": "Звіт за {period}",
            "reminder": "Звіт за {period} ще не отримано",
            "last": "Сьогодні останній день подання за {period}",
            "overdue": "Звіт за {period} прострочено",
        },
        "button": "Відкрити форму звіту",
        "link_hint": "Якщо кнопка не відкривається, перейдіть за посиланням:",
        "deadline": "Термін подання: {deadline} включно.",
        "prepare_title": "Що підготувати",
        "prepare": [
            "Підсумковий оборот за місяць",
            "Файл звіту з Location ID кожного клієнта Altegio та сумою оплати: Excel, CSV або PDF.",
        ],
        "note": (
            "Форма попросить короткий коментар лише щодо клієнтів, які були активні "
            "протягом місяця і відсутні у вашому файлі."
        ),
        "signature": "Partnerships, Altegio",
        "footer": (
            "Дані використовуються лише для розрахунку партнерського revshare та "
            "звірки з Altegio."
        ),
    },
    "pl": {
        "brand": "Altegio Integration Hub",
        "titles": {
            "invite": "Raport za {period}",
            "reminder": "Raport za {period} jeszcze nie wpłynął",
            "last": "Dziś mija termin za {period}",
            "overdue": "Raport za {period} jest po terminie",
        },
        "button": "Otwórz formularz raportu",
        "link_hint": "Jeśli przycisk nie działa, użyj tego linku:",
        "deadline": "Termin: {deadline} włącznie.",
        "prepare_title": "Co przygotować",
        "prepare": [
            "Łączny obrót za miesiąc",
            "Plik raportu z Location ID każdego klienta Altegio i kwotą płatności: Excel, CSV lub PDF.",
        ],
        "note": (
            "Formularz poprosi o krótki komentarz tylko do klientów, którzy byli "
            "aktywni w danym miesiącu i których nie ma w pliku."
        ),
        "signature": "Partnerships, Altegio",
        "footer": (
            "Dane służą tylko do wyliczenia revshare partnera i uzgodnienia z Altegio."
        ),
    },
    "hu": {
        "brand": "Altegio Integration Hub",
        "titles": {
            "invite": "Jelentés: {period}",
            "reminder": "A {period} jelentés még hiányzik",
            "last": "Ma van a {period} határideje",
            "overdue": "A {period} jelentés késik",
        },
        "button": "Jelentésűrlap megnyitása",
        "link_hint": "Ha a gomb nem nyílik meg, használja ezt a linket:",
        "deadline": "Határidő: {deadline} bezárólag.",
        "prepare_title": "Mit készítsen elő",
        "prepare": [
            "A hónap teljes forgalma",
            "Jelentésfájl minden Altegio ügyfél Location ID-jával és fizetési összegével: Excel, CSV vagy PDF.",
        ],
        "note": (
            "Az űrlap csak azokhoz az ügyfelekhez kér rövid megjegyzést, akik a hónapban "
            "aktívak voltak, de hiányoznak a fájlból."
        ),
        "signature": "Partnerships, Altegio",
        "footer": (
            "Az adatokat csak a partneri revshare számításához és az Altegio "
            "egyeztetéséhez használjuk."
        ),
    },
    "pt": {
        "brand": "Altegio Integration Hub",
        "titles": {
            "invite": "Relatório de {period}",
            "reminder": "O relatório de {period} ainda não chegou",
            "last": "Hoje é o prazo de {period}",
            "overdue": "O relatório de {period} está em atraso",
        },
        "button": "Abrir o formulário de relatório",
        "link_hint": "Se o botão não abrir, use esta ligação:",
        "deadline": "Prazo: {deadline}, inclusive.",
        "prepare_title": "O que preparar",
        "prepare": [
            "Faturação total do mês",
            "Ficheiro do relatório com o Location ID de cada cliente Altegio e o valor pago: Excel, CSV ou PDF.",
        ],
        "note": (
            "O formulário pede um comentário curto só sobre clientes que estiveram "
            "ativos no mês e não aparecem no ficheiro."
        ),
        "signature": "Partnerships, Altegio",
        "footer": (
            "Os dados servem só para calcular o revshare do parceiro e confrontá-los "
            "com o Altegio."
        ),
    },
}


def letter_language(language: str | None) -> str:
    return language if language in LETTER_TEXTS else "en"


def subject_for(notice_key: str, language: str, name: str, period: str) -> str:
    texts = SUBJECTS[letter_language(language)]
    template = texts.get(notice_key) or SUBJECTS["en"].get(notice_key, "{name}")
    return format_notice(template, name=name, period=period)


def message_paragraphs(message: str, link: str) -> list[str]:
    paragraphs = []
    for raw in message.splitlines():
        line = raw.replace(link, "").strip() if link else raw.strip()
        if line:
            paragraphs.append(line)
    return paragraphs


def build_letter(
    notice_key: str,
    language: str,
    *,
    name: str,
    period: str,
    deadline: str,
    link: str,
    message: str,
) -> dict[str, str]:
    lang = letter_language(language)
    texts = LETTER_TEXTS[lang]
    title = format_notice(
        texts["titles"].get(notice_key, texts["titles"]["invite"]),
        period=period,
        name=name,
    )
    deadline_line = format_notice(texts["deadline"], deadline=deadline)
    paragraphs = message_paragraphs(message, link)
    subject = subject_for(notice_key, lang, name, period)

    text_lines = [*paragraphs]
    if link:
        text_lines.extend(["", f"{texts['button']}: {link}"])
    text_lines.extend(["", deadline_line, ""])
    text_lines.append(f"{texts['prepare_title']}:")
    text_lines.extend(f"- {item}" for item in texts["prepare"])
    text_lines.extend(["", texts["note"], "", texts["signature"], texts["footer"]])

    return {
        "subject": subject,
        "text": "\n".join(text_lines).strip() + "\n",
        "html": render_html(
            lang=lang,
            subject=subject,
            title=title,
            paragraphs=paragraphs,
            link=link,
            deadline_line=deadline_line,
        ),
    }


def render_html(
    *,
    lang: str,
    subject: str,
    title: str,
    paragraphs: list[str],
    link: str,
    deadline_line: str,
) -> str:
    texts = LETTER_TEXTS[lang]
    safe_link = escape(link, quote=True)
    body = "".join(
        f'<p style="margin:0 0 12px;font:400 15px/1.6 Arial,Helvetica,sans-serif;'
        f'color:#343442;">{escape(paragraph)}</p>'
        for paragraph in paragraphs
    )
    prepare = "".join(f"<li>{escape(item)}</li>" for item in texts["prepare"])
    link_block = ""
    if link:
        link_block = f'''  <tr><td style="padding:10px 28px 0;">
    <a href="{safe_link}" style="display:inline-block;background:#ffcb00;color:#22222e;font:700 15px/1 Arial,Helvetica,sans-serif;padding:15px 24px;border-radius:12px;text-decoration:none;">{escape(texts["button"])}</a>
  </td></tr>
  <tr><td style="padding:14px 28px 0;font:400 12px/1.6 Arial,Helvetica,sans-serif;color:#626c77;">
    {escape(texts["link_hint"])}<br>
    <a href="{safe_link}" style="color:#626c77;">{escape(link)}</a>
  </td></tr>
'''
    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(subject)}</title>
</head>
<body style="margin:0;padding:0;background:#f2f3f7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f3f7;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:16px;overflow:hidden;">
  <tr><td style="background:#22222e;padding:18px 28px;">
    <span style="display:inline-block;background:#ffcb00;color:#22222e;font:700 12px/1 Arial,Helvetica,sans-serif;padding:7px 12px;border-radius:999px;">{escape(texts["brand"])}</span>
  </td></tr>
  <tr><td style="padding:28px 28px 4px;">
    <h1 style="margin:0 0 14px;font:700 22px/1.3 Arial,Helvetica,sans-serif;color:#22222e;">{escape(title)}</h1>
    {body}
  </td></tr>
{link_block}
  <tr><td style="padding:22px 28px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f0f1f8;border-radius:12px;">
      <tr><td style="padding:16px 18px;">
        <strong style="font:700 13px Arial,Helvetica,sans-serif;color:#22222e;">{escape(texts["prepare_title"])}</strong>
        <ul style="margin:8px 0 0;padding-left:18px;font:400 13px/1.7 Arial,Helvetica,sans-serif;color:#343442;">{prepare}</ul>
        <p style="margin:12px 0 0;font:400 12px/1.6 Arial,Helvetica,sans-serif;color:#626c77;">{escape(texts["note"])}</p>
      </td></tr>
    </table>
  </td></tr>
  <tr><td style="padding:22px 28px 26px;font:400 13px/1.6 Arial,Helvetica,sans-serif;color:#343442;">
    <p style="margin:0 0 6px;">{escape(deadline_line)}</p>
    <p style="margin:0;color:#626c77;">{escape(texts["signature"])}</p>
  </td></tr>
  <tr><td style="padding:16px 28px;background:#f0f1f8;font:400 11px/1.5 Arial,Helvetica,sans-serif;color:#626c77;">{escape(texts["footer"])}</td></tr>
</table>
</td></tr></table>
</body>
</html>
"""
