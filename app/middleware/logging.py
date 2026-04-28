import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("uvicorn.error")


def _mask_auth_header(value: bytes) -> str:
    """Oscura token sensibili mantenendo solo prefisso e ultimi caratteri."""

    raw = value.decode("utf-8")
    if raw in ("", "-"):
        return "-"
    if len(raw) <= 16:
        return "***"
    return f"{raw[:10]}...{raw[-4:]}"


class LoggingMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        start_time = time.time()
        request_id = scope.get("aws.request_id", "-")  # Example for AWS Lambda

        # Extract request headers
        req_headers = dict(scope.get("headers", []))
        auth_masked = _mask_auth_header(req_headers.get(b"authorization", b"-"))
        logger.info(
            'path="%s" auth="%s"',
            scope["path"],
            auth_masked,
        )
        user_agent = req_headers.get(b"user-agent", b"-").decode("utf-8")
        req_content_length = req_headers.get(
            b"content-length",
            b"0",
        ).decode("utf-8")
        client_addr = scope.get("client")
        client_host = client_addr[0] if client_addr else "-"
        client_port = client_addr[1] if client_addr else "-"

        async def log_response_info(message: Message) -> None:
            if message["type"] == "http.response.start":
                process_time = time.time() - start_time
                # Extract response headers
                res_headers = dict(message.get("headers", []))
                res_content_length = res_headers.get(b"content-length", b"0").decode(
                    "utf-8"
                )

                logger.info(
                    'request_id="%s" method="%s" path="%s" status_code=%s '
                    'duration=%.4fs req_bytes=%s res_bytes=%s user_agent="%s"',
                    request_id,
                    scope.get("method", "-"),
                    scope["path"],
                    message["status"],
                    process_time,
                    req_content_length,
                    res_content_length,
                    user_agent,
                )
            await send(message)

        if scope["type"] == "http":
            await self.app(scope, receive, log_response_info)
        else:  # websocket
            logger.info(
                'request_id="%s" client="%s:%s" ws_path="%s" user_agent="%s"',
                request_id,
                client_host,
                client_port,
                scope["path"],
                user_agent,
            )
            await self.app(scope, receive, send)
