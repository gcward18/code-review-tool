"""Secret scan with gitleaks before any code leaves the machine."""

import json
import os
import subprocess
import tempfile


class ScanUnavailable(RuntimeError):
    pass


def scanner(executable):
    """Return scan(text) -> list of rule IDs found (empty means clean)."""
    if not executable:
        raise ScanUnavailable(
            "gitleaks not found: install it or set AUTOSDE_GITLEAKS to its path"
        )

    def scan(text):
        with tempfile.TemporaryDirectory(prefix="autosde-scan-") as directory:
            report = os.path.join(directory, "report.json")
            try:
                result = subprocess.run(
                    [
                        executable,
                        "stdin",
                        "--no-banner",
                        "--redact",
                        "--log-level",
                        "error",
                        "--report-format",
                        "json",
                        "--report-path",
                        report,
                    ],
                    input=text.encode(),
                    capture_output=True,
                    timeout=300,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ScanUnavailable("gitleaks did not run") from None
            if result.returncode not in (0, 1):
                raise ScanUnavailable("gitleaks failed")
            try:
                with open(report) as handle:
                    findings = json.load(handle) or []
            except (OSError, ValueError):
                findings = []
            if result.returncode == 1 and not findings:
                raise ScanUnavailable("gitleaks reported leaks without details")
            return sorted({item.get("RuleID", "secret") for item in findings})

    return scan
