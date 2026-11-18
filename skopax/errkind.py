def kind(e: BaseException) -> str:
    name = next((c.__name__ for c in type(e).__mro__ if not c.__name__.startswith('_')), type(e).__name__)
    code = getattr(e, 'code', None)
    return f'{name} code {code}' if isinstance(code, int) and not isinstance(code, bool) else name


class Authored:
    @property
    def text(self) -> str:
        return str(self)


class Refusal(Authored, ValueError):
    pass


class NoAccess(Exception):
    part = ''
    permission = ''


def text_of(e: BaseException) -> str:
    return e.text if isinstance(e, Authored) else kind(e)
