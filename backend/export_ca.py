"""Export the machine's trusted CA certificates to corporate-ca.pem.

Run this once if scans fail with:

    [SSL: CERTIFICATE_VERIFY_FAILED] self signed certificate in certificate chain

That means the network inspects TLS - normal on managed corporate and school
networks - and re-signs HTTPS with a private root CA that Python's bundled
certificate list does not contain. analyzer.py picks the exported file up
automatically on the next start.

    python export_ca.py

The generated file is gitignored: it describes this machine's network, and on a
managed device it may identify the employer.
"""
import os
import subprocess
import sys

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corporate-ca.pem")

POWERSHELL = r"""
$sb = New-Object System.Text.StringBuilder
$count = 0
foreach ($store in @("Cert:\LocalMachine\Root","Cert:\CurrentUser\Root","Cert:\LocalMachine\CA","Cert:\CurrentUser\CA")) {
  try {
    Get-ChildItem $store -ErrorAction Stop | ForEach-Object {
      try {
        $b64 = [Convert]::ToBase64String($_.RawData, 'InsertLineBreaks')
        [void]$sb.AppendLine("# " + $_.Subject)
        [void]$sb.AppendLine("-----BEGIN CERTIFICATE-----")
        [void]$sb.AppendLine($b64)
        [void]$sb.AppendLine("-----END CERTIFICATE-----")
        $count++
      } catch {}
    }
  } catch {}
}
[System.IO.File]::WriteAllText("__OUT__", $sb.ToString())
Write-Output $count
"""


def main() -> int:
    if sys.platform != "win32":
        print("This exporter is Windows-only.")
        print("On macOS/Linux, point SSL_CERT_FILE at your system CA bundle, e.g.")
        print("  export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt")
        return 1

    script = POWERSHELL.replace("__OUT__", OUT.replace("\\", "\\\\"))
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True,
    )

    if result.returncode != 0 or not os.path.exists(OUT):
        print("Export failed.")
        print(result.stderr[:500])
        return 1

    count = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "?"
    size_kb = os.path.getsize(OUT) / 1024
    print(f"Wrote {count} certificates to {OUT} ({size_kb:.0f} KB)")
    print("Restart the backend - analyzer.py will pick it up automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
