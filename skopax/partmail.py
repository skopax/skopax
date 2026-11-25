import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from skopax import brand, tags
from skopax.errkind import kind as error_kind
from skopax.mail import BUTTON, INK, PAPER, SIDE, SUBJECT_MAX, WEB_LINK, MailDown, MailRefused, Mailer, address_ok, cell, esc, flat, footer_cell, footer_lines, framed, head_cells, line_cell, sev_badge
from skopax.messages import msg
from skopax.noticemail import WHY_LIMIT, masked, scrub

log = logging.getLogger('skopax.partmail')

MAIL_KIND = 'part'


@dataclass(frozen=True)
class Row:
    text: str
    link: str | None = None
    lead: str = ''
    sev: str = ''
    sev_text: str = ''
    rest: str = ''


@dataclass(frozen=True)
class Section:
    heading: str
    link: str | None = None
    rows: tuple[Row, ...] = field(default_factory=tuple)


def web(link: str | None) -> str | None:
    return link if isinstance(link, str) and WEB_LINK.fullmatch(link) else None


def linked(text: str, link: str | None, style: str) -> str:
    found = web(link)
    return f'<a href="{esc(found)}" style="{style}">{esc(text)}</a>' if found else esc(text)


def badge(sev: str, text: str) -> str:
    return sev_badge(sev, text)


def row_html(r: Row) -> str:
    if not r.lead:
        return linked(r.text, r.link, f'color:{INK};')
    return linked(r.lead, r.link, f'color:{INK};') + badge(r.sev, r.sev_text) + esc(r.rest)


def button(link: str) -> str:
    return cell(f'<a href="{esc(link)}" style="display:inline-block;background-color:{BUTTON};color:#ffffff;text-decoration:none;padding:10px 18px;border-radius:6px;font-weight:bold;">'
                f'{esc(msg("mail.part.board"))}</a>', f'padding-top:16px;padding-bottom:24px;{SIDE}background-color:{PAPER};')


def compose(subject: str, head: str, title: str, sections: Iterable[Section], intro: str = '', board: str | None = None) -> tuple[str, str, str]:
    sections = list(sections)
    footer, why, attribution = msg('mail.footer'), msg('mail.part.why'), brand.attribution_line()
    subject = flat(subject)[:SUBJECT_MAX]
    parts = [head, '', title] + (['', intro] if intro else [])
    rows = head_cells(head, title) + ([line_cell(esc(intro))] if intro else [])
    link_style = f'color:{INK};font-weight:bold;'
    for s in sections:
        found = web(s.link)
        parts += ['', s.heading] + ([found] if found else []) if s.heading else ['']
        if s.heading:
            rows.append(cell(linked(s.heading, s.link, link_style), f'padding-top:12px;padding-bottom:6px;{SIDE}font-size:15px;font-weight:bold;color:{INK};background-color:{PAPER};'))
        for r in s.rows:
            target = web(r.link)
            parts.append(f'- {r.text}' + (f' {target}' if target else ''))
            rows.append(line_cell(row_html(r)))
    found = web(board)
    if found:
        parts += ['', f"{msg('mail.part.board')}: {found}"]
        rows.append(button(found))
    parts += [''] + footer_lines([footer, why], attribution)
    rows.append(footer_cell(footer, attribution, f'<br>{esc(why)}'))
    return subject, '\n'.join(parts) + '\n', framed(subject, ''.join(rows))


def product_subject(title: str) -> str:
    return f'[{brand.product()}] {title}'


class PartMail:
    def __init__(self, mailer: Callable[[], Mailer | None], on: Callable[[], bool], owners: Callable[[], dict[str, list[str]]], labels: Callable[[], dict[str, str]] = tags.all_tags,
                 left_out: Callable[[], set[str]] = set):
        self.mailer = mailer
        self.on = on
        self.owners = owners
        self.labels = labels
        self.left_out = left_out

    def sender(self) -> Mailer | None:
        try:
            if not self.on():
                return None
            found = self.mailer()
            return found if found is not None and found.ready() else None
        except Exception as e:
            log.warning('the mail server for responsible mails could not be read: %s', error_kind(e))
            return None

    def ready(self) -> bool:
        return self.sender() is not None

    def held(self) -> dict[str, list[str]]:
        known, gone = self.labels(), {str(e).strip().lower() for e in self.left_out()}
        out: dict[str, list[str]] = {}
        for tag, emails in self.owners().items():
            if tag not in known:
                continue
            kept = [e for e in dict.fromkeys(str(e).strip().lower() for e in emails if isinstance(e, str)) if address_ok(e) and e not in gone]
            if kept:
                out[tag] = kept
        return out

    def people(self, wanted: Iterable[str] | None = None) -> dict[str, list[str]]:
        held = self.held()
        chosen = list(held) if wanted is None else [t for t in dict.fromkeys(wanted) if t in held]
        out: dict[str, list[str]] = {}
        for tag in chosen:
            for email in held[tag]:
                out.setdefault(email, []).append(tag)
        return out

    def send(self, sender: Mailer, mails: list[tuple[str, tuple[str, str, str]]], what: str) -> int:
        done = 0
        for i, (address, (subject, text, html)) in enumerate(mails):
            try:
                sender.send([address], subject, text, html, kind=MAIL_KIND)
            except MailRefused as e:
                why = scrub(e.text, address)[:WHY_LIMIT]
                if e.recipient:
                    log.warning('the %s mail to %s was refused and is not sent again: %s', what, masked(address), why)
                    continue
                log.warning('the %s mails stopped, the mail server refuses them all; %d not sent and not tried again: %s', what, len(mails) - i, why)
                break
            except MailDown as e:
                log.warning('the %s mails stopped, the mail server did not answer; %d not sent and not tried again: %s', what, len(mails) - i, scrub(e.text, address)[:WHY_LIMIT])
                break
            except Exception as e:
                log.warning('the %s mail to %s was not sent and is not tried again: %s', what, masked(address), error_kind(e))
                continue
            done += 1
        return done
