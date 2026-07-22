from __future__ import annotations

from typing import Any, NoReturn
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request


class RejectRedirectHandler(HTTPRedirectHandler):
    """Fail before urllib can replay a credential-bearing request."""

    @staticmethod
    def _reject(
        request: Request,
        response: Any,
        code: int,
        _message: str,
        headers: Any,
    ) -> NoReturn:
        raise HTTPError(
            request.full_url,
            code,
            "redirect responses are forbidden",
            headers,
            response,
        )

    def http_error_301(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any
    ) -> Any | None:
        return self._reject(req, fp, code, msg, headers)

    def http_error_302(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any
    ) -> Any | None:
        return self._reject(req, fp, code, msg, headers)

    def http_error_303(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any
    ) -> Any | None:
        return self._reject(req, fp, code, msg, headers)

    def http_error_307(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any
    ) -> Any | None:
        return self._reject(req, fp, code, msg, headers)

    def http_error_308(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any
    ) -> Any | None:
        return self._reject(req, fp, code, msg, headers)


__all__ = ["RejectRedirectHandler"]
