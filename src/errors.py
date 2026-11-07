"""ServiceError: HTTP-semantic failure contract shared by every layer.

Storage repositories and services raise it directly so transport layers map
it one-to-one onto responses; no layer may wrap or re-classify it.
"""


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail
