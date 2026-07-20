from __future__ import annotations

from typing import Any
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        """Fail on redirects before urllib can replay credentials to another origin."""

        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects())


def open_url_no_redirect(request: Request, timeout: float) -> Any:
    """Open one URL without following any HTTP redirect response."""

    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)
