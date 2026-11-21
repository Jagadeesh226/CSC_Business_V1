"""Cap the size of upload requests before anything else reads them.

FastAPI parses a multipart form (spooling files to disk) before it runs the
sign-in dependency, so without this an anonymous client could make the
server receive and store a body of any size. For upload paths, this ASGI
middleware rejects a declared Content-Length over the limit at once, and
otherwise reads the body with a running count, answering 413 as soon as the
limit is crossed. Accepted bodies are replayed to the app unchanged.

A reverse proxy limit (e.g. nginx client_max_body_size) is still advised.
"""

import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

UPLOAD_PATHS = re.compile(
    r"^/api/v1/(customers/\d+/documents|documents/\d+/versions)/?$"
)
# The 10 MB file limit plus room for multipart headers and form fields.
MAX_UPLOAD_BODY = 10 * 1024 * 1024 + 256 * 1024


async def _send_413(send: Send) -> None:
    body = b'{"detail":"Files can be at most 10 MB."}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class UploadBodyLimit:
    def __init__(self, app: ASGIApp, max_body: int = MAX_UPLOAD_BODY) -> None:
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not UPLOAD_PATHS.match(scope["path"])
        ):
            await self.app(scope, receive, send)
            return

        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    if int(value) > self.max_body:
                        await _send_413(send)
                        return
                except ValueError:
                    pass

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return  # client went away
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_body:
                await _send_413(send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": b"".join(chunks),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay, send)
