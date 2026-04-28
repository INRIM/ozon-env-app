from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from datetime import datetime

from app.app_settings import EnvSettings
from app.core.models import AttachmentScanStatus
from app.core.timezone import now_utc


class AntivirusError(RuntimeError):
    pass


class AntivirusUnavailableError(AntivirusError):
    pass


class AntivirusFileInfectedError(AntivirusError):
    def __init__(self, signature: str) -> None:
        super().__init__(signature)
        self.signature = signature


@dataclass(frozen=True)
class AntivirusScanResult:
    status: AttachmentScanStatus
    signature: str
    scanned_at: datetime
    engine: str = "clamav"


def parse_clamav_response(response: str) -> AntivirusScanResult:
    message = response.strip("\0 \n\r\t")
    if not message:
        raise AntivirusUnavailableError("ClamAV returned an empty response")

    if message.endswith("OK"):
        return AntivirusScanResult(
            status=AttachmentScanStatus.CLEAN,
            signature="",
            scanned_at=now_utc(),
        )

    if message.endswith("FOUND"):
        _, _, signature = message.partition(":")
        infected_signature = signature.removesuffix("FOUND").strip(" :")
        raise AntivirusFileInfectedError(
            infected_signature or "unknown-signature"
        )

    raise AntivirusUnavailableError(f"Unexpected ClamAV response: {message}")


class ClamAVScanner:
    def __init__(self, settings: EnvSettings) -> None:
        self.settings = settings

    async def scan_bytes(self, content: bytes) -> AntivirusScanResult:
        if not self.settings.clamav_enabled:
            return AntivirusScanResult(
                status=AttachmentScanStatus.SKIPPED,
                signature="",
                scanned_at=now_utc(),
                engine="clamav-disabled",
            )

        if len(content) > self.settings.clamav_max_stream_bytes:
            raise AntivirusUnavailableError(
                "File exceeds configured ClamAV streaming limit"
            )

        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.settings.clamav_host,
                    self.settings.clamav_port,
                ),
                timeout=self.settings.clamav_timeout_seconds,
            )
            writer.write(b"zINSTREAM\0")

            chunk_size = 64 * 1024
            for offset in range(0, len(content), chunk_size):
                chunk = content[offset : offset + chunk_size]
                writer.write(struct.pack(">I", len(chunk)))
                writer.write(chunk)

            writer.write(struct.pack(">I", 0))
            await asyncio.wait_for(
                writer.drain(),
                timeout=self.settings.clamav_timeout_seconds,
            )
            raw_response = await asyncio.wait_for(
                reader.readuntil(b"\0"),
                timeout=self.settings.clamav_timeout_seconds,
            )
            return parse_clamav_response(
                raw_response.decode("utf-8", errors="replace")
            )
        except AntivirusFileInfectedError:
            raise
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            raise AntivirusUnavailableError("Unable to scan file with ClamAV") from exc
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()


async def scan_upload_non_blocking(
    scanner: ClamAVScanner | None,
    content: bytes,
) -> AntivirusScanResult:
    if scanner is None:
        return AntivirusScanResult(
            status=AttachmentScanStatus.SKIPPED,
            signature="",
            scanned_at=now_utc(),
            engine="disabled",
        )
    try:
        return await scanner.scan_bytes(content)
    except AntivirusUnavailableError as exc:
        return AntivirusScanResult(
            status=AttachmentScanStatus.ERROR,
            signature=str(exc),
            scanned_at=now_utc(),
            engine="clamav-unavailable",
        )
