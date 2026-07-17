from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aioclamd import BufferTooLongError
from aioclamd import ClamdAsyncClient
from aioclamd import ClamdError

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


def _parse_instream_result(result: dict | None) -> AntivirusScanResult:
    if not result:
        raise AntivirusUnavailableError("ClamAV returned an empty response")

    _, (status, reason) = next(iter(result.items()))

    if status == "OK":
        return AntivirusScanResult(
            status=AttachmentScanStatus.CLEAN,
            signature="",
            scanned_at=now_utc(),
        )

    if status == "FOUND":
        raise AntivirusFileInfectedError(reason or "unknown-signature")

    raise AntivirusUnavailableError(f"Unexpected ClamAV response: {status} {reason}")


class ClamAVScanner:
    def __init__(self, settings: EnvSettings) -> None:
        self.settings = settings

    async def scan_file(self, file_path: Path) -> AntivirusScanResult:
        if not self.settings.clamav_enabled:
            return AntivirusScanResult(
                status=AttachmentScanStatus.SKIPPED,
                signature="",
                scanned_at=now_utc(),
                engine="clamav-disabled",
            )

        if file_path.stat().st_size > self.settings.clamav_max_stream_bytes:
            raise AntivirusUnavailableError(
                "File exceeds configured ClamAV streaming limit"
            )

        client = ClamdAsyncClient(
            self.settings.clamav_host,
            self.settings.clamav_port,
        )
        try:
            with file_path.open("rb") as handle:
                result = await asyncio.wait_for(
                    client.instream(handle),
                    timeout=self.settings.clamav_timeout_seconds,
                )
        except AntivirusFileInfectedError:
            raise
        except BufferTooLongError as exc:
            raise AntivirusUnavailableError(
                "File exceeds ClamAV StreamMaxLength"
            ) from exc
        except (ClamdError, OSError, asyncio.TimeoutError) as exc:
            raise AntivirusUnavailableError("Unable to scan file with ClamAV") from exc

        return _parse_instream_result(result)


async def scan_upload_non_blocking(
    scanner: ClamAVScanner | None,
    file_path: Path,
) -> AntivirusScanResult:
    if scanner is None:
        return AntivirusScanResult(
            status=AttachmentScanStatus.SKIPPED,
            signature="",
            scanned_at=now_utc(),
            engine="disabled",
        )
    try:
        return await scanner.scan_file(file_path)
    except AntivirusUnavailableError as exc:
        return AntivirusScanResult(
            status=AttachmentScanStatus.ERROR,
            signature=str(exc),
            scanned_at=now_utc(),
            engine="clamav-unavailable",
        )
