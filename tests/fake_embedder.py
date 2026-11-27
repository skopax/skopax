import hashlib
import math

from skopax import embedclient
from skopax.embedclient import Breaker, EmbedError

DIMS = 384


def trigrams(text: str) -> list[str]:
    padded = f'  {text.lower()}  '
    return [padded[i:i + 3] for i in range(max(1, len(padded) - 2))]


def embed_text(text: str, dims: int = DIMS) -> list[float]:
    out = [0.0] * dims
    for gram in trigrams(text):
        digest = hashlib.sha256(gram.encode('utf-8')).digest()
        out[int.from_bytes(digest[:4], 'big') % dims] += 1.0 if digest[4] % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in out)) or 1.0
    return [v / norm for v in out]


class FakeEmbedder:
    def __init__(self, model: str | None = None):
        self.calls: list[list[str]] = []
        self.down: str | None = None
        self.model = model
        self.breaker = Breaker()

    @property
    def texts(self) -> list[str]:
        return [t for call in self.calls for t in call]

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        if self.down:
            raise EmbedError(self.down)
        self.calls.append(list(texts))
        if self.model:
            embedclient.LEARNED.setdefault(self.model, DIMS)
        return [embed_text(t) for t in texts]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self.embed_passages(texts)
