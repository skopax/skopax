import time
from collections.abc import Callable, Sequence

from skopax.contract import CodeHost, SourceDown
from skopax.errkind import NoAccess
from skopax.messages import msg

TAG = 'codeHost'
DOWN_S = 60.0


class Undecided(SourceDown):
    pass


class CodeHosts:
    def __init__(self, hosts: Sequence[CodeHost], clock: Callable[[], float] = time.monotonic):
        self.hosts = tuple(hosts)
        self.clock = clock
        self._down: dict[int, tuple[float, str]] = {}

    def _on(self) -> list[int]:
        return [i for i, h in enumerate(self.hosts) if h.enabled]

    @property
    def enabled(self) -> bool:
        return bool(self._on())

    def _lone(self) -> CodeHost | None:
        on = self._on()
        if len(on) > 1:
            return None
        return self.hosts[on[0] if on else 0]

    def repo(self, name_or_path: str) -> dict | None:
        lone = self._lone()
        if lone is not None:
            return lone.repo(name_or_path)
        for i in self._on():
            held = self._down.get(i)
            if held is not None and self.clock() < held[0]:
                raise Undecided(held[1])
            try:
                found = self.hosts[i].repo(name_or_path)
            except SourceDown as e:
                self._down[i] = (self.clock() + DOWN_S, e.text)
                raise Undecided(e.text) from e
            self._down.pop(i, None)
            if found:
                return {**found, TAG: i}
        return None

    def similar(self, name: str, limit: int = 5) -> list[str]:
        lone = self._lone()
        if lone is not None:
            return lone.similar(name, limit)
        out: list[str] = []
        for i in self._on():
            try:
                out += [p for p in self.hosts[i].similar(name, limit) if p not in out]
            except NoAccess:
                raise
            except SourceDown:
                continue
        return out[:limit]

    def _of(self, repo) -> CodeHost:
        if isinstance(repo, dict) and isinstance(repo.get(TAG), int) and not isinstance(repo[TAG], bool) and 0 <= repo[TAG] < len(self.hosts):
            host = self.hosts[repo[TAG]]
            if not host.enabled:
                raise SourceDown(msg('codehosts.off', repo=str(repo.get('full') or repo.get('path') or '')[:200]))
            return host
        lone = self._lone()
        if lone is not None:
            return lone
        if isinstance(repo, str):
            found = self.repo(repo)
            if found:
                return self.hosts[found[TAG]]
        return self.hosts[self._on()[0]]

    def _read(self, repo, name: str, *args):
        host = self._of(repo)
        if self._lone() is not None:
            return getattr(host, name)(repo, *args)
        try:
            return getattr(host, name)(repo, *args)
        except (NoAccess, Undecided):
            raise
        except SourceDown as e:
            raise Undecided(e.text) from e

    def commit(self, repo, ref: str) -> dict:
        return self._read(repo, 'commit', ref)

    def commits(self, repo, old: str, new: str) -> list[dict]:
        return self._read(repo, 'commits', old, new)

    def commits_since(self, repo, ref: str, since: str) -> list[dict]:
        return self._read(repo, 'commits_since', ref, since)

    def file(self, repo, path: str, ref: str) -> str:
        return self._read(repo, 'file', path, ref)

    def blame(self, repo, path: str, ref: str, first: int, last: int) -> list[dict]:
        return self._read(repo, 'blame', path, ref, first, last)

    def commit_url(self, repo: dict, sha: str) -> str:
        return self._of(repo).commit_url(repo, sha)

    def compare_url(self, repo: dict, old: str, new: str) -> str:
        return self._of(repo).compare_url(repo, old, new)

    def file_url(self, repo: dict, sha: str, path: str, first: int, last: int) -> str:
        return self._of(repo).file_url(repo, sha, path, first, last)

    def link_problem(self, url: str) -> str | None:
        lone = self._lone()
        if lone is not None:
            return lone.link_problem(url)
        for i in self._on():
            problem = self.hosts[i].link_problem(url)
            if problem:
                return problem
        return None
