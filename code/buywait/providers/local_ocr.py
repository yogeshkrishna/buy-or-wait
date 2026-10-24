"""Optional local OCR adapters. No network, shell interpolation, or image-supplied paths."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import sys
import importlib.metadata

from buywait.domain.errors import ProviderUnavailableError, UnresolvedEvidenceError
from buywait.evidence.types import OcrDocument, OcrWord


def validate_png(data: bytes) -> tuple[int, int]:
    if len(data) < 33 or len(data) > 20 * 1024 * 1024 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise UnresolvedEvidenceError("Invalid or oversized PNG.", code="malformed_image")
    width, height = struct.unpack(">II", data[16:24])
    if min(width, height) < 1 or max(width, height) > 10000 or width * height > 30_000_000:
        raise UnresolvedEvidenceError("Image dimensions exceed OCR limits.", code="malformed_image")
    return width, height


class WindowsOcrProvider:
    provider_id = "windows-ocr"

    def __init__(self, timeout: float = 30):
        if os.name != "nt":
            raise ProviderUnavailableError("Windows OCR requires Windows.")
        self.timeout = timeout
        self.executable = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        self.script = Path(__file__).with_name("windows_ocr.ps1")
        probe = self._run(["-Probe"])
        self.version = str(probe["version"]) + ":" + probe["language"]

    def _run(self, args: list[str]) -> dict:
        try:
            result = subprocess.run([self.executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(self.script), *args], capture_output=True, timeout=self.timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise ProviderUnavailableError("Windows OCR failed; check installed OCR language support.")
            return json.loads(result.stdout.decode("utf-8-sig"))
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise ProviderUnavailableError("Local OCR unavailable or timed out.") from error

    def recognize(self, image: bytes) -> OcrDocument:
        return self._recognize(image)

    def variants(self, image: bytes):
        """Yield optional independent crops with their own coordinate frames."""
        try:
            from PIL import Image, ImageOps
        except ImportError:
            return
        with Image.open(io.BytesIO(image)) as original:
            original.load()
            for name, box in (("lower-half", (0, original.height // 2, original.width, original.height)),
                              ("lower-right", (original.width // 2, original.height // 2, original.width, original.height)),
                              ("right-column", (original.width * 3 // 4, 0, original.width, original.height))):
                cropped = ImageOps.grayscale(original.crop(box))
                scale = min(2, 2500 / max(cropped.size))
                cropped = cropped.resize((round(cropped.width * scale), round(cropped.height * scale)), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                cropped.save(output, format="PNG")
                from dataclasses import replace
                yield replace(self._recognize(output.getvalue()), model=self.version + ":crop-v1:" + name)

    def _recognize(self, image: bytes) -> OcrDocument:
        validate_png(image)
        with tempfile.TemporaryDirectory(prefix="buywait-ocr-") as temp:
            path = Path(temp) / "input.png"
            path.write_bytes(image)
            data = self._run(["-ImagePath", str(path)])
        try:
            words = tuple(OcrWord(str(word["text"]), *(round(word[key]) for key in ("x", "y", "width", "height")))
                          for line in data["lines"] for word in line["words"])
            return OcrDocument(data["text"], words, self.provider_id, self.version, data["language"], data["width"], data["height"])
        except (KeyError, TypeError, ValueError) as error:
            raise UnresolvedEvidenceError("Malformed OCR response.", code="malformed_ocr") from error


class TesseractOcrProvider:
    provider_id = "tesseract"

    def __init__(self, timeout: float = 30):
        self.executable = shutil.which("tesseract")
        self.timeout = timeout
        if not self.executable:
            raise ProviderUnavailableError("Tesseract is not installed.")
        try:
            result = subprocess.run([self.executable, "--version"], capture_output=True, timeout=5, check=True)
            self.version = result.stdout.decode().splitlines()[0]
        except (OSError, subprocess.SubprocessError) as error:
            raise ProviderUnavailableError("Tesseract is unavailable.") from error

    def recognize(self, image: bytes) -> OcrDocument:
        width, height = validate_png(image)
        try:
            result = subprocess.run([self.executable, "stdin", "stdout", "--psm", "11", "tsv"], input=image,
                                    capture_output=True, timeout=self.timeout, check=True)
            words = tuple(OcrWord(row["text"], *(int(row[key]) for key in ("left", "top", "width", "height")))
                          for row in csv.DictReader(io.StringIO(result.stdout.decode()), delimiter="\t")
                          if row["text"].strip())
            return OcrDocument(" ".join(word.text for word in words), words, self.provider_id, self.version, "eng", width, height)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            raise ProviderUnavailableError("Tesseract failed or timed out.") from error


class RapidOcrProvider:
    provider_id = "rapidocr-onnxruntime"

    def __init__(self, timeout: float = 45):
        try:
            self.version = importlib.metadata.version("rapidocr-onnxruntime") + ":det1600-v1"
        except importlib.metadata.PackageNotFoundError as error:
            raise ProviderUnavailableError("Optional RapidOCR is not installed.") from error
        self.timeout = timeout

    def recognize(self, image: bytes) -> OcrDocument:
        width, height = validate_png(image)
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("rapid_worker.py"))], input=image,
                                    capture_output=True, timeout=self.timeout, check=True,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            data = json.loads(result.stdout)
            words = []
            for box, text, score in data["result"]:
                x, y = round(min(point[0] for point in box)), round(min(point[1] for point in box))
                right, bottom = round(max(point[0] for point in box)), round(max(point[1] for point in box))
                words.append(OcrWord(text, x, y, right - x, bottom - y))
            return OcrDocument("\n".join(w.text for w in words), tuple(words), self.provider_id, self.version, "multilingual", width, height)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            raise ProviderUnavailableError("RapidOCR failed or timed out.") from error

    def variants(self, image: bytes):
        """Generic lower-receipt OCR retries; crops are not authoritative repairs."""
        from PIL import Image, ImageOps, ImageEnhance
        from dataclasses import replace
        with Image.open(io.BytesIO(image)) as original:
            original.load()
            for name, mode in (("lower-color", "color"), ("lower-gray", "gray"), ("lower-contrast", "contrast")):
                cropped = original.crop((0, original.height * 2 // 3, original.width, original.height))
                cropped = cropped.resize((cropped.width * 2, cropped.height * 2), Image.Resampling.LANCZOS)
                if mode != "color":
                    cropped = ImageOps.grayscale(cropped)
                if mode == "contrast":
                    cropped = ImageEnhance.Contrast(cropped).enhance(2)
                output = io.BytesIO()
                cropped.save(output, format="PNG")
                yield replace(self.recognize(output.getvalue()), model=self.version + ":crop-v1:" + name)


def local_ocr(name: str = "auto"):
    if name == "none":
        return None
    choices = [RapidOcrProvider, WindowsOcrProvider, TesseractOcrProvider] if name == "auto" else [
        {"rapidocr": RapidOcrProvider, "windows": WindowsOcrProvider, "tesseract": TesseractOcrProvider}[name]]
    for adapter in choices:
        try:
            return adapter()
        except ProviderUnavailableError:
            continue
    return None
